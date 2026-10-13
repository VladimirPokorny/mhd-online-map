"""Live state of IDS JMK public transport: where every vehicle is right now and how late.

Everything is kept in memory; nothing is stored. Sources:
  * positions and delays – the KORDIS vehicle stream (WebSocket on gis.brno.cz,
    one message per vehicle about every 30 s);
  * which timetable trip a vehicle runs – KORDIS GTFS Realtime: its vehicle label is
    the stream's vehicle number, and it carries the GTFS trip_id. With the trip, the
    remaining stops get their scheduled and expected times;
  * service alerts per line (closures, diversions, moved stops) – GTFS Realtime;
  * car traffic and road events in Brno (closures, roadworks) – dopravniinfo.cz
    (Národní dopravní informační centrum). These are the endpoints its public map
    uses, not a documented API; the official equivalent is the NDIC DATEX II feed.
"""
import json
import math
import threading
import time
import traceback
import urllib.request
from collections import deque

STREAM_URL = "wss://gis.brno.cz/geoevent/ws/services/Kordis_stream/StreamServer/subscribe"
GTFS_RT_URL = "https://kordis-jmk.cz/gtfs/gtfsReal.dat"
TRAFFIC_URL = "https://api.dopravniinfo.cz/TownData.ashx?town=582786"  # 582786 = Brno
RT_POLL_S = 30
TRAFFIC_POLL_S = 120  # the dopravniinfo.cz map itself refreshes every 2 minutes
TRAIL = 120  # samples kept per vehicle, about an hour
USER_AGENT = "idsjmk-map/1.0 (live map)"
# GTFS Realtime VehicleStopStatus
INCOMING_AT, STOPPED_AT, IN_TRANSIT_TO = 0, 1, 2


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _text(translated) -> str:
    for t in translated.translation:
        return t.text
    return ""


# ------------------------------------------------- dopravniinfo.cz geometry ---

def _polyline_ints(s: str) -> list[int]:
    """Values of a Google-encoded polyline string (no scaling)."""
    out, i = [], 0
    while i < len(s):
        res = shift = 0
        while True:
            b = ord(s[i]) - 63
            i += 1
            res |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        out.append(~(res >> 1) if res & 1 else res >> 1)
    return out


_krovak = None


def _from_krovak(xs, ys):
    """S-JTSK / Křovák (EPSG:5514) metres → (lons, lats)."""
    global _krovak
    if _krovak is None:
        from pyproj import Transformer
        _krovak = Transformer.from_crs("EPSG:5514", "EPSG:4326", always_xy=True)
    return _krovak.transform(xs, ys)


def segment_geometry(geo: str) -> list:
    """Decode a dopravniinfo.cz segment geometry into polylines of [lat, lon].

    The string is "<points per part> <coordinates>", both polyline-encoded; the
    coordinates are delta-encoded whole metres in S-JTSK / Křovák (EPSG:5514)."""
    counts, coords = geo.split(" ", 1)
    v = _polyline_ints(coords)
    xs, ys, x, y = [], [], 0, 0
    for dx, dy in zip(v[0::2], v[1::2]):
        x += dx
        y += dy
        xs.append(x)
        ys.append(y)
    lons, lats = _from_krovak(xs, ys)
    pts = [[round(a, 5), round(b, 5)] for a, b in zip(lats, lons)]
    out, i = [], 0
    for n in _polyline_ints(counts) or [len(pts)]:
        if n > 1:
            out.append(pts[i:i + n])
        i += n
    return out


# --------------------------------------------------------------- the state ---

