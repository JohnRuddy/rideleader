import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, abort, current_app, g, jsonify, redirect, render_template, request, url_for
from geoalchemy2.elements import WKTElement
from geoalchemy2.shape import to_shape

from . import categories, db
from . import export as exp
from . import climb_service as cs
from . import edit_service as es
from . import history
from . import hours as hrs
from . import pace
from .bulk import apply_bulk
from .jobs import enqueue_scan
from .corridor import RouteIndex
from .models import Climb, PaceGroup, Ride, Route, ScanJob, User, Waypoint
from .plan import DEFAULT_GROUPS, clock_now, get_or_create_ride as _get_or_create_ride, planned_s as _planned_s
from .routemodel import parse_gpx, route_arrays
from .route_factory import create_route
from .security import can_change, visible_routes_filter
from .sponsor_views import sponsor_options

bp = Blueprint("main", __name__)


def _index(error=None, status=200):
    routes = Route.query.filter(visible_routes_filter()).order_by(Route.created_at.desc()).all()
    today = clock_now().astimezone(ZoneInfo("Europe/Dublin")).date()
    # upcoming rides first, soonest at the top; everything else as before (newest first)
    upcoming = sorted([r for r in routes if r.ride and r.ride.date >= today], key=lambda r: (r.ride.date, r.ride.start_time))
    rest = [r for r in routes if r not in upcoming]
    nxt = upcoming[0] if upcoming else None
    owner_ids = {r.owner_id for r in routes if r.owner_id}
    names = {u.id: (u.display_name or "") for u in User.query.filter(User.id.in_(owner_ids)).all()} if owner_ids else {}
    return render_template("index.html", routes=upcoming + rest, error=error, today=today, next_ride=nxt, leaders=names,
                           days_to_next=(nxt.ride.date - today).days if nxt else None), status


def _production() -> bool:
    return current_app.config.get("RLK_ENV") == "production"


@bp.get("/welcome")
def welcome():
    """The product page. Always reachable; it is also what signed-out visitors see at / in production."""
    return render_template("landing.html")


@bp.get("/")
def index():
    if g.user is None and _production():
        return welcome()
    return _index()


@bp.post("/routes/upload")
def upload():
    if g.user is None and _production():       # no anonymous writes on the public site
        return redirect(url_for("auth.login", next="/"))
    f = request.files.get("gpx")
    if not f or not f.filename:
        return _index("Choose a GPX file first.", 400)
    if not f.filename.lower().endswith(".gpx"):
        return _index("Only GPX files for now (TCX and FIT come later).", 400)
    try:
        parsed = parse_gpx(f.read())
    except Exception as e:  # gpxpy raises several exception types for malformed input
        return _index(f"Couldn't read that file: {e}", 400)

    owner = g.user
    # a signed-in leader's route is shared with their club from the start (one switch makes it private); with no
    # club to share it with it stays private; a signed-out upload is the old open kind
    route = create_route(parsed, owner_id=owner.id if owner else None,
                         visibility=("club" if g.get("club") else "private") if owner else "club")
    db.session.add(route)
    db.session.commit()
    return redirect(url_for("main.show", route_id=route.id))


def _next_step(route: Route) -> dict | None:
    """The one thing a leader should do next with this route: find stops, then share it."""
    scanned = any(w.origin in ("osm", "club") for w in route.waypoints)
    if not scanned:
        return {"tab": "stops", "text": "Find the cafés, water and defibrillators along the route.", "button": "Find stops"}
    if not (route.ride and route.ride.share_token):
        return {"tab": "share", "text": "Looking good. Add a word for the group and send them the plan.", "button": "Share it"}
    return None


@bp.get("/routes/<int:route_id>")
def show(route_id):
    route = db.session.get(Route, route_id) or abort(404)
    can_edit = can_change(route, g.user)
    return render_template("route.html", route=route, tile_url=current_app.config["TILE_URL"],
                           categories=categories.public_json(), device=_current_device(),
                           can_edit=can_edit, next_step=_next_step(route) if can_edit and g.user else None,
                           sponsor_options=sponsor_options(route) if can_edit and g.user else None,
                           can_run_again=bool(g.user) and route.source != "strava")


