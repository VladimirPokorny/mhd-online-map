"""Load a GTFS feed and turn it into a compact network description for the map.

The IDS JMK feed has no shapes.txt, so track geometry comes from OpenStreetMap
(see osm_tracks.py).
"""
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

import pandas as pd

from osm_tracks import build_tracks, segment_paths

ROUTE_TYPES = {
    0: "Tram",
    2: "Train",
    3: "Bus",
    4: "Ferry",
    11: "Trolleybus",
    800: "Trolleybus",
}

CACHE_VERSION = 3
# A stop this far from the middle of the network has broken coordinates (the feed has had a latitude
# of 8.80 instead of 48.80); the network spans about 150 km.
STOP_OUTLIER_KM = 250


def _read(gtfs_dir: Path, name: str, **kwargs) -> pd.DataFrame:
    """Read one GTFS file of the feed into a DataFrame."""
    return pd.read_csv(gtfs_dir / f"{name}.txt", encoding="utf-8-sig", **kwargs)


def _fix_coordinates(stops: pd.DataFrame) -> pd.DataFrame:
    """Stops with broken coordinates moved to their station's other platforms, or dropped if it has none.

    One wrong stop would otherwise stretch the OpenStreetMap query around the network to another
    continent (and miss its cache), and draw lines across the map."""
    lat0, lon0 = stops.stop_lat.median(), stops.stop_lon.median()
    km = ((stops.stop_lat - lat0) ** 2 + ((stops.stop_lon - lon0) * math.cos(math.radians(lat0))) ** 2) ** 0.5 * 111.32
    bad = km > STOP_OUTLIER_KM
    if not bad.any():
        return stops
    good = stops[~bad]
    parent = stops.get("parent_station", pd.Series(index=stops.index, dtype=object))
    stations = good.assign(parent=parent[~bad]).dropna(subset=["parent"]).groupby("parent")[["stop_lat", "stop_lon"]].median()
    stops = stops.copy()
    drop = []
    for i in stops.index[bad]:
        r = stops.loc[i]
        # A platform's station, or the station itself (its platforms name it as parent).
        key = parent.get(i) if isinstance(parent.get(i), str) and parent.get(i) in stations.index else r.stop_id
        if key in stations.index:
            stops.loc[i, ["stop_lat", "stop_lon"]] = stations.loc[key].values
            print(f"  stop {r.stop_id} {r.stop_name}: coordinates {r.stop_lat}, {r.stop_lon} are off the map, "
                  f"using its station's other platforms")
        else:
            drop.append(i)
            print(f"  stop {r.stop_id} {r.stop_name}: coordinates {r.stop_lat}, {r.stop_lon} are off the map, left out")
    return stops.drop(index=drop)


