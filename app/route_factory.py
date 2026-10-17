"""Create a stored Route from a parsed GPX. Used by file upload and by the Strava import."""
from geoalchemy2.elements import WKTElement

from . import edit_service as es
from .elevation import ElevationError, fill_elevations
from .models import Route, Waypoint
from .routemodel import ParsedRoute, locate_on_route


def current_club_id() -> int | None:
    """The club the signed-in person is acting in, when there is a request to ask (a worker has none)."""
    from flask import g, has_request_context
    club = g.get("club") if has_request_context() else None
    return club.id if club else None


def create_route(parsed: ParsedRoute, *, owner_id: int | None = None, source: str = "gpx", visibility: str = "club",
                 title: str | None = None, strava_route_id: int | None = None, strava_expires_at=None,
                 keep_gpx_waypoints: bool = True, club_id: int | None = None) -> Route:
    """Build (not commit) a Route. Elevation comes from the file, else from the elevation service if it answers."""
    if not parsed.has_elevation:
        try:
            filled = fill_elevations([(p.lon, p.lat) for p in parsed.points], [p.dist_m for p in parsed.points],
                                     es.elevation_client())
            for p, e in zip(parsed.points, filled):
                p.ele = e
        except ElevationError:
            pass                      # carry on without elevation; the page says so
    has_ele = parsed.has_elevation
    up, down = parsed.ascent_descent() if has_ele else (None, None)
    wkt = "LINESTRING(" + ",".join(f"{p.lon} {p.lat}" for p in parsed.points) + ")"
    route = Route(
        title=(title or parsed.name)[:200], geom=WKTElement(wkt, srid=4326), source=source, owner_id=owner_id,
        club_id=club_id if club_id is not None else (current_club_id() if owner_id else None),
        visibility=visibility, strava_route_id=strava_route_id, strava_expires_at=strava_expires_at,
        length_m=parsed.length_m, ascent_m=up, descent_m=down,
        dist_m=[p.dist_m for p in parsed.points], ele_m=[p.ele for p in parsed.points] if has_ele else None)
    if keep_gpx_waypoints:
        for w in parsed.waypoints:
            s, off = locate_on_route(parsed, w["lat"], w["lon"])
            route.waypoints.append(Waypoint(
                name=w["name"][:200], note=w["desc"], s_m=s, offset_m=off, origin="gpx",
                geom=WKTElement(f"POINT({w['lon']} {w['lat']})", srid=4326), tags={"sym": w["sym"]}))
    return route
