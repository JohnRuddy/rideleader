"""Everything the app does with Strava data, in one place so the rules are easy to audit.

Rules this module enforces (from Strava's API terms as briefed; see README for what is still to verify):
- Read only. Strava data is cached for at most CACHE_TTL and is deleted when it expires, when the athlete
  disconnects or revokes access, when they delete their account, and when it disappears from Strava.
- Strava data is only ever returned to the user it belongs to (every query here is scoped by user_id).
- No analytics or aggregation across athletes; no machine learning of any kind touches this data.
- Publishing makes a separate copy the leader owns, only after they confirm it is their own route.
"""
import hashlib
import logging
from datetime import datetime, timedelta, timezone

from geoalchemy2.elements import WKTElement
from geoalchemy2.shape import to_shape

from . import crypto, db
from .mailer import get_mailer
from .models import ConsentLog, PaceGroup, Ride, Route, StravaConnection, StravaRouteCache, User, Waypoint
from .route_factory import create_route
from .routemodel import parse_gpx
from .strava import StravaClient, StravaError, StravaNotFound, StravaUnauthorized, TokenSet

log = logging.getLogger("rlk.strava")

CACHE_TTL = timedelta(days=7)
LIST_FRESH_FOR = timedelta(hours=1)        # how long a fetched route list is reused before asking Strava again
REFRESH_MARGIN = timedelta(minutes=5)
REVALIDATE_AFTER = timedelta(hours=20)     # with a daily run this reflects Strava-side deletions well inside 48 hours
REVALIDATE_MAX_CALLS = 60                  # per run; Strava's default limit is 100 requests per 15 minutes

CONNECT_CONSENT_VERSION = "2026-10"
CONNECT_CONSENT_TEXT = (
    "I allow Ride Leader Kit to read my Strava routes (including private ones) so I can plan a ride from them. "
    "I understand the app keeps what it reads for at most 7 days and shows it only to me, that it cannot "
    "create or change anything on Strava, and that I can disconnect or ask for everything to be deleted at any time.")
PUBLISH_CONSENT_VERSION = "2026-10"
PUBLISH_CONSENT_TEXT = (
    "This is my own route. I am choosing to share it with my club. The shared copy is stored as a route file "
    "that I own, separate from Strava, and stays until I delete it.")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _now(now):
    return now or datetime.now(timezone.utc)


class StravaDisconnected(RuntimeError):
    """The athlete's access is gone (revoked or rejected); their Strava data has been deleted."""


# ------------------------------------------------------------------ consent and connection

def record_consent(user: User, kind: str, version: str, text: str, detail: dict | None = None, now=None) -> None:
    db.session.add(ConsentLog(user_id=user.id, kind=kind, version=version, text_hash=text_hash(text), detail=detail,
                              at=_now(now)))


def connection_for(user: User) -> StravaConnection | None:
    return StravaConnection.query.filter_by(user_id=user.id).first()


def has_read_all(conn: StravaConnection) -> bool:
    return "read_all" in (conn.scope or "").replace(" ", ",").split(",")


def complete_connection(user: User, tokens: TokenSet, now=None) -> StravaConnection:
    """Store the tokens (encrypted) for a user who consented. One athlete can only be linked to one user."""
    if tokens.athlete_id is None:
        raise StravaError("Strava didn't say which athlete this is.")
    other = StravaConnection.query.filter_by(athlete_id=tokens.athlete_id).first()
    if other is not None and other.user_id != user.id:
        raise StravaError("That Strava account is already connected to another person here.")
    conn = connection_for(user) or StravaConnection(user_id=user.id, athlete_id=tokens.athlete_id,
                                                    consent_version=CONNECT_CONSENT_VERSION, consented_at=_now(now),
                                                    access_token_enc="", refresh_token_enc="", expires_at=_now(now))
    conn.athlete_id, conn.scope = tokens.athlete_id, tokens.scope
    _store_tokens(conn, tokens)
    db.session.add(conn)
    db.session.commit()
    return conn


def _store_tokens(conn: StravaConnection, tokens: TokenSet) -> None:
    conn.access_token_enc = crypto.encrypt(tokens.access_token)
    conn.refresh_token_enc = crypto.encrypt(tokens.refresh_token)      # always the newest: the old one is now dead
    conn.expires_at = tokens.expires_at
    if tokens.scope:
        conn.scope = tokens.scope


