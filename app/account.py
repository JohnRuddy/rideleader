"""Account page, privacy policy, personal data export and account deletion (GDPR: access, portability, erasure)."""
import json
from datetime import datetime, timezone

from flask import Blueprint, Response, abort, current_app, g, redirect, render_template, request, session, url_for
from geoalchemy2.shape import to_shape

from . import db
from . import strava_service as svc
from .mailer import get_mailer
from .models import ClubStop, Closure, ConsentLog, LoginToken, Route, StravaRouteCache, User
from .security import login_required
from .strava import get_client

bp = Blueprint("account", __name__)


@bp.get("/privacy")
def privacy():
    return render_template("privacy.html", controller=current_app.config["CONTROLLER_NAME"],
                           contact=current_app.config["CONTROLLER_CONTACT"])


@bp.get("/account")
@login_required
def account():
    conn = svc.connection_for(g.user)
    return render_template("account.html", conn=conn, strava_routes=StravaRouteCache.query.filter_by(user_id=g.user.id).count(),
                           my_routes=Route.query.filter_by(owner_id=g.user.id).count())


def _export(user: User) -> dict:
    conn = svc.connection_for(user)
    routes = []
    for r in Route.query.filter_by(owner_id=user.id).order_by(Route.id).all():
        routes.append({
            "id": r.id, "title": r.title, "source": r.source, "visibility": r.visibility,
            "created_at": r.created_at.isoformat() if r.created_at else None, "length_m": r.length_m,
            "ascent_m": r.ascent_m, "descent_m": r.descent_m,
            "strava_data_expires_at": r.strava_expires_at.isoformat() if r.strava_expires_at else None,
            "ride": None if r.ride is None else {
                "date": r.ride.date.isoformat(), "start_time": r.ride.start_time.strftime("%H:%M"), "meeting_point": r.ride.meeting_name,
                "leader_phone": r.ride.leader_phone, "notes": r.ride.notes, "share_link_active": bool(r.ride.share_token),
                "pace_groups": [{"name": g.name, "speed_kmh": g.speed_kmh, "stop_minutes": g.stop_min} for g in r.ride.groups]},
            "coordinates_lon_lat_ele": [[x, y, e] for (x, y), e in zip(to_shape(r.geom).coords, r.ele_m or [None] * len(r.dist_m))],
            "waypoints": [{"name": w.name, "kind": w.kind, "note": w.note, "distance_along_route_m": w.s_m,
                           "lon": to_shape(w.geom).x, "lat": to_shape(w.geom).y, "included_in_export": w.included}
                          for w in r.waypoints]})
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "account": {"email": user.email, "created_at": user.created_at.isoformat() if user.created_at else None,
                    "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None},
        "routes": routes,
        "strava": None if conn is None else {
            "athlete_id": conn.athlete_id, "scope": conn.scope, "connected_at": conn.connected_at.isoformat(),
            "note": "Access tokens are stored encrypted and are not included.",
            "cached_route_listings": [{"strava_route_id": c.strava_route_id, "name": c.name, "distance_m": c.distance_m,
                                       "private": c.private, "fetched_at": c.fetched_at.isoformat(),
                                       "deleted_automatically_at": c.expires_at.isoformat()}
                                      for c in StravaRouteCache.query.filter_by(user_id=user.id).all()]},
        "club_stops_you_added": [{"name": c.name, "kind": c.kind, "note": c.note, "hours": c.hours,
                                  "lat": to_shape(c.geom).y, "lon": to_shape(c.geom).x,
                                  "verified_at": c.last_verified_at.isoformat() if c.last_verified_at else None}
                                 for c in ClubStop.query.filter_by(created_by=user.id).all()],
        "closures_you_entered": [{"description": c.description, "starts_at": c.starts_at.isoformat(), "ends_at": c.ends_at.isoformat(),
                                  "lat": to_shape(c.geom).y, "lon": to_shape(c.geom).x}
                                 for c in Closure.query.filter_by(entered_by=user.id).all()],
        "consents": [{"kind": c.kind, "version": c.version, "at": c.at.isoformat(), "text_sha256": c.text_hash}
                     for c in ConsentLog.query.filter_by(user_id=user.id).order_by(ConsentLog.at).all()],
    }


@bp.get("/account/export.json")
@login_required
def export_data():
    body = json.dumps(_export(g.user), indent=2, default=str)
    return Response(body, mimetype="application/json",
                    headers={"Content-Disposition": 'attachment; filename="ride-leader-kit-my-data.json"'})


@bp.post("/account/delete")
@login_required
def delete_account():
    """Right to be forgotten: remove the account and everything attached to it, then confirm by email."""
    user = g.user
    if (request.form.get("confirm_email") or "").strip().lower() != user.email:
        return render_template("account.html", conn=svc.connection_for(user), my_routes=Route.query.filter_by(owner_id=user.id).count(),
                               strava_routes=StravaRouteCache.query.filter_by(user_id=user.id).count(),
                               error="Type your email address exactly to confirm."), 400
    email = user.email
    n_routes = Route.query.filter_by(owner_id=user.id).count()   # count first: Strava copies go in disconnect()
    svc.disconnect(user, get_client(current_app.config), "you deleted your account", notify=False)
    for r in Route.query.filter_by(owner_id=user.id).all():
        db.session.delete(r)
    LoginToken.query.filter_by(email=email).delete()
    ConsentLog.query.filter_by(user_id=user.id).delete()
    db.session.delete(user)
    db.session.commit()
    session.clear()
    get_mailer().send(email, "Your Ride Leader Kit account has been deleted",
                      f"Your account and everything attached to it has been permanently deleted: {n_routes} route(s), "
                      "your waypoints, any Strava connection and data, and your sign-in history. This includes routes "
                      "you had shared with your club.\n\nIf you didn't ask for this, contact the club straight away.")
    return render_template("strava_result.html", title="Your account has been deleted",
                           message=f"Everything has been deleted and we've emailed a confirmation to {email}.")
