"""Strava pages and endpoints: consent + connect, OAuth callback, route picker, import, disconnect, webhook,
and sharing an imported route with the club."""
import secrets
from pathlib import Path

from flask import Blueprint, abort, current_app, g, jsonify, redirect, render_template, request, session, url_for

from . import db, ratelimit
from . import strava_service as svc
from .models import Route, StravaConnection
from .security import login_required
from .strava import StravaError, StravaNotFound, get_client

bp = Blueprint("strava", __name__)

STATIC = Path(__file__).parent / "static" / "strava"


def _client():
    return get_client(current_app.config)


def _need_client():
    client = _client()
    if client is None:
        abort(404)
    return client


def _redirect_uri() -> str:
    base = (current_app.config["APP_BASE_URL"] or request.url_root).rstrip("/")
    return base + url_for("strava.callback")


def brand_assets() -> dict:
    """Strava's brand rules require its official 'Connect with Strava' button and 'Powered by Strava' mark.
    The unmodified official SVGs live in app/static/strava/; if one is ever missing, plain text stands in."""
    return {"connect_img": (STATIC / "connect_with_strava_orange.svg").exists(), "powered_img": (STATIC / "powered_by_strava_black.svg").exists()}


@bp.app_context_processor
def _brand():
    return {"strava_brand": brand_assets()}


@bp.get("/strava")
@login_required
def home():
    return redirect(url_for("strava.routes" if svc.connection_for(g.user) else "strava.connect"))


@bp.get("/strava/connect")
@login_required
def connect():
    _need_client()
    if svc.connection_for(g.user):
        return redirect(url_for("strava.routes"))
    return render_template("strava_consent.html", consent_text=svc.CONNECT_CONSENT_TEXT)


@bp.post("/strava/connect")
@login_required
def connect_post():
    client = _need_client()
    if not ratelimit.allow("strava-connect", str(g.user.id), 20, 3600):
        return render_template("strava_consent.html", consent_text=svc.CONNECT_CONSENT_TEXT,
                               error="Too many attempts. Try again later."), 429
    if request.form.get("consent") != "yes":
        return render_template("strava_consent.html", consent_text=svc.CONNECT_CONSENT_TEXT,
                               error="Tick the box to say you agree, then connect."), 400
    svc.record_consent(g.user, "strava_connect", svc.CONNECT_CONSENT_VERSION, svc.CONNECT_CONSENT_TEXT)
    db.session.commit()
    state = secrets.token_urlsafe(24)
    session["strava_state"] = state
    return redirect(client.authorize_url(state, _redirect_uri()))


@bp.get("/strava/callback")
@login_required
def callback():
    client = _need_client()
    expected = session.pop("strava_state", None)
    if request.args.get("error"):
        return render_template("strava_result.html", title="Strava wasn't connected",
                               message="You chose not to give access, so nothing was connected and nothing was stored."), 200
    if not expected or request.args.get("state") != expected:
        abort(400, description="That Strava sign-in didn't start here, so it was ignored. Please start again.")
    # the person must have ticked the consent box in this app before we got here
    from .models import ConsentLog
    if not ConsentLog.query.filter_by(user_id=g.user.id, kind="strava_connect", version=svc.CONNECT_CONSENT_VERSION).first():
        abort(400, description="Please agree on the consent screen first.")
    try:
        tokens = client.exchange_code(request.args.get("code", ""))
        conn = svc.complete_connection(g.user, tokens)
    except StravaError as e:
        return render_template("strava_result.html", title="Couldn't connect to Strava", message=str(e)), 400
    note = "" if svc.has_read_all(conn) else "private-routes-missing"
    return redirect(url_for("strava.routes", note=note) if note else url_for("strava.routes"))


@bp.get("/strava/routes")
@login_required
def routes():
    client = _need_client()
    conn = svc.connection_for(g.user)
    if conn is None:
        return redirect(url_for("strava.connect"))
    error = None
    try:
        rows = svc.list_routes(g.user, client, force=request.args.get("refresh") == "1")
    except svc.StravaDisconnected as e:
        return render_template("strava_result.html", title="Strava is disconnected", message=str(e)), 200
    except StravaError as e:
        from .models import StravaRouteCache
        rows, error = StravaRouteCache.query.filter_by(user_id=g.user.id).all(), str(e)
    imported = {r.strava_route_id: r.id for r in Route.query.filter_by(owner_id=g.user.id, source="strava").all()}
    rows = sorted(rows, key=lambda r: (r.tidy_name or r.name).lower())      # '063 km · Naas' sorts by distance
    return render_template("strava_routes.html", rows=rows, imported=imported, error=error, conn=conn,
                           tidied=request.args.get("tidied", type=int), left=request.args.get("left", type=int),
                           untidy=sum(1 for r in rows if not r.tidy_name),
                           missing_private=request.args.get("note") == "private-routes-missing" or not svc.has_read_all(conn))


