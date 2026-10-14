"""Real track geometry for GTFS lines, taken from OpenStreetMap.

1. Lines mapped in OSM as route relations of the IDS JMK network are matched by
   mode + ref (line number) and their ways are used as the track. A match is only
   accepted when most of the line's GTFS stops lie close to the OSM geometry.
2. Remaining lines are routed through their stops: buses/trolleybuses via the
   public OSRM server, trams/trains as shortest paths over OSM railway tracks.
3. If everything else fails, consecutive stops are joined by straight lines.

All downloads are cached in ``osm_cache/`` so they happen only once.
"""
import hashlib
import heapq
import json
import math
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving/"
USER_AGENT = "idsjmk-map/1.0 (GTFS visualisation)"
NETWORK_REGEX = "Jihomoravsk|JMK"

MODES = {0: "tram", 2: "train", 3: "bus", 4: "ferry", 11: "trolleybus", 800: "trolleybus"}
RAIL_TAGS = {"tram": "^(tram|light_rail)$", "train": "^(rail|light_rail|narrow_gauge)$"}

STOP_MATCH_M = 200       # a stop counts as "on the track" within this distance
MIN_COVERAGE = 0.8       # share of stops that must be on an OSM relation's track
SIMPLIFY_M = 4           # Douglas-Peucker tolerance for the output geometry
MAX_SNAP_M = 400         # max stop -> rail node distance for rail routing


# ---------------------------------------------------------------- geometry ---

def _dist_m(a, b):
    """Approximate distance in metres between two (lat, lon) points."""
    lat = math.radians((a[0] + b[0]) / 2)
    dx = (b[1] - a[1]) * 111_320 * math.cos(lat)
    dy = (b[0] - a[0]) * 110_540
    return math.hypot(dx, dy)


def _point_segment_m(p, a, b):
    """Approximate distance in metres from point ``p`` to the segment ``a``–``b``."""
    k = math.cos(math.radians(p[0]))
    ax, ay = (a[1] - p[1]) * 111_320 * k, (a[0] - p[0]) * 110_540
    bx, by = (b[1] - p[1]) * 111_320 * k, (b[0] - p[0]) * 110_540
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
    return math.hypot(ax + t * dx, ay + t * dy)


def simplify(points, tol_m=SIMPLIFY_M, fixed=frozenset()):
    """Douglas-Peucker; points in ``fixed`` (e.g. junctions) are always kept."""
    if len(points) < 3:
        return points
    keep = [p in fixed for p in points]
    keep[0] = keep[-1] = True
    anchors = [i for i, k in enumerate(keep) if k]
    stack = list(zip(anchors, anchors[1:]))
    while stack:
        i, j = stack.pop()
        best, idx = 0.0, None
        for k in range(i + 1, j):
            d = _point_segment_m(points[k], points[i], points[j])
            if d > best:
                best, idx = d, k
        if idx is not None and best > tol_m:
            keep[idx] = True
            stack += [(i, idx), (idx, j)]
    return [p for p, k in zip(points, keep) if k]


class SegmentGrid:
    """Spatial hash of line segments for 'distance from stop to track' queries."""

    CELL = 0.005

    def __init__(self, lines):
        """Index every segment of ``lines`` in the cells its bounding box touches."""
        self.cells = defaultdict(list)
        for line in lines:
            for a, b in zip(line, line[1:]):
                for cx in range(int(min(a[1], b[1]) / self.CELL), int(max(a[1], b[1]) / self.CELL) + 1):
                    for cy in range(int(min(a[0], b[0]) / self.CELL), int(max(a[0], b[0]) / self.CELL) + 1):
                        self.cells[(cx, cy)].append((a, b))

    def near(self, p, limit_m):
        """Whether any segment lies within ``limit_m`` metres of point ``p``."""
        cx, cy = int(p[1] / self.CELL), int(p[0] / self.CELL)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for a, b in self.cells.get((cx + dx, cy + dy), ()):
                    if _point_segment_m(p, a, b) <= limit_m:
                        return True
        return False


# ---------------------------------------------------------------- downloads ---