def _build(gtfs_dir: Path, osm_cache: Path) -> dict:
    """Build the network from the GTFS files: lines with track geometry, stops and stop-to-stop pieces."""
    agency = _read(gtfs_dir, "agency", dtype=str)
    routes = _read(gtfs_dir, "routes", dtype=str, keep_default_na=False)
    stops = _read(
        gtfs_dir, "stops", dtype={"stop_id": str, "stop_name": str, "zone_id": str}
    ).dropna(subset=["stop_lat", "stop_lon"])
    stops = _fix_coordinates(stops)
    trips = _read(gtfs_dir, "trips", usecols=["route_id", "trip_id"], dtype=str)
    stop_times = _read(
        gtfs_dir,
        "stop_times",
        usecols=["trip_id", "stop_id", "stop_sequence"],
        dtype={"trip_id": str, "stop_id": str, "stop_sequence": int},
    )

    stop_times = stop_times.sort_values(["trip_id", "stop_sequence"])
    patterns = stop_times.groupby("trip_id", sort=False)["stop_id"].agg(tuple)
    patterns = patterns.rename("pattern").reset_index().merge(trips, on="trip_id")
    trip_counts = patterns.groupby("route_id").size()
    unique_patterns = patterns.drop_duplicates(["route_id", "pattern"])

    coords = {
        r.stop_id: (round(r.stop_lat, 5), round(r.stop_lon, 5))
        for r in stops.itertuples()
    }

    route_patterns = defaultdict(set)
    route_stops = defaultdict(set)
    route_pairs = defaultdict(set)
    for r in unique_patterns.itertuples():
        seq = [s for s in r.pattern if s in coords]
        seq = [s for i, s in enumerate(seq) if i == 0 or s != seq[i - 1]]
        route_stops[r.route_id].update(seq)
        route_pairs[r.route_id].update(zip(seq, seq[1:]))
        if len(seq) > 1:
            route_patterns[r.route_id].add(tuple(coords[s] for s in seq))

    routes = routes[routes.route_id.isin(list(route_patterns))]
    ways, tracks = build_tracks(
        [
            {
                "id": r.route_id,
                "short": r.route_short_name,
                "route_type": int(r.route_type),
                "patterns": sorted(route_patterns[r.route_id]),
            }
            for r in routes.itertuples()
        ],
        osm_cache,
    )

    used_stop_ids = sorted(set().union(*route_stops.values()))
    stop_index = {sid: i for i, sid in enumerate(used_stop_ids)}
    stop_routes = defaultdict(list)

    print("  computing stop-to-stop track pieces for vehicle positions...")
    segments = {}
    out_routes = []
    for r in routes.itertuples():
        idx = len(out_routes)
        rtype = int(r.route_type)
        for sid in route_stops[r.route_id]:
            stop_routes[sid].append(idx)
        pairs = route_pairs[r.route_id]
        paths = segment_paths(
            ways, tracks[r.route_id]["ways"], {(coords[a], coords[b]) for a, b in pairs}
        )
        for a, b in pairs:
            segments[f"{idx}|{a}|{b}"] = [list(p) for p in paths[(coords[a], coords[b])]]
        out_routes.append(
            {
                "id": r.route_id,
                "short": r.route_short_name,
                "long": r.route_long_name,
                "type": ROUTE_TYPES.get(rtype, f"Other ({rtype})"),
                "color": "#" + (r.route_color or "3388ff"),
                "text": "#" + (r.route_text_color or "000000"),
                "trips": int(trip_counts.get(r.route_id, 0)),
                "track": tracks[r.route_id]["source"],
                "ways": tracks[r.route_id]["ways"],
                "stops": sorted(stop_index[s] for s in route_stops[r.route_id]),
            }
        )

    stops_by_id = stops.set_index("stop_id")
    out_stops = []
    for sid in used_stop_ids:
        row = stops_by_id.loc[sid]
        lat, lon = coords[sid]
        out_stops.append(
            {
                "id": sid,
                "name": row["stop_name"],
                "lat": lat,
                "lon": lon,
                "zone": row["zone_id"] if isinstance(row["zone_id"], str) else "",
                "routes": stop_routes[sid],
            }
        )

    return {
        "agency": agency.iloc[0]["agency_name"],
        "routes": out_routes,
        "ways": [[list(p) for p in w] for w in ways],
        "stops": out_stops,
        # Server-only: track polyline between consecutive stops, "route|stopA|stopB".
        "_segments": segments,
    }


def load_network(gtfs_dir: Path, cache_file: Path) -> dict:
    """Return the network, rebuilding the JSON cache when the GTFS files change."""
    # Size + whole-second mtime: survives copying into Docker image layers,
    # which do not keep sub-second timestamps.
    stamp = [CACHE_VERSION] + [
        (p.name, p.stat().st_size, int(p.stat().st_mtime))
        for p in sorted(gtfs_dir.glob("*.txt"))
    ]
    stamp = json.loads(json.dumps(stamp))
    version = hashlib.sha1(json.dumps(stamp).encode()).hexdigest()[:12]
    if cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if cached.get("stamp") == stamp:
            return {**cached["network"], "version": version}

    network = _build(gtfs_dir, cache_file.parent / "osm_cache")
    cache_file.write_text(
        json.dumps({"stamp": stamp, "network": network}, ensure_ascii=False),
        encoding="utf-8",
    )
    return {**network, "version": version}