def access_token(user: User, client: StravaClient, now=None) -> str:
    """A valid access token, refreshing (and storing the rotated refresh token) when it is close to expiry."""
    conn = connection_for(user)
    if conn is None:
        raise StravaDisconnected("Strava isn't connected.")
    if conn.expires_at - _now(now) > REFRESH_MARGIN:
        return crypto.decrypt(conn.access_token_enc)
    try:
        fresh = client.refresh(crypto.decrypt(conn.refresh_token_enc))
    except StravaUnauthorized:
        disconnect(user, client, "Strava no longer accepts the saved connection", now=now, revoke=False)
        raise StravaDisconnected("Strava no longer accepts the saved connection, so your Strava data was deleted.")
    _store_tokens(conn, fresh)
    db.session.commit()
    return fresh.access_token


# ------------------------------------------------------------------ reading routes

def _upsert_cache(user: User, r, now: datetime) -> StravaRouteCache:
    row = StravaRouteCache.query.filter_by(user_id=user.id, strava_route_id=r.id).first()
    if row is None:
        row = StravaRouteCache(user_id=user.id, strava_route_id=r.id, fetched_at=now, expires_at=now + CACHE_TTL)
        db.session.add(row)
    row.name, row.distance_m, row.elevation_gain_m = r.name[:200], r.distance_m, r.elevation_gain_m
    row.summary_polyline, row.private = r.summary_polyline, r.private
    return row


def list_routes(user: User, client: StravaClient, now=None, force: bool = False) -> list[StravaRouteCache]:
    """The athlete's routes, from the cache if it is under an hour old, else fetched (up to 3 pages) and re-cached.

    Routes that are no longer in Strava's answer are removed here, along with any imported copy."""
    now = _now(now)
    purge_expired(now)
    rows = StravaRouteCache.query.filter_by(user_id=user.id).order_by(StravaRouteCache.name).all()
    if rows and not force and all(now - r.fetched_at < LIST_FRESH_FOR for r in rows):
        return rows
    conn = connection_for(user)
    token = access_token(user, client, now)
    seen = set()
    for page in (1, 2, 3):
        try:
            batch = client.list_routes(token, conn.athlete_id, page=page)
        except StravaUnauthorized:
            disconnect(user, client, "Strava rejected our access while listing routes", now=now, revoke=False)
            raise StravaDisconnected("Strava no longer accepts the saved connection, so your Strava data was deleted.")
        for r in batch:
            seen.add(r.id)
            row = _upsert_cache(user, r, now)
            if row.fetched_at is None:
                row.fetched_at = now
        if len(batch) < 30:
            break
    for row in StravaRouteCache.query.filter_by(user_id=user.id).all():
        if row.strava_route_id not in seen:
            _delete_route_data(user, row.strava_route_id, row)
    for row in StravaRouteCache.query.filter_by(user_id=user.id).all():
        row.last_validated_at = now
    db.session.commit()
    return StravaRouteCache.query.filter_by(user_id=user.id).order_by(StravaRouteCache.name).all()


def import_route(user: User, client: StravaClient, strava_route_id: int, now=None) -> Route:
    """Bring one of the athlete's own routes in for planning. The copy is private to them and expires with the cache."""
    now = _now(now)
    row = StravaRouteCache.query.filter_by(user_id=user.id, strava_route_id=strava_route_id).first()
    if row is None:
        raise StravaNotFound("Pick a route from your Strava list first.", 404)
    existing = Route.query.filter_by(owner_id=user.id, source="strava", strava_route_id=strava_route_id).first()
    if existing is not None:
        return existing
    conn = connection_for(user)
    token = access_token(user, client, now)
    try:
        gpx = client.export_gpx(token, strava_route_id)
    except StravaNotFound:
        _delete_route_data(user, strava_route_id, row)
        db.session.commit()
        raise
    except StravaUnauthorized:
        disconnect(user, client, "Strava rejected our access while importing", now=now, revoke=False)
        raise StravaDisconnected("Strava no longer accepts the saved connection, so your Strava data was deleted.")
    parsed = parse_gpx(gpx)
    route = create_route(parsed, owner_id=user.id, source="strava", visibility="private", title=row.tidy_name or row.name,
                         strava_route_id=strava_route_id, strava_expires_at=row.expires_at, keep_gpx_waypoints=False)
    db.session.add(route)
    db.session.commit()
    return route


# ------------------------------------------------------------------ names

