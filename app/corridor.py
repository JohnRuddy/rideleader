"""Corridor matching: where along the route is a point, and how far off is it?

Works in a local equirectangular projection (fine at route scale). A point near a
route that passes it twice (out-and-back, figure-of-eight) yields one Pass per visit.
"""
import math
from dataclasses import dataclass

import numpy as np

EARTH_R = 6371008.8


@dataclass(frozen=True)
class Pass:
    s_m: float
    offset_m: float
    side: str | None  # 'L' or 'R' relative to direction of travel


class RouteIndex:
    def __init__(self, lats, lons, dist_m):
        lats, lons = np.asarray(lats, float), np.asarray(lons, float)
        self.dist = np.asarray(dist_m, float)
        deg = math.radians(1) * EARTH_R
        self.kx, self.ky = deg * math.cos(math.radians(float(lats.mean()))), deg
        x, y = lons * self.kx, lats * self.ky
        self.ax, self.ay = x[:-1], y[:-1]
        self.dx, self.dy = np.diff(x), np.diff(y)
        self.l2 = self.dx ** 2 + self.dy ** 2
        self.seg_len = np.diff(self.dist)

    def passes(self, lat: float, lon: float, corridor_m: float, merge_gap_m: float = 200.0) -> list[Pass]:
        px, py = lon * self.kx, lat * self.ky
        with np.errstate(divide="ignore", invalid="ignore"):
            t = ((px - self.ax) * self.dx + (py - self.ay) * self.dy) / self.l2
        t = np.where(self.l2 > 0, np.clip(t, 0.0, 1.0), 0.0)
        cx, cy = self.ax + t * self.dx, self.ay + t * self.dy
        d = np.hypot(px - cx, py - cy)
        idx = np.flatnonzero(d <= corridor_m)
        if idx.size == 0:
            return []
        # Segments within the corridor form a "pass"; a gap of more than merge_gap_m of route
        # between them means the route has left and come back, i.e. a separate visit.
        groups = [[int(idx[0])]]
        for i in idx[1:]:
            i = int(i)
            if self.dist[i] - self.dist[groups[-1][-1] + 1] > merge_gap_m:
                groups.append([i])
            else:
                groups[-1].append(i)
        out = []
        for g in groups:
            k = g[int(np.argmin(d[g]))]
            cross = self.dx[k] * (py - cy[k]) - self.dy[k] * (px - cx[k])
            out.append(Pass(
                s_m=float(self.dist[k] + t[k] * self.seg_len[k]),
                offset_m=float(d[k]),
                side="L" if cross > 0 else "R" if cross < 0 else None,
            ))
        return out
