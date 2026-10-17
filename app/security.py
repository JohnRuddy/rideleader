"""Per-request security: who is signed in, CSRF protection, and who may see or change which route.

Access rules for a route:
- Imported from Strava: only the athlete who imported it, for reading and writing. Sharing one makes a separate,
  processed copy for the club (see strava_views); the import itself is never shown to anyone else.
- Otherwise: visible if it has no owner (legacy or signed-out upload), is yours, or is shared (visibility 'club')
  with a club you belong to. The platform admin belongs to every club.
- Changing a route that has an owner needs that owner. Routes with no owner stay editable by anyone, as before.

Who is signed in also decides the current club (g.club): the one chosen in the club switcher, else the first.
With INVITE_REQUIRED on, a person who belongs to no club is treated as signed out, so removing someone from
their club takes effect on their next request.
"""
import hmac
import secrets

from flask import abort, current_app, g, jsonify, redirect, request, session, url_for
from markupsafe import Markup
from sqlalchemy import or_

from . import db
from . import clubs
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
    """SQL filter for the routes the current user may see in lists: their own, and the current club's shared ones
    (Strava imports only for their owner). Routes saved before clubs existed (no club) stay shared with everyone, as
    they were; every route made since belongs to a club."""
    uid = g.user.id if g.get("user") else None
    own = Route.owner_id == uid if uid else False
    club = g.get("club")
    shared = Route.visibility == "club"
    shared = shared & (or_(Route.club_id == club.id, Route.club_id.is_(None)) if (uid and club) else Route.club_id.is_(None))
    return or_(
        (Route.source != "strava") & (Route.owner_id.is_(None) | shared),
        own,
    )


def can_see(route: Route, user) -> bool:
    if route.source == "strava":
        return bool(user and route.owner_id == user.id)
    if route.owner_id is None or (user and route.owner_id == user.id):
        return True
    if route.visibility != "club":
        return False
    if route.club_id is None:
        return True                                       # saved before clubs existed: shared with everyone, as it was
    return bool(user) and (clubs.is_platform_admin(user) or clubs.membership(user, route.club_id) is not None)


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
        if g.user is not None and current_app.config.get("INVITE_REQUIRED", True) and not clubs.has_any_club(g.user):
            session.clear()                               # removed from every club (or never joined one): signed out
            g.user = None
        g.clubs = clubs.clubs_for(g.user)
        chosen = session.get("club_id")
        g.club = next((c for c in g.clubs if c.id == chosen), g.clubs[0] if g.clubs else None)
        g.club_admin = bool(g.user) and clubs.is_club_admin(g.user, g.club.id if g.club else None)

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
        return {"current_user": g.get("user"), "current_club": g.get("club"), "my_clubs": g.get("clubs") or [],
                "is_club_admin": g.get("club_admin", False), "is_platform_admin": clubs.is_platform_admin(g.get("user")),
                "strava_enabled": get_client(current_app.config) is not None,
                "strava_mode": current_app.config.get("STRAVA_MODE")}
