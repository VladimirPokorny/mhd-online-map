"""Hourly counts of cyclists and pedestrians: Brno counting cameras (live and history) and TSB sensors."""
from datetime import timedelta

from .. import db
from ..http import arcgis_features, arcgis_pages
from ..timeutil import from_ms, local_hour
from . import Job, Result

LIVE_URL = "https://services6.arcgis.com/fUWVlHWZNxUvTUh8/arcgis/rest/services/cyklodetektory/FeatureServer/0"
HISTORY_URL = "https://gis.brno.cz/ags1/rest/services/Hosted/detektory_all/FeatureServer/0"
TSB_URL = "https://gis.brno.cz/ags1/rest/services/Hosted/TSB_senzory/FeatureServer/0"
PAGES_PER_RUN = 150   # a backfill continues over several runs instead of one very long one


def _int(v):
    """A count as int (some layers publish doubles); None stays None."""
    return None if v is None else int(round(v))


class BikeCountsLive(Job):
    """The last full hour of every counting camera, both directions."""
    name = "bikes_live"
    interval = 900
    stale_after = 4 * 3600

    def run(self, pool) -> Result:
        """Store the latest hour of each camera; hours already stored are skipped."""
        rows, newest = 0, None
        feats = arcgis_features(LIVE_URL)
        with pool.connection() as conn, conn.transaction():
            for a in feats:
                end = from_ms(a.get("EndOfInterval"))
                if not a.get("UnitId") or end is None:
                    continue
                newest = max(newest or end, end)
                station = f"{a['UnitId']}/{a.get('LocationId') or 0}"
                conn.execute("""INSERT INTO bike_station VALUES ('live', %s, %s, %s, %s)
                                ON CONFLICT (source, station) DO UPDATE SET name = EXCLUDED.name,
                                    lat = EXCLUDED.lat, lon = EXCLUDED.lon""",
                             (station, a.get("UnitName"), a.get("Latitude"), a.get("Longitude")))
                for side in ("First", "Second"):
                    direction = a.get(f"{side}Direction_Name")
                    if not direction:
                        continue
                    cur = conn.execute("INSERT INTO bike_count VALUES ('live', %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                                       (station, direction, end - timedelta(hours=1), a.get(f"{side}Direction_Cyclists"),
                                        a.get(f"{side}Direction_Pedestrians"), a.get(f"{side}Direction_Total")))
                    rows += cur.rowcount
        return Result(rows, f"{len(feats)} cameras", newest)


class _Incremental(Job):
    """A large layer read by object id: every page is stored together with the cursor, so a
    failed or interrupted run resumes where it stopped, and later runs fetch only new rows."""
    url = ""
    fields = ""
    interval = 1800
    retry = 300

    def store(self, conn, feats: list[dict]) -> tuple[int, object]:
        """Store one page; returns (rows written, newest time in the page)."""
        raise NotImplementedError

    def run(self, pool) -> Result:
        """Fetch up to PAGES_PER_RUN new pages."""
        key = f"{self.name}.last_objectid"
        with pool.connection() as conn:
            after = db.get_state(conn, key, 0)
        rows = pages = 0
        newest = None
        for feats in arcgis_pages(self.url, "objectid", after, outFields=self.fields):
            with pool.connection() as conn, conn.transaction():
                n, t = self.store(conn, feats)
                db.set_state(conn, key, feats[-1]["objectid"])
            rows += n
            newest = max(newest or t, t) if t else newest
            pages += 1
            if pages >= PAGES_PER_RUN:
                return Result(rows, f"{pages} pages, more to fetch", newest, more=True)
        return Result(rows, f"{pages} pages" if pages else "no new rows", newest)


class BikeCountsHistory(_Incremental):
    """All hourly counts of the cameras since 2020 (detector and direction numbers)."""
    name = "bikes_history"
    url = HISTORY_URL
    fields = "objectid,detektor_id,smer_id,date_,hour_,cyclists,pedestrians,total"

    def store(self, conn, feats):
        """Store a page of the history layer."""
        rows = []
        for a in feats:
            if a.get("detektor_id") is None or a.get("date_") is None or a.get("hour_") is None:
                continue
            rows.append((str(a["detektor_id"]), str(a.get("smer_id") or 0), local_hour(a["date_"], int(a["hour_"])),
                         _int(a.get("cyclists")), _int(a.get("pedestrians")), _int(a.get("total"))))
        with conn.cursor() as cur:
            cur.executemany("""INSERT INTO bike_count VALUES ('history', %s, %s, %s, %s, %s, %s)
                               ON CONFLICT (source, station, direction, hour_start) DO UPDATE SET
                                   cyclists = EXCLUDED.cyclists, pedestrians = EXCLUDED.pedestrians, total = EXCLUDED.total""",
                            rows)
        return len(rows), max((r[2] for r in rows), default=None)


class TsbCounts(_Incremental):
    """Hourly counts of the Technické sítě Brno sensors since 2023."""
    name = "tsb_counts"
    url = TSB_URL
    fields = "objectid,sensor_id,category_id,direction,count_,place,date_,hour_int,latitude,longitude"

    def store(self, conn, feats):
        """Store a page of the sensor layer."""
        rows = []
        for a in feats:
            if a.get("sensor_id") is None or a.get("date_") is None or a.get("hour_int") is None:
                continue
            rows.append((a["sensor_id"], a.get("category_id") or 0, a.get("direction") or "",
                         local_hour(a["date_"], int(a["hour_int"])), a.get("count_"), a.get("place"),
                         a.get("latitude"), a.get("longitude")))
        with conn.cursor() as cur:
            cur.executemany("""INSERT INTO tsb_count VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                               ON CONFLICT (sensor_id, category_id, direction, hour_start) DO UPDATE SET
                                   count = EXCLUDED.count""", rows)
        return len(rows), max((r[3] for r in rows), default=None)
