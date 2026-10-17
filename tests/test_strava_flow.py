"""Strava end to end against a fake client: consent, connect, token rotation, 7-day cache, purge, disconnect,
webhook, re-checking deletions, sharing with the club, and the user's own data export and deletion."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

from app import crypto, db, polyline
from app import scheduler
from app import strava_service as svc
from app.models import ConsentLog, Route, StravaConnection, StravaRouteCache, User, Waypoint
from app.strava import StravaError, StravaNotFound, StravaRoute, StravaUnauthorized, TokenSet
from tests.conftest import hilly_points, make_gpx
from tests.test_auth_security import make_user, other_client, outbox, sign_in


def now():
    return datetime.now(timezone.utc)


class FakeStrava:
    """Records what the app asks of Strava and can be told to misbehave."""

    def __init__(self, athlete_id=4242, scope="read,read_all"):
        self.athlete_id, self.scope, self.n = athlete_id, scope, 0
        poly = polyline.encode([(53.1, -6.8), (53.2, -6.7)])
        self.routes = {101: StravaRoute(101, "Curragh loop", 60000, 500, poly, False),
                       102: StravaRoute(102, "Hill of Allen", 45000, 650, poly, False),
                       103: StravaRoute(103, "Private recovery", 30000, 120, poly, True)}
        self.gpx = make_gpx(hilly_points(), waypoints=[("Strava's own waypoint", 53.16, -6.85)], name="From Strava")
        self.calls, self.revoked, self.fail, self.missing = [], [], {}, set()

    def _maybe(self, name):
        if name in self.fail:
            raise self.fail[name]

    def authorize_url(self, state, redirect_uri):
        return f"https://strava.test/oauth/authorize?state={state}&redirect_uri={redirect_uri}"

    def _tokens(self, with_athlete):
        self.n += 1
        return TokenSet(f"acc{self.n}", f"ref{self.n}", now() + timedelta(hours=6), self.scope if with_athlete else "",
                        self.athlete_id if with_athlete else None)

    def exchange_code(self, code):
        self.calls.append(("exchange", code)); self._maybe("exchange")
        return self._tokens(True)

    def refresh(self, refresh_token):
        self.calls.append(("refresh", refresh_token)); self._maybe("refresh")
        return self._tokens(False)

    def list_routes(self, access_token, athlete_id, page=1, per_page=30):
        self.calls.append(("list", access_token, athlete_id, page)); self._maybe("list")
        return [r for rid, r in self.routes.items() if rid not in self.missing] if page == 1 else []

    def get_route(self, access_token, route_id):
        self.calls.append(("get", route_id)); self._maybe("get")
        if route_id in self.missing or route_id not in self.routes:
            raise StravaNotFound("gone", 404)
        return self.routes[route_id]

    def export_gpx(self, access_token, route_id):
        self.calls.append(("export", route_id)); self._maybe("export")
        if route_id in self.missing:
            raise StravaNotFound("gone", 404)
        return self.gpx

    def revoke(self, access_token):
        self.revoked.append(access_token)

    def create_subscription(self, callback_url, verify_token):
        return 7

    def count(self, kind):
        return sum(1 for c in self.calls if c[0] == kind)


@pytest.fixture
def world(client):
    fake = FakeStrava()
    client.application.config.update(STRAVA_CLIENT=fake, QUEUE_INLINE=True, STRAVA_VERIFY_TOKEN="verify-me",
                                     STRAVA_SUBSCRIPTION_ID="7")
    return client, fake


def connect(c, email="leader@example.com", callback_extra=""):
    """Sign in, consent, and complete the OAuth round trip. Returns the user."""
    user = User.query.filter_by(email=email).first() or make_user(email)
    sign_in(c, user)
    r = c.post("/strava/connect", data={"consent": "yes"})
    assert r.status_code == 302, r.data
    state = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]
    cb = c.get(f"/strava/callback?code=abc&state={state}&scope=read,read_all{callback_extra}")
    assert cb.status_code == 302, cb.data
    return user


def rows(user=None):
    q = StravaRouteCache.query
    return q.filter_by(user_id=user.id).all() if user else q.all()


def strava_routes():
    return Route.query.filter_by(source="strava").all()


# ---------------- off, and sign-in required ----------------

def test_with_strava_unconfigured_everything_else_still_works(client):
    sign_in(client, make_user())
    assert client.get("/strava/connect").status_code == 404 and client.get("/strava/routes").status_code == 404
    assert client.get("/strava/webhook?hub.mode=subscribe&hub.verify_token=x&hub.challenge=c").status_code == 403
    assert b"bring in a route from your Strava" not in client.get("/").data
    up = client.post("/routes/upload", data={"gpx": (__import__("io").BytesIO(make_gpx(hilly_points())), "a.gpx")},
                     content_type="multipart/form-data")
    assert up.status_code == 302 and client.get(up.headers["Location"]).status_code == 200
    account = client.get("/account").data
    assert b"Connect Strava" not in account and b"Disconnect Strava" not in account and b"/strava/" not in account


def test_connecting_needs_sign_in(world):
    c, _ = world
    r = c.get("/strava/connect")
    assert r.status_code == 302 and "/login" in r.headers["Location"] and "next=" in r.headers["Location"]
    assert c.get("/strava/routes").status_code == 302


# ---------------- consent screen ----------------

def test_the_consent_screen_says_what_how_how_long_and_how_to_withdraw(world):
    c, _ = world
    sign_in(c, make_user())
    page = c.get("/strava/connect").data.decode()
    for needle in ("list of your routes", "private", "route file", "athlete number", "read-only", "at most 7 days",
                   "Only you", "Disconnect Strava", "Delete my account", "Carry on without Strava", 'name="consent"', svc.CONNECT_CONSENT_TEXT):
        assert needle in page, needle
    assert "connect_with_strava_orange.svg" in page and 'height="48"' in page      # the official button, at Strava's size


def test_nothing_happens_without_ticking_the_box(world):
    c, fake = world
    sign_in(c, make_user())
    r = c.post("/strava/connect", data={})
    assert r.status_code == 400 and b"Tick the box" in r.data
    assert ConsentLog.query.count() == 0 and not fake.calls


def test_consent_is_recorded_with_version_and_text_hash_then_we_go_to_strava(world):
    c, _ = world
    user = make_user()
    sign_in(c, user)
    r = c.post("/strava/connect", data={"consent": "yes"})
    loc = urlparse(r.headers["Location"])
    assert r.status_code == 302 and loc.netloc == "strava.test" and "callback" in parse_qs(loc.query)["redirect_uri"][0]
    log = ConsentLog.query.one()
    assert (log.user_id, log.kind, log.version) == (user.id, "strava_connect", svc.CONNECT_CONSENT_VERSION)
    assert log.text_hash == hashlib.sha256(svc.CONNECT_CONSENT_TEXT.encode()).hexdigest()
    with c.session_transaction() as s:
        assert s["strava_state"] == parse_qs(loc.query)["state"][0]


# ---------------- callback ----------------

def test_callback_rejects_missing_wrong_state_and_missing_consent(world):
    c, fake = world
    user = make_user()
    sign_in(c, user)
    assert c.get("/strava/callback?code=x&state=y").status_code == 400                      # we never started a connect
    c.post("/strava/connect", data={"consent": "yes"})
    assert c.get("/strava/callback?code=x&state=forged").status_code == 400                 # CSRF on the OAuth hop
    assert fake.count("exchange") == 0 and StravaConnection.query.count() == 0
    ConsentLog.query.delete(); db.session.commit()
    with c.session_transaction() as s:
        s["strava_state"] = "known"
    r = c.get("/strava/callback?code=x&state=known")
    assert r.status_code == 400 and b"agree on the consent screen" in r.data and fake.count("exchange") == 0


def test_denying_access_on_strava_stores_nothing(world):
    c, fake = world
    sign_in(c, make_user())
    c.post("/strava/connect", data={"consent": "yes"})
    r = c.get("/strava/callback?error=access_denied&state=whatever")
    assert r.status_code == 200 and b"nothing was stored" in r.data
    assert StravaConnection.query.count() == 0 and fake.count("exchange") == 0


def test_a_code_strava_rejects_does_not_connect(world):
    c, fake = world
    fake.fail["exchange"] = StravaUnauthorized("bad code", 400)
    user = make_user()
    sign_in(c, user)
    r = c.post("/strava/connect", data={"consent": "yes"})
    state = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]
    out = c.get(f"/strava/callback?code=bad&state={state}")
    assert out.status_code == 400 and StravaConnection.query.count() == 0


def test_connecting_stores_tokens_encrypted_and_remembers_the_athlete(world):
    c, fake = world
    user = connect(c)
    conn = StravaConnection.query.one()
    assert conn.user_id == user.id and conn.athlete_id == 4242 and conn.scope == "read,read_all"
    assert "acc1" not in conn.access_token_enc and "ref1" not in conn.refresh_token_enc
    assert crypto.decrypt(conn.access_token_enc) == "acc1" and crypto.decrypt(conn.refresh_token_enc) == "ref1"
    assert conn.consent_version == svc.CONNECT_CONSENT_VERSION
    with c.session_transaction() as s:
        assert "strava_state" not in s                                                      # one-time state


def test_an_athlete_cannot_be_linked_to_two_people(world):
    c, fake = world
    connect(c, "first@example.com")
    c2 = other_client(c)
    sign_in(c2, make_user("second@example.com"))
    r = c2.post("/strava/connect", data={"consent": "yes"})
    state = parse_qs(urlparse(r.headers["Location"]).query)["state"][0]
    out = c2.get(f"/strava/callback?code=x&state={state}")
    assert out.status_code == 400 and b"already connected to another person" in out.data
    assert StravaConnection.query.count() == 1


def test_missing_private_scope_is_explained(world):
    c, fake = world
    fake.scope = "read"
    connect(c)
    page = c.get("/strava/routes").data
    assert b"permission to read your private routes" in page
    fake.scope = "read,read_all"
    StravaConnection.query.update({"scope": "read,read_all"}); db.session.commit()
    assert b"permission to read your private routes" not in c.get("/strava/routes").data


# ---------------- listing and the 7-day cache ----------------

def test_route_list_is_cached_for_seven_days_and_attributed(world):
    c, fake = world
    user = connect(c)
    page = c.get("/strava/routes").data.decode()
    for needle in ("Curragh loop", "Hill of Allen", "Private recovery", "private</span>", "Powered by Strava", "Import"):
        assert needle in page, needle
    assert (now() + timedelta(days=7)).strftime("%d %b %Y") in page
    for r in rows(user):
        assert timedelta(days=7) - timedelta(seconds=10) < r.expires_at - r.fetched_at <= timedelta(days=7)
    assert fake.count("list") == 1
    c.get("/strava/routes")
    assert fake.count("list") == 1                                                         # reused: under an hour old
    c.get("/strava/routes?refresh=1")
    assert fake.count("list") == 2


def test_refreshing_does_not_extend_the_seven_days(world):
    c, fake = world
    user = connect(c)
    c.get("/strava/routes")
    first = {r.strava_route_id: r.expires_at for r in rows(user)}
    c.get("/strava/routes?refresh=1")
    assert {r.strava_route_id: r.expires_at for r in rows(user)} == first


def test_a_route_deleted_on_strava_disappears_here_including_the_imported_copy(world):
    c, fake = world
    user = connect(c)
    c.get("/strava/routes")
    c.post("/strava/routes/101/import")
    assert len(strava_routes()) == 1
    del fake.routes[101]
    c.get("/strava/routes?refresh=1")
    assert {r.strava_route_id for r in rows(user)} == {102, 103} and strava_routes() == []


def test_an_unreachable_strava_shows_what_we_saved_and_says_so(world):
    c, fake = world
    connect(c)
    c.get("/strava/routes")
    fake.fail["list"] = StravaError("Strava is limiting requests right now.", 429)
    page = c.get("/strava/routes?refresh=1").data.decode()
    assert "limiting requests" in page and "Curragh loop" in page


# ---------------- importing ----------------

def test_import_makes_a_private_expiring_route_without_strava_waypoints(world):
    c, fake = world
    user = connect(c)
    c.get("/strava/routes")
    r = c.post("/strava/routes/101/import")
    assert r.status_code == 302
    route = Route.query.one()
    assert (route.source, route.owner_id, route.visibility, route.strava_route_id, route.title) == ("strava", user.id, "private", 101, "Curragh loop")
    cache = StravaRouteCache.query.filter_by(strava_route_id=101).one()
    assert route.strava_expires_at == cache.expires_at                                     # dies with the cache
    assert route.waypoints == [] and route.length_m > 10_000                               # Strava's own waypoint was dropped
    page = c.get(r.headers["Location"]).data.decode()
    assert "From your Strava" in page and "Only you can see this route" in page and "Powered by Strava" in page
    assert c.post("/strava/routes/101/import").headers["Location"] == r.headers["Location"]   # idempotent
    assert fake.count("export") == 1


def test_only_listed_routes_can_be_imported(world):
    c, fake = world
    connect(c)
    c.get("/strava/routes")
    assert c.post("/strava/routes/999/import").status_code == 404 and fake.count("export") == 0


def test_a_route_that_vanished_between_listing_and_import_is_cleaned_up(world):
    c, fake = world
    user = connect(c)
    c.get("/strava/routes")
    fake.missing.add(101)
    assert c.post("/strava/routes/101/import").status_code == 404
    assert 101 not in {r.strava_route_id for r in rows(user)} and strava_routes() == []


def test_nobody_but_the_athlete_can_see_or_touch_an_imported_route(world):
    c, fake = world
    connect(c)
    c.get("/strava/routes")
    c.post("/strava/routes/101/import")
    other = other_client(c)
    sign_in(other, make_user("clubmate@example.com"))
    for client_ in (other, other_client(c)):                                               # a signed-in stranger and a signed-out visitor
        for url in ("/routes/1", "/api/routes/1", "/api/routes/1/waypoints", "/routes/1/export", "/routes/1/export/gpx/download",
                    "/api/routes/1/climbs", "/routes/1/publish"):
            assert client_.get(url).status_code == 404, url
        assert client_.post("/routes/1/delete").status_code == 404
        assert client_.post("/routes/1/publish", data={"confirm_own": "yes"}).status_code == 404
        assert "Curragh" not in client_.get("/").data.decode()
    assert "Curragh" in c.get("/").data.decode() and Route.query.count() == 1


def test_even_a_route_marked_shared_stays_private_if_it_came_from_strava(world):
    c, fake = world
    connect(c)
    c.get("/strava/routes")
    c.post("/strava/routes/101/import")
    Route.query.update({"visibility": "club"}); db.session.commit()
    assert other_client(c).get("/api/routes/1").status_code == 404


def test_users_only_ever_see_their_own_strava_listings(world):
    c, fake = world
    a = connect(c, "a@example.com")
    c.get("/strava/routes")
    fake.athlete_id = 5555
    fake.routes = {201: StravaRoute(201, "B's secret loop", 10000, 50, "", True)}
    c2 = other_client(c)
    b = connect(c2, "b@example.com")
    c2.get("/strava/routes")
    assert {r.name for r in rows(a)} == {"Curragh loop", "Hill of Allen", "Private recovery"} and {r.name for r in rows(b)} == {"B's secret loop"}
    assert "secret" not in c.get("/strava/routes").data.decode() and "Curragh" not in c2.get("/strava/routes").data.decode()


# ---------------- tokens ----------------

def test_an_expiring_token_is_refreshed_and_the_rotated_refresh_token_is_stored(world):
    c, fake = world
    connect(c)
    conn = StravaConnection.query.one()
    conn.expires_at = now() + timedelta(minutes=2)
    db.session.commit()
    c.get("/strava/routes")
    assert fake.calls[1] == ("refresh", "ref1")
    conn = StravaConnection.query.one()
    assert crypto.decrypt(conn.access_token_enc) == "acc2" and crypto.decrypt(conn.refresh_token_enc) == "ref2"   # old one is dead
    assert conn.expires_at > now() + timedelta(hours=5) and conn.scope == "read,read_all"
    assert ("list", "acc2", 4242, 1) in fake.calls


def test_a_healthy_token_is_not_refreshed(world):
    c, fake = world
    connect(c)
    c.get("/strava/routes")
    assert fake.count("refresh") == 0 and ("list", "acc1", 4242, 1) in fake.calls


@pytest.mark.parametrize("where", ["refresh", "list"])
def test_when_strava_rejects_us_everything_of_theirs_is_deleted(world, where):
    c, fake = world
    user = connect(c)
    c.get("/strava/routes")
    c.post("/strava/routes/101/import")
    fake.fail[where] = StravaUnauthorized("revoked", 401)
    if where == "refresh":
        StravaConnection.query.update({"expires_at": now() - timedelta(minutes=1)}); db.session.commit()
    page = c.get("/strava/routes?refresh=1").data.decode()
    assert "Strava is disconnected" in page and "deleted" in page
    assert StravaConnection.query.count() == 0 and rows() == [] and strava_routes() == []
    assert outbox(c)[-1]["subject"] == "Your Strava data has been deleted" and fake.revoked == []


# ---------------- purge ----------------

def test_the_seven_day_purge_deletes_cache_and_imported_copies_but_not_shared_copies(world):
    c, fake = world
    user = connect(c)
    c.get("/strava/routes")
    c.post("/strava/routes/101/import")
    c.post("/routes/1/publish", data={"confirm_own": "yes"})
    assert Route.query.count() == 2
    assert svc.purge_expired(now() + timedelta(days=6, hours=23)) == {"cache_rows": 0, "routes": 0}
    assert len(rows()) == 3 and len(strava_routes()) == 1
    out = svc.purge_expired(now() + timedelta(days=7, minutes=1))
    assert out == {"cache_rows": 3, "routes": 1}
    assert rows() == [] and strava_routes() == []
    kept = Route.query.one()
    assert kept.source == "gpx" and kept.visibility == "club" and kept.owner_id == user.id and kept.title == "Curragh loop"


def test_listing_purges_what_has_already_expired_first(world):
    c, fake = world
    connect(c)
    c.get("/strava/routes")
    c.post("/strava/routes/101/import")
    past = now() - timedelta(minutes=1)
    StravaRouteCache.query.update({"expires_at": past}); Route.query.update({"strava_expires_at": past}); db.session.commit()
    c.get("/strava/routes")
    assert strava_routes() == []                                                            # the expired import is gone...
    assert len(rows()) == 3 and all(r.expires_at > now() + timedelta(days=6) for r in rows())   # ...listing re-fetched fresh data


def test_the_scheduler_purges_every_pass_and_rechecks_strava_every_six_hours(world):
    c, fake = world
    connect(c)
    c.get("/strava/routes")
    app = c.application
    t0 = now() + timedelta(days=8)
    first = scheduler.tick(app, now=t0)
    assert first["purged"]["cache_rows"] == 3 and first["revalidated"] is not None and first["last_revalidate"] == t0
    assert scheduler.tick(app, now=t0 + timedelta(hours=1), last_revalidate=t0)["revalidated"] is None
    assert scheduler.tick(app, now=t0 + timedelta(hours=6), last_revalidate=t0)["revalidated"] is not None
    app.config["STRAVA_CLIENT"] = None
    assert scheduler.tick(app, now=t0 + timedelta(hours=20))["revalidated"] is None


# ---------------- disconnect ----------------

def test_disconnect_revokes_deletes_everything_and_confirms_in_writing(world):
    c, fake = world
    user = connect(c)
    c.get("/strava/routes")
    c.post("/strava/routes/101/import")
    c.post("/routes/1/publish", data={"confirm_own": "yes"})
    assert c.post("/strava/disconnect", data={}).status_code == 400
    r = c.post("/strava/disconnect", data={"confirm": "yes"})
    assert r.status_code == 200 and b"emailed you a confirmation" in r.data and b"leader@example.com" in r.data
    assert fake.revoked == ["acc1"]
    assert StravaConnection.query.count() == 0 and rows() == [] and strava_routes() == []
    mail = outbox(c)[-1]
    assert mail["to"] == "leader@example.com" and mail["subject"] == "Your Strava data has been deleted"
    assert "3 route listing(s), 1 imported route(s)" in mail["body"] and "permanently deleted" in mail["body"]
    kinds = [x.kind for x in ConsentLog.query.order_by(ConsentLog.id).all()]
    assert kinds == ["strava_connect", "strava_publish", "strava_disconnect"]
    assert Route.query.one().source == "gpx"                                                # their shared copy is theirs and stays


def test_disconnecting_one_person_leaves_everyone_else_alone(world):
    c, fake = world
    a = connect(c, "a@example.com")
    c.get("/strava/routes")
    fake.athlete_id = 5555
    c2 = other_client(c)
    b = connect(c2, "b@example.com")
    c2.get("/strava/routes")
    c.post("/strava/disconnect", data={"confirm": "yes"})
    assert StravaConnection.query.one().user_id == b.id and len(rows(b)) == 3 and rows(a) == []


def test_revoke_failure_never_blocks_deletion(world):
    c, fake = world
    connect(c)
    c.get("/strava/routes")
    fake.revoke = lambda token: (_ for _ in ()).throw(RuntimeError("strava down"))
    assert c.post("/strava/disconnect", data={"confirm": "yes"}).status_code == 200
    assert StravaConnection.query.count() == 0 and rows() == []


# ---------------- webhook ----------------

def event(**kw):
    return {"object_type": "athlete", "aspect_type": "update", "object_id": 4242, "owner_id": 4242, "subscription_id": 7,
            "updates": {"authorized": "false"}, **kw}


def test_webhook_handshake(world):
    c, _ = world
    ok = c.get("/strava/webhook?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=abc123")
    assert ok.status_code == 200 and ok.get_json() == {"hub.challenge": "abc123"} and ok.mimetype == "application/json"
    assert c.get("/strava/webhook?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=abc").status_code == 403
    assert c.get("/strava/webhook?hub.mode=unsubscribe&hub.verify_token=verify-me&hub.challenge=abc").status_code == 403
    c.application.config["STRAVA_VERIFY_TOKEN"] = ""
    assert c.get("/strava/webhook?hub.mode=subscribe&hub.verify_token=&hub.challenge=abc").status_code == 403


def test_revoking_on_strava_deletes_their_data_and_emails_them(world):
    c, fake = world
    connect(c)
    c.get("/strava/routes")
    c.post("/strava/routes/101/import")
    r = other_client(c).post("/strava/webhook", json=event())                               # no session, no CSRF: it is Strava calling
    assert r.status_code == 200
    assert StravaConnection.query.count() == 0 and rows() == [] and strava_routes() == []
    assert outbox(c)[-1]["subject"] == "Your Strava data has been deleted" and "you revoked access on Strava" in outbox(c)[-1]["body"]
    assert fake.revoked == []                                                               # already revoked by them


@pytest.mark.parametrize("payload", [
    event(owner_id=999, object_id=999),                         # an athlete we don't know
    event(updates={"authorized": "true"}),                      # not a revocation
    event(updates={"title": "x"}),
    event(object_type="activity", aspect_type="delete"),        # activities are none of our business
    event(aspect_type="create"),
    event(subscription_id=99),                                  # someone else's subscription
])
def test_other_events_change_nothing(world, payload):
    c, fake = world
    connect(c)
    c.get("/strava/routes")
    assert c.post("/strava/webhook", json=payload).status_code == 200
    assert StravaConnection.query.count() == 1 and len(rows()) == 3


def test_malformed_webhook_bodies_are_refused(world):
    c, _ = world
    assert c.post("/strava/webhook", json=[1, 2]).status_code == 400
    assert c.post("/strava/webhook", data="not json", content_type="application/json").status_code == 400
    assert c.post("/strava/webhook", json=event(owner_id="abc", object_id="abc")).status_code == 200


# ---------------- re-checking for deletions made on Strava ----------------

def test_routes_deleted_on_strava_are_removed_by_the_recheck(world):
    c, fake = world
    user = connect(c)
    c.get("/strava/routes")
    c.post("/strava/routes/101/import")
    fake.missing.add(101)
    stats = svc.revalidate(fake, now() + timedelta(hours=21))          # the listing counted as a check; wait it out
    assert stats == {"checked": 3, "removed": 1, "disconnected": 0}
    assert {r.strava_route_id for r in rows(user)} == {102, 103} and strava_routes() == []
    assert all(r.last_validated_at for r in rows(user))


def test_the_recheck_only_looks_at_routes_not_checked_in_the_last_20_hours(world):
    c, fake = world
    connect(c)
    c.get("/strava/routes")                                                                  # listing counts as a check
    assert svc.revalidate(fake, now() + timedelta(hours=19))["checked"] == 0
    assert svc.revalidate(fake, now() + timedelta(hours=21))["checked"] == 3
    assert svc.revalidate(fake, now() + timedelta(hours=22))["checked"] == 0


def test_the_recheck_respects_a_call_budget_and_rate_limits(world):
    c, fake = world
    connect(c)
    c.get("/strava/routes")
    later = now() + timedelta(hours=30)
    assert svc.revalidate(fake, later, max_calls=2)["checked"] == 2
    assert svc.revalidate(fake, later, max_calls=2)["checked"] == 1                         # the rest next time
    StravaRouteCache.query.update({"last_validated_at": None}); db.session.commit()
    fake.fail["get"] = StravaError("slow down", 429)
    asked = fake.count("get")
    assert svc.revalidate(fake, later)["checked"] == 0                                       # a rate-limited attempt is not a check...
    assert fake.count("get") == asked + 1                                                    # ...and we stop after the first one
    assert len(rows()) == 3


def test_the_recheck_deletes_everything_if_strava_says_access_is_gone(world):
    c, fake = world
    connect(c)
    c.get("/strava/routes")
    c.post("/strava/routes/101/import")
    fake.fail["get"] = StravaUnauthorized("revoked", 401)
    stats = svc.revalidate(fake, now() + timedelta(hours=30))
    assert stats["disconnected"] == 1 and StravaConnection.query.count() == 0 and rows() == [] and strava_routes() == []
    assert outbox(c)[-1]["subject"] == "Your Strava data has been deleted"


# ---------------- sharing with the club ----------------

def imported_route(c, fake):
    user = connect(c)
    c.get("/strava/routes")
    c.post("/strava/routes/101/import")
    return user


def test_the_consent_screen_for_sharing_explains_what_happens(world):
    c, fake = world
    imported_route(c, fake)
    page = c.get("/routes/1/publish").data.decode()
    assert "separate copy" in page and "no link back to Strava" in page and svc.PUBLISH_CONSENT_TEXT in page and 'name="confirm_own"' in page


def test_sharing_needs_the_explicit_confirmation(world):
    c, fake = world
    imported_route(c, fake)
    r = c.post("/routes/1/publish", data={})
    assert r.status_code == 400 and b"confirm that this is your own route" in r.data
    assert Route.query.count() == 1 and ConsentLog.query.filter_by(kind="strava_publish").count() == 0


def test_the_shared_copy_is_independent_of_strava_and_visible_to_the_club(world):
    from geoalchemy2.elements import WKTElement
    c, fake = world
    user = imported_route(c, fake)
    route = Route.query.one()
    db.session.add_all([
        Waypoint(route_id=route.id, name="Café", kind="cafe", s_m=5000, origin="osm", included=True, osm_ref="node/1",
                 geom=WKTElement("POINT(-6.85 53.16)", srid=4326)),
        Waypoint(route_id=route.id, name="Strava's waypoint", kind="generic", s_m=100, origin="gpx",
                 geom=WKTElement("POINT(-6.89 53.16)", srid=4326)),
        Waypoint(route_id=route.id, name="Ease back", kind="regroup", s_m=7000, origin="computed",
                 geom=WKTElement("POINT(-6.8 53.16)", srid=4326))])
    db.session.commit()
    r = c.post("/routes/1/publish", data={"confirm_own": "yes"})
    assert r.status_code == 302
    copy = db.session.get(Route, int(r.headers["Location"].rsplit("/", 1)[1]))
    assert copy.id != route.id and copy.source == "gpx" and copy.owner_id == user.id and copy.visibility == "club"
    assert copy.strava_route_id is None and copy.strava_expires_at is None and copy.title == route.title
    assert copy.length_m == route.length_m and list(copy.dist_m) == list(route.dist_m)
    assert [w.name for w in copy.waypoints] == ["Café"]                                      # nothing that came from Strava's file
    log = ConsentLog.query.filter_by(kind="strava_publish").one()
    assert log.text_hash == hashlib.sha256(svc.PUBLISH_CONSENT_TEXT.encode()).hexdigest() and log.detail == {"source_route_id": route.id}
    assert other_client(c).get(f"/routes/{copy.id}").status_code == 200                       # the club can see it
    assert other_client(c).get(f"/routes/{route.id}").status_code == 404                      # the original is still just theirs


def test_only_the_importing_athlete_can_share_and_only_strava_routes(world):
    c, fake = world
    imported_route(c, fake)
    other = other_client(c)
    sign_in(other, make_user("clubmate@example.com"))
    assert other.post("/routes/1/publish", data={"confirm_own": "yes"}).status_code == 404
    c.post("/routes/1/publish", data={"confirm_own": "yes"})
    shared = Route.query.filter_by(source="gpx").one()
    assert c.get(f"/routes/{shared.id}/publish").status_code == 404                           # an ordinary route has nothing to publish
    assert Route.query.count() == 2


# ---------------- GDPR: export and erasure ----------------

def test_the_data_export_is_complete_scoped_to_the_user_and_has_no_secrets(world):
    c, fake = world
    a = imported_route(c, fake)
    c.post("/routes/1/publish", data={"confirm_own": "yes"})
    c2 = other_client(c)
    sign_in(c2, make_user("other@example.com"))
    c2.post("/routes/upload", data={"gpx": (__import__("io").BytesIO(make_gpx(hilly_points(), name="Other's route")), "a.gpx")},
            content_type="multipart/form-data")
    r = c.get("/account/export.json")
    assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
    raw = r.data.decode()
    data = json.loads(raw)
    assert data["account"]["email"] == "leader@example.com"
    assert {x["title"] for x in data["routes"]} == {"Curragh loop"} and {x["source"] for x in data["routes"]} == {"strava", "gpx"}
    assert all(len(x["coordinates_lon_lat_ele"]) > 100 for x in data["routes"])
    assert data["strava"]["athlete_id"] == 4242 and len(data["strava"]["cached_route_listings"]) == 3
    assert [x["kind"] for x in data["consents"]] == ["strava_connect", "strava_publish"]
    conn = StravaConnection.query.one()
    for secret in ("acc1", "ref1", conn.access_token_enc, conn.refresh_token_enc):
        assert secret not in raw
    assert "Other's route" not in raw and "other@example.com" not in raw
    assert other_client(c).get("/account/export.json").status_code == 302                     # sign-in required


def test_deleting_an_account_erases_everything_and_confirms_by_email(world):
    c, fake = world
    a = imported_route(c, fake)
    c.post("/routes/1/publish", data={"confirm_own": "yes"})
    c2 = other_client(c)
    fake.athlete_id = 5555
    b = connect(c2, "keeper@example.com")
    c2.get("/strava/routes")
    wrong = c.post("/account/delete", data={"confirm_email": "nope@example.com"})
    assert wrong.status_code == 400 and User.query.count() == 2
    ok = c.post("/account/delete", data={"confirm_email": "LEADER@example.com "})
    assert ok.status_code == 200 and b"emailed a confirmation" in ok.data
    assert User.query.filter_by(email="leader@example.com").count() == 0
    assert StravaConnection.query.one().user_id == b.id and {r.user_id for r in rows()} == {b.id}
    assert Route.query.filter_by(owner_id=a.id).count() == 0 and ConsentLog.query.filter_by(user_id=a.id).count() == 0
    assert fake.revoked == ["acc1"]
    mail = outbox(c)[-1]
    assert mail["to"] == "leader@example.com" and "permanently deleted" in mail["body"] and "2 route(s)" in mail["body"]
    assert c.get("/account").status_code == 302                                               # signed out
    assert len(rows(b)) == 3                                                                  # the other person's data untouched


# ---------------- the development mock, through the real pages ----------------

def test_mock_mode_works_through_the_pages(client):
    cfg = client.application.config
    cfg.update(STRAVA_CLIENT=None, STRAVA_MODE="mock", QUEUE_INLINE=True)
    user = make_user("dev@example.com")
    sign_in(client, user)
    page = client.get("/strava/connect").data.decode()
    assert "pretend Strava athlete" in page
    r = client.post("/strava/connect", data={"consent": "yes"})
    cb = client.get(urlparse(r.headers["Location"]).path + "?" + urlparse(r.headers["Location"]).query)   # the mock loops straight back
    assert cb.status_code == 302 and cb.headers["Location"].endswith("/strava/routes")
    lst = client.get("/strava/routes").data.decode()
    assert "Mock: Curragh loop" in lst and "Mock: Private recovery spin" in lst and "private</span>" in lst
    imp = client.post("/strava/routes/9001/import")
    route = client.get(imp.headers["Location"]).data.decode()
    assert imp.status_code == 302 and "From your Strava" in route
    assert Route.query.one().length_m > 5_000 and Route.query.one().ele_m is not None


def test_the_subscribe_command_creates_the_webhook_subscription(world):
    c, fake = world
    runner = c.application.test_cli_runner()
    out = runner.invoke(args=["strava-subscribe", "https://club.example/strava/webhook"])
    assert out.exit_code == 0 and "id 7" in out.output and "STRAVA_SUBSCRIPTION_ID=7" in out.output
    c.application.config["STRAVA_VERIFY_TOKEN"] = ""
    bad = runner.invoke(args=["strava-subscribe", "https://club.example/strava/webhook"])
    assert bad.exit_code != 0 and "STRAVA_VERIFY_TOKEN" in bad.output


def test_the_find_stops_tick_box_sends_you_to_the_route_with_the_search_flagged(world):
    c, fake = world
    connect(c)
    c.get("/strava/routes")
    page = c.get("/strava/routes").data.decode()
    assert 'id="auto-scan"' in page and 'name="scan" value="1"' in page
    loc = c.post("/strava/routes/101/import", data={"scan": "1"}).headers["Location"]
    assert loc.endswith("?autoscan=1")


def test_an_imported_route_offers_the_hand_off_back_to_strava_and_uses_the_official_marks(world):
    c, fake = world
    connect(c)
    c.get("/strava/routes")
    loc = c.post("/strava/routes/101/import").headers["Location"]
    page = c.get(loc).data.decode()
    assert "Send cues back to Strava" in page and "device=strava" in page and "View on Strava" in page
    assert "powered_by_strava_black.svg" in page and 'alt="Powered by Strava"' in page
    exp = c.get(loc + "/export?device=strava").data.decode()
    assert "Send to Strava" in exp and "only lets apps read your routes" in exp


# ---------------- sortable names ----------------

class PlacesOverpass:
    """Answers a place query with towns and villages around a fixed spot."""
    def __init__(self, places):
        self.places, self.calls = places, 0

    def run(self, q):
        self.calls += 1
        return self.places


def _place(name, kind, lat, lon, **extra):
    return {"type": "node", "id": hash(name) % 10**6, "lat": lat, "lon": lon, "tags": {"place": kind, "name": name, **extra}}


def test_names_put_zero_padded_distance_first_and_the_turning_place_after_it():
    from app import naming, polyline
    pts = [(53.20, -6.70), (53.22, -6.60), (53.25, -6.40), (53.22, -6.60), (53.20, -6.70)]    # out and back, turns at -6.40
    enc = polyline.encode(pts)
    ov = PlacesOverpass([_place("Hamlet Row", "hamlet", 53.2501, -6.4002), _place("Ballymore", "village", 53.2540, -6.4001),
                         _place("Nás na Ríogh", "town", 53.2575, -6.4000, **{"name:en": "Naas"})])
    assert naming.suggest(63_200, enc, "02.09.2017", ov) == "063 km · Naas"                      # a town beats a nearer village; English name wins
    assert naming.title_for(8_900, None, "Morning loop") == "009 km · Morning loop"              # nothing found: keep the old name
    assert naming.suggest(63_200, "", "x", PlacesOverpass([])) == "063 km · x"
    assert sorted(["120 km · A", "063 km · Z", "009 km · M"]) == ["009 km · M", "063 km · Z", "120 km · A"]


def test_an_overpass_failure_still_gives_a_distance_prefix_and_duplicates_stay_unique():
    from app import naming

    class Dead:
        def run(self, q):
            raise RuntimeError("busy")

    assert naming.suggest(20_000, "_p~iF~ps|U_ulLnnqC", "Old name", Dead()) == "020 km · Old name"
    names = naming.suggest_many([(20_000, "", "Loop"), (20_100, "", "Loop"), (35_000, "", "Hills")], Dead())
    assert names == ["020 km · Loop", "020 km · Loop (2)", "035 km · Hills"]


def test_tidy_renames_listings_and_untouched_imports_but_never_a_name_the_leader_typed(world):
    c, fake = world
    user = connect(c)
    c.get("/strava/routes")
    c.post("/strava/routes/101/import")
    route = Route.query.one()
    assert route.title == "Curragh loop"
    ov = PlacesOverpass([_place("Newbridge", "town", 0, 0)])
    from app import naming
    out = svc.tidy_names(user, type("O", (), {"run": lambda self, q: [_place("Newbridge", "town", 53.2, -6.8)], "workers": 1})())
    assert out["left"] == 0 and out["named"] >= 1
    db.session.refresh(route)
    assert " km · " in route.title and route.title[:3].isdigit()
    row = StravaRouteCache.query.filter_by(strava_route_id=101).one()
    assert row.tidy_name == route.title and row.name == "Curragh loop"
    # a leader's own rename survives a second tidy of a new listing
    route.title = "Sunday best"
    db.session.commit()
    row.tidy_name = None
    db.session.commit()
    svc.tidy_names(user, type("O", (), {"run": lambda self, q: [], "workers": 1})())
    db.session.refresh(route)
    assert route.title == "Sunday best"
