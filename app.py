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
  LIVE                        live map of vehicles with their delays, /zive (default true)
  LIVE_TRAFFIC                car traffic and road closures from dopravniinfo.cz on it (default true)
"""
import argparse
import gzip
import math
import os
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from gtfs_loader import load_network
from gtfs_update import DEFAULT_URL, FeedStore
from live import STOPPED_AT, LiveState
from reach import Reachability
from timetable import Timetable

BASE_DIR = Path(__file__).resolve().parent
PRAGUE = ZoneInfo("Europe/Prague")  # timetable times are local time

app = Flask(__name__)


@app.context_processor
def analytics():
    """Optional Umami analytics, enabled by setting UMAMI_WEBSITE_ID."""
    website_id = os.environ.get("UMAMI_WEBSITE_ID", "").strip()
    if not website_id:
        return {"umami": None}
    script_url = os.environ.get("UMAMI_SCRIPT_URL", "").strip() or "https://cloud.umami.is/script.js"
    recorder = os.environ.get("UMAMI_RECORDER", "").strip().lower() in ("1", "true", "yes", "on")
    return {"umami": {
        "website_id": website_id,
        "script_url": script_url,
        "domains": os.environ.get("UMAMI_DOMAINS", "").strip(),
        # Session replays and heatmaps; recorder.js is served next to script.js.
        "recorder_url": script_url.rsplit("/", 1)[0] + "/recorder.js" if recorder else None,
    }}


@app.after_request
def compress(response):
    """Gzip large JSON (the live vehicle list is ~150 kB raw, ~30 kB compressed)."""
    if (response.mimetype == "application/json" and not response.direct_passthrough
            and "gzip" in request.headers.get("Accept-Encoding", "")
            and response.content_length and response.content_length > 20_000):
        response.set_data(gzip.compress(response.get_data(), compresslevel=5))
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Vary"] = "Accept-Encoding"
    return response


@app.after_request
def frame_ancestors(response):
    """Let the Umami dashboard frame the pages for its heatmap overlay."""
    origins = os.environ.get("UMAMI_FRAME_ANCESTORS", "").strip()
    if origins:
        response.headers["Content-Security-Policy"] = f"frame-ancestors 'self' {origins}"
    return response


@dataclass
class State:
    """Everything derived from one GTFS version; replaced as a whole on update."""
    network: dict
    timetable: Timetable
    reachability: Reachability
    feed: dict = field(default_factory=dict)

    @property
    def version(self) -> str:
        """Identifier of the GTFS version this state was built from."""
        return self.network["version"]


state: State = None
store: FeedStore = None
live: LiveState = None
_build_lock = threading.Lock()


def _current() -> State:
    """The state for this request; 409 if the page was loaded from an older version."""
    s = state
    v = request.args.get("v")
    if v and v != s.version:
        abort(409)
    return s


def _date_arg(s: State) -> date:
    """The ``date`` query parameter; 400 if missing or outside the timetable validity."""
    try:
        d = date.fromisoformat(request.args["date"])
    except (KeyError, ValueError):
        abort(400, "date=YYYY-MM-DD required")
    if not s.timetable.start <= d <= s.timetable.end:
        abort(400, "date outside the timetable validity")
    return d


def _time_arg() -> int:
    """The ``time`` query parameter (seconds since midnight); 400 if missing or out of range."""
    t = request.args.get("time", type=int)
    if t is None or not 0 <= t < 86_400:
        abort(400, "time=<seconds since midnight> required")
    return t


@app.errorhandler(409)
def stale(_):
    """Tell the page that it was loaded from an older timetable version, so it can reload."""
    return jsonify({"error": "stale", "version": state.version}), 409


@app.route("/healthz")
def healthz():
    """Health check for the container: status, timetable version, number of lines and feed info."""
    s = state
    return {"status": "ok", "version": s.version, "lines": len(s.network["routes"]), "feed": s.feed}


@app.route("/")
def index():
    """The network map with its timetable playback."""
    return render_template("index.html", agency=state.network["agency"])


def _cz_date(d) -> str:
    """A date in Czech notation, e.g. 14. 10. 2026."""
    return f"{d.day}. {d.month}. {d.year}"


@app.route("/o-aplikaci")
def about():
    """The About page with help and statistics of the current timetable."""
    s = state
    published = s.feed.get("published")
    if published:
        try:
            published = _cz_date(datetime.fromisoformat(published))
        except ValueError:
            published = None
    modes = {}
    for r in s.network["routes"]:
        modes[r["type"]] = modes.get(r["type"], 0) + 1
    return render_template(
        "about.html",
        agency=s.network["agency"],
        lines=len(s.network["routes"]),
        stops=len({st["name"] for st in s.network["stops"]}),
        trips=len(s.timetable.trip_route),
        modes=modes,
        valid_from=_cz_date(s.timetable.start),
        valid_to=_cz_date(s.timetable.end),
        published=published,
        auto_update=bool(store and store.url),
    )


# ------------------------------------------------------- home-screen app ---

@app.route("/manifest.webmanifest")
def manifest():
    """Web app manifest: lets phones add the app to the home screen. It opens the live map."""
    start = "/zive" if live is not None else "/"
    response = jsonify({
        "name": "MHD Brno a JMK – živá mapa",
        "short_name": "MHD JMK",
        "description": "Kde právě jsou vozidla IDS JMK a jaké mají zpoždění, mapa sítě a jízdní řád.",
        "lang": "cs",
        "id": "/",
        "start_url": start,
        "scope": "/",
        "display": "standalone",
        "background_color": "#ffffff",
        "theme_color": "#00224b",
        "icons": [
            {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
            {"src": "/static/icons/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
        "shortcuts": ([{"name": "Živá mapa", "url": "/zive", "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192"}]}]
                      if live is not None else []) + [
            {"name": "Mapa sítě a jízdní řád", "short_name": "Mapa sítě", "url": "/",
             "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192"}]},
        ],
    })
    response.mimetype = "application/manifest+json"
    return response


@app.route("/sw.js")
def service_worker():
    """The service worker, served from the root so that it may handle every page of the app."""
    response = send_from_directory(BASE_DIR / "static", "sw.js", mimetype="text/javascript", max_age=0)
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/offline")
def offline():
    """The page the service worker shows when there is no connection."""
    return render_template("offline.html")


@app.route("/api/network")
def api_network():
    """Lines, stops and track geometry of the current timetable, with feed info."""
    s = state
    return jsonify({**s.network, "feed": s.feed})


@app.route("/api/calendar")
def api_calendar():
    """Validity of the timetable and the public holidays within it."""
    s = _current()
    return jsonify(s.timetable.calendar_info())


@app.route("/api/profile")
def api_profile():
    """Number of vehicles in service per mode over one day, for the timeline chart."""
    s = _current()
    return jsonify(s.timetable.profile(_date_arg(s)))


@app.route("/api/vehicles")
def api_vehicles():
    """Scheduled positions of all vehicles at the given date and time."""
    s = _current()
    d, t = _date_arg(s), _time_arg()
    return jsonify({"date": d.isoformat(), "time": t, "vehicles": s.timetable.vehicles(d, t)})


@app.route("/api/reach")
def api_reach():
    """Where one can get from a stop within the given number of minutes."""
    s = _current()
    d, t = _date_arg(s), _time_arg()
    stop = request.args.get("stop", type=int)
    budget = request.args.get("minutes", default=60, type=int)
    if stop is None or not 0 <= stop < len(s.network["stops"]):
        abort(400, "stop=<stop index> required")
    if not 5 <= budget <= 240:
        abort(400, "minutes must be between 5 and 240")
    return jsonify(s.reachability.query(d, t, stop, budget * 60))


# --------------------------------------------------------------------- live ---

_stop_cache = {}


def _stop_names() -> dict:
    """KORDIS stop number (the "U<number>" of a GTFS stop_id) → stop name."""
    s = state
    if s.version not in _stop_cache:
        names = {}
        for st in s.network["stops"]:
            m = re.match(r"U(\d+)", st["id"])
            if m:
                names.setdefault(int(m.group(1)), st["name"])
        _stop_cache.clear()
        _stop_cache[s.version] = names
    return _stop_cache[s.version]


def _lines() -> dict:
    """KORDIS LineID → line of the current timetable (GTFS route_id is "L<LineID>D<agency>")."""
    out = {}
    for r in state.network["routes"]:
        m = re.match(r"L(\d+)D", r["id"])
        if m:
            out[int(m.group(1))] = {"short": r["short"], "long": r["long"], "type": r["type"],
                                    "color": r["color"], "text": r["text"]}
    return out


def _live() -> LiveState:
    """The live state; 404 when the live map is turned off."""
    if live is None:
        abort(404, "the live map is turned off (LIVE=false)")
    return live


def _hhmm(seconds: int) -> str:
    """Seconds since midnight as HH:MM (times past midnight wrap around)."""
    return f"{seconds // 3600 % 24:02d}:{seconds // 60 % 60:02d}"


def _flat(pts) -> list:
    """A list of [lat, lon] points as one flat list rounded to 5 decimals, for smaller JSON."""
    return [round(v, 5) for p in pts for v in p]


def _stop_key(stop_id: str):
    """(stop number, post number) of a KORDIS stop_id; GTFS Realtime zero-pads them (U09440Z01 = U9440Z1)."""
    m = re.match(r"U(\d+)Z(\d+)", stop_id or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _estimate(trip_id: str, stop_id: str, status: int, lat: float, lon: float, ts: int):
    """Line, delay (min) and last / final stop of a vehicle from its GTFS Realtime position and the timetable.

    Used while the vehicle stream, which reports delays itself, is silent. A vehicle at a stop
    is late by the time past the scheduled departure (early: before the arrival). Between two
    stops, the scheduled time at its position is interpolated by the straight-line distances
    to the previous and the next stop."""
    s = state
    found = s.timetable.trip_stops(trip_id)
    if found is None:
        return None
    route_idx, _, stops = found
    m = re.match(r"L(\d+)D", s.network["routes"][route_idx]["id"])
    keys = [_stop_key(s.network["stops"][i]["id"]) for i, _, _ in stops]
    key = _stop_key(stop_id)
    if not m or key not in keys:
        return None
    i = keys.index(key)
    local = datetime.fromtimestamp(ts, PRAGUE)
    now = local.hour * 3600 + local.minute * 60 + local.second
    if stops[0][2] - now > 12 * 3600:
        now += 86_400  # a trip of the previous service day, after midnight
    if now < stops[0][2]:
        # Waiting for its next trip: on time at the terminus, or not in service yet (parked).
        if stops[0][2] - now > 10 * 60:
            return None
        return {"line": int(m.group(1)), "delay": 0.0, "last_stop": keys[0][0], "final_stop": keys[-1][0]}
    _, arr, dep = stops[i]
    if status == STOPPED_AT or i == 0:
        delay = now - arr if now < arr and i else max(0, now - dep)
    else:
        prev = s.network["stops"][stops[i - 1][0]]
        nxt = s.network["stops"][stops[i][0]]
        k = math.cos(math.radians(lat))
        dist = lambda st: math.hypot(st["lat"] - lat, (st["lon"] - lon) * k)
        a, b = dist(prev), dist(nxt)
        f = a / (a + b) if a + b else 1
        delay = now - (stops[i - 1][2] + f * (arr - stops[i - 1][2]))
    if abs(delay) > 90 * 60:
        return None  # most likely the feed still names a trip the vehicle has finished
    last = i if status == STOPPED_AT else max(i - 1, 0)
    return {"line": int(m.group(1)), "delay": round(delay / 60, 1),
            "last_stop": keys[last][0], "final_stop": keys[-1][0]}


def _trip_detail(trip, v: dict):
    """The stops of the trip a vehicle runs, with scheduled and expected times."""
    trip_id, rt_stop, status = trip
    found = state.timetable.trip_stops(trip_id)
    if found is None:
        return None
    route_idx, headsign, stops = found
    if not re.match(rf"L{v['line']}D", state.network["routes"][route_idx]["id"]):
        return None  # the realtime feed still has the vehicle's previous trip
    net = state.network["stops"]
    keys = [_stop_key(net[s]["id"]) for s, _, _ in stops]
    nodes = [k[0] if k else None for k in keys]
    # Where the vehicle is: the stream's last stop is fresher than the realtime feed's,
    # unless the position itself came from the realtime feed.
    nxt, here = None, None
    if v.get("last_stop") in nodes and not v.get("estimated"):
        here = nodes.index(v["last_stop"])
        nxt = here + 1
    elif _stop_key(rt_stop) in keys:
        i = keys.index(_stop_key(rt_stop))
        here, nxt = (i, i + 1) if status == STOPPED_AT else (None, i)
    delay = int(round((v.get("delay") or 0) * 60))
    out = []
    for i, (s, arr, dep) in enumerate(stops):
        future = nxt is not None and i >= nxt
        t = arr if i else dep
        out.append({
            "name": net[s]["name"], "lat": net[s]["lat"], "lon": net[s]["lon"],
            "time": _hhmm(t), "expected": _hhmm(t + delay) if future and delay else None,
            "passed": nxt is not None and i < nxt,
        })
    return {"trip_id": trip_id, "number": state.timetable.trip_number.get(str(trip_id)),
            "headsign": headsign, "stops": out, "next": nxt,
            "at_stop": here is not None and status == STOPPED_AT and here == nxt - 1}


@app.route("/zive")
def live_page():
    """The live map of vehicles with their delays."""
    return render_template("live.html", agency=state.network["agency"], enabled=live is not None)


@app.route("/api/live/meta")
def api_live_meta():
    """Lines of the current timetable by KORDIS LineID, and whether car traffic is available."""
    lv = _live()
    return jsonify({"lines": _lines(), "traffic": lv.traffic_fetched is not None or LIVE_TRAFFIC})


@app.route("/api/live/vehicles")
def api_live_vehicles():
    """Every vehicle heard from in the last 30 minutes:
    [number, LineID, lat, lon, delay (min), bearing, age (s), inactive, low-floor, destination, course]."""
    lv = _live()
    names, now = _stop_names(), time.time()
    rows = [[vid, v["line"], v["lat"], v["lon"], v["delay"], v["bearing"], round(now - v["ms"] / 1000),
             int(v["inactive"]), int(v["lf"]), names.get(v["final_stop"]), v["course"]]
            for vid, v in lv.snapshot()]
    return jsonify({"time": int(now), "vehicles": rows, "connected": lv.connected, "realtime": lv.rt_fetched,
                    "source": lv.source()})


@app.route("/api/live/vehicle/<int:vid>")
def api_live_vehicle(vid: int):
    """Everything known about one vehicle: position, delay, trail, its trip's stops and line alerts."""
    lv = _live()
    v = lv.vehicle(vid)
    if v is None:
        abort(404, "unknown vehicle")
    names = _stop_names()
    trip = lv.trip_of(vid)
    return jsonify({
        "id": vid, "line": v["line"], "lat": v["lat"], "lon": v["lon"], "delay": v["delay"], "bearing": v["bearing"],
        "age": round(time.time() - v["ms"] / 1000), "updated": v["ms"], "low_floor": v["lf"], "inactive": v["inactive"],
        "course": v["course"], "vtype": v["vtype"], "estimated": v["estimated"],
        "last_stop": names.get(v["last_stop"]), "final_stop": names.get(v["final_stop"]),
        "trail": v["trail"], "trip": _trip_detail(trip, v) if trip else None,
        "alerts": lv.alerts_of(v["line"]), "usual": None,
    })


