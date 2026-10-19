"""Read side of the history database that the collector fills (python -m collector), for the /statistiky page.

The web app connects only when the database is configured, with the collector's variables
(DATABASE_URL, or PGHOST, PGUSER, PGPASSWORD and PGDATABASE). Every connection is read-only
with a statement timeout, and answers are cached for a short while, since the aggregates
over vehicle_sample scan up to a month of rows.

Times are bucketed in Prague local time and returned as "YYYY-MM-DDTHH:MM" strings.
"""
import bisect
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

CACHE_S = 300          # how long an answer is reused
STATEMENT_TIMEOUT_MS = 30_000
RANGES = {1: "hour", 7: "hour", 30: "day"}   # days shown → bucket size
# Delays outside this range (minutes) are errors in the source rather than real delays.
DELAY_MIN, DELAY_MAX = -30, 120
LATE_MIN = 3           # a vehicle this many minutes late counts as delayed
# Start of the period: ``days`` back, rounded down to a whole bucket in Prague time, so the
# first bucket is complete (query parameters "days" and "unit").
SINCE = ("(date_trunc(%(unit)s, now() AT TIME ZONE 'Europe/Prague' - make_interval(days => %(days)s))"
         " AT TIME ZONE 'Europe/Prague')")
PRAGUE = ZoneInfo("Europe/Prague")

JAM_LEVEL = 4          # traffic level from which a segment counts as jammed (speed below 50 % of free flow)
# The traffic job runs every 2 minutes; a longer pause between its successful runs means no data,
# and a jam stored before the pause ends where the pause begins instead of lasting through it.
TRAFFIC_GAP = timedelta(minutes=10)
STOP_RADIUS_M = 700    # a segment is named after the nearest stop within this distance

# WMO weather codes (Open-Meteo) in Czech.
WEATHER_CODES = {
    0: "Jasno", 1: "Skoro jasno", 2: "Polojasno", 3: "Zataženo", 45: "Mlha", 48: "Mlha s námrazou",
    51: "Slabé mrholení", 53: "Mrholení", 55: "Silné mrholení", 56: "Mrznoucí mrholení", 57: "Mrznoucí mrholení",
    61: "Slabý déšť", 63: "Déšť", 65: "Silný déšť", 66: "Mrznoucí déšť", 67: "Mrznoucí déšť",
    71: "Slabé sněžení", 73: "Sněžení", 75: "Silné sněžení", 77: "Sněhová zrna",
    80: "Přeháňky", 81: "Přeháňky", 82: "Silné přeháňky", 85: "Sněhové přeháňky", 86: "Sněhové přeháňky",
    95: "Bouřka", 96: "Bouřka s kroupami", 99: "Bouřka s kroupami",
}


def configured() -> bool:
    """Whether the database is configured for the web app."""
    return bool(os.environ.get("DATABASE_URL", "").strip() or os.environ.get("PGHOST", "").strip())


def _local(t: datetime | None) -> str | None:
    """A bucket (naive Prague time) as "YYYY-MM-DDTHH:MM"."""
    return t.strftime("%Y-%m-%dT%H:%M") if t else None


def _iso(t: datetime | None) -> str | None:
    """A timezone-aware time as ISO 8601."""
    return t.isoformat() if t else None


def _num(v, digits: int = 1):
    """A database number rounded for JSON; None stays None."""
    return None if v is None else round(float(v), digits)


def _period_start(days: int, unit: str) -> datetime:
    """Start of the period as SINCE computes it: ``days`` back, rounded down to a whole bucket in Prague time."""
    t = datetime.now(PRAGUE).replace(tzinfo=None) - timedelta(days=days)
    t = t.replace(minute=0, second=0, microsecond=0)
    if unit == "day":
        t = t.replace(hour=0)
    return t.replace(tzinfo=PRAGUE).astimezone(timezone.utc)


