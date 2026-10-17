"""Per-request security: who is signed in, CSRF protection, and who may see or change which route.

Access rules for a route:
- Imported from Strava: only the athlete who imported it, for reading and writing. Strava data may be shown only
  to the person it belongs to, so there is no sharing path for these routes (publishing makes a separate copy).
- Otherwise: visible if it has no owner (legacy or signed-out upload), is shared (visibility 'club'), or is yours.
- Changing a route that has an owner needs that owner. Routes with no owner stay editable by anyone, as before.
"""
import hmac
import secrets

from flask import abort, current_app, g, jsonify, redirect, request, session, url_for
from markupsafe import Markup
from sqlalchemy import or_

from . import db
from .models import Climb, Route, ScanJob, User, Waypoint

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_EXEMPT = {"strava.webhook"}      # called by Strava servers, with no browser session to protect


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def csrf_input() -> Markup:
    return Markup(f'<input type="hidden" name="csrf_token" value="{csrf_token()}">')


def visible_routes_filter():
    """SQL filter for the routes the current user may see in lists (Strava imports only for their owner)."""
    uid = g.user.id if g.get("user") else None
    own = Route.owner_id == uid if uid else False
    return or_(
        (Route.source != "strava") & (or_(Route.owner_id.is_(None), Route.visibility == "club")),
        own,
    )


def can_see(route: Route, user) -> bool:
    if route.source == "strava":
        return bool(user and route.owner_id == user.id)
    return route.owner_id is None or route.visibility == "club" or bool(user and route.owner_id == user.id)


def can_change(route: Route, user) -> bool:
    return route.owner_id is None and route.source != "strava" or bool(user and route.owner_id == user.id)


def _route_for_request() -> Route | None:
    va = request.view_args or {}
    if "route_id" in va:
        return db.session.get(Route, va["route_id"])
    if "wp_id" in va:
        w = db.session.get(Waypoint, va["wp_id"])
        return w.route if w else None
    if "climb_id" in va:
        c = db.session.get(Climb, va["climb_id"])
        return c.route if c else None
    if "job_id" in va:
        j = db.session.get(ScanJob, va["job_id"])
        return db.session.get(Route, j.route_id) if j else None
    return None


def login_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*a, **kw):
        if not g.get("user"):
            if request.path.startswith("/api/"):
                return jsonify(error="Sign in first."), 401
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return fn(*a, **kw)
    return wrapper


def init_app(app) -> None:
    @app.before_request
    def load_user():
        uid = session.get("uid")
        g.user = db.session.get(User, uid) if uid else None

    @app.before_request
    def csrf_protect():
        if not current_app.config.get("CSRF_ENABLED", True) or request.method in SAFE_METHODS:
            return None
        if request.endpoint in CSRF_EXEMPT:
            return None
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
        if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
            if request.path.startswith("/api/"):
                return jsonify(error="Your session expired. Reload the page and try again."), 403
            abort(403, description="Your session expired. Go back, reload the page and try again.")

    @app.before_request
    def route_access():
        route = _route_for_request()
        if route is None:
            return None
        if not can_see(route, g.user):
            abort(404)                                    # don't reveal that it exists
        if request.method not in SAFE_METHODS and not can_change(route, g.user):
            abort(403, description="Only the person who owns this route can change it.")

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["csrf_input"] = csrf_input

    @app.context_processor
    def inject_globals():
        from .strava import get_client
        return {"current_user": g.get("user"), "strava_enabled": get_client(current_app.config) is not None,
                "strava_mode": current_app.config.get("STRAVA_MODE")}
