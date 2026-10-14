"""Read side of the history database that the collector fills (python -m collector), for the /statistiky page.

The web app connects only when the database is configured, with the collector's variables
(DATABASE_URL, or PGHOST, PGUSER, PGPASSWORD and PGDATABASE). Every connection is read-only
with a statement timeout, and answers are cached for a short while, since the aggregates
over vehicle_sample scan up to a month of rows.

Times are bucketed in Prague local time and returned as "YYYY-MM-DDTHH:MM" strings.
"""
import os
import threading
import time
from datetime import datetime

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
