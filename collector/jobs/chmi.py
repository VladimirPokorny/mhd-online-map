"""Measurements of the ČHMÚ stations in Brno (opendata.chmi.cz): 10-minute, hourly and daily values.

Brno, Tuřany is the full professional station (also dew point, sea-level pressure, cloud cover,
visibility); Brno, Žabovřesky measures temperature, humidity, wind, rain and sunshine; Brno, Jundrov
and Brno, Židenice are rainfall stations with daily values only (rain, snow).

Where the files are (all JSON, one per station):
  now/data/                    10m-/1h-<station>-<YYYYMMDD>.json, the last 3 days, updated every hour
  recent/data/<kind>/          the current month: a file per day (10min, 1hour), one file (daily)
  recent/data/<kind>/<MM>/     the last 12 finished months, one file per month
Values that are still missing are published empty and filled in later (rainfall stations report
with a lag of a day or two), so files are read again and changed values replace the stored ones.
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone

from .. import db
from ..http import NotModified, open_url
from . import Job, Result

log = logging.getLogger("collector.chmi")

BASE = "https://opendata.chmi.cz/meteorology/climate"
# Station → the periods it publishes.
STATIONS = {
    "0-20000-0-11723": ("10m", "1h", "1d"),      # Brno, Tuřany
    "0-203-0-11721": ("10m", "1h", "1d"),        # Brno, Žabovřesky
    "0-203-0-41501153002": ("1d",),              # Brno, Jundrov
    "0-203-0-41502109601": ("1d",),              # Brno, Židenice
}
# Our period → (folder under recent/data, file prefix, metadata OBS_TYPE).
KINDS = {"10m": ("10min", "10m", "10M"), "1h": ("1hour", "1h", "1H"), "1d": ("daily", "dly", "DLY")}
BACKFILL_MONTHS = 12      # what the recent/ folder keeps
NOW_DAYS = 3              # what the now/ folder keeps
LATE_MONTHS = 2           # finished months whose daily values are still checked for late reports


def _rows(doc: dict, period: str) -> list[tuple]:
    """chmi_observation rows of one published file; empty (missing) values are left out."""
    data = doc["data"]["data"]
    header = data["header"].split(",")
    i = {name: header.index(name) for name in header}
    out = []
    for r in data["values"]:
        value = r[i["VAL"]]
        if value in ("", None):
            continue
        out.append((r[i["STATION"]], period, r[i["ELEMENT"]], r[i["VTYPE"]] if "VTYPE" in i else "",
                    datetime.fromisoformat(r[i["DT"]].replace("Z", "+00:00")), float(value),
                    r[i["FLAG"]] or None, None if r[i["QUALITY"]] in ("", None) else int(r[i["QUALITY"]])))
    return out


def _fetch(url: str, etag: str | None = None) -> tuple[dict | None, str | None]:
    """(document, ETag) of a published file; (None, etag) if unchanged since ``etag``, (None, None) if not published."""
    try:
        with open_url(url, timeout=120, headers={"If-None-Match": etag} if etag else None) as resp:
            return json.loads(resp.read().decode("utf-8-sig")), resp.headers.get("ETag")
    except NotModified:
        return None, etag
    except Exception as exc:
        if getattr(exc, "code", None) == 404:
            return None, None
        raise


def _store(conn, rows: list[tuple]) -> int:
    """Insert or update observations; returns how many rows were new or changed."""
    if not rows:
        return 0
    conn.execute("""CREATE TEMP TABLE IF NOT EXISTS chmi_in (
                        station text, period text, element text, vtype text, observed_at timestamptz,
                        value real, flag text, quality smallint) ON COMMIT DELETE ROWS""")
    with conn.cursor().copy("COPY chmi_in FROM STDIN") as copy:
        for r in rows:
            copy.write_row(r)
    # A station that appeared in the source after the last metadata refresh gets a bare row.
    conn.execute("INSERT INTO chmi_station (station) SELECT DISTINCT station FROM chmi_in ON CONFLICT DO NOTHING")
    conn.execute("""INSERT INTO chmi_series (station, period, element, vtype)
                    SELECT DISTINCT station, period, element, vtype FROM chmi_in ON CONFLICT DO NOTHING""")
    return conn.execute("""
        INSERT INTO chmi_observation
        SELECT DISTINCT ON (s.series_id, i.observed_at) s.series_id, i.observed_at, i.value, i.flag, i.quality
        FROM chmi_in i JOIN chmi_series s USING (station, period, element, vtype)
        ON CONFLICT (series_id, observed_at) DO UPDATE
            SET value = EXCLUDED.value, flag = EXCLUDED.flag, quality = EXCLUDED.quality
            WHERE (chmi_observation.value, chmi_observation.flag, chmi_observation.quality)
                  IS DISTINCT FROM (EXCLUDED.value, EXCLUDED.flag, EXCLUDED.quality)""").rowcount


def _month_url(period: str, station: str, month: date, current: bool) -> str:
    """The monthly file of a station (daily values of the current month, or any period of a finished month)."""
    folder, prefix, _ = KINDS[period]
    sub = "" if current else f"{month:%m}/"
    return f"{BASE}/recent/data/{folder}/{sub}{prefix}-{station}-{month:%Y%m}.json"


class Chmi(Job):
    """Every 10 minutes: the files of the last days that changed (conditional requests, so an
    unchanged file costs one short answer). Once a day: station and element metadata. On its first
    runs, and after a long outage, it also fetches the missing days from recent/, one month per run."""
    name = "chmi"
    interval = 600
    stale_after = 3 * 3600

    def run(self, pool) -> Result:
        """Refresh metadata if due, store the changed recent files, then catch up one more month if needed."""
        today = datetime.now(timezone.utc).date()
        notes = []
        with pool.connection() as conn:
            etags = db.get_state(conn, "chmi.etags", {})
            meta_day = db.get_state(conn, "chmi.meta_day")
        if meta_day != today.isoformat():
            notes.append(self._metadata(pool, today))

        rows, newest, fresh = 0, None, {}
        urls = []
        for station, periods in STATIONS.items():
            for period in periods:
                if period == "1d":
                    urls.append((period, _month_url(period, station, today, current=True)))
                    # Rainfall stations publish a month weeks after it ends: keep checking the last two.
                    month = today.replace(day=1)
                    for _ in range(LATE_MONTHS):
                        month = (month - timedelta(days=1)).replace(day=1)
                        urls.append((period, _month_url(period, station, month, current=False)))
                    continue
                prefix = KINDS[period][1]
                for back in range(NOW_DAYS):
                    day = today - timedelta(days=back)
                    urls.append((period, f"{BASE}/now/data/{prefix}-{station}-{day:%Y%m%d}.json"))
        changed = 0
        for period, url in urls:
            doc, etag = _fetch(url, etags.get(url))
            if etag:
                fresh[url] = etag
            if doc is None:
                continue
            found = _rows(doc, period)
            with pool.connection() as conn, conn.transaction():
                rows += _store(conn, found)
                db.set_state(conn, "chmi.etags", {**etags, **fresh})
            changed += 1
            if period != "1d" and found:
                newest = max(newest or found[0][4], max(r[4] for r in found))
        with pool.connection() as conn, conn.transaction():
            db.set_state(conn, "chmi.etags", fresh)   # forget files that left the window
        notes.append(f"{changed} of {len(urls)} files changed")

        n, done, more = self._backfill(pool, today)
        if done:
            notes.append(f"backfill {done}")
        return Result(rows + n, ", ".join(note for note in notes if note), newest, more=more)

    def _metadata(self, pool, today: date) -> str:
        """Stations and their elements from the newest published metadata (recent/metadata, a day behind)."""
        for back in range(1, 5):
            day = today - timedelta(days=back)
            stations, _ = _fetch(f"{BASE}/recent/metadata/meta1-{day:%Y%m%d}.json")
            elements, _ = _fetch(f"{BASE}/recent/metadata/meta2-{day:%Y%m%d}.json")
            if stations and elements:
                break
        else:
            log.warning("no ČHMÚ metadata found for the last days")
            return "metadata not found"
        types = {obs: period for period, (_, _, obs) in KINDS.items()}
        with pool.connection() as conn, conn.transaction():
            for wsi, gh_id, name, lon, lat, elevation, begin in stations["data"]["data"]["values"]:
                if wsi not in STATIONS:
                    continue
                conn.execute("""INSERT INTO chmi_station VALUES (%s, %s, %s, %s, %s, %s, %s)
                                ON CONFLICT (station) DO UPDATE SET gh_id = EXCLUDED.gh_id, name = EXCLUDED.name,
                                    lat = EXCLUDED.lat, lon = EXCLUDED.lon, elevation = EXCLUDED.elevation,
                                    begin_date = EXCLUDED.begin_date""",
                             (wsi, gh_id, name, lat, lon, elevation, begin[:10] if begin else None))
            for obs, wsi, element, name, unit, height, _schedule in elements["data"]["data"]["values"]:
                if wsi not in STATIONS or obs not in types:
                    continue
                conn.execute("""INSERT INTO chmi_element VALUES (%s, %s, %s, %s, %s, %s)
                                ON CONFLICT (station, period, element) DO UPDATE SET name = EXCLUDED.name,
                                    unit = EXCLUDED.unit, height = EXCLUDED.height""",
                             (wsi, types[obs], element, name, (unit or "").strip(), height))
            db.set_state(conn, "chmi.meta_day", today.isoformat())
        return "metadata updated"

    def _backfill(self, pool, today: date) -> tuple[int, str, bool]:
        """Fetch the days before the now/ window that are not stored yet; the cursor is the next day to fetch.

        A finished month comes as one file per station and period; days of the current month
        come from the per-day files. Returns (rows stored, what was fetched, whether more remain)."""
        first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        for _ in range(BACKFILL_MONTHS - 1):
            first = (first - timedelta(days=1)).replace(day=1)
        with pool.connection() as conn:
            start = date.fromisoformat(db.get_state(conn, "chmi.backfill_next", first.isoformat()))
        start = max(start, first)
        stop = today - timedelta(days=NOW_DAYS - 1)   # from here on the now/ folder has it
        if start >= stop:
            return 0, "", False
        rows = 0
        if start.month != today.month or start.year != today.year:
            # One finished month: every station and period at once.
            month = start.replace(day=1)
            nxt = (month + timedelta(days=32)).replace(day=1)
            for station, periods in STATIONS.items():
                for period in periods:
                    doc, _ = _fetch(_month_url(period, station, month, current=False))
                    if doc:
                        with pool.connection() as conn, conn.transaction():
                            rows += _store(conn, _rows(doc, period))
            with pool.connection() as conn, conn.transaction():
                db.set_state(conn, "chmi.backfill_next", nxt.isoformat())
            return rows, f"{month:%Y-%m}", nxt < stop
        # Days of the current month before the now/ window (the daily values are fetched every run).
        day = start
        while day < stop:
            for station, periods in STATIONS.items():
                for period in periods:
                    if period == "1d":
                        continue
                    folder, prefix, _ = KINDS[period]
                    doc, _ = _fetch(f"{BASE}/recent/data/{folder}/{prefix}-{station}-{day:%Y%m%d}.json")
                    if doc:
                        with pool.connection() as conn, conn.transaction():
                            rows += _store(conn, _rows(doc, period))
            day += timedelta(days=1)
        with pool.connection() as conn, conn.transaction():
            db.set_state(conn, "chmi.backfill_next", stop.isoformat())
        return rows, f"{start}–{stop - timedelta(days=1)}", False
