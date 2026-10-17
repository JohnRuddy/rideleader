"""Write a coordinate list into a stored Route (geometry, cumulative distance, elevation, totals)."""
from geoalchemy2.elements import WKTElement
from geoalchemy2.shape import to_shape

from .editor import cum_dist
from .routemodel import ascent_descent


def route_coords(route) -> list[tuple[float, float, float | None]]:
    """(lon, lat, ele|None) for every stored vertex."""
    xy = list(to_shape(route.geom).coords)
    ele = route.ele_m or [None] * len(xy)
    return [(c[0], c[1], e) for c, e in zip(xy, ele)]


def apply_coords(route, coords) -> None:
    dist = cum_dist(list(coords))
    route.geom = WKTElement("LINESTRING(" + ",".join(f"{c[0]:.7f} {c[1]:.7f}" for c in coords) + ")", srid=4326)
    route.dist_m = dist
    has_ele = all(c[2] is not None for c in coords)
    route.ele_m = [float(c[2]) for c in coords] if has_ele else None
    route.length_m = dist[-1]
    route.ascent_m, route.descent_m = ascent_descent(route.ele_m) if has_ele else (None, None)
