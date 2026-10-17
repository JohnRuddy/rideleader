"""Sharing a ride: a link anyone can open (no sign-in), the WhatsApp text, the printable cue card, and a leader's preview.

The link is the capability: a long random token, not guessable, revocable by the leader. A route that came from Strava
can never be shared, because Strava data may only ever be shown to the athlete it belongs to: they have to publish
their own copy to the club first (see strava_views)."""
import secrets

from flask import Blueprint, Response, abort, current_app, g, jsonify, render_template, request, url_for

from . import cue_card, db
from . import ride_sheet as rs
from .models import Ride, Route
from .plan import Plan, get_or_create_ride
from .security import login_required

bp = Blueprint("share", __name__)

SW_JS = """// Network first, so warnings and weather are always fresh when there is signal; the last copy when there isn't.
const CACHE = 'rlk-ride-sheet-v1';
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (e.request.method !== 'GET' || !u.pathname.startsWith('/r/') || u.pathname.endsWith('/sw.js')) return;
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(e.request, copy)); }
    return r;
  }).catch(() => caches.match(e.request).then(m => m || new Response('You are offline and this page was not saved yet.', { status: 503 }))));
});
"""


def _share_url(token: str) -> str:
    base = (current_app.config["APP_BASE_URL"] or request.url_root).rstrip("/")
    return base + url_for("share.public_sheet", token=token)


def _public_ride(token: str) -> Ride:
    ride = Ride.query.filter_by(share_token=token).first() if token and len(token) >= 16 else None
    if ride is None or ride.route.source == "strava":
        abort(404)
    return ride


def _safe(resp: Response) -> Response:
    resp.headers["Cache-Control"] = "no-cache"                     # warnings and weather must be fresh when there is signal
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"             # a private link, not something to index
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


# ---------------- turning sharing on and off ----------------

@bp.post("/api/routes/<int:route_id>/share")
@login_required
def share(route_id):
    route = db.session.get(Route, route_id) or abort(404)
    if route.source == "strava":
        return jsonify(error="This route came from your Strava, so it can't be shared as it is. Share a copy with your club first "
                             "(Share a copy with your club on the route page), then share that."), 409
    ride = get_or_create_ride(route)
    if not ride.share_token:
        ride.share_token = secrets.token_urlsafe(16)
        db.session.commit()
    return jsonify({"token": ride.share_token, "url": _share_url(ride.share_token)})


@bp.delete("/api/routes/<int:route_id>/share")
@login_required
def unshare(route_id):
    route = db.session.get(Route, route_id) or abort(404)
    ride = get_or_create_ride(route)
    ride.share_token = None
    db.session.commit()
    return "", 204


@bp.get("/api/routes/<int:route_id>/share")
def share_status(route_id):
    route = db.session.get(Route, route_id) or abort(404)
    t = get_or_create_ride(route).share_token
    return jsonify({"shared": bool(t), "url": _share_url(t) if t else None, "can_share": route.source != "strava"})


# ---------------- the public page (no sign-in) ----------------

def _page(route: Route, public: bool, token: str | None = None) -> Response:
    s = rs.build(route, Plan(route), public=public)
    url = _share_url(token) if token else None
    base = f"/r/{token}" if token else f"/routes/{route.id}"
    return _safe(Response(render_template("ride_sheet.html", s=s, public=public, share_url=url, wa_text=rs.summary_text(s, url),
                                          base=base, route=route)))


@bp.get("/r/<token>")
def public_sheet(token):
    return _page(_public_ride(token).route, True, token)


@bp.get("/r/<token>/summary.txt")
def public_summary(token):
    route = _public_ride(token).route
    s = rs.build(route, Plan(route), public=True)
    return _safe(Response(rs.summary_text(s, _share_url(token)), mimetype="text/plain; charset=utf-8"))


@bp.get("/r/<token>/cue-card.pdf")
def public_card(token):
    route = _public_ride(token).route
    return _pdf(route, public=True)


@bp.get("/r/sw.js")
def service_worker():
    return Response(SW_JS, mimetype="text/javascript", headers={"Cache-Control": "no-cache"})


# ---------------- the leader's own copy (signed in, route access rules apply) ----------------

@bp.get("/routes/<int:route_id>/sheet")
def sheet(route_id):
    route = db.session.get(Route, route_id) or abort(404)
    return _page(route, False, get_or_create_ride(route).share_token)


@bp.get("/routes/<int:route_id>/summary.txt")
def summary(route_id):
    route = db.session.get(Route, route_id) or abort(404)
    s = rs.build(route, Plan(route))
    t = get_or_create_ride(route).share_token
    return _safe(Response(rs.summary_text(s, _share_url(t) if t else None), mimetype="text/plain; charset=utf-8"))


@bp.get("/routes/<int:route_id>/cue-card.pdf")
def card(route_id):
    route = db.session.get(Route, route_id) or abort(404)
    return _pdf(route, public=False)


def _pdf(route: Route, public: bool) -> Response:
    s = rs.build(route, Plan(route), public=public)
    slug = "".join(c if c.isalnum() else "-" for c in route.title.lower()).strip("-")[:50] or "ride"
    return _safe(Response(cue_card.build_pdf(s), mimetype="application/pdf",
                          headers={"Content-Disposition": f'inline; filename="{slug}-cue-card.pdf"'}))
