"""Weather: hourly values and issued forecasts for Brno (Open-Meteo), air temperature at Teplárny Brno sites."""
from datetime import date, timedelta

from .. import db
from ..http import arcgis_features, get_json
from ..timeutil import from_ms
from . import Job, Result

BRNO = (49.195, 16.608)
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
# Open-Meteo variable → weather_hour column.
HOURLY = {"temperature_2m": "temperature", "precipitation": "precipitation", "rain": "rain", "snowfall": "snowfall",
          "snow_depth": "snow_depth", "visibility": "visibility", "wind_speed_10m": "wind_speed",
          "wind_gusts_10m": "wind_gusts", "cloud_cover": "cloud_cover", "relative_humidity_2m": "humidity",
          "weather_code": "weather_code"}
FORECAST = {"temperature_2m": "temperature", "precipitation": "precipitation", "rain": "rain", "snowfall": "snowfall",
            "visibility": "visibility", "wind_gusts_10m": "wind_gusts", "weather_code": "weather_code",
            "precipitation_probability": "precipitation_probability"}
BACKFILL_FROM = date(2020, 6, 1)   # the bike counters' history starts here
TEMPERATURES_URL = ("https://services6.arcgis.com/fUWVlHWZNxUvTUh8/arcgis/rest/services/"
                    "Pr%C5%AFm%C4%9Brn%C3%A9_aktu%C3%A1ln%C3%AD_teploty_vzduchu_Average_air_temperaturatures_NOV%C3%89/"
                    "FeatureServer/0")


def _hours(data: dict, variables: dict) -> list[dict]:
    """Rows of an Open-Meteo hourly answer (unixtime format): {"hour_start": datetime, column: value, …}."""
    h = data["hourly"]
    out = []
    for i, t in enumerate(h["time"]):
        row = {"hour_start": from_ms(t * 1000)}
        for var, col in variables.items():
            row[col] = (h.get(var) or [None] * len(h["time"]))[i]
        out.append(row)
    return out


class Weather(Job):
    """Every hour: the recent hours (weather_hour) and the forecast as issued now (weather_forecast).
    On its first runs it also backfills weather_hour from the reanalysis archive, one year per run."""
    name = "weather"
    interval = 3600
    stale_after = 3 * 3600

    def run(self, pool) -> Result:
        """Store recent weather and the current forecast, then backfill one more year if needed."""
        now = db.utc_now()
        data = get_json(FORECAST_URL, {
            "latitude": BRNO[0], "longitude": BRNO[1], "hourly": ",".join(set(HOURLY) | set(FORECAST)),
            "past_days": 2, "forecast_days": 3, "timezone": "GMT", "timeformat": "unixtime"})
        past = [r for r in _hours(data, HOURLY) if r["hour_start"] <= now]
        future = [r for r in _hours(data, FORECAST) if r["hour_start"] > now]
        issued = now.replace(minute=0, second=0, microsecond=0)
        rows = 0
        with pool.connection() as conn, conn.transaction():
            rows += self._store_hours(conn, past, "forecast")
            cols = list(FORECAST.values())
            for r in future:
                cur = conn.execute(
                    f"INSERT INTO weather_forecast (issued_at, hour_start, {', '.join(cols)}) "
                    f"VALUES (%s, %s, {', '.join(['%s'] * len(cols))}) ON CONFLICT DO NOTHING",
                    (issued, r["hour_start"], *[r[c] for c in cols]))
                rows += cur.rowcount
        note = f"{len(past)} past hours, forecast of {len(future)} hours"
        n, backfilled, more = self._backfill(pool)
        if backfilled:
            note += f", archive {backfilled}"
        return Result(rows + n, note, past[-1]["hour_start"] if past else None, more=more)

    @staticmethod
    def _store_hours(conn, rows: list[dict], source: str) -> int:
        """Upsert hourly weather; archive rows never replace values from the forecast model."""
        cols = list(HOURLY.values())
        n = 0
        for r in rows:
            if all(r[c] is None for c in cols):
                continue
            cur = conn.execute(
                f"INSERT INTO weather_hour (hour_start, {', '.join(cols)}, source, updated_at) "
                f"VALUES (%s, {', '.join(['%s'] * len(cols))}, %s, now()) "
                f"ON CONFLICT (hour_start) DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in cols)}, "
                f"source = EXCLUDED.source, updated_at = now() "
                f"WHERE weather_hour.source = 'archive' OR EXCLUDED.source = 'forecast'",
                (r["hour_start"], *[r[c] for c in cols], source))
            n += cur.rowcount
        return n

    def _backfill(self, pool) -> tuple[int, str, bool]:
        """Fetch one more year of reanalysis into weather_hour; the cursor is the next day to fetch.
        Returns (hours stored, what was fetched, whether more years remain)."""
        with pool.connection() as conn:
            start = date.fromisoformat(db.get_state(conn, "weather.backfill_next", BACKFILL_FROM.isoformat()))
        last = date.today() - timedelta(days=6)   # the reanalysis lags about 5 days behind
        end = min(date(start.year, 12, 31), last)
        if start > end:
            return 0, "", False
        data = get_json(ARCHIVE_URL, {
            "latitude": BRNO[0], "longitude": BRNO[1], "hourly": ",".join(HOURLY),
            "start_date": start.isoformat(), "end_date": end.isoformat(), "timezone": "GMT", "timeformat": "unixtime"},
            timeout=180)
        with pool.connection() as conn, conn.transaction():
            n = self._store_hours(conn, _hours(data, HOURLY), "archive")
            db.set_state(conn, "weather.backfill_next", (end + timedelta(days=1)).isoformat())
        return n, f"{start}–{end}", end < last


class Temperatures(Job):
    """Air temperature at the Teplárny Brno sites (spatially averaged, updated about every 15 minutes)."""
    name = "temperatures"
    interval = 600
    stale_after = 2 * 3600

    def run(self, pool) -> Result:
        """Store the current temperature of every site."""
        rows, newest = 0, None
        feats = arcgis_features(TEMPERATURES_URL)
        with pool.connection() as conn, conn.transaction():
            for a in feats:
                at = from_ms(a.get("cas"))
                if a.get("id") is None or at is None:
                    continue
                newest = max(newest or at, at)
                conn.execute("""INSERT INTO temperature_station VALUES (%s, %s, %s, %s, %s)
                                ON CONFLICT (station_id) DO UPDATE SET name = EXCLUDED.name, district = EXCLUDED.district,
                                    lat = EXCLUDED.lat, lon = EXCLUDED.lon""",
                             (a["id"], a.get("nazev"), a.get("mestske_casti"), a.get("latitude"), a.get("longitude")))
                cur = conn.execute("INSERT INTO temperature_observation VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                                   (a["id"], at, a.get("teplota")))
                rows += cur.rowcount
        return Result(rows, f"{len(feats)} sites", newest)
