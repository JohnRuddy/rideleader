"""Conditions for a planned ride: weather warnings, forecast along the route, the 10-day strip, daylight, closures.

Every endpoint takes the route's ride plan (date, start time, pace groups) as its question, so changing the day or
time and calling again recalculates everything time-dependent."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, g, jsonify, request
from geoalchemy2.elements import WKTElement

from . import along_route as ar
from . import closures as cl
from . import club as clubmod
from . import clubs
from . import counties, db, editor, history, pace
from . import edit_service as es
from . import warnings_feed as wf
from . import weather
from .models import Closure, Route
from .routemodel import route_arrays
from .routing import RoutingError
from .security import login_required
from .plan import Plan, clock_now as _now

bp = Blueprint("conditions", __name__)


def _route(route_id) -> Route:
    return db.session.get(Route, route_id) or abort(404)


def review_message(route: Route, plan: Plan, banner: dict) -> str | None:
    """A message the leader can paste into the group chat when an Orange or Red warning means the spin is under review."""
    if not banner["postpone_prompt"]:
        return None
    top = banner["warnings"][0]
    day = plan.start.strftime("%A")
    where = ", ".join(top["counties"]) or "our area"
    return (f"⚠️ {day}'s spin ({route.title}) is under review. Met Éireann has an {top['level'].title()} {top['type']} warning "
            f"for {where} ({top['valid_from']} to {top['valid_to']}). I'll confirm whether it's on as soon as I can. "
            f"Please don't set off until I do. Details: {wf.WARNINGS_PAGE}")


@bp.get("/api/routes/<int:route_id>/warnings")
def warnings(route_id):
    route = _route(route_id)
    plan = Plan(route)
    b = plan.banner()
    b["review_message"] = review_message(route, plan, b)
    b["window"] = {"start": plan.start.isoformat(), "end": plan.finish.isoformat(), "buffer_h": plan.settings["warning_buffer_h"]}
    return jsonify(b)


@bp.get("/api/routes/<int:route_id>/weather")
def route_weather(route_id):
    route = _route(route_id)
    plan = Plan(route)
    day = plan.daylight()
    res = ar.weather_along(plan.line, plan.start, plan.groups, plan.planned, plan.summits(), plan.forecast, plan.settings,
                           _now(), plan.tz, day["lights_needed"])
    res["daylight"] = day
    res["date"] = plan.ride.date.isoformat()
    # the licence ties forecast data to the warnings banner, so the answer carries it
    res["warnings"] = plan.banner()
    return jsonify(res)


@bp.get("/api/routes/<int:route_id>/daylight")
def route_daylight(route_id):
    return jsonify(Plan(_route(route_id)).daylight())


@bp.get("/api/routes/<int:route_id>/days")
def days(route_id):
    plan = Plan(_route(route_id))
    tz = ZoneInfo(plan.tz)
    now = _now()
    cs = plan.counties()
    mid = plan.line.point(plan.line.length / 2)
    duration = max(s["total_s"] for s in plan.summaries) / 3600
    wf.ensure_fresh(wf.get_provider(current_app.config), now)

    def level(start, end):
        return wf.day_level(cs, start, end, plan.settings["warning_buffer_h"], now)

    strip = ar.day_strip(mid, plan.ride.start_time, plan.tz, plan.settings["usual_days"], duration, plan.forecast, level,
                         now.astimezone(tz).date(), now)
    for d in strip:
        d["selected"] = d["date"] == plan.ride.date.isoformat()
    return jsonify({"days": strip, "start_time": plan.ride.start_time.strftime("%H:%M"),
                    "attribution": [weather.ATTRIBUTION["met-eireann"]], "warnings": plan.banner(),
                    "selected": plan.ride.date.isoformat()})


# ---------------- closures ----------------

def _closure_check(plan: Plan) -> dict:
    return cl.check(plan.lats, plan.lons, plan.route.dist_m, plan.start, plan.finish, cl.providers(current_app.config, plan.route.club_id))


@bp.get("/api/routes/<int:route_id>/closures")
def route_closures(route_id):
    out = _closure_check(Plan(_route(route_id)))
    out.pop("_hits")
    return jsonify(out)


@bp.post("/api/routes/<int:route_id>/closures/reroute")
def reroute_around(route_id):
    """Offer to route round a closure: replace the stretch it blocks (plus a little either side) with a detour."""
    route = _route(route_id)
    plan = Plan(route)
    wanted = (request.get_json(silent=True) or {}).get("closure_id")
    hit = next((h for h in _closure_check(plan)["_hits"] if h["id"] == wanted), None)
    if hit is None:
        return jsonify(error="That closure isn't on the route any more."), 404
    history.before(route)
    try:
        new, region = editor.reroute_section(es.get_track(route), max(hit["s_start"] - 300, 0), min(hit["s_end"] + 300, route.length_m),
                                             es.routing_client(), avoid=cl.avoid_circles(hit))
    except RoutingError as e:
        db.session.rollback()
        return jsonify(error=str(e)), 422
    except ValueError as e:
        db.session.rollback()
        return jsonify(error=str(e)), 400
    result = es.apply_edit(route, new, region)
    history.record(route, "Reroute around closure")
    after = _closure_check(Plan(route))
    return jsonify({"length_m": route.length_m, "version": route.version, "scan_job_id": result.get("scan_job_id"),
                    "still_blocked": any(h["id"] == wanted for h in after["_hits"]),
                    "other_closures": [h["description"] for h in after["hits"] if h["id"] != wanted]})


def _parse_dt(v, end_of_day=False) -> datetime:
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if len(str(v)) <= 10 and end_of_day:
        d = d.replace(hour=23, minute=59)
    return d if d.tzinfo else d.replace(tzinfo=ZoneInfo("Europe/Dublin"))


@bp.post("/api/closures")
@login_required
def add_closure():
    b = request.get_json(silent=True) or {}
    try:
        lat, lon = float(b["lat"]), float(b["lon"])
        radius = float(b.get("radius_m", 100))
        text = str(b.get("description", "")).strip()
        starts, ends = _parse_dt(b["starts_at"]), _parse_dt(b["ends_at"], True)
    except (KeyError, TypeError, ValueError):
        return jsonify(error="A closure needs a position, a description, and from and to dates."), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify(error="That position isn't on Earth."), 400
    if not text or len(text) > 300:
        return jsonify(error="Describe the closure in up to 300 characters."), 400
    if not 20 <= radius <= 2000:
        return jsonify(error="The affected area should be 20 to 2000 m across."), 400
    if ends <= starts:
        return jsonify(error="The end date must be after the start."), 400
    c = Closure(geom=WKTElement(f"POINT({lon} {lat})", srid=4326), radius_m=radius, description=text, starts_at=starts,
                ends_at=ends, entered_by=g.user.id, club_id=clubs.current_club_id())
    db.session.add(c)
    db.session.commit()
    return jsonify(_closure_json(c)), 201


def _closure_json(c: Closure) -> dict:
    from geoalchemy2.shape import to_shape
    p = to_shape(c.geom)
    return {"id": c.id, "lat": p.y, "lon": p.x, "radius_m": c.radius_m, "description": c.description,
            "starts_at": c.starts_at.isoformat(), "ends_at": c.ends_at.isoformat(), "entered_by": c.entered_by}


@bp.get("/api/closures")
@login_required
def list_closures():
    """Closures that have not yet expired, for the club to see and tidy up."""
    rows = clubs.scoped(Closure.query, Closure).filter(Closure.ends_at >= _now()).order_by(Closure.ends_at).all()
    return jsonify([_closure_json(c) for c in rows])


@bp.delete("/api/closures/<int:closure_id>")
@login_required
def delete_closure(closure_id):
    c = db.session.get(Closure, closure_id) or abort(404)
    if c.club_id != clubs.current_club_id() and not clubs.is_platform_admin(g.user):
        abort(404)
    if c.entered_by != g.user.id and g.user.role != "admin" and not g.club_admin:
        abort(403, description="Only the person who entered a closure, or a club admin, can remove it.")
    db.session.delete(c)
    db.session.commit()
    return "", 204


# ---------------- club settings ----------------

@bp.get("/api/club")
@login_required
def get_club():
    club = clubmod.get_club(clubs.current_club_id())
    return jsonify({"name": club.name, "settings": clubmod.settings(club.id), "defaults": clubmod.DEFAULT_SETTINGS,
                    "your_role": "admin" if (g.user.role == "admin" or g.club_admin) else g.user.role})


@bp.put("/api/club")
@login_required
def put_club():
    if g.user.role != "admin" and not g.club_admin:
        abort(403, description="Only a club admin can change the club's settings.")
    body = request.get_json(silent=True) or {}
    club = clubmod.get_club(clubs.current_club_id())
    try:
        updates = clubmod.validate(body)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if "name" in body:
        club.name = str(body["name"]).strip()[:120]
    club.settings = {**(club.settings or {}), **updates}
    db.session.commit()
    return jsonify({"name": club.name, "settings": clubmod.settings(club.id)})
