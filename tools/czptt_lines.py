"""Turn the national rail GTFS (JrUtil CZPTT_GTFS.zip) into a feed the map can use.

The rail feed has one GTFS route per train number ("Os 24140"), so a single
regional line consists of hundreds of routes and most of them run only a few
times. The map treats a route as a line, so this script builds real lines:

1. Only trips running in a date window are kept (the feed spans several years).
2. Trips are grouped by train category + both terminal stations (direction-
   independent), e.g. "Os Brno hl. n. – Bzenec".
3. A group whose stations form an ordered subset of a longer group of the same
   category (a short working such as Brno–Tišnov of Brno–Žďár) is merged into it.
4. With --bbox, trips are clipped to the part between their first and last
   station inside the box; trips with fewer than two stations inside are dropped.

The train number is kept as trip_short_name. Platforms are folded into their
station. The output is a plain GTFS folder that app.py can load with --gtfs.

    python tools/czptt_lines.py CZPTT_GTFS.zip out/ --bbox 49.0,16.0,50.3,17.9
    python app.py --gtfs out/ --cache out-cache/ --url ""
"""
import argparse
import csv
import zipfile
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

LOCAL = {"Os", "Sp", "TL"}
LOCAL_COLOR, FAST_COLOR = "800000", "C8102E"


def _read(z: zipfile.ZipFile, name: str, **kw) -> pd.DataFrame:
    with z.open(name) as f:
        return pd.read_csv(f, encoding="utf-8-sig", dtype=str, keep_default_na=False, **kw)


def _is_subsequence(short, long) -> bool:
    it = iter(long)
    return all(s in it for s in short)