@bp.post("/routes/<int:route_id>/delete")
def delete(route_id):
    route = db.session.get(Route, route_id) or abort(404)
    db.session.delete(route)
    db.session.commit()
    return redirect(url_for("main.index"))


shown = categories.shown


def wp_json(w: Waypoint) -> dict:
    pt = to_shape(w.geom)
    return {"id": w.id, "kind": w.kind, "kind_label": categories.label_for(w.kind),
            "color": categories.color_for(w.kind), "name": w.name, "note": w.note,
            "s_m": w.s_m, "offset_m": w.offset_m, "side": w.side, "included": w.included,
            "pinned": w.pinned, "origin": w.origin, "lon": pt.x, "lat": pt.y,
            "planned_stop": w.planned_stop, "club_hours": w.club_hours,
            "osm_hours": (w.tags or {}).get("opening_hours"),
            "club": w.origin == "club", "verified_at": (w.tags or {}).get("verified_at"), "club_stop_id": w.club_stop_id,
            "reason": (w.tags or {}).get("reason"), "mode": (w.tags or {}).get("mode"),
            "climb_id": (w.tags or {}).get("climb_id")}


@bp.get("/api/routes/<int:route_id>")
def api_route(route_id):
    """Track as parallel arrays plus waypoints; the map and profile both read this."""
    route = db.session.get(Route, route_id) or abort(404)
    return jsonify(
        id=route.id, title=route.title, length_m=route.length_m,
        ascent_m=route.ascent_m, descent_m=route.descent_m,
        coords=[[x, y] for x, y in to_shape(route.geom).coords],
        dist_m=route.dist_m, ele_m=route.ele_m,
        waypoints=[wp_json(w) for w in shown(route.waypoints)],
    )


@bp.patch("/api/routes/<int:route_id>")
def rename_route(route_id):
    """The only editable field on the route itself is its title (Strava names are often just a date)."""
    route = db.session.get(Route, route_id) or abort(404)
    title = str((request.get_json(silent=True) or {}).get("title") or "").strip()
    if not title:
        return jsonify(error="Give the route a name."), 400
    route.title = title[:200]
    db.session.commit()
    return jsonify(id=route.id, title=route.title)


@bp.get("/api/routes/<int:route_id>/readiness")
def api_readiness(route_id):
    from .readiness import readiness
    route = db.session.get(Route, route_id) or abort(404)
    return jsonify(readiness(route))


@bp.get("/api/routes/<int:route_id>/waypoints")
def api_waypoints(route_id):
    route = db.session.get(Route, route_id) or abort(404)
    return jsonify([wp_json(w) for w in shown(route.waypoints)])


# ---- scanning ----

@bp.post("/routes/<int:route_id>/scan")
def start_scan(route_id):
    route = db.session.get(Route, route_id) or abort(404)
    body = request.get_json(silent=True) or {}
    corridors = {}
    for k, v in (body.get("corridors") or {}).items():
        if k not in categories.SCANNABLE or not isinstance(v, (int, float)) or not 10 <= v <= 5000:
            return jsonify(error=f"bad corridor for {k!r} (10-5000 m)"), 400
        corridors[k] = v
    cats = body.get("categories")
    if cats is not None and (not cats or any(c not in categories.SCANNABLE for c in cats)):
        return jsonify(error="pick at least one valid category"), 400
    job = ScanJob(route_id=route.id, route_version=route.version,
                  params={"corridors": corridors, "categories": cats})
    db.session.add(job)
    db.session.commit()
    enqueue_scan(job.id)
    return jsonify(job_id=job.id), 202


@bp.get("/api/scans/<int:job_id>")
def scan_status(job_id):
    job = db.session.get(ScanJob, job_id) or abort(404)
    return jsonify(id=job.id, status=job.status, progress=job.progress, message=job.message, summary=job.summary)


# ---- waypoint editing ----

