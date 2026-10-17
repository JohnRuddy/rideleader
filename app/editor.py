"""Route editing engine: pure, no database.

A route is a chain of *legs* between *anchors*:

    end ──leg── cut ──leg── via ──leg── cut ──leg── end
   (start)     (hidden)    (yours)     (hidden)     (finish)

- An imported GPX starts as one `original` leg: its geometry is the leader's, and is never re-snapped behind
  their back.
- Dragging the line, or clicking to add a via point, reroutes only a stretch around that spot (CUT_M each way).
  The rest of an original leg is kept exactly as it was. `cut` anchors mark where the original resumes.
- `routed` legs come from the RoutingProvider. Moving or deleting a via point reroutes its two neighbouring legs.
- Every operation returns a new Track plus a Region describing which stretch of the line changed, so waypoints
  anchored by distance-along-route can be moved along with it.
"""
from copy import deepcopy
from dataclasses import dataclass, field

from .routemodel import haversine_m
from .routing import Coord, RouteOptions, RoutingError, RoutingProvider

CUT_M = 1500.0   # how far either side of a dragged point the original is replaced at most
REJOIN_TOL_M = 15.0   # a rerouted path this close to the original line is the original: keep the original's own points


@dataclass
class Anchor:
    id: int
    lat: float
    lon: float
    kind: str   # 'end' | 'via' | 'cut'


@dataclass
class Leg:
    src: str                 # 'original' | 'routed'
    pts: list[Coord]         # includes both end vertices


@dataclass(frozen=True)
class Region:
    """The stretch of line that changed, in metres along the route, before and after."""
    a_old: float
    b_old: float
    a_new: float
    b_new: float

    def remap(self, s: float) -> float:
        if s <= self.a_old:
            return s
        if s >= self.b_old:
            return s - self.b_old + self.b_new
        span = self.b_old - self.a_old
        return self.a_new + (s - self.a_old) / span * (self.b_new - self.a_new) if span > 0 else self.a_new


IDENTITY = Region(0, 0, 0, 0)


def cum_dist(pts: list[Coord]) -> list[float]:
    out = [0.0]
    for a, b in zip(pts, pts[1:]):
        out.append(out[-1] + haversine_m(a[1], a[0], b[1], b[0]))
    return out


def _lerp(a: Coord, b: Coord, t: float) -> Coord:
    ele = None if a[2] is None or b[2] is None else a[2] + (b[2] - a[2]) * t
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, ele)


def point_at(pts: list[Coord], cum: list[float], s: float) -> Coord:
    s = min(max(s, 0.0), cum[-1])
    for i in range(1, len(pts)):
        if cum[i] >= s:
            seg = cum[i] - cum[i - 1]
            return _lerp(pts[i - 1], pts[i], (s - cum[i - 1]) / seg if seg > 0 else 0.0)
    return pts[-1]


def slice_pts(pts: list[Coord], s0: float, s1: float) -> list[Coord]:
    """Vertices between distances s0 and s1 (from the start of pts), with exact interpolated ends."""
    cum = cum_dist(pts)
    out = [point_at(pts, cum, s0)]
    out += [p for p, c in zip(pts, cum) if s0 < c < s1]
    out.append(point_at(pts, cum, s1))
    return out


def _dedupe(pts: list[Coord]) -> list[Coord]:
    out = [pts[0]]
    for p in pts[1:]:
        if (p[0], p[1]) != (out[-1][0], out[-1][1]):
            out.append(p)
    return out


def _join(pts: list[Coord], start: Coord, end: Coord) -> list[Coord]:
    """Make a routed polyline begin and end exactly on its anchors (the router snaps to the nearest road)."""
    pts = list(pts)
    if (pts[0][0], pts[0][1]) != (start[0], start[1]):
        pts.insert(0, start)
    if (pts[-1][0], pts[-1][1]) != (end[0], end[1]):
        pts.append(end)
    return _dedupe(pts)