class LiveState:
    def __init__(self):
        self.lock = threading.Lock()
        self.vehicles = {}    # vehicle number → latest state and trail
        self.trips = {}       # vehicle number → (trip_id, stop_id, stop status)
        self.alerts = {}      # LineID → [alert, …]
        self.events = []      # road events in Brno
        self.segments = {}    # traffic segment id → (name, road, [polyline, …])
        self.traffic = {}     # traffic segment id → (speed / free-flow speed, delay s)
        self.connected = False
        self.received = 0
        self.rt_fetched = self.traffic_fetched = None
        self.traffic_stamp = None

    def start(self, traffic: bool = True):
        for target in (self.run_stream, self.run_rt) + ((self.run_traffic,) if traffic else ()):
            threading.Thread(target=target, daemon=True).start()

    # ------------------------------------------------------ vehicle stream ---

    def run_stream(self):
        import websocket  # websocket-client
        backoff = 5
        while True:
            try:
                ws = websocket.create_connection(STREAM_URL, timeout=120, header=[f"User-Agent: {USER_AGENT}"])
                self.connected, backoff = True, 5
                print("Live: connected to the vehicle stream", flush=True)
                while True:
                    try:
                        self.update(json.loads(ws.recv())["attributes"])
                    except (KeyError, TypeError, ValueError):
                        continue
            except Exception as exc:
                self.connected = False
                print(f"Live: vehicle stream {type(exc).__name__}: {exc}; reconnecting in {backoff}s", flush=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 600)

    def update(self, a: dict):
        """One message of the KORDIS stream (its "attributes")."""
        try:
            vid, ms = int(a["ID"]), int(a["TimeUpdated"])
            lat, lon = float(a["Lat"]), float(a["Lng"])
        except (KeyError, TypeError, ValueError):
            return
        line = int(a.get("LineID") or 0)
        course = a.get("Course") or ""
        delay = a.get("Delay")
        with self.lock:
            self.received += 1
            v = self.vehicles.get(vid)
            if v is None:
                v = self.vehicles[vid] = {"trail": deque(maxlen=TRAIL)}
            if v.get("ms") == ms:
                return
            if v.get("run") != (line, course):
                v["trail"].clear()  # a new run starts a new trail
            v.update(
                ms=ms, lat=lat, lon=lon, line=line, run=(line, course), course=course,
                delay=None if delay is None else float(delay), bearing=a.get("Bearing"),
                lf=str(a.get("LF")).lower() == "true", inactive=str(a.get("IsInactive")).lower() == "true",
                last_stop=a.get("LastStopID"), final_stop=a.get("FinalStopID"), vtype=a.get("VType"),
            )
            v["trail"].append((ms, round(lat, 6), round(lon, 6), v["delay"]))
            if self.received % 5000 == 0:
                self._prune()

    def _prune(self):
        """Forget vehicles not heard from for 6 hours (the caller holds the lock)."""
        cutoff = (time.time() - 6 * 3600) * 1000
        for vid in [k for k, v in self.vehicles.items() if v["ms"] < cutoff]:
            del self.vehicles[vid]

    def snapshot(self, max_age_s: int = 1800) -> list:
        """Vehicles heard from in the last ``max_age_s`` seconds: (number, state) pairs."""
        cutoff = (time.time() - max_age_s) * 1000
        with self.lock:
            return [(vid, {k: v[k] for k in v if k != "trail"}) for vid, v in self.vehicles.items() if v["ms"] >= cutoff]

    def vehicle(self, vid: int):
        with self.lock:
            v = self.vehicles.get(vid)
            return None if v is None else {**v, "trail": list(v["trail"])}

    # ------------------------------------------------------- GTFS Realtime ---

    def run_rt(self):
        while True:
            try:
                self.poll_rt()
            except Exception:
                print("Live: GTFS Realtime poll failed:", flush=True)
                traceback.print_exc()
            time.sleep(RT_POLL_S)

    def poll_rt(self):
        from google.transit import gtfs_realtime_pb2
        feed = gtfs_realtime_pb2.FeedMessage()
        feed.ParseFromString(_get(GTFS_RT_URL))
        trips, alerts = {}, {}
        for e in feed.entity:
            if e.HasField("vehicle"):
                v = e.vehicle
                try:
                    vid = int(v.vehicle.label)
                except ValueError:
                    continue
                if v.trip.trip_id:
                    trips[vid] = (v.trip.trip_id, v.stop_id, v.current_status)
            elif e.HasField("alert"):
                a = e.alert
                item = {
                    "header": _text(a.header_text), "description": _text(a.description_text).strip(),
                    "url": _text(a.url), "effect": a.Effect.Name(a.effect),
                    "from": a.active_period[0].start if a.active_period else None,
                    "to": a.active_period[0].end if a.active_period else None,
                }
                for ie in a.informed_entity:
                    if ie.route_id.isdigit():
                        alerts.setdefault(int(ie.route_id), []).append(item)
        with self.lock:
            self.trips, self.alerts = trips, alerts
            self.rt_fetched = time.time()

    def trip_of(self, vid: int):
        with self.lock:
            return self.trips.get(vid)

    def alerts_of(self, line: int) -> list:
        with self.lock:
            return list(self.alerts.get(line, []))

    # --------------------------------------------- car traffic, road events ---

    def run_traffic(self):
        time.sleep(10)
        while True:
            try:
                self.poll_traffic()
            except Exception as exc:
                print(f"Live: traffic poll failed: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(TRAFFIC_POLL_S)

    def poll_traffic(self):
        data = json.loads(_get(TRAFFIC_URL).decode("utf-8-sig"))
        segments, traffic, stamp = dict(self.segments), {}, None
        for f in data.get("Fcd") or []:
            sid = f.get("Id")
            if sid not in segments and f.get("Geo"):
                try:
                    segments[sid] = (f.get("SectionName"), f.get("RoadNumber"), segment_geometry(f["Geo"]))
                except (ValueError, IndexError):
                    continue
            if f.get("VD2") is not None and f.get("VD5"):
                traffic[sid] = (round(f["VD2"] / f["VD5"], 3), f.get("VD4"))
                stamp = max(stamp or "", f.get("StampUtc") or "")
        events = []
        for i in data.get("Information") or []:
            try:
                lons, lats = _from_krovak([float(i["X"])], [float(i["Y"])])
            except (KeyError, TypeError, ValueError):
                continue
            if not (math.isfinite(lats[0]) and math.isfinite(lons[0])):
                continue
            events.append({
                "title": i.get("Title"), "locality": i.get("Locality"), "description": i.get("Description"),
                "from": i.get("ValidFrom"), "to": i.get("ValidTo"), "icon": i.get("Icon"),
                "lat": round(lats[0], 5), "lon": round(lons[0], 5),
            })
        with self.lock:
            self.segments, self.traffic, self.events = segments, traffic, events
            self.traffic_stamp, self.traffic_fetched = stamp, time.time()
