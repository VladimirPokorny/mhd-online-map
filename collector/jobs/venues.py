"""People in STAREZ swimming pools, ice rinks and wellness (data.brno.cz live layer)."""
from ..http import arcgis_features, layer_info
from ..timeutil import from_ms
from . import Job, Result

URL = "https://services6.arcgis.com/fUWVlHWZNxUvTUh8/arcgis/rest/services/starez4/FeatureServer/0"


class Venues(Job):
    """The layer has no timestamp per venue; the time of its last data edit stands in for it,
    so an unchanged layer adds nothing."""
    name = "venues"
    interval = 300
    stale_after = 6 * 3600

    def run(self, pool) -> Result:
        """Store the occupancy of every venue if the layer changed since the last run."""
        edit = layer_info(URL).get("editingInfo") or {}
        at = from_ms(edit.get("dataLastEditDate") or edit.get("lastEditDate"))
        if at is None:
            raise RuntimeError("the layer reports no time of its last edit")
        rows = 0
        feats = arcgis_features(URL)
        with pool.connection() as conn, conn.transaction():
            for a in feats:
                venue = (a.get("Name") or "").strip()
                if not venue:
                    continue
                cur = conn.execute("INSERT INTO venue_occupancy VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                                   (venue, at, a.get("Capacity"), a.get("Count_"), a.get("Day_Count")))
                rows += cur.rowcount
        return Result(rows, f"{len(feats)} venues" if rows else "layer unchanged", at)