@app.route("/api/live/route/<int:line>")
def api_live_route(line: int):
    """Track and stops of one line, to draw under a selected vehicle."""
    route = next((r for r in state.network["routes"] if re.match(rf"L{line}D", r["id"])), None)
    if route is None:
        abort(404, "unknown line")
    stops = state.network["stops"]
    return jsonify({
        "ways": [_flat(state.network["ways"][w]) for w in route["ways"]],
        "stops": [[stops[i]["lat"], stops[i]["lon"], stops[i]["name"]] for i in route["stops"]],
    })


@app.route("/api/live/events")
def api_live_events():
    """Road events in Brno (closures, roadworks, accidents) from dopravniinfo.cz."""
    lv = _live()
    return jsonify({"events": lv.events, "fetched": lv.traffic_fetched})


@app.route("/api/live/traffic/geometry")
def api_live_traffic_geometry():
    """Road segments with car traffic data: [[id, name, road, [flat lat/lon list per part]], …]."""
    lv = _live()
    return jsonify([[sid, name, road, [_flat(p) for p in parts]] for sid, (name, road, parts) in lv.segments.items()])


@app.route("/api/live/traffic")
def api_live_traffic():
    """Car traffic now: {segment id: [speed / free-flow speed, delay s]}."""
    lv = _live()
    return jsonify({"stamp": lv.traffic_stamp, "segs": {sid: list(t) for sid, t in lv.traffic.items()}})


def _off(name: str) -> bool:
    """Whether the environment variable ``name`` turns a feature off (it is on by default)."""
    return os.environ.get(name, "true").strip().lower() in ("0", "false", "no", "off")


LIVE_TRAFFIC = not _off("LIVE_TRAFFIC")


def _start_live():
    """Start the live data threads unless the live map is turned off (LIVE=false)."""
    global live
    if not _off("LIVE"):
        live = LiveState()
        live.estimate = _estimate
        live.start(traffic=LIVE_TRAFFIC)


# ------------------------------------------------------------------ loading ---

def build_state(gtfs_dir: Path, cache_dir: Path, feed: dict) -> State:
    """Load one GTFS version: network, timetable and reachability index."""
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
    """Background thread: check for a new timetable every ``interval_s`` seconds."""
    time.sleep(5)  # let the server come up first
    while True:
        check_for_update(cache_dir)
        time.sleep(interval_s)


def _init(gtfs_dir: Path, cache_dir: Path, url: str):
    """Set up the feed store and load the current timetable, downloading one if there is none."""
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
    _start_live()
    return app


def main():
    """Command line entry point for development, prebuilding the cache and updates."""
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
    _start_live()
    print(f"Open http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