def _rejoin_index(path: list[Coord], window: list[Coord], from_end: bool) -> int | None:
    """How far a router's path simply retraces the original line.

    Returns the index of the last point (from the start, or the first from the end) that is still within REJOIN_TOL_M
    of `window`, the original stretch being replaced, provided the path stays on it continuously from that end.
    Always leaves at least two points routed so a leg never collapses to nothing.
    """
    import math

    from shapely.geometry import LineString, Point
    if len(path) < 3 or len(window) < 2:
        return None
    k = math.cos(math.radians(window[0][1]))
    line = LineString([(p[0] * k * 111_320.0, p[1] * 111_320.0) for p in window])
    seq = list(reversed(path)) if from_end else path
    n = 0
    for p in seq:
        if line.distance(Point(p[0] * k * 111_320.0, p[1] * 111_320.0)) <= REJOIN_TOL_M:
            n += 1
        else:
            break
    n = min(n, len(path) - 2)
    if n < 2:
        return None
    return len(path) - n if from_end else n - 1


def _s_on(window: list[Coord], q: Coord) -> float:
    """Metres along `window` of the point on it nearest q."""
    import math

    from shapely.geometry import LineString, Point
    k = math.cos(math.radians(window[0][1]))
    line = LineString([(p[0] * k * 111_320.0, p[1] * 111_320.0) for p in window])
    along = line.project(Point(q[0] * k * 111_320.0, q[1] * 111_320.0))
    total = cum_dist(window)[-1]
    return along * (total / line.length) if line.length > 0 else 0.0


@dataclass
class Track:
    anchors: list[Anchor]
    legs: list[Leg]
    next_id: int = 1
    options: RouteOptions = field(default_factory=RouteOptions)

    # ---- construction / serialisation ----
    @staticmethod
    def from_coords(coords: list[Coord], options: RouteOptions | None = None) -> "Track":
        first, last = coords[0], coords[-1]
        return Track([Anchor(1, first[1], first[0], "end"), Anchor(2, last[1], last[0], "end")],
                     [Leg("original", list(coords))], 3, options or RouteOptions())

    def coords(self) -> list[Coord]:
        out = list(self.legs[0].pts)
        for leg in self.legs[1:]:
            out += leg.pts[1:]
        return out

    def leg_ranges(self) -> list[tuple[float, float]]:
        out, s = [], 0.0
        for leg in self.legs:
            length = cum_dist(leg.pts)[-1]
            out.append((s, s + length))
            s += length
        return out

    def to_json(self) -> dict:
        return {"anchors": [{"id": a.id, "lat": a.lat, "lon": a.lon, "kind": a.kind} for a in self.anchors],
                "legs": [{"src": l.src, "n": len(l.pts)} for l in self.legs],
                "next_id": self.next_id, "options": self.options.to_json()}

    @staticmethod
    def from_json(data: dict, coords: list[Coord]) -> "Track":
        legs, i = [], 0
        for spec in data["legs"]:
            legs.append(Leg(spec["src"], coords[i:i + spec["n"]]))
            i += spec["n"] - 1
        if i + 1 != len(coords):
            raise ValueError("editor state does not match the stored geometry")
        return Track([Anchor(**a) for a in data["anchors"]], legs, data["next_id"],
                     RouteOptions.from_json(data.get("options")))

    def anchor_index(self, anchor_id: int) -> int:
        for i, a in enumerate(self.anchors):
            if a.id == anchor_id:
                return i
        raise ValueError("No such via point.")

    def _new_id(self) -> int:
        self.next_id += 1
        return self.next_id - 1


def _route(router: RoutingProvider, a: Coord, b: Coord, opts: RouteOptions) -> list[Coord]:
    return router.route([(a[1], a[0]), (b[1], b[0])], opts)


def _normalise(t: Track) -> None:
    """A cut anchor with routed legs on both sides marks nothing any more: merge through it."""
    i = 1
    while i < len(t.anchors) - 1:
        if t.anchors[i].kind == "cut" and t.legs[i - 1].src == "routed" and t.legs[i].src == "routed":
            merged = Leg("routed", t.legs[i - 1].pts + t.legs[i].pts[1:])
            t.legs[i - 1:i + 1] = [merged]
            del t.anchors[i]
        else:
            i += 1


