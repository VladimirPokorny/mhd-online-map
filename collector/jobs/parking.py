"""Free spaces in Brno car parks and P+R (data.brno.cz, live layer updated about every 15 minutes)."""
from ..http import arcgis_features
from ..timeutil import from_local_ms
from . import Job, Result

URL = "https://services6.arcgis.com/fUWVlHWZNxUvTUh8/arcgis/rest/services/carparks_live1/FeatureServer/0"


class Parking(Job):
    """Snapshot of every car park; a repeated snapshot (same time) is skipped by the primary key."""
    name = "parking"
    interval = 300
    stale_after = 3600

    def run(self, pool) -> Result:
        """Store the current state of all car parks."""
        rows, newest = 0, None
        feats = arcgis_features(URL)
        with pool.connection() as conn, conn.transaction():
            for a in feats:
                lot = (a.get("name") or "").strip()
                at = from_local_ms(a.get("startdate"))
                if not lot or at is None:
                    continue
                newest = max(newest or at, at)
                conn.execute("""INSERT INTO parking_lot (lot, lat, lon, capacity, updated) VALUES (%s, %s, %s, %s, now())
                                ON CONFLICT (lot) DO UPDATE SET lat = EXCLUDED.lat, lon = EXCLUDED.lon,
                                    capacity = EXCLUDED.capacity, updated = now()""",
                             (lot, a.get("Latitude"), a.get("Longitude"), a.get("capacity")))
                cur = conn.execute("""INSERT INTO parking_observation VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                      ON CONFLICT DO NOTHING""",
                                   (lot, at, a.get("capacity"), a.get("CapacityForPublic"), a.get("free"),
                                    a.get("spacesSubscribersVacant"), a.get("spacesSubscribersOccupied"),
                                    a.get("spacesAllUsersVacant"), a.get("spacesAllUsersOccupied"), a.get("cars")))
                rows += cur.rowcount
        return Result(rows, f"{len(feats)} car parks", newest)
