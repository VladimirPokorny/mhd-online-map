"""Reachability ("where can I be in N minutes") from a stop at a given moment.

Earliest arrivals are found with the Connection Scan Algorithm over the day's
timetable, with walking transfers between nearby stops. The result is then
spread into a travel-time grid over the whole region: from every reached stop
the time grows at walking speed (a chamfer distance transform), so every place
gets the time of its fastest stop + walk combination.

The search runs beyond the requested budget (``HORIZON_FACTOR``) so the map can
also show what is reachable a bit later.
"""
import base64
import math
from collections import defaultdict
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from timetable import DAY, Timetable

WALK_MPS = 1.0           # straight-line walking speed (~4.5 km/h with detours)
MAX_WALK_TRANSFER_M = 400
MIN_CHANGE_S = 60        # minimum time to change vehicles at the same stop
HORIZON_FACTOR = 2       # search up to this multiple of the budget...
MAX_HORIZON_S = 240 * 60  # ...but never further than 4 hours
GRID_MAX_CELLS = 1000    # longest side of the region grid
EARTH_R = 6378137.0
NO_DATA = 255


def _merc(lat, lon):
    """Web Mercator (EPSG:3857) metres of lat/lon arrays."""
    x = EARTH_R * np.radians(lon)
    y = EARTH_R * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))
    return x, y