def _midpoint(geometry) -> tuple[float, float] | None:
    """(lat, lon) of the middle point of a segment's geometry ([[[lat, lon], …], …])."""
    points = [p for part in geometry or [] for p in part]
    return tuple(points[len(points) // 2]) if points else None


def _nearest_stop(point, stops) -> str | None:
    """Name of the stop nearest to ``point`` within STOP_RADIUS_M; ``stops`` is [(name, lat, lon)]."""
    if not point:
        return None
    lat, lon = point
    kx = math.cos(math.radians(lat))
    best, best_d = None, (STOP_RADIUS_M / 111_320) ** 2
    for name, slat, slon in stops:
        d = (slat - lat) ** 2 + ((slon - lon) * kx) ** 2
        if d < best_d:
            best, best_d = name, d
    return best


class History:
    """Queries of the collector's tables, each answer cached for CACHE_S seconds."""

    def __init__(self):
        from psycopg_pool import ConnectionPool
        url = os.environ.get("DATABASE_URL", "").strip()
        self.pool = ConnectionPool(
            url, min_size=0, max_size=3, open=True, timeout=10, check=ConnectionPool.check_connection,
            kwargs={"application_name": "idsjmk-map", "autocommit": True,
                    "options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS} -c default_transaction_read_only=on"})
        self._cache = {}
        self._lock = threading.Lock()

    def _cached(self, key, compute):
        """``compute()``, reused for CACHE_S seconds per key."""
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < CACHE_S:
                return hit[1]
        value = compute()
        with self._lock:
            self._cache[key] = (now, value)
            if len(self._cache) > 200:   # forget old keys (lot names come from the URL)
                for k, (t, _) in list(self._cache.items()):
                    if now - t >= CACHE_S:
                        del self._cache[k]
        return value

    def _rows(self, sql: str, params=None) -> list[tuple]:
        """All rows of a query."""
        with self.pool.connection() as conn:
            return conn.execute(sql, params).fetchall()

    # ----------------------------------------------------------- collector ---

    def status(self) -> list[dict]:
        """Last run of every collector job: success, rows, newest source data, last error."""
        def compute():
            from collector import scheduler
            return [{"job": job, "started": _iso(started), "ok": ok, "rows": rows, "source_time": _iso(source_time),
                     "last_ok": _iso(last_ok), "error": (error or "").splitlines()[0][:160] if error else None}
                    for job, started, ok, rows, source_time, last_ok, error in scheduler.status(self.pool)]
        return self._cached("status", compute)

    # ----------------------------------------------------------- transport ---

    def delays(self, days: int, line_modes: dict[int, str]) -> dict:
        """Average delay and share of delayed samples per time bucket and mode, and the most delayed lines.

        ``line_modes`` maps KORDIS LineID → mode (Tram, Bus, …) from the current timetable."""
        def compute():
            unit = RANGES[days]
            ids, modes = list(line_modes), list(line_modes.values())
            where = (f"observed_at >= {SINCE} "
                     f"AND delay_min BETWEEN {DELAY_MIN} AND {DELAY_MAX}")
            params = {"days": days, "unit": unit, "ids": ids, "modes": modes, "late": LATE_MIN}
            series = self._rows(f"""
                SELECT date_trunc(%(unit)s, s.observed_at AT TIME ZONE 'Europe/Prague') AS bucket,
                       coalesce(m.mode, 'Other'), sum(s.delay_min), count(*),
                       count(*) FILTER (WHERE s.delay_min >= %(late)s)
                FROM vehicle_sample s
                LEFT JOIN unnest(%(ids)s::int[], %(modes)s::text[]) AS m(line_id, mode) USING (line_id)
                WHERE {where}
                GROUP BY 1, 2 ORDER BY 1""", params)
            lines = self._rows(f"""
                SELECT line_id, avg(delay_min), count(*), count(*) FILTER (WHERE delay_min >= %(late)s),
                       count(DISTINCT vehicle_id)
                FROM vehicle_sample WHERE {where}
                GROUP BY line_id HAVING count(*) >= 30
                ORDER BY avg(delay_min) DESC LIMIT 15""", params)
            days_stored = self._rows("""
                SELECT day, source, rows FROM vehicle_day
                WHERE day >= (now() AT TIME ZONE 'Europe/Prague')::date - %s ORDER BY day""", (days,))
            buckets = {}
            for bucket, mode, total, n, late in series:
                b = buckets.setdefault(_local(bucket), {})
                b[mode] = [int(total), n, late]
            return {
                "unit": unit,
                # [bucket, {mode: [sum of delays, samples, delayed samples]}]
                "series": sorted(buckets.items()),
                "lines": [{"line": line, "avg": _num(avg, 2), "samples": n, "late": round(late / n, 3),
                           "vehicles": vehicles} for line, avg, n, late, vehicles in lines],
                "days": [{"day": d.isoformat(), "source": "archiv" if source == 2 else "živě", "rows": rows}
                         for d, source, rows in days_stored],
            }
        return self._cached(("delays", days, tuple(sorted(line_modes.items()))), compute)

    def diversions(self) -> list[dict]:
        """IDS JMK diversions, closures and moved stops that are published and valid now or later."""
        def compute():
            rows = self._rows("""
                SELECT diversion_id, kind, title, stop_label, affected_lines, public_text, valid_from, valid_to
                FROM ids_diversion
                WHERE last_seen > now() - interval '2 days' AND (valid_to IS NULL OR valid_to > now())
                ORDER BY valid_from DESC NULLS LAST LIMIT 200""")
            return [{"id": i, "kind": kind, "title": title, "stop": stop, "lines": lines or [], "text": text,
                     "from": _iso(vf), "to": _iso(vt)} for i, kind, title, stop, lines, text, vf, vt in rows]
        return self._cached("diversions", compute)

    # ---------------------------------------------------------- car traffic ---

    def traffic(self, days: int, stops: list[tuple[str, float, float]]) -> dict:
        """Traffic jams on Brno's roads: jammed kilometres over time and by hour of day, the segments
        jammed longest, the longest jams, and road events (closures, roadworks, accidents).

        traffic_sample has a row only when a segment's traffic changes, so a row holds until the
        segment's next one; everything here is weighted by that time. ``stops`` is [(name, lat, lon)]
        of the timetable, to name segments, which the source identifies only by codes."""
        def compute():
            unit = RANGES[days]
            since, now = _period_start(days, unit), datetime.now(timezone.utc)
            params = {"since": since, "jam": JAM_LEVEL, "gap": TRAFFIC_GAP}
            # Pauses of the collector: from the end of a run to the start of the next one, or to now (open).
            gaps = self._rows("""
                SELECT finished_at, coalesce(next_start, now()), next_start IS NULL FROM (
                    SELECT started_at, finished_at, lead(started_at) OVER (ORDER BY started_at) AS next_start
                    FROM collector_run
                    WHERE job = 'traffic' AND ok AND started_at >= %(since)s - interval '1 day') r
                WHERE coalesce(next_start, now()) - started_at > %(gap)s ORDER BY 1""", params)
            # Jam rows with the time of the segment's next row (from a day earlier: a jam may have begun before).
            rows = self._rows("""
                SELECT segment_id, observed_at, next_at, level, delay_s FROM (
                    SELECT segment_id, observed_at, level, delay_s,
                           lead(observed_at) OVER (PARTITION BY segment_id ORDER BY observed_at) AS next_at
                    FROM traffic_sample WHERE observed_at >= %(since)s - interval '1 day') s
                WHERE level >= %(jam)s AND (next_at IS NULL OR next_at > %(since)s)
                ORDER BY segment_id, observed_at""", params)
            seg_ids = sorted({r[0] for r in rows})
            meta = {sid: rest for sid, *rest in self._rows("""
                SELECT segment_id, road_class, road_number, chainage_from, length_m, geometry
                FROM traffic_segment WHERE segment_id = ANY(%s)""", (seg_ids,))}
            events = self._rows("""
                SELECT event_id, title, icon, locality, description, first_seen, last_seen, valid_from, valid_to
                FROM road_event WHERE last_seen >= %(since)s ORDER BY first_seen DESC""", params)
            return _traffic_stats(days, unit, since, now, gaps, rows, meta, events, stops)
        return self._cached(("traffic", days), compute)

    # ------------------------------------------------------------- weather ---

    def weather(self, days: int) -> dict:
        """Hourly weather of the last ``days`` days, the latest forecast and the Teplárny Brno sites."""
        def compute():
            past = self._rows("""
                SELECT hour_start AT TIME ZONE 'Europe/Prague', temperature, precipitation, snowfall,
                       wind_speed, wind_gusts, cloud_cover, humidity, weather_code
                FROM weather_hour
                WHERE hour_start >= now() - make_interval(days => %s) AND hour_start <= now()
                ORDER BY hour_start""", (days,))
            forecast = self._rows("""
                SELECT hour_start AT TIME ZONE 'Europe/Prague', temperature, precipitation,
                       precipitation_probability, weather_code, issued_at
                FROM weather_forecast
                WHERE issued_at = (SELECT max(issued_at) FROM weather_forecast) AND hour_start > now()
                ORDER BY hour_start LIMIT 48""")
            sites = self._rows("""
                SELECT DISTINCT ON (o.station_id) s.name, s.district, o.observed_at, o.temperature
                FROM temperature_observation o JOIN temperature_station s USING (station_id)
                WHERE o.observed_at > now() - interval '1 day'
                ORDER BY o.station_id, o.observed_at DESC""")
            now = next((r for r in reversed(past) if r[1] is not None), None)
            return {
                # [time, temperature °C, precipitation mm, snowfall cm, wind km/h, gusts km/h, cloud %, humidity %]
                "hours": [[_local(t), _num(temp), _num(prec), _num(snow), _num(wind), _num(gust), _num(cloud, 0),
                           _num(hum, 0)] for t, temp, prec, snow, wind, gust, cloud, hum, _ in past],
                # [time, temperature °C, precipitation mm, probability %]
                "forecast": [[_local(t), _num(temp), _num(prec), _num(prob, 0)] for t, temp, prec, prob, _, _ in forecast],
                "issued": _iso(forecast[0][5]) if forecast else None,
                "now": {"time": _local(now[0]), "temperature": _num(now[1]), "wind": _num(now[4]),
                        "humidity": _num(now[7], 0), "text": WEATHER_CODES.get(now[8])} if now else None,
                "sites": sorted(({"name": name, "district": district, "time": _iso(t), "temperature": _num(temp)}
                                 for name, district, t, temp in sites), key=lambda s: s["name"] or ""),
            }
        return self._cached(("weather", days), compute)

    def chmi(self, days: int) -> dict:
        """Measurements of the ČHMÚ stations in Brno: latest values, hourly series and daily rainfall.

        Hourly series sit at the full hour, like Open-Meteo's: temperature, humidity, dew point,
        pressure and cloud cover as measured then; wind as the mean and the strongest gust of the
        hour before; rain as the hour's total."""
        def compute():
            params = {"unit": "hour", "days": days}
            stations = self._rows("SELECT station, name, elevation, lat, lon FROM chmi_station ORDER BY station")
            hourly = self._rows(f"""
                SELECT date_trunc('hour', observed_at + interval '50 minutes') AT TIME ZONE 'Europe/Prague',
                       station, element,
                       CASE element WHEN 'Fmax' THEN max(value) WHEN 'SRA1H' THEN sum(value) ELSE avg(value) END
                FROM chmi_value
                WHERE observed_at >= {SINCE} AND observed_at <= now()
                  AND ((period = '10m' AND element IN ('F', 'Fmax'))
                       OR (period = '10m' AND element IN ('T', 'H') AND extract(minute FROM observed_at) = 0)
                       OR (period = '1h' AND element IN ('Td', 'P_hm', 'N', 'SRA1H')))
                GROUP BY 1, 2, 3 ORDER BY 1""", params)
            # Daily rainfall is read at 06:00 UTC (7:00 in winter) for the 24 hours before.
            rain = self._rows("""
                SELECT (observed_at AT TIME ZONE 'Europe/Prague')::date, station, value FROM chmi_value
                WHERE period = '1d' AND element = 'SRA' AND observed_at >= now() - make_interval(days => %s)
                ORDER BY 1""", (max(days, 7),))
            latest = self._rows("""
                SELECT DISTINCT ON (station, element) station, element, observed_at, value FROM chmi_value
                WHERE observed_at > now() - interval '6 hours' AND period IN ('10m', '1h')
                  AND element IN ('T', 'H', 'F', 'Fmax', 'Td', 'P_hm', 'N', 'VV')
                ORDER BY station, element, observed_at DESC""")
            buckets = sorted({_local(b) for b, *_ in hourly})
            index = {b: i for i, b in enumerate(buckets)}
            series = {}
            for b, station, element, value in hourly:
                series.setdefault(f"{station}:{element}", [None] * len(buckets))[index[_local(b)]] = _num(value)
            rain_days = sorted({d for d, _, _ in rain})
            day_index = {d: i for i, d in enumerate(rain_days)}
            rain_series = {}
            for d, station, value in rain:
                rain_series.setdefault(station, [None] * len(rain_days))[day_index[d]] = _num(value)
            now = {}
            for station, element, t, value in latest:
                now.setdefault(station, {})[element] = _num(value)
                now[station]["time"] = max(now[station].get("time") or "", _iso(t))
            return {
                "stations": [{"id": s, "name": name, "elevation": elev, "lat": lat, "lon": lon}
                             for s, name, elev, lat, lon in stations],
                "hours": buckets,
                # "<station>:<element>" → values per hour (T, H, Td °C/%, P_hm hPa, N eighths, SRA1H mm, F/Fmax m/s)
                "series": series,
                "rain_days": [d.isoformat() + "T00:00" for d in rain_days],
                "rain": rain_series,   # station → daily rainfall, mm
                "now": now,            # station → {element: latest value, "time": newest}
            }
        return self._cached(("chmi", days), compute)

    # ------------------------------------------------------------- parking ---

    def parking(self, days: int, lot: str | None) -> dict:
        """Car parks now, and free spaces over time of one car park (or all together)."""
        def compute():
            unit = RANGES[days]
            latest = self._rows("""
                SELECT DISTINCT ON (lot) lot, observed_at, coalesce(capacity, capacity_public), free
                FROM parking_observation WHERE observed_at > now() - interval '2 days'
                ORDER BY lot, observed_at DESC""")
            # Average per car park and bucket first, so a car park that reports more often weighs no more.
            series = self._rows(f"""
                SELECT bucket, sum(free), sum(capacity) FROM (
                    SELECT date_trunc(%(unit)s, observed_at AT TIME ZONE 'Europe/Prague') AS bucket, lot,
                           avg(free) AS free, avg(coalesce(capacity, capacity_public)) AS capacity
                    FROM parking_observation
                    WHERE observed_at >= {SINCE} AND free IS NOT NULL
                      AND (%(lot)s::text IS NULL OR lot = %(lot)s)
                    GROUP BY 1, 2) per_lot
                GROUP BY bucket ORDER BY bucket""", {"unit": unit, "days": days, "lot": lot})
            return {
                "unit": unit,
                "lots": sorted(({"lot": name, "time": _iso(t), "capacity": cap, "free": free}
                                for name, t, cap, free in latest), key=lambda r: r["lot"]),
                # [bucket, free spaces, capacity]
                "series": [[_local(b), _num(free, 0), _num(cap, 0)] for b, free, cap in series],
            }
        return self._cached(("parking", days, lot), compute)

    # ----------------------------------------------------------- the city ---

    def counts(self, days: int) -> dict:
        """Cyclists and pedestrians at the counting cameras over time, and the busiest places.

        The live and history layers count the same cameras under different ids; only the one
        with more data in the period is used, so nobody is counted twice."""
        def compute():
            unit = RANGES[days]
            params = {"unit": unit, "days": days}
            best = self._rows(f"""
                SELECT source, count(*) FROM bike_count
                WHERE hour_start >= {SINCE}
                GROUP BY source ORDER BY 2 DESC LIMIT 1""", params)
            if not best:
                return {"unit": unit, "source": None, "series": [], "places": []}
            params["source"] = best[0][0]
            series = self._rows(f"""
                SELECT date_trunc(%(unit)s, hour_start AT TIME ZONE 'Europe/Prague'), sum(cyclists), sum(pedestrians)
                FROM bike_count
                WHERE source = %(source)s AND hour_start >= {SINCE}
                GROUP BY 1 ORDER BY 1""", params)
            places = self._rows(f"""
                SELECT coalesce(s.name, c.station), sum(c.cyclists), sum(c.pedestrians)
                FROM bike_count c LEFT JOIN bike_station s USING (source, station)
                WHERE c.source = %(source)s AND c.hour_start >= {SINCE}
                GROUP BY 1 ORDER BY coalesce(sum(c.cyclists), 0) + coalesce(sum(c.pedestrians), 0) DESC
                LIMIT 10""", params)
            return {
                "unit": unit, "source": best[0][0],
                # [bucket, cyclists, pedestrians]
                "series": [[_local(b), cyc, ped] for b, cyc, ped in series],
                "places": [{"name": name, "cyclists": cyc, "pedestrians": ped} for name, cyc, ped in places],
            }
        return self._cached(("counts", days), compute)

    def city(self) -> dict:
        """Swimming pools and ice rinks now, water outages and today's cultural events."""
        def compute():
            venues = self._rows("""
                SELECT DISTINCT ON (venue) venue, observed_at, capacity, present, today
                FROM venue_occupancy WHERE observed_at > now() - interval '2 days'
                ORDER BY venue, observed_at DESC""")
            outages = self._rows("""
                SELECT municipality, street, starts_at, ends_at, supply_restricted, replacement, note
                FROM water_outage
                WHERE last_seen > now() - interval '2 days' AND (ends_at IS NULL OR ends_at > now())
                ORDER BY starts_at NULLS LAST LIMIT 50""")
            events = self._rows("""
                WITH today AS (   -- local midnight as timestamptz
                    SELECT date_trunc('day', now() AT TIME ZONE 'Europe/Prague') AT TIME ZONE 'Europe/Prague' AS start)
                SELECT name, categories, starts_at, ends_at, url FROM city_event, today
                WHERE last_seen > now() - interval '2 days'
                  AND starts_at < today.start + interval '1 day'
                  AND coalesce(ends_at, starts_at) >= today.start
                ORDER BY starts_at""")
            return {
                "venues": sorted(({"venue": v, "time": _iso(t), "capacity": cap, "present": present, "today": today}
                                  for v, t, cap, present, today in venues), key=lambda r: r["venue"]),
                "outages": [{"place": ", ".join(p for p in (town, street) if p), "from": _iso(s), "to": _iso(e),
                             "restricted": restricted, "replacement": repl, "note": note}
                            for town, street, s, e, restricted, repl, note in outages],
                "events": [{"name": name, "categories": cats, "from": _iso(s), "to": _iso(e), "url": url}
                           for name, cats, s, e, url in events[:40]],
                "event_count": len(events),
            }
        return self._cached("city", compute)


# ------------------------------------------------------------ car traffic ---

HOUR = timedelta(hours=1)
JAM_MERGE = timedelta(minutes=5)   # a jam that eases for at most this long is still the same jam


def _event_kind(title: str | None, icon: str | None) -> str:
    """Kind of a road event from its published title and icon: accident, incident, works or closure."""
    t, i = (title or "").lower(), (icon or "").removeprefix("p_")
    if "nehod" in t:
        return "accident"
    if i.startswith("U4") or "práce" in t:
        return "works"
    if i.startswith("U3"):   # "Dopravní situace", "Zdržení a čekací doby"
        return "incident"
    return "closure"


def _traffic_stats(days, unit, since, now, gaps, rows, meta, events, stops) -> dict:
    """The answer of History.traffic from its rows (see there); times are UTC datetimes."""
    gap_starts = [g[0] for g in gaps]
    stale = bool(gaps) and gaps[-1][2]   # the collector has not run for a while: nothing is known about now

    # Seconds with data in every hour of the period. Hours start on whole UTC hours, which are whole
    # Prague hours too, so the period start (a whole Prague hour) is one of them.
    hours, h = [], since
    while h < now:
        end = min(h + HOUR, now)
        hours.append((h, max(0.0, (end - h).total_seconds()
                             - sum(max(0.0, (min(end, ge) - max(h, gs)).total_seconds()) for gs, ge, _ in gaps))))
        h = end

    jam = {}        # hour start → [jammed km·s on level 4, on level 5]
    per_seg = {}    # segment → [jammed s, level-5 s, delay·s, max delay s]
    episodes = []   # [segment, start, end, max delay s, max level, ongoing]
    delay_now = {}  # segment jammed now → its delay now, s
    for sid, t0, t1, level, delay in rows:
        t0 = t0.astimezone(timezone.utc)
        end = (t1 or now).astimezone(timezone.utc)
        i = bisect.bisect_left(gap_starts, t0)
        if i < len(gap_starts) and gap_starts[i] < end:
            end = gap_starts[i]   # the collector paused: the jam is known only until then
        start, end = max(t0, since), min(end, now)
        if end <= start:
            continue
        ongoing = t1 is None and end == now and not stale
        if ongoing:
            delay_now[sid] = delay or 0
        km, d, dur = (meta.get(sid, (None,) * 5)[3] or 0) / 1000, delay or 0, (end - start).total_seconds()
        h = start.replace(minute=0, second=0, microsecond=0)
        while h < end:
            jam.setdefault(h, [0.0, 0.0])[level >= 5] += km * (min(end, h + HOUR) - max(start, h)).total_seconds()
            h += HOUR
        seg = per_seg.setdefault(sid, [0.0, 0.0, 0.0, 0])
        seg[0] += dur
        seg[1] += dur if level >= 5 else 0
        seg[2] += d * dur
        seg[3] = max(seg[3], d)
        last = episodes[-1] if episodes else None
        if last and last[0] == sid and start - last[2] <= JAM_MERGE:
            last[2], last[3], last[4], last[5] = end, max(last[3], d), max(last[4], level), ongoing
        else:
            episodes.append([sid, start, end, d, level, ongoing])

    # Average jammed kilometres per bucket, over the time with data.
    buckets, profile = {}, {wk: [[0.0, 0.0] for _ in range(24)] for wk in (False, True)}
    for h, covered in hours:
        local = h.astimezone(PRAGUE).replace(tzinfo=None)
        j = jam.get(h, (0.0, 0.0))
        b = buckets.setdefault(local.replace(hour=0) if unit == "day" else local, [0.0, 0.0, 0.0])
        b[0], b[1], b[2] = b[0] + j[0], b[1] + j[1], b[2] + covered
        p = profile[local.weekday() >= 5][local.hour]
        p[0], p[1] = p[0] + j[0] + j[1], p[1] + covered
    peak = max(((h, sum(jam.get(h, (0, 0))) / c) for h, c in hours if c >= 1800), key=lambda x: x[1], default=None)
    covered = sum(c for _, c in hours)
    total = sum(a + b for a, b in jam.values())

    def label(sid):
        cls, road, chainage, length, geometry = meta.get(sid, (None,) * 5)
        return {"road": road, "km": _num(chainage / 1000) if road and chainage is not None else None,
                "stop": _nearest_stop(_midpoint(geometry), stops), "length": length}

    jams, jammed_now = {}, {e[0] for e in episodes if e[5]}
    for e in episodes:
        jams[e[0]] = jams.get(e[0], 0) + 1
    segments = []
    for sid, (jam_s, stop_s, delay_ss, max_delay) in sorted(per_seg.items(), key=lambda x: -x[1][0]):
        geometry = (meta.get(sid) or (None,) * 5)[4] or []
        segments.append({"id": sid, **label(sid), "hours": _num(jam_s / 3600, 2), "stopped": _num(stop_s / 3600, 2),
                         "share": round(jam_s / covered, 4) if covered else None,
                         "avg_delay": round(delay_ss / jam_s), "max_delay": max_delay,
                         "jams": jams[sid], "now": sid in jammed_now,
                         "geometry": [[[round(lat, 5), round(lon, 5)] for lat, lon in part] for part in geometry]})
    current = [s for s in segments if s["now"]]
    longest = sorted(episodes, key=lambda e: e[1] - e[2])[:10]

    latest = max((e[6] for e in events), default=None)
    ev_now = {"closure": 0, "works": 0, "incident": 0, "accident": 0}
    incidents = []
    for eid, title, icon, locality, description, first, last, vf, vt in events:
        kind = _event_kind(title, icon)
        if last >= latest - TRAFFIC_GAP and (vf is None or vf <= now) and (vt is None or vt > now):
            ev_now[kind] += 1
        if kind in ("accident", "incident") and first >= since:
            incidents.append({"kind": kind, "title": title, "place": locality, "text": description,
                              "from": _iso(first), "to": _iso(last), "current": last >= latest - TRAFFIC_GAP})

    return {
        "unit": unit,
        # [bucket, average jammed km on level 4, on level 5]
        "series": [[_local(b), _num(k4 / c, 2), _num(k5 / c, 2)] if c >= 60 else [_local(b), None, None]
                   for b, (k4, k5, c) in sorted(buckets.items())],
        # average jammed km by hour of day (Prague), on workdays (Mon–Fri) and at weekends; None for one day
        "profile": {"work": [_num(a / c, 2) if c else None for a, c in profile[False]],
                    "weekend": [_num(a / c, 2) if c else None for a, c in profile[True]]} if days > 1 else None,
        "avg_km": _num(total / covered, 2) if covered else None,
        "km_hours": _num(total / 3600, 1),
        "peak": [_local(peak[0].astimezone(PRAGUE).replace(tzinfo=None)), _num(peak[1], 1)] if peak else None,
        "coverage": round(covered / sum((min(h + HOUR, now) - h).total_seconds() for h, _ in hours), 3) if hours else None,
        "now": None if stale else {"segments": len(current), "km": _num(sum(s["length"] or 0 for s in current) / 1000, 1),
                                   "max_delay": max(delay_now.values(), default=None)},
        "segments": segments,   # every segment jammed in the period, longest jammed first
        "longest": [{"id": sid, "from": _iso(s), "to": _iso(e), "minutes": round((e - s).total_seconds() / 60),
                     "max_delay": d, "level": lv, "ongoing": on} for sid, s, e, d, lv, on in longest],
        "events": {"now": ev_now, "incidents": incidents[:30], "incident_count": len(incidents),
                   "accidents": sum(1 for i in incidents if i["kind"] == "accident")},
    }