def add_via(t: Track, s_m: float, lat: float, lon: float, router: RoutingProvider,
            options: RouteOptions | None = None, cut_m: float = CUT_M) -> tuple[Track, Region]:
    """Pull the route through (lat, lon), grabbing the line at distance s_m. Reroutes only around that spot."""
    opts = options or t.options
    ranges = t.leg_ranges()
    k = next((i for i, (a, b) in enumerate(ranges) if a <= s_m <= b), len(ranges) - 1)
    leg = t.legs[k]
    n = deepcopy(t)
    n.options = opts
    start_s = ranges[k][0]

    if leg.src == "original":
        cum = cum_dist(leg.pts)
        local = min(max(s_m - start_s, 0.0), cum[-1])
        s_in, s_out = max(0.0, local - cut_m), min(cum[-1], local + cut_m)
        pre = slice_pts(leg.pts, 0, s_in) if s_in > 0 else None
        post = slice_pts(leg.pts, s_out, cum[-1]) if s_out < cum[-1] else None
        c_in = pre[-1] if pre else leg.pts[0]          # where the reroute starts: the cut, or the leg's own start
        c_out = post[0] if post else leg.pts[-1]
        r1 = _route(router, c_in, (lon, lat, None), opts)
        p_coord = r1[-1]
        r2 = _route(router, p_coord, c_out, opts)
        # Where the router's path just retraces the original, keep the original's own points: the leader's line
        # stays exactly as it was, and only the part that truly differs is replaced.
        window = slice_pts(leg.pts, s_in, s_out)
        i1 = _rejoin_index(r1, window, from_end=False)
        if i1 is not None:
            s_in = min(max(s_in + _s_on(window, r1[i1]), s_in), local)
            r1 = r1[i1:]
            pre = slice_pts(leg.pts, 0, s_in) if s_in > 0 else None
            c_in = pre[-1] if pre else leg.pts[0]
        i2 = _rejoin_index(r2, window, from_end=True)
        if i2 is not None:
            s_out = max(min(s_in + _s_on(slice_pts(leg.pts, s_in, cum[-1]), r2[i2]), s_out), local)
            r2 = r2[:i2 + 1]
            post = slice_pts(leg.pts, s_out, cum[-1]) if s_out < cum[-1] else None
            c_out = post[0] if post else leg.pts[-1]
        leg1 = Leg("routed", _join(r1, c_in, p_coord))
        leg2 = Leg("routed", _join(r2, p_coord, c_out))
        new_legs, new_anchors = [], []
        if pre:
            new_legs.append(Leg("original", pre))
            new_anchors.append(Anchor(n._new_id(), c_in[1], c_in[0], "cut"))
        new_legs += [leg1, leg2]
        new_anchors.append(Anchor(n._new_id(), p_coord[1], p_coord[0], "via"))
        if post:
            new_legs.append(Leg("original", post))
            new_anchors.append(Anchor(n._new_id(), c_out[1], c_out[0], "cut"))
        n.legs[k:k + 1] = new_legs
        n.anchors[k + 1:k + 1] = new_anchors
        a_old, b_old = start_s + s_in, start_s + s_out
    else:
        a_c, b_c = leg.pts[0], leg.pts[-1]
        r1 = _route(router, a_c, (lon, lat, None), opts)
        p_coord = r1[-1]
        r2 = _route(router, p_coord, b_c, opts)
        n.legs[k:k + 1] = [Leg("routed", _join(r1, a_c, p_coord)), Leg("routed", _join(r2, p_coord, b_c))]
        n.anchors.insert(k + 1, Anchor(n._new_id(), p_coord[1], p_coord[0], "via"))
        a_old, b_old = ranges[k]
    new_len = cum_dist(leg1.pts if leg.src == "original" else n.legs[k].pts)[-1] + \
        cum_dist(leg2.pts if leg.src == "original" else n.legs[k + 1].pts)[-1]
    region = Region(a_old, b_old, a_old, a_old + new_len)
    _normalise(n)
    return n, region


