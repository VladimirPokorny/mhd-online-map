"""Timetable engine: which vehicles are running at a given date and time.

Every trip running at the requested moment is one vehicle. Its position is
interpolated between the scheduled departure from one stop and the arrival at
the next, along the line's track geometry.
"""
import bisect
import math
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

DAY = 86_400


def _seconds(series: pd.Series) -> np.ndarray:
    parts = series.str.split(":", expand=True).astype(int)
    return (parts[0] * 3600 + parts[1] * 60 + parts[2]).to_numpy(np.int32)


def _parse_date(s: str) -> date:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def czech_holidays(year: int) -> dict:
    fixed = {
        (1, 1): "Den obnovy samostatného českého státu", (5, 1): "Svátek práce",
        (5, 8): "Den vítězství", (7, 5): "Den slovanských věrozvěstů Cyrila a Metoděje",
        (7, 6): "Den upálení mistra Jana Husa", (9, 28): "Den české státnosti",
        (10, 28): "Den vzniku samostatného československého státu",
        (11, 17): "Den boje za svobodu a demokracii", (12, 24): "Štědrý den",
        (12, 25): "1. svátek vánoční", (12, 26): "2. svátek vánoční",
    }
    out = {date(year, m, d): name for (m, d), name in fixed.items()}
    easter = _easter(year)
    out[easter - timedelta(days=2)] = "Velký pátek"
    out[easter + timedelta(days=1)] = "Velikonoční pondělí"
    return out


class _PolyLine:
    """Polyline with cumulative lengths for interpolation by fraction."""

    __slots__ = ("pts", "cum")

    def __init__(self, pts):
        self.pts = pts
        cum = [0.0]
        for a, b in zip(pts, pts[1:]):
            k = math.cos(math.radians(a[0]))
            cum.append(cum[-1] + math.hypot((b[0] - a[0]), (b[1] - a[1]) * k))
        self.cum = cum

    def at(self, f):
        total = self.cum[-1]
        if total == 0:
            return self.pts[0]
        d = min(max(f, 0.0), 1.0) * total
        i = min(bisect.bisect_right(self.cum, d), len(self.pts) - 1)
        a, b = self.pts[i - 1], self.pts[i]
        seg = self.cum[i] - self.cum[i - 1]
        t = (d - self.cum[i - 1]) / seg if seg else 0
        return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


