"""Internal route model: ordered track points with cumulative distance.

Everything else (waypoints, climbs, ETAs) is anchored on dist_m along the route.
"""
from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

import gpxpy

EARTH_R = 6371008.8
ASCENT_THRESHOLD_M = 3.0  # hysteresis so GPS noise doesn't inflate ascent


@dataclass
class TrackPoint:
    lat: float
    lon: float
    ele: float | None
    dist_m: float = 0.0


@dataclass
class ParsedRoute:
    name: str
    points: list[TrackPoint]
    waypoints: list[dict]

    @property
    def length_m(self) -> float:
        return self.points[-1].dist_m if self.points else 0.0

    @property
    def has_elevation(self) -> bool:
        return all(p.ele is not None for p in self.points)

    def ascent_descent(self) -> tuple[float, float]:
        return ascent_descent([p.ele for p in self.points if p.ele is not None])


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = radians(lat1), radians(lat2)
    a = sin((p2 - p1) / 2) ** 2 + cos(p1) * cos(p2) * sin(radians(lon2 - lon1) / 2) ** 2
    return 2 * EARTH_R * asin(sqrt(a))


def ascent_descent(eles: list[float], threshold: float = ASCENT_THRESHOLD_M) -> tuple[float, float]:
    """Total ascent/descent, ignoring wiggles smaller than threshold."""
    if len(eles) < 2:
        return 0.0, 0.0
    up = down = 0.0
    ref = eles[0]
    for e in eles[1:]:
        if e - ref >= threshold:
            up += e - ref
            ref = e
        elif ref - e >= threshold:
            down += ref - e
            ref = e
    return up, down


def parse_gpx(data: bytes | str) -> ParsedRoute:
    gpx = gpxpy.parse(data)
    pts: list[TrackPoint] = []
    # Prefer tracks; fall back to routes (rtept). All segments concatenated in order.
    for trk in gpx.tracks:
        for seg in trk.segments:
            pts += [TrackPoint(p.latitude, p.longitude, p.elevation) for p in seg.points]
    if not pts:
        for rte in gpx.routes:
            pts += [TrackPoint(p.latitude, p.longitude, p.elevation) for p in rte.points]
    # Drop consecutive duplicates
    deduped: list[TrackPoint] = []
    for p in pts:
        if not deduped or (p.lat, p.lon) != (deduped[-1].lat, deduped[-1].lon):
            deduped.append(p)
    pts = deduped
    if len(pts) < 2:
        raise ValueError("GPX contains no usable track or route (need at least 2 distinct points)")
    for i in range(1, len(pts)):
        pts[i].dist_m = pts[i - 1].dist_m + haversine_m(pts[i - 1].lat, pts[i - 1].lon, pts[i].lat, pts[i].lon)
    name = (gpx.tracks[0].name if gpx.tracks else gpx.routes[0].name if gpx.routes else None) or gpx.name or "Untitled route"
    wpts = [
        {"name": w.name or "Waypoint", "lat": w.latitude, "lon": w.longitude, "sym": w.symbol, "desc": w.description}
        for w in gpx.waypoints
    ]
    return ParsedRoute(name=name, points=pts, waypoints=wpts)


def route_arrays(route) -> tuple[list[float], list[float]]:
    """(lats, lons) of a stored Route's geometry, aligned with route.dist_m."""
    from geoalchemy2.shape import to_shape
    xy = list(to_shape(route.geom).coords)
    return [c[1] for c in xy], [c[0] for c in xy]


def locate_on_route(route: ParsedRoute, lat: float, lon: float) -> tuple[float, float]:
    """Return (dist_along_m, offset_m) of the nearest track vertex.

    Vertex-level only for step 1; segment projection with a monotonic window
    (for out-and-back routes) arrives with the POI scan.
    """
    best = min(route.points, key=lambda p: haversine_m(p.lat, p.lon, lat, lon))
    return best.dist_m, haversine_m(best.lat, best.lon, lat, lon)