def _cached_fetch(cache_dir: Path, key: str, fetch):
    """Return cached JSON for ``key``, or call ``fetch`` and cache its result."""
    path = cache_dir / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".json")
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    data = fetch()
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def _http_json(url, data=None, timeout=600):
    """GET (or POST with ``data``) a URL and parse the JSON response."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _overpass(cache_dir, query):
    """Run an Overpass query, retrying when the server is busy; results are cached."""
    def fetch():
        """POST the query to Overpass, retrying up to 4 times."""
        for attempt in range(4):
            try:
                body = urllib.parse.urlencode({"data": query}).encode()
                return _http_json(OVERPASS_URL, body)
            except Exception as exc:  # Overpass is often busy; back off and retry
                print(f"  Overpass error ({exc}), retrying...")
                time.sleep(15 * (attempt + 1))
        raise RuntimeError("Overpass API unavailable")
    return _cached_fetch(cache_dir, query, fetch)


def _bbox(points, margin=0.05, snap=0.25):
    """Bounding box, widened to a ``snap``-degree grid so that the Overpass query
    (and therefore its cache entry) stays the same when a new timetable moves or
    adds a few stops."""
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    down = lambda v: math.floor((v - margin) / snap) * snap
    up = lambda v: math.ceil((v + margin) / snap) * snap
    return f"{down(min(lats)):.2f},{down(min(lons)):.2f},{up(max(lats)):.2f},{up(max(lons)):.2f}"


# ------------------------------------------------------------- OSM relations ---

def _fetch_relations(cache_dir, bbox):
    """Download the IDS JMK route relations: way geometry and (mode, ref) → sets of way ids."""
    print("  downloading IDS JMK route relations from OpenStreetMap (Overpass)...")
    q = (
        "[out:json][timeout:600];"
        f'relation[type=route][route~"^(bus|tram|trolleybus|train|ferry)$"][network~"{NETWORK_REGEX}"]({bbox})->.r;'
        ".r out body;way(r.r);out geom;"
    )
    data = _overpass(cache_dir, q)
    ways = {
        e["id"]: [(round(g["lat"], 6), round(g["lon"], 6)) for g in e["geometry"]]
        for e in data["elements"] if e["type"] == "way" and "geometry" in e
    }
    relations = defaultdict(list)  # (mode, ref) -> [set(way ids)]
    for e in data["elements"]:
        if e["type"] != "relation":
            continue
        tags = e.get("tags", {})
        track = {
            m["ref"] for m in e.get("members", [])
            if m["type"] == "way" and "platform" not in m.get("role", "") and m["ref"] in ways
        }
        if track:
            for ref in tags.get("ref", "").split(";"):
                relations[(tags.get("route"), ref.strip())].append(track)
    return ways, relations


# ----------------------------------------------------------- routing fallback ---

def _covering_patterns(patterns):
    """Smallest-ish set of stop patterns that together cover every stop pair."""
    chosen, seen = [], set()
    for p in sorted(patterns, key=len, reverse=True):
        pairs = set(zip(p, p[1:]))
        if pairs - seen:
            chosen.append(p)
            seen |= pairs
    return chosen


def _osrm_route(cache_dir, pattern):
    """Road route through a stop pattern from OSRM (in chunks), or None if it fails."""
    lines = []
    for start in range(0, len(pattern) - 1, 90):  # keep well under OSRM waypoint limit
        chunk = pattern[start:start + 91]
        coords = ";".join(f"{lon},{lat}" for lat, lon in chunk)
        url = f"{OSRM_URL}{coords}?overview=full&geometries=geojson"

        def fetch():
            """Ask the public OSRM server for the route of this chunk."""
            time.sleep(1)  # be polite to the public demo server
            return _http_json(url, timeout=60)
        data = _cached_fetch(cache_dir, url, fetch)
        if data.get("code") != "Ok":
            return None
        lines.append([(round(lat, 6), round(lon, 6)) for lon, lat in data["routes"][0]["geometry"]["coordinates"]])
    return [p for line in lines for p in line]


class TrackGraph:
    """Undirected graph over polylines (nodes shared by identical coordinates)."""

    CELL = 0.01

    def __init__(self, polylines, densify_m=None, link_m=None):
        """Build the graph; ``densify_m`` adds intermediate nodes, ``link_m`` joins parallel tracks."""
        self.adj = defaultdict(list)
        for pts in polylines:
            if densify_m:
                pts = _densify(pts, densify_m)
            for a, b in zip(pts, pts[1:]):
                if a == b:
                    continue
                d = _dist_m(a, b)
                self.adj[a].append((b, d))
                self.adj[b].append((a, d))
        self.grid = defaultdict(list)
        for n in self.adj:
            self.grid[(int(n[0] / self.CELL), int(n[1] / self.CELL))].append(n)
        if link_m:
            self._link_parallel(link_m)

    def _link_parallel(self, link_m):
        """Join nearby nodes of different tracks (e.g. the two tracks of a double
        track line), with a penalty so real track is preferred."""
        cell = link_m / 111_000
        fine = defaultdict(list)
        for n in list(self.adj):
            fine[(int(n[0] / cell), int(n[1] / cell))].append(n)
        for (cy, cx), nodes in fine.items():
            near = [m for dy in (0, 1) for dx in (-1, 0, 1) if (dy, dx) != (0, -1)
                    for m in fine.get((cy + dy, cx + dx), ())]
            for n in nodes:
                linked = {m for m, _ in self.adj[n]}
                for m in near:
                    if m != n and m not in linked:
                        d = _dist_m(n, m)
                        if d <= link_m:
                            self.adj[n].append((m, d * 3 + 20))
                            self.adj[m].append((n, d * 3 + 20))

    def nearest(self, p, max_m=MAX_SNAP_M):
        """The graph node closest to ``p`` within ``max_m`` metres, or None."""
        cy, cx = int(p[0] / self.CELL), int(p[1] / self.CELL)
        cands = [n for dy in (-1, 0, 1) for dx in (-1, 0, 1) for n in self.grid.get((cy + dy, cx + dx), ())]
        best = min(cands, key=lambda n: _dist_m(p, n), default=None)
        return best if best is not None and _dist_m(p, best) <= max_m else None

    def path(self, a, b, max_ratio=None):
        """A* shortest path between two graph nodes.

        With ``max_ratio`` the search gives up once the path would be longer than
        ``max_ratio`` x the straight-line distance (guards against detours through
        unrelated branches)."""
        limit = max(_dist_m(a, b) * max_ratio, 1000) if max_ratio else math.inf
        open_ = [(_dist_m(a, b), 0.0, a)]
        g, prev = {a: 0.0}, {}
        while open_:
            f, cost, n = heapq.heappop(open_)
            if f > limit:
                return None
            if n == b:
                out = [n]
                while n in prev:
                    n = prev[n]
                    out.append(n)
                return out[::-1]
            if cost > g[n]:
                continue
            for m, d in self.adj[n]:
                c = cost + d
                if c < g.get(m, math.inf):
                    g[m], prev[m] = c, n
                    heapq.heappush(open_, (c + _dist_m(m, b), c, m))
        return None

    def route(self, pattern):
        """Path through a stop pattern: (points, True if every leg was on the track), or None."""
        nodes = [self.nearest(p) for p in pattern]
        if sum(n is not None for n in nodes) < 2:
            return None
        out, ok = [], True
        for (pa, na), (pb, nb) in zip(zip(pattern, nodes), zip(pattern[1:], nodes[1:])):
            seg = self.path(na, nb) if na and nb else None
            if seg is None:
                ok = False
                seg = [pa, pb]
            out += seg if not out else seg[1:]
        return out, ok


def _densify(points, step_m):
    """Add points so that no two consecutive points are more than ``step_m`` metres apart."""
    out = [points[0]]
    for a, b in zip(points, points[1:]):
        n = int(_dist_m(a, b) // step_m)
        for k in range(1, n + 1):
            t = k / (n + 1)
            out.append((round(a[0] + (b[0] - a[0]) * t, 6), round(a[1] + (b[1] - a[1]) * t, 6)))
        out.append(b)
    return out


def _rail_graph(cache_dir, mode, bbox):
    """Download the tram or train tracks in ``bbox`` and build a routing graph."""
    print(f"  downloading {mode} tracks for routing ({bbox})...")
    q = (
        "[out:json][timeout:600];"
        f'way[railway~"{RAIL_TAGS[mode]}"][service!~"^(yard|siding)$"]({bbox});out geom;'
    )
    data = _overpass(cache_dir, q)
    return TrackGraph(
        [(round(g["lat"], 7), round(g["lon"], 7)) for g in e.get("geometry", [])]
        for e in data["elements"]
    )


def segment_paths(ways, route_ways, stop_pairs):
    """Track geometry between consecutive stops of one line, for vehicle positions.

    ``stop_pairs`` is a set of ((lat, lon), (lat, lon)) pairs. Returns a dict
    pair -> polyline that starts and ends at the stops' snapped track positions.
    Pairs that cannot be routed on the line's own track get a straight line.
    """
    graph = TrackGraph((ways[w] for w in route_ways), densify_m=25, link_m=15)
    snapped = {}
    out = {}
    for a, b in stop_pairs:
        for p in (a, b):
            if p not in snapped:
                snapped[p] = graph.nearest(p, 150)
        na, nb = snapped[a], snapped[b]
        path = graph.path(na, nb, max_ratio=3) if na and nb else None
        out[(a, b)] = simplify(path) if path and len(path) > 1 else [a, b]
    return out


# ------------------------------------------------------------------- driver ---

def build_tracks(lines, cache_dir: Path):
    """Compute track geometry for every line.

    ``lines``: list of dicts with ``id``, ``short``, ``route_type`` and
    ``patterns`` (list of stop-coordinate sequences, one per distinct trip pattern).

    Returns ``(ways, tracks)`` where ``ways`` is a list of polylines and
    ``tracks[route_id] = {"source": ..., "ways": [indices into ways]}``.
    """
    cache_dir.mkdir(exist_ok=True)
    all_stops = [p for line in lines for pat in line["patterns"] for p in pat]
    osm_ways, relations = _fetch_relations(cache_dir, _bbox(all_stops))

    ways, way_index, tracks, todo = [], {}, {}, []
    node_use = defaultdict(int)
    for pts in osm_ways.values():
        for p in set(pts):
            node_use[p] += 1
    junctions = {p for p, n in node_use.items() if n > 1}

    def add_way(key, points):
        """Index of a way in the output list, adding it (simplified) on first use."""
        if key not in way_index:
            way_index[key] = len(ways)
            ways.append(simplify(points, fixed=junctions))
        return way_index[key]

    for line in lines:
        mode = MODES.get(line["route_type"], "bus")
        way_ids = set().union(*relations.get((mode, line["short"]), [set()]))
        stops = {p for pat in line["patterns"] for p in pat}
        if way_ids:
            grid = SegmentGrid([osm_ways[w] for w in way_ids])
            coverage = sum(grid.near(s, STOP_MATCH_M) for s in stops) / len(stops)
            if coverage >= MIN_COVERAGE:
                tracks[line["id"]] = {
                    "source": "osm",
                    "ways": sorted(add_way(("osm", w), osm_ways[w]) for w in way_ids),
                }
                continue
            print(f"  line {line['short']}: OSM relation covers only {coverage:.0%} of stops, rerouting")
        todo.append((line, mode))

    rail_graphs = {}
    for mode in ("tram", "train"):
        pts = [p for line, m in todo if m == mode for pat in line["patterns"] for p in pat]
        if pts:
            rail_graphs[mode] = _rail_graph(cache_dir, mode, _bbox(pts, 0.1))

    for line, mode in todo:
        source, polylines = "routed", []
        for i, pat in enumerate(_covering_patterns(line["patterns"])):
            if len(pat) < 2:
                continue
            if mode in rail_graphs:
                res = rail_graphs[mode].route(pat)
                if res is None:
                    source = "straight"
                    polylines.append(pat)
                else:
                    polylines.append(res[0])
                    if not res[1]:
                        source = "partial"
            elif mode in ("bus", "trolleybus"):
                pts = _osrm_route(cache_dir, pat)
                if pts is None:
                    source = "straight"
                polylines.append(pts or pat)
            else:
                source = "straight"
                polylines.append(pat)
        tracks[line["id"]] = {
            "source": source,
            "ways": [add_way(("route", line["id"], i), pl) for i, pl in enumerate(polylines)],
        }
        print(f"  line {line['short']} ({mode}): {source}")

    return ways, tracks
