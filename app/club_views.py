"""The club stop library: a page and a small API. Signed-in members can add and verify stops; removing one needs the
person who added it or a club admin."""
from datetime import datetime, timezone

from flask import Blueprint, abort, g, jsonify, render_template, request

from . import categories, club_stops, db
from .models import ClubStop, Waypoint
from .security import login_required

bp = Blueprint("club", __name__)


@bp.get("/club/stops")
@login_required
def page():
    stops = ClubStop.query.filter_by(active=True).order_by(ClubStop.kind, ClubStop.name).all()
    return render_template("club_stops.html", stops=[club_stops.stop_json(s) for s in stops], categories=categories.public_json(),
                           admin=g.user.role == "admin")


@bp.get("/api/club/stops")
@login_required
def list_stops():
    return jsonify([club_stops.stop_json(s) for s in ClubStop.query.filter_by(active=True).order_by(ClubStop.kind, ClubStop.name).all()])


@bp.post("/api/club/stops")
@login_required
def add():
    try:
        fields = club_stops.validate(request.get_json(silent=True) or {})
    except ValueError as e:
        return jsonify(error=str(e)), 400
    s = ClubStop(**fields, active=True, created_by=g.user.id)
    if (request.get_json(silent=True) or {}).get("verified"):
        s.verified_by, s.last_verified_at = g.user.id, datetime.now(timezone.utc)
    db.session.add(s)
    db.session.commit()
    return jsonify(club_stops.stop_json(s)), 201


def _get(stop_id) -> ClubStop:
    s = db.session.get(ClubStop, stop_id)
    if s is None or not s.active:
        abort(404)
    return s


@bp.patch("/api/club/stops/<int:stop_id>")
@login_required
def update(stop_id):
    s = _get(stop_id)
    try:
        fields = club_stops.validate(request.get_json(silent=True) or {}, partial=True)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    for k, v in fields.items():
        setattr(s, k, v)
    db.session.commit()
    return jsonify(club_stops.stop_json(s))


@bp.post("/api/club/stops/<int:stop_id>/verify")
@login_required
def verify(stop_id):
    """'I've checked this is still right.' Sets the last-verified date to now."""
    s = _get(stop_id)
    s.verified_by, s.last_verified_at = g.user.id, datetime.now(timezone.utc)
    db.session.commit()
    return jsonify(club_stops.stop_json(s))


@bp.delete("/api/club/stops/<int:stop_id>")
@login_required
def remove(stop_id):
    s = _get(stop_id)
    if s.created_by != g.user.id and g.user.role != "admin":
        abort(403, description="Only the person who added a stop, or a club admin, can remove it.")
    db.session.delete(s)
    db.session.commit()
    return "", 204


@bp.post("/api/waypoints/<int:wp_id>/save-to-club")
@login_required
def save_waypoint(wp_id):
    """Put a stop you've found (and perhaps corrected the hours of) into the club library, verified now."""
    w = db.session.get(Waypoint, wp_id) or abort(404)
    if w.origin == "computed":
        return jsonify(error="That one is worked out from the route, so there is nothing to save."), 400
    if w.origin == "club" and w.club_stop_id:
        return jsonify(error="That stop is already in the club library."), 409
    stop = club_stops.save_from_waypoint(w, g.user.id)
    w.origin = "club"
    w.osm_ref = f"club/{stop.id}"
    w.tags = {**(w.tags or {}), "club": True, "club_stop_id": stop.id, "verified_at": stop.last_verified_at.isoformat()}
    db.session.commit()
    return jsonify(club_stops.stop_json(stop)), 201
