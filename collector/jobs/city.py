"""City life: water supply outages (BVK) and cultural events (GoToBrno)."""
from datetime import datetime

from psycopg.types.json import Jsonb

from ..config import PRAGUE
from ..http import arcgis_pages, layer_info
from ..timeutil import from_ms, parse_local
from . import Job, Result

WATER_URL = "https://services6.arcgis.com/fUWVlHWZNxUvTUh8/arcgis/rest/services/havarie/FeatureServer/0"
EVENTS_URL = "https://services6.arcgis.com/fUWVlHWZNxUvTUh8/arcgis/rest/services/Events/FeatureServer/0"


def _event_time(ms):
    """Event dates are published as midnight UTC of the day (no time); that day's local midnight is stored."""
    if ms is None or ms % 86_400_000:
        return from_ms(ms)
    day = from_ms(ms).date()
    return datetime(day.year, day.month, day.day, tzinfo=PRAGUE)


def _edited(url: str):
    """When the publisher last changed the layer's data (the records themselves carry no such time)."""
    edit = layer_info(url).get("editingInfo") or {}
    return from_ms(edit.get("dataLastEditDate") or edit.get("lastEditDate"))


def _all(url: str, oid: str = "ObjectId") -> list[dict]:
    """Every feature of a layer, however many pages it takes."""
    return [a for page in arcgis_pages(url, oid, 0) for a in page]


class WaterOutages(Job):
    """Keeps every outage ever published, with the first and last time it was seen."""
    name = "water"
    interval = 3600
    stale_after = 7 * 86400

    def run(self, pool) -> Result:
        """Upsert the outages published now."""
        feats = _all(WATER_URL)
        rows = 0
        with pool.connection() as conn, conn.transaction():
            for a in feats:
                if not a.get("id"):
                    continue
                restricted = (a.get("omezeni_dodavky") or "").strip().lower()
                cur = conn.execute("""
                    INSERT INTO water_outage VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), now())
                    ON CONFLICT (outage_id) DO UPDATE SET starts_at = EXCLUDED.starts_at, ends_at = EXCLUDED.ends_at,
                        supply_restricted = EXCLUDED.supply_restricted, replacement = EXCLUDED.replacement,
                        municipality = EXCLUDED.municipality, street = EXCLUDED.street, note = EXCLUDED.note,
                        lat = EXCLUDED.lat, lon = EXCLUDED.lon, last_seen = now()""",
                    (a["id"], parse_local(a.get("datum_zahajeni")), parse_local(a.get("datum_ukonceni")),
                     {"ano": True, "ne": False}.get(restricted), a.get("nahradni_zasobovani"), a.get("obec"),
                     a.get("ulice"), a.get("poznamka_full"), a.get("latitude"), a.get("longitude")))
                rows += cur.rowcount
        return Result(rows, f"{len(feats)} published", _edited(WATER_URL))


class CityEvents(Job):
    """Keeps every event ever announced (name, dates, place, categories and the full record)."""
    name = "events"
    interval = 6 * 3600
    retry = 600
    stale_after = 7 * 86400

    def run(self, pool) -> Result:
        """Upsert the events announced now."""
        feats = _all(EVENTS_URL)
        rows = 0
        with pool.connection() as conn, conn.transaction():
            for a in feats:
                if a.get("ID") is None:
                    continue
                # Long texts stay in the source; the stored record keeps everything else.
                raw = {k: v for k, v in a.items() if k not in ("text", "text_en")}
                cur = conn.execute("""
                    INSERT INTO city_event VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now(), %s)
                    ON CONFLICT (event_id) DO UPDATE SET name = EXCLUDED.name, categories = EXCLUDED.categories,
                        starts_at = EXCLUDED.starts_at, ends_at = EXCLUDED.ends_at, url = EXCLUDED.url,
                        lat = EXCLUDED.lat, lon = EXCLUDED.lon, last_seen = now(), raw = EXCLUDED.raw""",
                    (str(a["ID"]), a.get("name"), a.get("categories"), _event_time(a.get("date_from")),
                     _event_time(a.get("date_to")), a.get("url"), a.get("latitude"), a.get("longitude"), Jsonb(raw)))
                rows += cur.rowcount
        return Result(rows, f"{len(feats)} announced", _edited(EVENTS_URL))