class Reachability:
    """Index for reachability queries: footpaths, sorted connections and the region grid."""
    def __init__(self, gtfs_dir: Path, network: dict, timetable: Timetable):
        """Precompute footpaths, the connection order and the travel-time grid over the network area."""
        self.tt = timetable
        self.stops = network["stops"]
        lat = np.array([s["lat"] for s in self.stops])
        lon = np.array([s["lon"] for s in self.stops])
        self.lat, self.lon = lat, lon
        self.mx, self.my = _merc(lat, lon)
        self.by_name = defaultdict(list)
        for i, s in enumerate(self.stops):
            self.by_name[s["name"]].append(i)
        self.foot = self._footpaths(gtfs_dir)
        self.order = np.argsort(timetable.seg_t0, kind="stable")

        # Fixed grid over the whole network area (Web Mercator).
        margin = 3000 / math.cos(math.radians(lat.mean()))
        self.gx0, self.gx1 = self.mx.min() - margin, self.mx.max() + margin
        self.gy0, self.gy1 = self.my.min() - margin, self.my.max() + margin
        self.cell = max(self.gx1 - self.gx0, self.gy1 - self.gy0) / GRID_MAX_CELLS
        self.gw = int(math.ceil((self.gx1 - self.gx0) / self.cell))
        self.gh = int(math.ceil((self.gy1 - self.gy0) / self.cell))
        self.stop_col = ((self.mx - self.gx0) / self.cell).astype(int)
        self.stop_row = ((self.gy1 - self.my) / self.cell).astype(int)
        # Walking seconds per cell step (orthogonal / diagonal), at the mean latitude.
        self.step_s = self.cell * math.cos(math.radians(lat.mean())) / WALK_MPS

    def _footpaths(self, gtfs_dir):
        """stop -> [(other stop, seconds)]: official transfers + short walks."""
        foot = defaultdict(dict)
        cell = 0.005
        grid = defaultdict(list)
        for i, (la, lo) in enumerate(zip(self.lat, self.lon)):
            grid[(int(la / cell), int(lo / cell))].append(i)
        for (cy, cx), members in grid.items():
            near = [j for dy in (-1, 0, 1) for dx in (-1, 0, 1) for j in grid.get((cy + dy, cx + dx), ())]
            for i in members:
                k = math.cos(math.radians(self.lat[i]))
                for j in near:
                    if i == j:
                        continue
                    d = math.hypot((self.lat[j] - self.lat[i]) * 110_540, (self.lon[j] - self.lon[i]) * 111_320 * k)
                    if d <= MAX_WALK_TRANSFER_M:
                        foot[i][j] = max(MIN_CHANGE_S, d / WALK_MPS)

        stop_idx = {s["id"]: i for i, s in enumerate(self.stops)}
        tr = pd.read_csv(gtfs_dir / "transfers.txt", encoding="utf-8-sig", dtype=str)
        tr = tr[tr.from_trip_id.isna() & tr.to_trip_id.isna()]
        for r in tr.itertuples():
            a, b = stop_idx.get(r.from_stop_id), stop_idx.get(r.to_stop_id)
            if a is None or b is None or a == b:
                continue
            secs = float(r.min_transfer_time) if isinstance(r.min_transfer_time, str) else MIN_CHANGE_S
            foot[a][b] = min(foot[a].get(b, math.inf), secs)
        return {i: list(d.items()) for i, d in foot.items()}

    def _connections(self, d, t_from, t_to):
        """Connections departing in [t_from, t_to] (seconds relative to day d), sorted."""
        tt = self.tt
        parts = []
        for day, offset in ((d - timedelta(days=1), -DAY), (d, 0), (d + timedelta(days=1), DAY)):
            running = tt.services_on(day)[tt.trip_service] if tt.start <= day <= tt.end else None
            if running is None:
                continue
            t0 = tt.seg_t0[self.order] + offset
            sel = self.order[running[tt.seg_trip[self.order]] & (t0 >= t_from) & (t0 <= t_to)]
            if len(sel):
                parts.append((sel, offset))
        if not parts:
            return [np.array([], int)] * 5
        dep = np.concatenate([tt.seg_t0[s] + o for s, o in parts])
        arr = np.concatenate([tt.seg_t1[s] + o for s, o in parts])
        sel = np.concatenate([s for s, _ in parts])
        # Trips of different days are different vehicles: make trip ids unique per day.
        trip = np.concatenate([tt.seg_trip[s].astype(np.int64) + (o // DAY + 1) * len(tt.trip_route) for s, o in parts])
        k = np.argsort(dep, kind="stable")
        return dep[k], arr[k], tt.seg_from[sel][k], tt.seg_next[sel][k], trip[k]

    def query(self, d, t, stop, budget_s):
        """Earliest arrivals from ``stop`` (and stops of the same name) leaving at second ``t`` of day ``d``."""
        tt = self.tt
        n_trips = len(tt.trip_route)
        origins = self.by_name.get(self.stops[stop]["name"], [stop])
        horizon_s = min(max(budget_s * HORIZON_FACTOR, budget_s + 1800), MAX_HORIZON_S)
        end = t + horizon_s
        n = len(self.stops)
        arr = np.full(n, math.inf)
        board = np.full(n, math.inf)
        pred_stop = np.full(n, -1)
        pred_route = np.full(n, -1)
        pred_dep = np.zeros(n)
        rides = np.zeros(n, int)  # vehicles used; breaks ties between equally fast journeys

        def walk_from(s, ts):
            """Relax the footpaths from stop ``s`` reached at time ``ts``."""
            for m, w in self.foot.get(s, ()):
                ta = ts + w
                if ta <= end and (ta < arr[m] or (ta == arr[m] and rides[s] < rides[m])):
                    arr[m] = ta
                    board[m] = min(board[m], ta)
                    rides[m] = rides[s]
                    pred_stop[m], pred_route[m], pred_dep[m] = s, -1, ts

        for s in origins:
            arr[s] = board[s] = t
        for s in origins:
            walk_from(s, t)

        dep, ar, frm, to, trip = (a.tolist() for a in self._connections(d, t, end))
        boarded = {}  # trip -> (boarding stop, departure, rides including this one)
        for c in range(len(dep)):
            tr, f = trip[c], frm[c]
            can_board = board[f] <= dep[c]
            if tr not in boarded:
                if not can_board:
                    continue
                boarded[tr] = (f, dep[c], rides[f] + 1)
            elif can_board and rides[f] + 1 < boarded[tr][2]:
                boarded[tr] = (f, dep[c], rides[f] + 1)  # same vehicle, fewer changes
            ta = ar[c]
            b = to[c]
            r = boarded[tr][2]
            if ta <= end and (ta < arr[b] or (ta == arr[b] and r < rides[b])):
                arr[b] = ta
                board[b] = min(board[b], ta + MIN_CHANGE_S)
                rides[b] = r
                pred_stop[b], pred_dep[b] = boarded[tr][:2]
                pred_route[b] = tt.trip_route[tr % n_trips]
                walk_from(b, ta)

        reached = np.nonzero(arr <= end)[0]
        stops_out = [
            [int(s), round(float(arr[s] - t) / 60, 1), int(pred_stop[s]), int(pred_route[s]),
             int(pred_dep[s] - t) if pred_stop[s] >= 0 else 0]
            for s in reached
        ]
        return {
            "origin": origins,
            "budget": budget_s // 60,
            "horizon": horizon_s // 60,
            "stops": stops_out,
            "grid": self._grid(reached, arr[reached] - t, horizon_s),
        }

    def _grid(self, reached, elapsed, horizon_s):
        """Travel-time raster over the region (minutes, NO_DATA beyond the horizon)."""
        grid = np.full((self.gh, self.gw), np.inf)
        np.minimum.at(grid, (self.stop_row[reached], self.stop_col[reached]), elapsed)
        _chamfer(grid, self.step_s)
        minutes = np.where(grid <= horizon_s, np.clip(grid / 60, 0, 254), NO_DATA).astype(np.uint8)
        return {
            "x0": float(self.gx0), "y1": float(self.gy1), "cell": float(self.cell),
            "w": self.gw, "h": self.gh,
            "data": base64.b64encode(minutes.tobytes()).decode(),
        }


def _scan_row(row, step):
    """In-place: row[j] = min_k(row[k] + |j-k| * step), via two running minima."""
    j = np.arange(len(row)) * step
    np.minimum(row, np.minimum.accumulate(row - j) + j, out=row)
    r = row[::-1]
    np.minimum(r, np.minimum.accumulate(r - j) + j, out=r)


def _chamfer(grid, step):
    """Spread times across the grid at walking speed (8-neighbour chamfer transform)."""
    diag = step * math.sqrt(2)
    h = grid.shape[0]
    for _ in range(2):
        for order in (range(1, h), range(h - 2, -1, -1)):
            for i in order:
                prev = grid[i - 1] if order.step == 1 else grid[i + 1]
                row = grid[i]
                np.minimum(row, prev + step, out=row)
                np.minimum(row[1:], prev[:-1] + diag, out=row[1:])
                np.minimum(row[:-1], prev[1:] + diag, out=row[:-1])
                _scan_row(row, step)