def tidy_names(user: User, overpass, workers: int = 1, limit: int = 40) -> dict:
    """Give listings (and routes already imported from them, if the leader hasn't renamed those) a sortable name:
    '063 km · Naas'. At most `limit` per call so a slow Overpass can't hang the page; press again for the rest."""
    from . import naming
    rows = StravaRouteCache.query.filter_by(user_id=user.id).order_by(StravaRouteCache.distance_m).all()
    todo = [r for r in rows if not r.tidy_name][:limit]
    if todo:
        names = naming.suggest_many([(r.distance_m, r.summary_polyline, r.name) for r in todo], overpass, workers)
        taken = {r.tidy_name for r in rows if r.tidy_name}
        for r, n in zip(todo, names):
            base, k = n, 1
            while n in taken:                      # unique across the whole list, not just this batch
                k += 1
                n = f"{base} ({k})"
            taken.add(n)
            for rt in Route.query.filter_by(owner_id=user.id, source="strava", strava_route_id=r.strava_route_id).all():
                if rt.title == r.name:              # a name the leader typed themselves is never overwritten
                    rt.title = n
            r.tidy_name = n
        db.session.commit()
    left = sum(1 for r in rows if not r.tidy_name)
    return {"named": len(todo), "left": left, "total": len(rows)}


# ------------------------------------------------------------------ deleting

def _delete_route_data(user: User, strava_route_id: int, row: StravaRouteCache | None = None) -> int:
    n = 0
    for r in Route.query.filter_by(owner_id=user.id, source="strava", strava_route_id=strava_route_id).all():
        db.session.delete(r)
        n += 1
    if row is not None:
        db.session.delete(row)
    return n


def purge_expired(now=None) -> dict:
    """Delete every cached Strava route and imported copy that has reached its 7 days. Safe to run any time."""
    now = _now(now)
    cache = StravaRouteCache.query.filter(StravaRouteCache.expires_at <= now).all()
    routes = Route.query.filter(Route.source == "strava", Route.strava_expires_at <= now).all()
    out = {"cache_rows": len(cache), "routes": len(routes)}
    for r in routes:
        db.session.delete(r)
    for c in cache:
        db.session.delete(c)
    db.session.commit()
    return out


def delete_strava_data(user: User) -> dict:
    """Remove all Strava-derived data for a user (tokens excluded; see disconnect). Returns counts."""
    routes = Route.query.filter_by(owner_id=user.id, source="strava").all()
    cache = StravaRouteCache.query.filter_by(user_id=user.id).all()
    for r in routes:
        db.session.delete(r)
    for c in cache:
        db.session.delete(c)
    return {"routes": len(routes), "cache_rows": len(cache)}


def disconnect(user: User, client: StravaClient | None, reason: str, now=None, revoke: bool = True,
               notify: bool = True) -> dict:
    """Disconnect Strava: revoke at Strava (best effort), delete tokens and every piece of Strava data, tell the person.

    Used for the athlete pressing Disconnect, for revocation on Strava's side (webhook), when Strava rejects our
    token, and as part of account deletion. Routes the leader published to the club are theirs and stay."""
    conn = connection_for(user)
    had_connection = conn is not None
    if conn is not None and revoke and client is not None:
        try:
            client.revoke(crypto.decrypt(conn.access_token_enc))
        except Exception as e:                 # revoking is a courtesy: never block the deletion on it
            log.warning("Strava revoke failed (%s); deleting local data anyway", e)
    counts = delete_strava_data(user)
    if conn is not None:
        db.session.delete(conn)
    if had_connection:
        record_consent(user, "strava_disconnect", None, reason, {"deleted": counts}, now)
    db.session.commit()
    if notify and had_connection:
        get_mailer().send(user.email, "Your Strava data has been deleted",
                          "Your Strava connection to Ride Leader Kit has ended and the Strava data we held "
                          f"({counts['cache_rows']} route listing(s), {counts['routes']} imported route(s)) has been "
                          f"permanently deleted. Reason: {reason}.\n\nRoutes you chose to share with your club are "
                          "yours and are not affected; delete them from the site if you want them gone.")
    return {**counts, "had_connection": had_connection}


def deauthorized_by_strava(athlete_id: int, client: StravaClient | None, now=None) -> bool:
    """Webhook: the athlete revoked our access on Strava. Delete everything of theirs."""
    conn = StravaConnection.query.filter_by(athlete_id=athlete_id).first()
    if conn is None:
        return False
    user = db.session.get(User, conn.user_id)
    disconnect(user, client, "you revoked access on Strava", revoke=False)
    return True


