"""Elevation profile analysis: smoothing, climb and descent detection, gradient and bend lookups.

Deterministic maths only. Distances are metres along the route, elevations metres.
"""
import math
from dataclasses import dataclass

import numpy as np

STEP_M = 20.0            # resampling interval
SMOOTH_M = 120.0         # moving-average window: removes GPS/DEM noise without flattening real climbs

# A climb must clear all three to be reported.
MIN_LENGTH_M = 300.0
MIN_GAIN_M = 20.0
MIN_AVG_GRADE = 2.5      # percent
RISE_TOL_M = 5.0         # rise from a low point that starts a candidate climb
DROP_TOL_M = 8.0         # a dip bigger than this ends the climb (smaller dips are ridden through)

# Heuristic difficulty: score = length (m) x average gradient (%), banded like Garmin ClimbPro's categories.
# It is a rule of thumb for "how hard is this", not an official scale.
CATEGORY_BANDS = ((80_000, "HC"), (64_000, "1"), (32_000, "2"), (16_000, "3"), (8_000, "4"))


@dataclass(frozen=True)
class Segment:
    s_start: float
    s_end: float
    length_m: float
    gain_m: float            # climbs: metres up; descents: metres down (positive)
    avg_grade: float         # percent, positive for both
    max_grade: float         # steepest 100 m, percent, positive for both
    score: float
    category: str | None     # "4".."1", "HC" or None


def category_for(score: float) -> str | None:
    for floor, name in CATEGORY_BANDS:
        if score >= floor:
            return name
    return None


class Profile:
    def __init__(self, lats, lons, dist_m, ele_m, smooth_m: float = SMOOTH_M):
        self.lat, self.lon = np.asarray(lats, float), np.asarray(lons, float)
        self.dist = np.asarray(dist_m, float)
        self.length = float(self.dist[-1])
        self.grid = np.append(np.arange(0.0, self.length, STEP_M), self.length)
        raw = np.interp(self.grid, self.dist, np.asarray(ele_m, float))
        w = max(1, int(round(smooth_m / STEP_M)))
        padded = np.pad(raw, (w // 2, w - 1 - w // 2), mode="edge")
        self.ele = np.convolve(padded, np.ones(w) / w, mode="valid")
        self._kx = math.cos(math.radians(float(self.lat.mean())))

    # ---- lookups ----
    def ele_at(self, s: float) -> float:
        return float(np.interp(s, self.grid, self.ele))

    def grade_at(self, s: float, window_m: float = 100.0) -> float:
        """Smoothed gradient (percent) over a window centred on s."""
        a, b = max(0.0, s - window_m / 2), min(self.length, s + window_m / 2)
        return 0.0 if b <= a else (self.ele_at(b) - self.ele_at(a)) / (b - a) * 100

    def point_at(self, s: float) -> tuple[float, float]:
        return float(np.interp(s, self.dist, self.lat)), float(np.interp(s, self.dist, self.lon))

    def bend_at(self, s: float, span_m: float = 50.0) -> float:
        """Heading change in degrees across the point at s (0 = straight, 90 = right angle)."""
        a, b, c = (self.point_at(min(max(x, 0.0), self.length)) for x in (s - span_m, s, s + span_m))
        def heading(p, q):
            return math.atan2((q[1] - p[1]) * self._kx, q[0] - p[0])
        d = math.degrees(abs(heading(b, c) - heading(a, b)))
        return min(d, 360 - d)

    # ---- detection ----
    def climbs(self, min_length=MIN_LENGTH_M, min_gain=MIN_GAIN_M, min_avg=MIN_AVG_GRADE) -> list[Segment]:
        return self._segments(self.ele, +1, min_length, min_gain, min_avg)

    def descents(self, min_length=500.0, min_drop=40.0, min_avg=4.0) -> list[Segment]:
        return self._segments(-self.ele, -1, min_length, min_drop, min_avg)

    def _segments(self, e: np.ndarray, sign: int, min_length, min_gain, min_avg) -> list[Segment]:
        spans, in_run, low, start, peak = [], False, 0, 0, 0
        for i in range(1, len(e)):
            if in_run:
                if e[i] > e[peak]:
                    peak = i
                elif e[peak] - e[i] >= DROP_TOL_M:
                    spans.append((start, peak))
                    in_run, low = False, i
            elif e[i] < e[low]:
                low = i
            elif e[i] - e[low] >= RISE_TOL_M:
                in_run, start, peak = True, low, i
        if in_run:
            spans.append((start, peak))

        out = []
        for i0, i1 in spans:
            i0 = self._trim_lead_in(e, i0, i1)
            i1 = self._trim_tail(e, i0, i1)
            length = float(self.grid[i1] - self.grid[i0])
            gain = float(e[i1] - e[i0])
            if length < min_length or gain < min_gain or length <= 0:
                continue
            avg = gain / length * 100
            if avg < min_avg:
                continue
            k = int(round(100 / STEP_M))
            steep = max((e[j + k] - e[j]) / (self.grid[j + k] - self.grid[j]) * 100
                        for j in range(i0, max(i0 + 1, i1 - k + 1))) if i1 - i0 >= k else avg
            score = length * avg
            out.append(Segment(float(self.grid[i0]), float(self.grid[i1]), length, gain, avg, max(steep, avg), score,
                               category_for(score) if sign > 0 else None))
        return out

    @staticmethod
    def _trim_tail(e: np.ndarray, i0: int, i1: int, k: int = 5, min_grade: float = 1.0) -> int:
        """Pull the end back to where the road last climbs. Without this, noise on a plateau after the summit
        keeps nudging the 'peak' forward and stretches the climb along the flat."""
        while i1 - k > i0 and (e[i1] - e[i1 - k]) / (k * STEP_M) * 100 < min_grade:
            i1 -= 1
        return i1

    @staticmethod
    def _trim_lead_in(e: np.ndarray, i0: int, i1: int, k: int = 5, min_grade: float = 1.0) -> int:
        """Advance the start past a flat run-up so the climb begins where it actually starts to rise."""
        while i0 + k < i1 and (e[i0 + k] - e[i0]) / (k * STEP_M) * 100 < min_grade:
            i0 += 1
        return i0