def convert(src: Path, out: Path, start: date, end: date, bbox=None):
    z = zipfile.ZipFile(src)

    # ---- services running in the window
    cal = _read(z, "calendar_dates.txt")
    lo, hi = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    cal = cal[(cal.date >= lo) & (cal.date <= hi)]
    active = set(cal.loc[cal.exception_type == "1", "service_id"])
    if not active:
        raise SystemExit(f"no service between {start} and {end}")

    routes = _read(z, "routes.txt", usecols=["route_id", "agency_id", "route_short_name"])
    train_no = dict(zip(routes.route_id, routes.route_short_name))
    route_agency = dict(zip(routes.route_id, routes.agency_id))
    trips = _read(z, "trips.txt", usecols=["route_id", "service_id", "trip_id", "trip_headsign"])
    trips = trips[trips.service_id.isin(active) & trips.route_id.isin(train_no.keys())]

    # ---- stations (platforms fold into their parent station)
    stops = _read(z, "stops.txt", usecols=["stop_id", "stop_name", "stop_lat", "stop_lon", "parent_station"])
    station_of = {r.stop_id: (r.parent_station or r.stop_id) for r in stops.itertuples()}
    stations = stops[stops.stop_id.isin(set(station_of.values()))].set_index("stop_id")
    # Some stations have no position (empty or 0,0); trips just skip them.
    stations = stations[pd.to_numeric(stations.stop_lat, errors="coerce").fillna(0).ne(0)
                        & pd.to_numeric(stations.stop_lon, errors="coerce").fillna(0).ne(0)]
    lat = stations.stop_lat.astype(float)
    lon = stations.stop_lon.astype(float)
    name = stations.stop_name

    # ---- stop times of the kept trips, in station terms
    st = _read(z, "stop_times.txt", usecols=["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"])
    st = st[st.trip_id.isin(set(trips.trip_id))].copy()
    st["stop_id"] = st.stop_id.map(station_of)
    st = st[st.stop_id.isin(stations.index)]
    st["stop_sequence"] = st.stop_sequence.astype(int)
    st = st.sort_values(["trip_id", "stop_sequence"])

    if bbox:
        s, w, n, e = bbox
        inside = st.stop_id.map(lambda x: s <= lat[x] <= n and w <= lon[x] <= e)
        st["inside"] = inside.to_numpy()
        pos = st.groupby("trip_id").cumcount()
        st["pos"] = pos.to_numpy()
        span = st[st.inside].groupby("trip_id").pos.agg(["min", "max"])
        span = span[span["max"] > span["min"]]
        st = st[st.trip_id.isin(span.index)]
        first = st.trip_id.map(span["min"])
        last = st.trip_id.map(span["max"])
        # Remember full-length terminals for naming before clipping.
        full = st.groupby("trip_id").stop_id.agg(list)
        st = st[(st.pos >= first) & (st.pos <= last)]
    else:
        full = st.groupby("trip_id").stop_id.agg(list)

    trips = trips[trips.trip_id.isin(full.index)]

    # ---- group trips into lines
    groups = defaultdict(list)   # (category, terminal A, terminal B) -> trip ids
    for t in trips.itertuples():
        seq = full[t.trip_id]
        cat = train_no[t.route_id].split()[0]
        a, b = sorted((name[seq[0]], name[seq[-1]]))
        groups[(cat, a, b)].append(t.trip_id)

    pattern = {}   # group -> longest station-name sequence, oriented A -> B
    for key, tids in groups.items():
        seq = max((full[t] for t in tids), key=len)
        names = [name[x] for x in seq]
        pattern[key] = names if names[0] == key[1] else names[::-1]

    # Longest groups first; shorter ones merge into an enclosing group.
    order = sorted(groups, key=lambda k: (-len(pattern[k]), -len(groups[k])))
    by_station = defaultdict(list)
    merged_into = {}
    for key in order:
        cat, a, b = key
        at_b = set(by_station[b])
        cands = [k for k in by_station[a] if k[0] == cat and k in at_b]
        target = None
        for k in sorted(cands, key=lambda k: -len(groups[k])):
            p, q = pattern[key], pattern[k]
            if _is_subsequence(p, q) or _is_subsequence(p[::-1], q):
                target = k
                break
        if target:
            merged_into[key] = target
            groups[target].extend(groups[key])
        else:
            for station in set(pattern[key]):
                by_station[station].append(key)
    lines = [k for k in order if k not in merged_into]

    # ---- write GTFS
    out.mkdir(parents=True, exist_ok=True)
    trip_line, route_rows, agencies = {}, [], set()
    tr_route = dict(zip(trips.trip_id, trips.route_id))
    for i, key in enumerate(sorted(lines, key=lambda k: (k[0] not in LOCAL, k[0], k[1], k[2]))):
        cat, a, b = key
        rid = f"T{i + 1}"
        tids = groups[key]
        agency = Counter(route_agency[tr_route[t]] for t in tids).most_common(1)[0][0]
        agencies.add(agency)
        for t in tids:
            trip_line[t] = rid
        route_rows.append([rid, agency, cat, f"{a} – {b}", 2, LOCAL_COLOR if cat in LOCAL else FAST_COLOR, "FFFFFF"])

    def write(name, header, rows):
        with open(out / name, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)

    write("routes.txt", ["route_id", "agency_id", "route_short_name", "route_long_name", "route_type",
                         "route_color", "route_text_color"], route_rows)
    write("trips.txt", ["route_id", "service_id", "trip_id", "trip_headsign", "trip_short_name"],
          [[trip_line[t.trip_id], t.service_id, t.trip_id, t.trip_headsign, train_no[t.route_id]]
           for t in trips.itertuples()])
    write("stop_times.txt", ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
          st[["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"]].itertuples(index=False))
    used = sorted(set(st.stop_id))
    write("stops.txt", ["stop_id", "stop_name", "stop_lat", "stop_lon", "zone_id"],
          [[x, name[x], stations.stop_lat[x], stations.stop_lon[x], ""] for x in used])
    used_services = set(trips.service_id)
    write("calendar.txt", ["service_id", "monday", "tuesday", "wednesday", "thursday", "friday",
                           "saturday", "sunday", "start_date", "end_date"], [])
    write("calendar_dates.txt", ["service_id", "date", "exception_type"],
          cal[cal.service_id.isin(used_services)][["service_id", "date", "exception_type"]].itertuples(index=False))
    write("transfers.txt", ["from_stop_id", "to_stop_id", "transfer_type", "min_transfer_time",
                            "from_trip_id", "to_trip_id"], [])
    ag = _read(z, "agency.txt")
    ag = ag[ag.agency_id.isin(agencies)]
    ag.to_csv(out / "agency.txt", index=False)

    print(f"{len(trips)} trips of {trips.route_id.nunique()} train numbers -> "
          f"{len(groups)} terminal groups -> {len(lines)} lines, {len(used)} stations")
    print("lines by category:", dict(Counter(k[0] for k in lines).most_common()))
    for key in sorted(lines, key=lambda k: -len(groups[k]))[:10]:
        print(f"  {len(groups[key]):5} trips  {key[0]} {key[1]} – {key[2]}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("src", type=Path, help="CZPTT_GTFS.zip from https://data.jr.ggu.cz/results/latest/")
    p.add_argument("out", type=Path, help="output GTFS folder")
    p.add_argument("--start", type=date.fromisoformat, default=date.today(), help="first day (default today)")
    p.add_argument("--days", type=int, default=60, help="length of the date window (default 60)")
    p.add_argument("--bbox", help="clip to south,west,north,east (degrees)")
    a = p.parse_args()
    bbox = tuple(float(x) for x in a.bbox.split(",")) if a.bbox else None
    convert(a.src, a.out, a.start, a.start + timedelta(days=a.days - 1), bbox)


if __name__ == "__main__":
    main()