def move_via(t: Track, anchor_id: int, lat: float, lon: float, router: RoutingProvider,
             options: RouteOptions | None = None) -> tuple[Track, Region]:
    opts = options or t.options
    i = t.anchor_index(anchor_id)
    if t.anchors[i].kind != "via":
        raise ValueError("Only via points can be moved.")
    ranges = t.leg_ranges()
    a_c, b_c = t.legs[i - 1].pts[0], t.legs[i].pts[-1]
    r1 = _route(router, a_c, (lon, lat, None), opts)
    p = r1[-1]
    r2 = _route(router, p, b_c, opts)
    n = deepcopy(t)
    n.options = opts
    l1, l2 = Leg("routed", _join(r1, a_c, p)), Leg("routed", _join(r2, p, b_c))
    n.legs[i - 1:i + 1] = [l1, l2]
    n.anchors[i] = Anchor(anchor_id, p[1], p[0], "via")
    a_old, b_old = ranges[i - 1][0], ranges[i][1]
    region = Region(a_old, b_old, a_old, a_old + cum_dist(l1.pts)[-1] + cum_dist(l2.pts)[-1])
    _normalise(n)
    return n, region


def delete_via(t: Track, anchor_id: int, router: RoutingProvider,
               options: RouteOptions | None = None) -> tuple[Track, Region]:
    opts = options or t.options
    i = t.anchor_index(anchor_id)
    if t.anchors[i].kind != "via":
        raise ValueError("Only via points can be removed.")
    ranges = t.leg_ranges()
    a_c, b_c = t.legs[i - 1].pts[0], t.legs[i].pts[-1]
    r = _route(router, a_c, b_c, opts)
    n = deepcopy(t)
    n.options = opts
    merged = Leg("routed", _join(r, a_c, b_c))
    n.legs[i - 1:i + 1] = [merged]
    del n.anchors[i]
    a_old, b_old = ranges[i - 1][0], ranges[i][1]
    region = Region(a_old, b_old, a_old, a_old + cum_dist(merged.pts)[-1])
    _normalise(n)
    return n, region


def reroute_section(t: Track, s_a: float, s_b: float, router: RoutingProvider, options: RouteOptions | None = None,
                    avoid: list[tuple[float, float, float]] | None = None) -> tuple[Track, Region]:
    """Replace the stretch from s_a to s_b with a routed detour that keeps out of the `avoid` circles (a closure).

    Works within the leg holding the middle of the stretch: a closure that straddles a via point is only partly
    rerouted, so callers re-check the result."""
    opts = options or t.options
    ranges = t.leg_ranges()
    mid = (s_a + s_b) / 2
    k = next((i for i, (a, b) in enumerate(ranges) if a <= mid <= b), len(ranges) - 1)
    leg, start_s = t.legs[k], ranges[k][0]
    cum = cum_dist(leg.pts)
    lo, hi = min(max(s_a - start_s, 0.0), cum[-1]), min(max(s_b - start_s, 0.0), cum[-1])
    if hi - lo < 1:
        raise ValueError("There's nothing to reroute there.")
    pre = slice_pts(leg.pts, 0, lo) if lo > 0 else None
    post = slice_pts(leg.pts, hi, cum[-1]) if hi < cum[-1] else None
    c_in = pre[-1] if pre else leg.pts[0]
    c_out = post[0] if post else leg.pts[-1]
    detour = _join(router.route([(c_in[1], c_in[0]), (c_out[1], c_out[0])], opts, avoid), c_in, c_out)
    n = deepcopy(t)
    n.options = opts
    if leg.src == "original":
        new_legs, new_anchors = [], []
        if pre:
            new_legs.append(Leg("original", pre))
            new_anchors.append(Anchor(n._new_id(), c_in[1], c_in[0], "cut"))
        new_legs.append(Leg("routed", detour))
        if post:
            new_anchors.append(Anchor(n._new_id(), c_out[1], c_out[0], "cut"))
            new_legs.append(Leg("original", post))
        n.legs[k:k + 1] = new_legs
        n.anchors[k + 1:k + 1] = new_anchors
    else:
        n.legs[k] = Leg("routed", (pre[:-1] if pre else []) + detour + (post[1:] if post else []))
    region = Region(start_s + lo, start_s + hi, start_s + lo, start_s + lo + cum_dist(detour)[-1])
    _normalise(n)
    return n, region