@bp.post("/strava/routes/tidy")
@login_required
def tidy():
    _need_client()
    from .overpass import get_client as overpass_client
    ov = overpass_client(current_app.config)
    out = svc.tidy_names(g.user, ov, workers=getattr(ov, "workers", 1))
    return redirect(url_for("strava.routes", tidied=out["named"], left=out["left"]))


@bp.post("/strava/routes/<int:strava_route_id>/import")
@login_required
def import_(strava_route_id):
    client = _need_client()
    try:
        route = svc.import_route(g.user, client, strava_route_id)
    except StravaNotFound:
        return render_template("strava_result.html", title="That route is gone",
                               message="It's no longer on Strava, so it was removed here too."), 404
    except svc.StravaDisconnected as e:
        return render_template("strava_result.html", title="Strava is disconnected", message=str(e)), 200
    except StravaError as e:
        return render_template("strava_result.html", title="Couldn't import that route", message=str(e)), 502
    if request.form.get("scan") == "1":      # the "find stops" tick box: the route page starts the search itself
        return redirect(url_for("main.show", route_id=route.id, autoscan=1))
    return redirect(url_for("main.show", route_id=route.id))


@bp.post("/strava/disconnect")
@login_required
def disconnect():
    if request.form.get("confirm") != "yes":
        abort(400, description="Please confirm.")
    result = svc.disconnect(g.user, _client(), "you disconnected Strava")
    return render_template("strava_result.html", title="Strava disconnected and your data deleted",
                           message=(f"We told Strava to revoke our access and deleted {result['cache_rows']} route listing(s) "
                                    f"and {result['routes']} imported route(s). We've emailed you a confirmation to {g.user.email}. "
                                    "Routes you shared with your club are yours and were not deleted."))


# ---- webhook ----

@bp.route("/strava/webhook", methods=["GET", "POST"])
def webhook():
    """GET answers Strava's subscription handshake. POST receives events: we act only on an athlete revoking access."""
    cfg = current_app.config
    if request.method == "GET":
        ok = (request.args.get("hub.mode") == "subscribe" and cfg["STRAVA_VERIFY_TOKEN"]
              and secrets.compare_digest(request.args.get("hub.verify_token", ""), cfg["STRAVA_VERIFY_TOKEN"]))
        if not ok:
            abort(403)
        return jsonify({"hub.challenge": request.args.get("hub.challenge", "")})
    if not ratelimit.allow("strava-webhook", request.remote_addr or "?", 600, 60):
        return jsonify({}), 429
    event = request.get_json(silent=True)
    if not isinstance(event, dict):
        return jsonify(error="bad event"), 400
    sub = cfg["STRAVA_SUBSCRIPTION_ID"]
    if sub and str(event.get("subscription_id")) != str(sub):
        return jsonify({}), 200                        # not ours
    updates = event.get("updates") or {}
    if (event.get("object_type") == "athlete" and event.get("aspect_type") == "update"
            and str(updates.get("authorized", "")).lower() == "false"):
        try:
            athlete_id = int(event.get("owner_id") or event.get("object_id"))
        except (TypeError, ValueError):
            return jsonify({}), 200
        from .jobs import enqueue_strava_deauth
        enqueue_strava_deauth(athlete_id)              # Strava wants a 200 within 2 seconds: do the deleting off-request
    return jsonify({}), 200


# ---- share an imported route with the club ----

def _own_strava_route(route_id) -> Route:
    route = db.session.get(Route, route_id) or abort(404)
    if route.source != "strava" or route.owner_id != g.user.id:
        abort(404)
    return route


@bp.get("/routes/<int:route_id>/publish")
@login_required
def publish(route_id):
    route = _own_strava_route(route_id)
    return render_template("publish.html", route=route, consent_text=svc.PUBLISH_CONSENT_TEXT)


@bp.post("/routes/<int:route_id>/publish")
@login_required
def publish_post(route_id):
    route = _own_strava_route(route_id)
    try:
        copy = svc.publish_to_club(g.user, route, request.form.get("confirm_own") == "yes")
    except ValueError as e:
        return render_template("publish.html", route=route, consent_text=svc.PUBLISH_CONSENT_TEXT, error=str(e)), 400
    return redirect(url_for("main.show", route_id=copy.id))
