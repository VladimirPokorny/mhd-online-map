"""Interactive map of the IDS JMK (South Moravian Region) public transport network.

Development:  python app.py  [--gtfs PATH] [--cache DIR] [--port 5000]
Production:   gunicorn "app:create_app()"  (configured by environment variables)
Prebuild:     python app.py --build-only   (fills the cache, then exits)
Update:       python app.py --update --build-only   (download the latest feed first)

Environment:
  GTFS_URL                    feed to follow (default: KORDIS JMK); empty = no updates
  GTFS_UPDATE_INTERVAL_HOURS  how often to check for a new feed (default 6)
  GTFS_DIR                    bundled fallback feed (default ./GTFS)
  CACHE_DIR                   downloaded feeds + derived caches (default ./ or /app/data)
"""
import argparse
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request

from gtfs_loader import load_network
from gtfs_update import DEFAULT_URL, FeedStore
from reach import Reachability
from timetable import Timetable

BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__)


@dataclass
class State:
    """Everything derived from one GTFS version; replaced as a whole on update."""
    network: dict
    timetable: Timetable
    reachability: Reachability
    feed: dict = field(default_factory=dict)

    @property
    def version(self) -> str:
        return self.network["version"]


state: State = None
store: FeedStore = None
_build_lock = threading.Lock()


def _current() -> State:
    """The state for this request; 409 if the page was loaded from an older version."""
    s = state
    v = request.args.get("v")
    if v and v != s.version:
        abort(409)
    return s


def _date_arg(s: State) -> date:
    try:
        d = date.fromisoformat(request.args["date"])
    except (KeyError, ValueError):
        abort(400, "date=YYYY-MM-DD required")
    if not s.timetable.start <= d <= s.timetable.end:
        abort(400, "date outside the timetable validity")
    return d


def _time_arg() -> int:
    t = request.args.get("time", type=int)
    if t is None or not 0 <= t < 86_400:
        abort(400, "time=<seconds since midnight> required")
    return t


@app.errorhandler(409)
def stale(_):
    return jsonify({"error": "stale", "version": state.version}), 409


@app.route("/healthz")
def healthz():
    s = state
    return {"status": "ok", "version": s.version, "lines": len(s.network["routes"]), "feed": s.feed}


@app.route("/")
def index():
    return render_template("index.html", agency=state.network["agency"])


@app.route("/api/network")
def api_network():
    s = state
    return jsonify({**s.network, "feed": s.feed})


@app.route("/api/calendar")
def api_calendar():
    s = _current()
    return jsonify(s.timetable.calendar_info())


@app.route("/api/profile")
def api_profile():
    s = _current()
    return jsonify(s.timetable.profile(_date_arg(s)))


@app.route("/api/vehicles")
def api_vehicles():
    s = _current()
    d, t = _date_arg(s), _time_arg()
    return jsonify({"date": d.isoformat(), "time": t, "vehicles": s.timetable.vehicles(d, t)})


@app.route("/api/reach")
def api_reach():
    s = _current()
    d, t = _date_arg(s), _time_arg()
    stop = request.args.get("stop", type=int)
    budget = request.args.get("minutes", default=60, type=int)
    if stop is None or not 0 <= stop < len(s.network["stops"]):
        abort(400, "stop=<stop index> required")
    if not 5 <= budget <= 240:
        abort(400, "minutes must be between 5 and 240")
    return jsonify(s.reachability.query(d, t, stop, budget * 60))


# ------------------------------------------------------------------ loading ---

def build_state(gtfs_dir: Path, cache_dir: Path, feed: dict) -> State:
    print(f"Loading GTFS from {gtfs_dir} (cache {cache_dir}) ...", flush=True)
    t0 = time.perf_counter()
    cache_dir.mkdir(parents=True, exist_ok=True)
    network = load_network(gtfs_dir, cache_dir / "network_cache.json")
    segments = network.pop("_segments")
    timetable = Timetable(gtfs_dir, network, segments)
    reachability = Reachability(gtfs_dir, network, timetable)
    print(
        f"Loaded {len(network['routes'])} lines, {len(network['stops'])} stops and "
        f"{len(timetable.trip_route)} trips in {time.perf_counter() - t0:.1f}s "
        f"(version {network['version']}, {feed.get('source')} feed)",
        flush=True,
    )
    return State(network, timetable, reachability, feed)


