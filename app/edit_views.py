"""Route editor API: via points (add / move / delete), routing options, and undo / redo."""
from flask import Blueprint, abort, jsonify, request

from . import db, edit_service as es
from . import editor, history
from .corridor import RouteIndex
from .models import Route
from .routemodel import route_arrays
from .routing import RouteOptions, RoutingError

bp = Blueprint("edit", __name__)


def _route(route_id) -> Route:
    return db.session.get(Route, route_id) or abort(404)


def _summary(route: Route, extra: dict | None = None) -> dict:
    return {"length_m": route.length_m, "ascent_m": route.ascent_m, "descent_m": route.descent_m,
            "version": route.version, **es.editor_json(route), **(extra or {})}


def _latlon(body) -> tuple[float, float]:
    try:
        lat, lon = float(body["lat"]), float(body["lon"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("That needs a latitude and longitude.")
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("That position isn't on Earth.")
    return lat, lon


@bp.get("/api/routes/<int:route_id>/editor")
def get_editor(route_id):
    return jsonify(_summary(_route(route_id)))


@bp.post("/api/routes/<int:route_id>/edit")
def edit(route_id):
    """body: {op: add_via, lat, lon, s_m?} | {op: move_via, anchor_id, lat, lon} | {op: delete_via, anchor_id}"""
    route = _route(route_id)
    body = request.get_json(silent=True) or {}
    op = body.get("op")
    try:
        track = es.get_track(route)
        if op == "add_via":
            lat, lon = _latlon(body)
            if body.get("s_m") is None:                    # a click: attach to the nearest point of the line
                lats, lons = route_arrays(route)
                near = RouteIndex(lats, lons, route.dist_m).passes(lat, lon, 1e9)
                s_m = min(near, key=lambda p: p.offset_m).s_m
            else:
                s_m = float(body["s_m"])
            history.before(route)
            new, region = editor.add_via(track, s_m, lat, lon, es.routing_client())
            label = "Add via point"
        elif op == "move_via":
            lat, lon = _latlon(body)
            history.before(route)
            new, region = editor.move_via(track, int(body["anchor_id"]), lat, lon, es.routing_client())
            label = "Move via point"
        elif op == "delete_via":
            history.before(route)
            new, region = editor.delete_via(track, int(body["anchor_id"]), es.routing_client())
            label = "Remove via point"
        else:
            return jsonify(error="Unknown edit."), 400
    except RoutingError as e:
        db.session.rollback()
        return jsonify(error=str(e)), 422
    except (ValueError, KeyError, TypeError) as e:
        db.session.rollback()
        return jsonify(error=str(e) if isinstance(e, ValueError) else "That edit was missing something."), 400
    result = es.apply_edit(route, new, region)
    history.record(route, label)
    return jsonify(_summary(route, {"scan_job_id": result.get("scan_job_id"), "warnings": result.get("warnings", [])}))


@bp.put("/api/routes/<int:route_id>/editor/options")
def put_options(route_id):
    """Routing preferences for edits from now on (existing legs are not rerouted)."""
    route = _route(route_id)
    body = request.get_json(silent=True) or {}
    cur = es.get_track(route)
    opts = {**cur.options.to_json(), **{k: body[k] for k in ("avoid_busy", "allow_unpaved") if k in body}}
    if not all(isinstance(v, bool) for v in opts.values()):
        return jsonify(error="Options must be true or false."), 400
    cur.options = RouteOptions.from_json(opts)
    route.editor = cur.to_json()
    db.session.commit()
    return jsonify(_summary(route))


@bp.get("/api/routes/<int:route_id>/history")
def get_history(route_id):
    return jsonify(history.info(_route(route_id)))


def _history_step(route_id, fn):
    route = _route(route_id)
    try:
        fn(route)
    except ValueError as e:
        return jsonify(error=str(e)), 409
    return jsonify(_summary(route))


@bp.post("/api/routes/<int:route_id>/history/undo")
def undo(route_id):
    return _history_step(route_id, history.undo)


@bp.post("/api/routes/<int:route_id>/history/redo")
def redo(route_id):
    return _history_step(route_id, history.redo)


@bp.post("/api/routes/<int:route_id>/history/goto")
def goto(route_id):
    body = request.get_json(silent=True) or {}
    try:
        seq = int(body["seq"])
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Say which step to go to."), 400
    return _history_step(route_id, lambda r: history.goto(r, seq))