def revalidate(client: StravaClient, now=None, max_calls: int = REVALIDATE_MAX_CALLS) -> dict:
    """Reflect deletions made on Strava. Webhooks do not cover routes, so this asks Strava about each cached
    route that has not been checked for REVALIDATE_AFTER. Run daily. A 404 removes our copy; a 401 means the
    athlete's access is gone, so all their Strava data goes."""
    now = _now(now)
    stats = {"checked": 0, "removed": 0, "disconnected": 0}
    due = (StravaRouteCache.query
           .filter((StravaRouteCache.last_validated_at.is_(None)) | (StravaRouteCache.last_validated_at < now - REVALIDATE_AFTER))
           .order_by(StravaRouteCache.last_validated_at.asc().nullsfirst()).all())
    gone_users: set[int] = set()
    for row in due:
        if stats["checked"] >= max_calls:
            break
        if row.user_id in gone_users:
            continue
        user = db.session.get(User, row.user_id)
        try:
            token = access_token(user, client, now)
            client.get_route(token, row.strava_route_id)
            row.last_validated_at = now
        except StravaNotFound:
            _delete_route_data(user, row.strava_route_id, row)
            stats["removed"] += 1
        except (StravaDisconnected, StravaUnauthorized):
            if connection_for(user) is not None:
                disconnect(user, client, "Strava no longer accepts the saved connection", now=now, revoke=False)
            gone_users.add(user.id)
            stats["disconnected"] += 1
        except StravaError as e:
            log.warning("revalidate: Strava error %s; will retry next run", e)
            if e.status == 429:
                break
        stats["checked"] += 1
        db.session.commit()
    return stats


# ------------------------------------------------------------------ publishing to the club

def _club_of(user: User) -> int | None:
    from flask import g, has_request_context
    club = g.get("club") if has_request_context() else None
    return club.id if club else None


def publish_to_club(user: User, route: Route, confirmed: bool, now=None) -> Route:
    """Make the club's own copy of a route the leader imported from Strava.

    Needs the explicit confirmation; stores a separate route (source 'gpx', owned by the leader, visible to the
    club) with no link to Strava, so it survives the 7-day purge and disconnecting. Only things the leader or the
    app added are copied: waypoints that came from Strava's file are left behind."""
    if route.owner_id != user.id or route.source != "strava":
        raise PermissionError("Only the athlete who imported this route can share it.")
    if not confirmed:
        raise ValueError("Please confirm that this is your own route and you are sharing it with your club.")
    coords = list(to_shape(route.geom).coords)
    copy = Route(title=route.title, source="gpx", owner_id=user.id, visibility="club", club_id=route.club_id or _club_of(user),
                 geom=WKTElement("LINESTRING(" + ",".join(f"{x} {y}" for x, y in coords) + ")", srid=4326),
                 length_m=route.length_m, ascent_m=route.ascent_m, descent_m=route.descent_m,
                 dist_m=list(route.dist_m), ele_m=list(route.ele_m) if route.ele_m else None)
    for w in route.waypoints:
        if w.origin in ("osm", "manual") and w.kind != "regroup":
            p = to_shape(w.geom)
            copy.waypoints.append(Waypoint(
                kind=w.kind, name=w.name, note=w.note, s_m=w.s_m, offset_m=w.offset_m, side=w.side, pinned=w.pinned,
                included=w.included, origin=w.origin, osm_ref=w.osm_ref, tags=w.tags, planned_stop=w.planned_stop,
                geom=WKTElement(f"POINT({p.x} {p.y})", srid=4326)))
    if route.ride is not None:                       # the leader's own plan travels with the copy
        r = route.ride
        copy.ride = Ride(date=r.date, start_time=r.start_time, tz=r.tz, meeting_name=r.meeting_name, meeting_lat=r.meeting_lat,
                         meeting_lon=r.meeting_lon, leader_phone=r.leader_phone, notes=r.notes,
                         groups=[PaceGroup(position=g.position, name=g.name, speed_kmh=g.speed_kmh, stop_min=g.stop_min)
                                 for g in r.groups])
    db.session.add(copy)
    record_consent(user, "strava_publish", PUBLISH_CONSENT_VERSION, PUBLISH_CONSENT_TEXT, {"source_route_id": route.id}, now)
    db.session.commit()
    return copy
