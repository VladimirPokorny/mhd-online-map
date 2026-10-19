"""Car traffic and road events in Brno from dopravniinfo.cz (the feed the live map shows).

Speeds come from vehicles driving through a segment (floating car data) and are updated about
every minute. Only changes are kept: a row when a segment's traffic level or its delay in whole
minutes changes. That keeps every jam with its start, end and worst delay, at about a quarter of
the updates.
"""
import math
from datetime import datetime

from psycopg.types.json import Jsonb

from live import TRAFFIC_URL, _from_krovak, segment_geometry

from .. import db
from ..http import get_json
from . import Job, Result


def _time(text) -> datetime | None:
    """A UTC time as published ("2026-10-19 13:07:12Z"); None if missing or not a time."""
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _geometry(geo):
    """Decoded geometry of a segment or event; None if missing or not decodable."""
    try:
        return segment_geometry(geo) if geo else None
    except (ValueError, IndexError):
        return None


def _point(x, y) -> tuple[float | None, float | None]:
    """(lat, lon) of S-JTSK coordinates; (None, None) if missing."""
    try:
        lons, lats = _from_krovak([float(x)], [float(y)])
    except (TypeError, ValueError):
        return None, None
    if not (math.isfinite(lats[0]) and math.isfinite(lons[0])):
        return None, None
    return round(lats[0], 6), round(lons[0], 6)


class Traffic(Job):
    """Every 2 minutes: the traffic on every segment that changed, and the road events published now."""
    name = "traffic"
    interval = 120
    stale_after = 30 * 60

    def __init__(self):
        self.last = {}      # segment → (level, delay in whole minutes) of its last stored row; None = no data
        self.stamps = {}    # segment → time of its last measurement seen
        self.known = set()  # segments whose traffic_segment row is up to date

    def run(self, pool) -> Result:
        """Store the segments that changed since the last run, new segments and the road events."""
        data = get_json(TRAFFIC_URL)
        now = db.utc_now()
        samples, segments, last, stamps, newest = [], [], {}, {}, None
        for f in data.get("Fcd") or []:
            try:
                sid = int(f["Id"])
            except (KeyError, TypeError, ValueError):
                continue
            if sid not in self.known:
                segments.append((sid, f.get("SectionName"), f.get("RoadClass"), f.get("RoadNumber"),
                                 f.get("ChainageFrom"), f.get("ChainageTo"), f.get("Direction"), f.get("Length"),
                                 f.get("Geo"), Jsonb(_geometry(f.get("Geo")))))
            stamp = _time(f.get("StampUtc"))
            if stamp is None or f.get("VD10") is None:
                continue
            newest = max(newest or stamp, stamp)
            stamps[sid] = stamp
            if self.stamps.get(sid) == stamp:
                continue   # not measured again since the last run
            delay = f.get("VD4")
            key = (f["VD10"], None if delay is None else round(delay / 60))
            if self.last.get(sid) == key:
                continue
            last[sid] = key
            samples.append((sid, stamp, f["VD10"], f.get("VD2"), f.get("VD5"), f.get("VD3"), f.get("VD6"), delay,
                            f.get("VD1")))
        # Segments that left the feed: no vehicle has passed them for a while.
        for sid, key in self.last.items():
            if key is not None and sid not in stamps:
                last[sid] = None
                samples.append((sid, now, None, None, None, None, None, None, None))

        events = []
        for i in data.get("Information") or []:
            try:
                eid = int(i["Id"])
            except (KeyError, TypeError, ValueError):
                continue
            lat, lon = _point(i.get("X"), i.get("Y"))
            events.append((eid, i.get("Guid"), i.get("Version"), i.get("Title"), i.get("Icon"), i.get("Locality"),
                           i.get("Description"), _time(i.get("ValidFrom")), _time(i.get("ValidTo")),
                           i.get("RoadNumber"), i.get("Chainage"), i.get("Direction"), lat, lon,
                           Jsonb(_geometry(i.get("Geo"))), now, now, Jsonb(i)))

        with pool.connection() as conn, conn.transaction():
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO traffic_segment VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (segment_id) DO UPDATE SET section_name = EXCLUDED.section_name,
                        road_class = EXCLUDED.road_class, road_number = EXCLUDED.road_number,
                        chainage_from = EXCLUDED.chainage_from, chainage_to = EXCLUDED.chainage_to,
                        direction = EXCLUDED.direction, length_m = EXCLUDED.length_m,
                        geo = EXCLUDED.geo, geometry = EXCLUDED.geometry
                    WHERE (traffic_segment.section_name, traffic_segment.road_class, traffic_segment.road_number,
                           traffic_segment.chainage_from, traffic_segment.chainage_to, traffic_segment.direction,
                           traffic_segment.length_m, traffic_segment.geo)
                          IS DISTINCT FROM (EXCLUDED.section_name, EXCLUDED.road_class, EXCLUDED.road_number,
                           EXCLUDED.chainage_from, EXCLUDED.chainage_to, EXCLUDED.direction,
                           EXCLUDED.length_m, EXCLUDED.geo)""", segments)
                written = cur.rowcount if segments else 0
                cur.executemany("INSERT INTO traffic_sample VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                                "ON CONFLICT DO NOTHING", samples)
                written += cur.rowcount if samples else 0
                cur.executemany("""
                    INSERT INTO road_event VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (event_id) DO UPDATE SET guid = EXCLUDED.guid, version = EXCLUDED.version,
                        title = EXCLUDED.title, icon = EXCLUDED.icon, locality = EXCLUDED.locality,
                        description = EXCLUDED.description, valid_from = EXCLUDED.valid_from,
                        valid_to = EXCLUDED.valid_to, road_number = EXCLUDED.road_number,
                        chainage = EXCLUDED.chainage, direction = EXCLUDED.direction, lat = EXCLUDED.lat,
                        lon = EXCLUDED.lon, geometry = EXCLUDED.geometry, last_seen = EXCLUDED.last_seen,
                        raw = EXCLUDED.raw""", events)
        # Only after the commit: a failed run is repeated in full by the next one.
        self.known.update(s[0] for s in segments)
        self.last.update(last)
        self.stamps = stamps
        jams = sum(1 for key in self.last.values() if key and key[0] >= 4)
        return Result(written,
                      f"{len(stamps)} segments ({jams} jammed), {len(samples)} changes, {len(events)} road events",
                      newest)