@bp.patch("/api/waypoints/<int:wp_id>")
def patch_waypoint(wp_id):
    w = db.session.get(Waypoint, wp_id) or abort(404)
    body = request.get_json(silent=True) or {}
    history.before(w.route)
    for field in ("included", "pinned", "planned_stop"):
        if field in body:
            if not isinstance(body[field], bool):
                return jsonify(error=f"{field} must be true or false"), 400
            setattr(w, field, body[field])
    if "name" in body:
        name = str(body["name"]).strip()
        if not name:
            return jsonify(error="name can't be empty"), 400
        w.name = name[:200]
    if "note" in body:
        w.note = str(body["note"]).strip() or None
    if "lat" in body or "lon" in body:
        try:
            lat, lon = float(body["lat"]), float(body["lon"])
        except (KeyError, TypeError, ValueError):
            return jsonify(error="A move needs both lat and lon."), 400
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return jsonify(error="That position isn't on Earth."), 400
        lats, lons = route_arrays(w.route)
        passes = RouteIndex(lats, lons, w.route.dist_m).passes(lat, lon, 3000.0)
        if not passes:
            return jsonify(error="That's more than 3 km from the route."), 400
        p = min(passes, key=lambda p: abs(p.s_m - w.s_m))   # out-and-back: stay on the pass it was on
        w.geom = WKTElement(f"POINT({lon} {lat})", srid=4326)
        w.s_m, w.offset_m, w.side = p.s_m, p.offset_m, p.side
        w.pinned = True                                      # moved by hand: keep it through rescans
    if "club_hours" in body:
        rules = str(body["club_hours"] or "").strip()
        if rules:
            try:
                res = _hours_client().evaluate([{"id": 0, "rules": rules, "lat": 53.0, "lon": -7.0,
                                                 "country_code": "ie", "tz": "Europe/Dublin", "arrivals": []}])[0]
            except hrs.HoursUnavailable:
                return jsonify(error="Couldn't check those hours right now; try again in a moment."), 503
            if res.get("error"):
                return jsonify(error=f"Not valid opening hours: {res['error']}"), 400
        w.club_hours, w.club_hours_at = rules or None, datetime.now(timezone.utc) if rules else None
    if "kind" in body:
        if body["kind"] not in categories.BY_KEY and body["kind"] != "generic":
            return jsonify(error="unknown category"), 400
        w.kind = body["kind"]
    db.session.commit()
    history.record(w.route, _wp_label(w, body))
    return jsonify(wp_json(w))


def _wp_label(w: Waypoint, body: dict) -> str:
    if "lat" in body:
        return f"Move {w.name}"
    if "included" in body:
        return f"{'Add' if w.included else 'Remove'} {w.name}"
    if "planned_stop" in body:
        return f"{'Plan a stop at' if w.planned_stop else 'Drop the stop at'} {w.name}"
    if "pinned" in body:
        return f"{'Pin' if w.pinned else 'Unpin'} {w.name}"
    if "club_hours" in body:
        return f"Set hours for {w.name}"
    if "name" in body:
        return f"Rename to {w.name}"
    return f"Edit {w.name}"


MAX_CUSTOM_WPS = 200
CUSTOM_KINDS = {"cue_left", "cue_right", "cue_straight", "hazard", "water", "cafe", "aed", "summit", "valley", "sprint",
                "toilets", "fuel", "historic", "generic"}