def load_current(cache_dir: Path):
    """Build the state from the feed the store points to (bundled feed as fallback)."""
    global state
    with _build_lock:
        gtfs_dir, feed = store.current()
        try:
            new = build_state(gtfs_dir, cache_dir, feed)
        except Exception:
            if gtfs_dir == store.fallback_dir:
                raise
            print("Stored feed could not be loaded, falling back to the bundled one:", flush=True)
            traceback.print_exc()
            new = build_state(store.fallback_dir, cache_dir, {"source": "bundled", "url": store.url})
        state = new  # atomic swap; requests in flight keep their old State


def check_for_update(cache_dir: Path) -> bool:
    """Download a newer feed if there is one and switch to it. Never raises."""
    if not store.url:
        return False
    global state
    try:
        pending = store.check()
        if pending is None:
            print("GTFS feed unchanged", flush=True)
            if state is not None:
                state.feed = {**state.feed, "checked": store.meta().get("checked")}
            return False
        folder, commit = pending
        print(f"New GTFS feed downloaded to {folder}, rebuilding ...", flush=True)
        with _build_lock:
            new = build_state(folder, cache_dir, {"source": "download"})
            commit()  # only now does the new feed become the current one
            new.feed = store.current()[1]
            state = new
        return True
    except Exception:  # keep serving the previous version
        print("GTFS update failed:", flush=True)
        traceback.print_exc()
        return False


def _update_loop(cache_dir: Path, interval_s: float):
    time.sleep(5)  # let the server come up first
    while True:
        check_for_update(cache_dir)
        time.sleep(interval_s)


def _init(gtfs_dir: Path, cache_dir: Path, url: str):
    global store
    store = FeedStore(cache_dir, url, gtfs_dir)
    folder, _ = store.current()
    if not (folder / "stop_times.txt").exists():
        # Nothing downloaded yet and no bundled feed: fetch one before serving.
        if not url:
            raise SystemExit(f"No GTFS data in {folder} and no GTFS_URL to download from")
        print(f"No local GTFS data, downloading {url} ...", flush=True)
        if not check_for_update(cache_dir):
            raise SystemExit("Could not download the GTFS feed")
        return
    load_current(cache_dir)


def create_app():
    """Application factory for gunicorn; configured from the environment."""
    gtfs_dir = Path(os.environ.get("GTFS_DIR", BASE_DIR / "GTFS"))
    cache_dir = Path(os.environ.get("CACHE_DIR", BASE_DIR))
    url = os.environ.get("GTFS_URL", DEFAULT_URL).strip()
    _init(gtfs_dir, cache_dir, url)
    if url:
        hours = float(os.environ.get("GTFS_UPDATE_INTERVAL_HOURS", "6"))
        threading.Thread(target=_update_loop, args=(cache_dir, hours * 3600), daemon=True).start()
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gtfs", type=Path, default=Path(os.environ.get("GTFS_DIR", BASE_DIR / "GTFS")), help="bundled GTFS folder")
    parser.add_argument("--cache", type=Path, default=Path(os.environ.get("CACHE_DIR", BASE_DIR)), help="cache folder")
    parser.add_argument("--url", default=os.environ.get("GTFS_URL", DEFAULT_URL), help="GTFS feed URL ('' = no updates)")
    parser.add_argument("--update", action="store_true", help="download the latest feed before starting")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--build-only", action="store_true", help="build the cache and exit")
    args = parser.parse_args()

    url = args.url.strip()
    _init(args.gtfs, args.cache, url)
    if args.update:
        check_for_update(args.cache)
    if args.build_only:
        return
    if url:
        hours = float(os.environ.get("GTFS_UPDATE_INTERVAL_HOURS", "6"))
        threading.Thread(target=_update_loop, args=(args.cache, hours * 3600), daemon=True).start()
    print(f"Open http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
