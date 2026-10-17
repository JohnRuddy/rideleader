"""Apply a route edit to the stored route: geometry, waypoints, climbs, and the background rescan."""
from flask import current_app
from geoalchemy2.elements import WKTElement
from geoalchemy2.shape import to_shape

from . import climb_service as cs
from . import db
from .corridor import RouteIndex
from .editor import Region, Track, cum_dist
from .elevation import ElevationError, OpenMeteoElevation, fill_elevations
from .geometry import apply_coords, route_coords
from .models import ScanJob
from .routemodel import route_arrays
from .routing import BRouterProvider, RouteOptions, RoutingError


def routing_client():
    return current_app.config.get("ROUTING_CLIENT") or BRouterProvider(
        current_app.config["BROUTER_URL"], min_interval=current_app.config["BROUTER_MIN_INTERVAL"])


def elevation_client():
    return current_app.config.get("ELEVATION_CLIENT") or OpenMeteoElevation(current_app.config["ELEVATION_URL"])


def get_track(route) -> Track:
    coords = route_coords(route)
    return Track.from_json(route.editor, coords) if route.editor else Track.from_coords(coords)


def fill_missing_elevation(coords):
    """If some points have no elevation, take the whole line from the elevation service (or give up cleanly)."""
    if all(c[2] is not None for c in coords):
        return coords
    try:
        eles = fill_elevations(coords, cum_dist(coords), elevation_client())
    except ElevationError:
        return [(c[0], c[1], None) for c in coords]
    return [(c[0], c[1], e) for c, e in zip(coords, eles)]


def reanchor_waypoints(route, region: Region) -> None:
    """Keep every waypoint's coordinates and recompute where along the new line it is.

    Out-and-back safe: of the passes near a waypoint, take the one closest to where its old distance maps to."""
    lats, lons = route_arrays(route)
    index = RouteIndex(lats, lons, route.dist_m)
    for w in route.waypoints:
        pt = to_shape(w.geom)
        mapped = region.remap(w.s_m)
        passes = index.passes(pt.y, pt.x, max((w.offset_m or 0) + 500, 800)) or index.passes(pt.y, pt.x, 1e9)
        best = min(passes, key=lambda p: abs(p.s_m - mapped))
        w.s_m, w.offset_m, w.side = best.s_m, best.offset_m, best.side


def apply_edit(route, track: Track, region: Region) -> dict:
    """Replace the route's line with the edited track and bring everything anchored to it along."""
    carry = [(region.remap(c.s_end_m), c.regroup_mode, c.ease_km) for c in route.climbs]
    coords = fill_missing_elevation(track.coords())
    apply_coords(route, coords)
    route.editor = track.to_json()
    route.version = (route.version or 1) + 1
    db.session.flush()
    reanchor_waypoints(route, region)
    result = {}
    if cs.profile_for(route) is not None:
        result = cs.detect(route, carry=carry)
        from . import hazard_service
        hazard_service.sync(route, None)          # busy/unpaved positions need a rescan; steep sections follow the new line now
    else:
        route.climbs = []
        route.climbs_version = route.version
    db.session.commit()
    result["scan_job_id"] = maybe_rescan(route)
    return result


def maybe_rescan(route) -> int | None:
    """If the leader has searched for stops before, search again along the new line in the background.
    Pinned waypoints are kept by the scan itself. A job already waiting will read the new line, so don't pile up."""
    prev = ScanJob.query.filter_by(route_id=route.id).order_by(ScanJob.id.desc()).first()
    if prev is None:
        return None
    waiting = ScanJob.query.filter_by(route_id=route.id, status="queued").first()
    if waiting:
        return waiting.id
    from .jobs import enqueue_scan
    job = ScanJob(route_id=route.id, route_version=route.version, params=prev.params)
    db.session.add(job)
    db.session.commit()
    enqueue_scan(job.id)
    return job.id


def editor_json(route) -> dict:
    from . import history
    track = get_track(route)
    return {"anchors": [{"id": a.id, "lat": a.lat, "lon": a.lon, "kind": a.kind} for a in track.anchors],
            "options": track.options.to_json(), "history": history.info(route)}
