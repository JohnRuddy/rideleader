from dataclasses import dataclass
from datetime import datetime, timezone

SPEED_MPS = 20 / 3.6   # synthetic pace for the timestamps head units require in course files
ODBL_NOTE = "Contains data © OpenStreetMap contributors (ODbL, https://www.openstreetmap.org/copyright)"


@dataclass(frozen=True)
class ExportPoint:
    lat: float
    lon: float
    ele: float | None
    dist_m: float


@dataclass(frozen=True)
class ExportWaypoint:
    kind: str
    name: str
    note: str | None
    lat: float
    lon: float
    s_m: float
    origin: str = "osm"


@dataclass(frozen=True)
class ExportRoute:
    title: str
    points: tuple[ExportPoint, ...]
    waypoints: tuple[ExportWaypoint, ...]   # included only, sorted by s_m
    created: datetime                       # fixed by the caller so output is reproducible

    @property
    def length_m(self) -> float:
        return self.points[-1].dist_m

    def time_at(self, dist_m: float) -> datetime:
        return datetime.fromtimestamp(self.created.timestamp() + dist_m / SPEED_MPS, tz=timezone.utc)

    @property
    def has_elevation(self) -> bool:
        return all(p.ele is not None for p in self.points)

    @property
    def uses_osm_data(self) -> bool:
        return any(w.origin == "osm" for w in self.waypoints)


def from_route(route, now: datetime | None = None) -> ExportRoute:
    from geoalchemy2.shape import to_shape
    xy = list(to_shape(route.geom).coords)
    ele = route.ele_m or [None] * len(xy)
    pts = tuple(ExportPoint(lat=c[1], lon=c[0], ele=e, dist_m=d) for c, e, d in zip(xy, ele, route.dist_m))
    wps = []
    for w in sorted(route.waypoints, key=lambda w: w.s_m):
        if w.included:
            p = to_shape(w.geom)
            wps.append(ExportWaypoint(w.kind, w.name, w.note, p.y, p.x, w.s_m, w.origin))
    created = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    return ExportRoute(route.title, pts, tuple(wps), created)