class Timetable:
    def __init__(self, gtfs_dir: Path, network: dict, segments: dict):
        read = lambda name, **kw: pd.read_csv(gtfs_dir / f"{name}.txt", encoding="utf-8-sig", **kw)
        route_idx = {r["id"]: i for i, r in enumerate(network["routes"])}
        self.route_type = [r["type"] for r in network["routes"]]
        self.types = sorted(set(self.route_type))
        stop_idx = {s["id"]: i for i, s in enumerate(network["stops"])}
        self.stop_coords = [(s["lat"], s["lon"]) for s in network["stops"]]

        # ---- calendar
        cal = read("calendar", dtype=str)
        cal_dates = read("calendar_dates", dtype=str)
        self.service_codes = {sid: i for i, sid in enumerate(sorted(set(cal.service_id) | set(cal_dates.service_id)))}
        self.calendar = [
            (self.service_codes[r.service_id], _parse_date(r.start_date), _parse_date(r.end_date),
             [r.monday, r.tuesday, r.wednesday, r.thursday, r.friday, r.saturday, r.sunday])
            for r in cal.itertuples()
        ]
        self.exceptions = {}
        for r in cal_dates.itertuples():
            self.exceptions.setdefault(_parse_date(r.date), []).append(
                (self.service_codes[r.service_id], r.exception_type == "1"))
        all_dates = [d for _, s, e, _ in self.calendar for d in (s, e)] + list(self.exceptions)
        self.start, self.end = min(all_dates), max(all_dates)

        # ---- trips
        trips = read("trips", usecols=["route_id", "service_id", "trip_id", "trip_headsign"], dtype=str)
        trips = trips[trips.route_id.isin(route_idx.keys())].reset_index(drop=True)
        trip_code = {t: i for i, t in enumerate(trips.trip_id)}
        self.trip_route = trips.route_id.map(route_idx).to_numpy(np.int32)
        self.trip_service = trips.service_id.map(self.service_codes).to_numpy(np.int32)
        self.trip_headsign = trips.trip_headsign.fillna("").tolist()

        # ---- stop times -> moving segments and dwell intervals
        st = read("stop_times", usecols=["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
                  dtype={"trip_id": str, "stop_id": str, "arrival_time": str, "departure_time": str})
        st = st[st.trip_id.isin(trip_code.keys()) & st.stop_id.isin(stop_idx.keys())]
        st = st.sort_values(["trip_id", "stop_sequence"]).reset_index(drop=True)
        trip = st.trip_id.map(trip_code).to_numpy(np.int32)
        arr, dep = _seconds(st.arrival_time), _seconds(st.departure_time)
        stop = st.stop_id.map(stop_idx).to_numpy(np.int32)
        stop_ids = st.stop_id.to_numpy()

        same = trip[1:] == trip[:-1]
        i = np.nonzero(same)[0]
        self.seg_trip, self.seg_t0, self.seg_t1 = trip[i], dep[i], arr[i + 1]
        self.seg_next = stop[i + 1]
        self.seg_from = stop[i]
        self.seg_path = [None] * len(i)
        cache = {}
        for n, (r, a, b) in enumerate(zip(self.trip_route[trip[i]], stop_ids[i], stop_ids[i + 1])):
            key = f"{r}|{a}|{b}"
            if key not in cache:
                pts = segments.get(key) or [self.stop_coords[stop_idx[a]], self.stop_coords[stop_idx[b]]]
                cache[key] = _PolyLine([tuple(p) for p in pts])
            self.seg_path[n] = cache[key]

        first = np.r_[True, ~same]
        last = np.r_[~same, True]
        # Waiting at an intermediate stop (the last stop's departure_time is the layover).
        dwell = (dep > arr) & ~first & ~last
        self.dw_trip, self.dw_t0, self.dw_t1, self.dw_stop = trip[dwell], arr[dwell], dep[dwell], stop[dwell]

        self.trip_start = np.full(len(trips), -1, np.int32)
        self.trip_end = np.full(len(trips), -1, np.int32)
        self.trip_start[trip[first]] = dep[first]
        self.trip_end[trip[last]] = arr[last]
        self._active_cache = {}

        # Every trip's stops in order, for the live map (GTFS-RT gives the trip_id of a vehicle).
        self.trip_code = trip_code
        order = np.argsort(trip, kind="stable")
        self.st_stop, self.st_arr, self.st_dep = stop[order], arr[order], dep[order]
        self.st_first = np.searchsorted(trip[order], np.arange(len(trips) + 1))

    # ----------------------------------------------------------- calendar ---

    def services_on(self, d: date) -> np.ndarray:
        if d not in self._active_cache:
            active = np.zeros(len(self.service_codes), bool)
            for code, start, end, days in self.calendar:
                if start <= d <= end and days[d.weekday()] == "1":
                    active[code] = True
            for code, added in self.exceptions.get(d, ()):
                active[code] = added
            self._active_cache[d] = active
        return self._active_cache[d]

    def calendar_info(self) -> dict:
        holidays = {}
        for y in range(self.start.year, self.end.year + 1):
            holidays.update(czech_holidays(y))
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "holidays": {d.isoformat(): n for d, n in sorted(holidays.items()) if self.start <= d <= self.end},
        }

    def _trips_on(self, d: date) -> np.ndarray:
        return self.services_on(d)[self.trip_service]

    # ----------------------------------------------------------- vehicles ---

    def vehicles(self, d: date, t: int) -> list:
        """Vehicles at second ``t`` of day ``d`` (includes trips started the day before)."""
        out = []
        for day, tt in ((d, t), (d - timedelta(days=1), t + DAY)):
            running = self._trips_on(day)
            moving = np.nonzero(running[self.seg_trip] & (self.seg_t0 <= tt) & (tt < self.seg_t1))[0]
            for n in moving:
                f = (tt - self.seg_t0[n]) / (self.seg_t1[n] - self.seg_t0[n])
                lat, lon = self.seg_path[n].at(f)
                out.append(self._vehicle(self.seg_trip[n], lat, lon, self.seg_next[n], False))
            dwelling = np.nonzero(running[self.dw_trip] & (self.dw_t0 <= tt) & (tt < self.dw_t1))[0]
            for n in dwelling:
                lat, lon = self.stop_coords[self.dw_stop[n]]
                out.append(self._vehicle(self.dw_trip[n], lat, lon, self.dw_stop[n], True))
        return out

    def trip_stops(self, trip_id: str):
        """(route index, headsign, [(stop index, arrival s, departure s), …]) of a GTFS trip, or None."""
        t = self.trip_code.get(str(trip_id))
        if t is None:
            return None
        a, b = self.st_first[t], self.st_first[t + 1]
        stops = list(zip(self.st_stop[a:b].tolist(), self.st_arr[a:b].tolist(), self.st_dep[a:b].tolist()))
        return int(self.trip_route[t]), self.trip_headsign[t], stops

    def _vehicle(self, trip, lat, lon, stop, at_stop):
        return [round(lat, 6), round(lon, 6), int(self.trip_route[trip]),
                self.trip_headsign[trip], int(stop), at_stop]

    def profile(self, d: date, step: int = 300) -> dict:
        """Number of vehicles in service per mode, sampled every ``step`` seconds."""
        times = np.arange(0, DAY + 1, step)
        counts = {t: np.zeros(len(times), int) for t in self.types}
        route_type = np.array(self.route_type)
        for day, offset in ((d, 0), (d - timedelta(days=1), DAY)):
            running = self._trips_on(day) & (self.trip_start >= 0)
            for mode in self.types:
                sel = running & (route_type[self.trip_route] == mode)
                starts = np.sort(self.trip_start[sel])
                ends = np.sort(self.trip_end[sel])
                tt = times + offset
                counts[mode] += np.searchsorted(starts, tt, "right") - np.searchsorted(ends, tt, "right")
        return {"step": step, "series": {m: c.tolist() for m, c in counts.items()}}