@bp.post("/api/routes/<int:route_id>/waypoints")
def add_waypoint(route_id):
    """A leader's own cue at a map position (right-click). Snapped to the nearest pass of the route, selected for export,
    pinned so rescans leave it alone."""
    route = db.session.get(Route, route_id) or abort(404)
    body = request.get_json(silent=True) or {}
    kind = body.get("kind", "generic")
    if kind not in CUSTOM_KINDS:
        return jsonify(error="That isn't a cue type you can place by hand."), 400
    try:
        lat, lon = float(body["lat"]), float(body["lon"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Click a place on the map first."), 400
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return jsonify(error="That position isn't on Earth."), 400
    name = str(body.get("name") or "").strip()[:200] or categories.label_for(kind)
    note = str(body.get("note") or "").strip()[:2000] or None
    if sum(1 for w in route.waypoints if w.origin == "manual") >= MAX_CUSTOM_WPS:
        return jsonify(error=f"That's {MAX_CUSTOM_WPS} cues already; delete some before adding more."), 400
    lats, lons = route_arrays(route)
    passes = RouteIndex(lats, lons, route.dist_m).passes(lat, lon, 3000.0)
    if not passes:
        return jsonify(error="That's more than 3 km from the route. Right-click closer to the line."), 400
    hint = body.get("s_hint")
    if isinstance(hint, (int, float)):       # out-and-back: the pass the leader was looking at
        p = min(passes, key=lambda q: (abs(q.s_m - hint), q.offset_m))
    else:
        p = min(passes, key=lambda q: q.offset_m)
    history.before(route)
    w = Waypoint(kind=kind, name=name, note=note, geom=WKTElement(f"POINT({lon} {lat})", srid=4326), s_m=p.s_m,
                 offset_m=p.offset_m, side=p.side, origin="manual", included=True, pinned=True, tags={"custom": True})
    route.waypoints.append(w)
    db.session.commit()
    history.record(route, f"Add {name}")
    return jsonify(wp_json(w)), 201


@bp.delete("/api/waypoints/<int:wp_id>")
def delete_waypoint(wp_id):
    w = db.session.get(Waypoint, wp_id) or abort(404)
    route, name = w.route, w.name
    history.before(route)
    db.session.delete(w)
    db.session.commit()
    history.record(route, f"Delete {name}")
    return "", 204


@bp.post("/api/routes/<int:route_id>/waypoints/bulk")
def bulk_waypoints(route_id):
    route = db.session.get(Route, route_id) or abort(404)
    body = request.get_json(silent=True) or {}
    history.before(route)
    open_ids = None
    if body.get("action") == "only_open":
        try:
            stops = _stop_status(route)["stops"]
        except hrs.HoursUnavailable:
            return jsonify(error="Couldn't check opening hours right now; try again in a moment."), 503
        open_ids = {x["id"] for x in stops if x["groups"] and all(g["status"] == hrs.OPEN for g in x["groups"])}
    try:
        changed = apply_bulk(route.waypoints, body.get("action"), body.get("kind"), body.get("value"), open_ids)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    db.session.commit()
    history.record(route, f"Bulk: {body.get('action')} {categories.label_for(body.get('kind'))}")
    return jsonify(changed=changed, waypoints=[wp_json(w) for w in shown(route.waypoints)])


# ---- export ----

DEVICE_COOKIE = "rlk_device"   # becomes a user preference once accounts exist


def _current_device(requested: str | None = None):
    key = requested if requested in exp.DEVICES else request.cookies.get(DEVICE_COOKIE)
    return exp.DEVICES.get(key) or exp.DEVICES[exp.DEFAULT_DEVICE]


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "route"


@bp.get("/routes/<int:route_id>/export")
def export_page(route_id):
    route = db.session.get(Route, route_id) or abort(404)
    device = _current_device(request.args.get("device"))
    data = exp.from_route(route)
    resp = current_app.make_response(render_template(
        "export.html", route=route, devices=exp.DEVICES.values(), device=device, count=len(data.waypoints),
        warnings=exp.warnings_for(data, device), strava_url=exp.devices.STRAVA_NEW_ROUTE_URL))
    resp.set_cookie(DEVICE_COOKIE, device.key, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return resp


@bp.get("/routes/<int:route_id>/export/<device_key>/download")
def export_download(route_id, device_key):
    route = db.session.get(Route, route_id) or abort(404)
    try:
        out = exp.render(exp.from_route(route), device_key, request.args.get("fmt"), _slug(route.title))
    except KeyError as e:
        abort(400, description=str(e))
    return Response(out.data, mimetype=out.fmt.mime,
                    headers={"Content-Disposition": f'attachment; filename="{out.filename}"'})


# ---- ride plan: date, start time, pace groups ----

def _hours_client():
    return current_app.config.get("HOURS_CLIENT") or hrs.HoursClient(current_app.config["HOURS_URL"])


def _ride_json(route: Route) -> dict:
    ride = _get_or_create_ride(route)
    start = pace.start_instant(ride.date, ride.start_time, ride.tz)
    planned = _planned_s(route)
    return {"date": ride.date.isoformat(), "start_time": ride.start_time.strftime("%H:%M"), "tz": ride.tz,
            "planned_stops": len(planned), "meeting_name": ride.meeting_name, "meeting_lat": ride.meeting_lat,
            "meeting_lon": ride.meeting_lon, "leader_phone": ride.leader_phone, "notes": ride.notes, "shared": bool(ride.share_token),
            "groups": [{"name": g.name, "speed_kmh": g.speed_kmh, "stop_min": g.stop_min} for g in ride.groups],
            "summary": [pace.summary(start, route.length_m, pace.Group(g.name, g.speed_kmh, g.stop_min), planned)
                        for g in ride.groups]}


@bp.get("/api/routes/<int:route_id>/ride")
def get_ride(route_id):
    route = db.session.get(Route, route_id) or abort(404)
    return jsonify(_ride_json(route))


@bp.put("/api/routes/<int:route_id>/ride")
def put_ride(route_id):
    route = db.session.get(Route, route_id) or abort(404)
    body = request.get_json(silent=True) or {}
    try:
        day = date.fromisoformat(str(body.get("date")))
        at = time.fromisoformat(str(body.get("start_time")))
    except ValueError:
        return jsonify(error="Pick a valid date and start time."), 400
    groups = body.get("groups")
    if not isinstance(groups, list) or not 1 <= len(groups) <= 6:
        return jsonify(error="Use between 1 and 6 pace groups."), 400
    clean, names = [], set()
    for g in groups:
        try:
            name, speed, stop = str(g["name"]).strip(), float(g["speed_kmh"]), int(g["stop_min"])
        except (KeyError, TypeError, ValueError):
            return jsonify(error="Each group needs a name, speed and stop time."), 400
        if not name or len(name) > 20 or name in names:
            return jsonify(error="Group names must be unique, 1-20 characters."), 400
        if not 8 <= speed <= 60 or not 0 <= stop <= 180:
            return jsonify(error=f"Group {name}: speed 8-60 km/h, stop 0-180 min."), 400
        names.add(name)
        clean.append((name, speed, stop))
    extra = {}
    try:
        if "meeting_name" in body:
            v = str(body["meeting_name"] or "").strip()
            if len(v) > 120:
                raise ValueError("Keep the meeting point name under 120 characters.")
            extra["meeting_name"] = v or None
        if "meeting_lat" in body or "meeting_lon" in body:
            la, lo = body.get("meeting_lat"), body.get("meeting_lon")
            if la in (None, "") and lo in (None, ""):
                extra["meeting_lat"] = extra["meeting_lon"] = None
            else:
                la, lo = float(la), float(lo)
                if not (-90 <= la <= 90 and -180 <= lo <= 180):
                    raise ValueError("That meeting point isn't on Earth.")
                extra["meeting_lat"], extra["meeting_lon"] = la, lo
        if "leader_phone" in body:
            v = str(body["leader_phone"] or "").strip()
            if v and not re.fullmatch(r"[+0-9 ()-]{5,30}", v):
                raise ValueError("Enter the phone number using digits, spaces, + or brackets.")
            extra["leader_phone"] = v or None
        if "notes" in body:
            v = str(body["notes"] or "").strip()
            if len(v) > 1000:
                raise ValueError("Keep the notes under 1000 characters.")
            extra["notes"] = v or None
    except (TypeError, ValueError) as e:
        return jsonify(error=str(e) if isinstance(e, ValueError) and "float" not in str(e) else "Those details weren't valid."), 400
    ride = _get_or_create_ride(route)
    for k, v in extra.items():
        setattr(ride, k, v)
    ride.date, ride.start_time = day, at.replace(second=0, microsecond=0, tzinfo=None)
    ride.groups = [PaceGroup(position=i, name=n, speed_kmh=v, stop_min=m) for i, (n, v, m) in enumerate(clean)]
    db.session.commit()
    return jsonify(_ride_json(route))


def _stop_status(route: Route) -> dict:
    ride = _get_or_create_ride(route)
    start = pace.start_instant(ride.date, ride.start_time, ride.tz)
    groups = [pace.Group(g.name, g.speed_kmh, g.stop_min) for g in ride.groups]
    stops = hrs.evaluate_stops(route.waypoints, start, groups, _planned_s(route), ride.tz, _hours_client())
    return {"date": ride.date.isoformat(), "stops": stops}


@bp.get("/api/routes/<int:route_id>/stops-status")
def stops_status(route_id):
    """Opening state per pace group for each café candidate. If the hours service is down we say so and
    return nothing, rather than guessing."""
    route = db.session.get(Route, route_id) or abort(404)
    try:
        return jsonify(_stop_status(route))
    except hrs.HoursUnavailable:
        return jsonify(error="Couldn't check opening hours right now.", stops=[]), 503


# ---- climbs and regroups ----

def _climbs_payload(route: Route, extra: dict | None = None) -> dict:
    return {"has_elevation": cs.profile_for(route) is not None, "settings": cs.settings_for(route),
            "climbs": [cs.climb_json(c) for c in route.climbs], **(extra or {})}


@bp.get("/api/routes/<int:route_id>/climbs")
def get_climbs(route_id):
    route = db.session.get(Route, route_id) or abort(404)
    extra = {}
    if route.climbs_version != route.version and cs.profile_for(route) is not None:
        extra = cs.detect(route)                              # first look at this route: detect lazily
    return jsonify(_climbs_payload(route, extra))


@bp.post("/api/routes/<int:route_id>/climbs/detect")
def detect_climbs(route_id):
    route = db.session.get(Route, route_id) or abort(404)
    history.before(route)
    try:
        extra = cs.detect(route)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    history.record(route, "Refresh climbs and regroups")
    return jsonify(_climbs_payload(route, extra))


@bp.put("/api/routes/<int:route_id>/climb-settings")
def put_climb_settings(route_id):
    """Saves the thresholds and re-applies them to every climb (this resets per-climb regroup choices)."""
    route = db.session.get(Route, route_id) or abort(404)
    history.before(route)
    try:
        route.climb_settings = {**cs.settings_for(route), **cs.validate_settings(request.get_json(silent=True) or {})}
    except (ValueError, TypeError) as e:
        return jsonify(error=str(e) if isinstance(e, ValueError) else "Settings must be numbers."), 400
    db.session.commit()
    try:
        extra = cs.detect(route)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    history.record(route, "Regroup settings")
    return jsonify(_climbs_payload(route, extra))


@bp.patch("/api/climbs/<int:climb_id>")
def patch_climb(climb_id):
    climb = db.session.get(Climb, climb_id) or abort(404)
    route = climb.route
    body = request.get_json(silent=True) or {}
    history.before(route)
    if "regroup_mode" in body:
        if body["regroup_mode"] not in cs.MODES:
            return jsonify(error="Regroup mode must be off, ease_back or stop."), 400
        climb.regroup_mode = body["regroup_mode"]
    if "ease_km" in body:
        try:
            km = float(body["ease_km"])
        except (TypeError, ValueError):
            return jsonify(error="Ease-back distance must be a number."), 400
        if not 0.5 <= km <= 10:
            return jsonify(error="Ease-back distance must be between 0.5 and 10 km."), 400
        climb.ease_km = km
    profile = cs.profile_for(route)
    warning = cs.apply_climb_regroup(route, climb, profile) if profile is not None else None
    db.session.commit()
    history.record(route, f"Regroup after climb: {climb.regroup_mode.replace('_', ' ')}")
    return jsonify(_climbs_payload(route, {"warning": warning}))
