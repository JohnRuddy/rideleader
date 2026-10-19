"""Create a stored Route from a parsed GPX. Used by file upload and by the Strava import."""
from geoalchemy2.elements import WKTElement

from . import db
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


def run_again(src: Route, user, club_id: int | None, new_date=None) -> Route:
    """Another run of the same ride: a new, separate route and ride record, so the earlier one (its date, notes, share
    link, sponsor choice and history) is never touched. The line, stops, climbs, pace groups and the leader's word are
    carried over as a starting point; checks that depend on the date (weather, daylight, closures, café hours) are worked
    out fresh when the new ride is opened. The share link and any explicit sponsor choice are not copied."""
    import copy as _copy
    from datetime import timedelta

    from geoalchemy2.shape import to_shape

    from .models import Climb, PaceGroup, Ride
    from .plan import clock_now, get_or_create_ride

    if src.source == "strava":
        raise PermissionError("A route from Strava has to be shared before it can be run again.")
    ride = get_or_create_ride(src)
    if new_date is None:
        today = clock_now().date()
        new_date = ride.date + timedelta(days=7)
        while new_date <= today:
            new_date += timedelta(days=7)
    coords = list(to_shape(src.geom).coords)
    new = Route(title=src.title, source=src.source if src.source in ("gpx", "editor") else "gpx", owner_id=user.id,
                visibility=src.visibility, club_id=club_id, version=1,
                geom=WKTElement("LINESTRING(" + ",".join(f"{x} {y}" for x, y in coords) + ")", srid=4326),
                length_m=src.length_m, ascent_m=src.ascent_m, descent_m=src.descent_m, dist_m=list(src.dist_m),
                ele_m=list(src.ele_m) if src.ele_m else None, road_summary=_copy.deepcopy(src.road_summary),
                climb_settings=_copy.deepcopy(src.climb_settings), editor=_copy.deepcopy(src.editor),
                reused_from_id=src.id, series_root_id=src.series_root_id or src.id)
    for w in src.waypoints:
        p = to_shape(w.geom)
        new.waypoints.append(Waypoint(
            kind=w.kind, name=w.name, note=w.note, s_m=w.s_m, offset_m=w.offset_m, pinned=w.pinned, included=w.included,
            origin=w.origin, tags=_copy.deepcopy(w.tags), side=w.side, osm_ref=w.osm_ref, planned_stop=w.planned_stop,
            club_hours=w.club_hours, club_hours_at=w.club_hours_at, osm_hours_changed_at=w.osm_hours_changed_at,
            club_stop_id=w.club_stop_id, geom=WKTElement(f"POINT({p.x} {p.y})", srid=4326)))
    for c in src.climbs:
        new.climbs.append(Climb(s_start_m=c.s_start_m, s_end_m=c.s_end_m, length_m=c.length_m, gain_m=c.gain_m,
                                avg_grade=c.avg_grade, max_grade=c.max_grade, score=c.score, category=c.category,
                                regroup_mode=c.regroup_mode, ease_km=c.ease_km))
    if src.climbs:
        new.climbs_version = 1
    new.ride = Ride(date=new_date, start_time=ride.start_time, tz=ride.tz, meeting_name=ride.meeting_name,
                    meeting_lat=ride.meeting_lat, meeting_lon=ride.meeting_lon, leader_phone=ride.leader_phone, notes=ride.notes,
                    sponsor_mode="auto",
                    groups=[PaceGroup(position=g.position, name=g.name, speed_kmh=g.speed_kmh, stop_min=g.stop_min) for g in ride.groups])
    db.session.add(new)
    db.session.flush()
    return new
