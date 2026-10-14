"""IDS JMK diversions, closures and moved stops (data.brno.cz, from the mapa.idsjmk.cz API, daily)."""
import re

from psycopg.types.json import Jsonb

from ..http import arcgis_features
from ..timeutil import parse_local
from . import Job, Result

URL = "https://gis.brno.cz/ags1/rest/services/Hosted/IDS_JMK_v%C3%BDluky_a_odklony_zdroj_GeoJSON/FeatureServer/0"


def _float(v):
    """A coordinate published as text; None if missing or not a number."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class IdsDiversions(Job):
    """Keeps every diversion ever published: first and last time it was seen, latest wording."""
    name = "diversions"
    interval = 3600
    stale_after = 3 * 86400

    def run(self, pool) -> Result:
        """Upsert the diversions published now."""
        feats = arcgis_features(URL)
        rows, newest = 0, None
        with pool.connection() as conn, conn.transaction():
            for a in feats:
                try:
                    did = int(a.get("diversionid"))
                except (TypeError, ValueError):
                    continue
                updated = parse_local(a.get("updatedat"))
                newest = max(newest or updated, updated) if updated else newest
                lines = [x for x in re.split(r"[,;\s]+", a.get("affectedlines") or "") if x]
                cur = conn.execute("""
                    INSERT INTO ids_diversion VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now(), %s)
                    ON CONFLICT (diversion_id) DO UPDATE SET kind = EXCLUDED.kind, title = EXCLUDED.title,
                        stop_label = EXCLUDED.stop_label, affected_lines = EXCLUDED.affected_lines,
                        public_text = EXCLUDED.public_text, valid_from = EXCLUDED.valid_from, valid_to = EXCLUDED.valid_to,
                        updated_at = EXCLUDED.updated_at, lat = EXCLUDED.lat, lon = EXCLUDED.lon,
                        last_seen = now(), raw = EXCLUDED.raw""",
                    (did, a.get("type_"), a.get("title"), a.get("number_"), lines, a.get("publictext"),
                     parse_local(a.get("validfrom")), parse_local(a.get("validto")), updated,
                     _float(a.get("latitude")), _float(a.get("longitude")), Jsonb(a)))
                rows += cur.rowcount
        return Result(rows, f"{len(feats)} published", newest)
