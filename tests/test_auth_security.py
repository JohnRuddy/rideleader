"""Sign-in links, CSRF, rate limiting, and who may see or change a route."""
import io
import re
from datetime import datetime, timedelta, timezone

import pytest

from app import create_app, db
from app.models import LoginToken, Route, User
from tests.conftest import hilly_points, make_gpx


def make_user(email="leader@example.com") -> User:
    u = User(email=email)
    db.session.add(u)
    db.session.commit()
    return u


def sign_in(c, user):
    with c.session_transaction() as s:
        s["uid"] = user.id


def other_client(c):
    return c.application.test_client()


def upload(c, name="Spin"):
    return c.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(hilly_points(), name=name)), "a.gpx")},
                  content_type="multipart/form-data")


def outbox(c):
    return c.application.config["MAILER"].outbox


def link_from_mail(c):
    return re.search(r"https?://\S+/login/\S+", outbox(c)[-1]["body"]).group(0)


# ---------------- magic link sign-in ----------------

def test_full_sign_in_flow_creates_the_user_and_signs_them_in(client):
    r = client.post("/login", data={"email": "New.Leader@Example.com"})
    assert r.status_code == 200 and b"Check your email" in r.data
    assert outbox(client)[-1]["to"] == "new.leader@example.com"
    link = link_from_mail(client)
    path = "/" + link.split("/", 3)[3]
    page = client.get(path)
    assert page.status_code == 200 and b"Finish signing in" in page.data
    with client.session_transaction() as s:
        assert "uid" not in s                                  # opening the link does NOT sign in (mail scanners open links)
    done = client.post(path)
    assert done.status_code == 302
    with client.session_transaction() as s:
        assert User.query.get(s["uid"]).email == "new.leader@example.com"
    assert b"new.leader@example.com" in client.get("/").data    # header shows who is signed in


def test_a_link_works_once_and_expires(client):
    client.post("/login", data={"email": "a@example.com"})
    path = "/" + link_from_mail(client).split("/", 3)[3]
    assert client.post(path).status_code == 302
    again = client.post(path)
    assert again.status_code == 400 and b"expired or was already used" in again.data
    client.post("/login", data={"email": "b@example.com"})
    path2 = "/" + link_from_mail(client).split("/", 3)[3]
    LoginToken.query.filter_by(email="b@example.com").update({"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)})
    db.session.commit()
    assert client.get(path2).status_code == 400 and client.post(path2).status_code == 400
    assert client.get("/login/not-a-real-token").status_code == 400


def test_only_a_hash_of_the_token_is_stored(client):
    client.post("/login", data={"email": "a@example.com"})
    token = link_from_mail(client).rsplit("/", 1)[1]
    assert LoginToken.query.filter_by(email="a@example.com").one().token_hash != token
    assert token not in str([r.token_hash for r in LoginToken.query.all()])


def test_unknown_and_known_addresses_get_the_same_answer(client):
    make_user("known@example.com")
    a = client.post("/login", data={"email": "known@example.com"})
    b = client.post("/login", data={"email": "stranger@example.com"})

    def norm(r):   # ignore the address itself and the (per-request) dev link
        return re.sub(r'href="[^"]*"', "", re.sub(r"\S+@example\.com", "X", r.data.decode()))
    assert a.status_code == b.status_code == 200 and norm(a) == norm(b)
    assert len(outbox(client)) == 2


@pytest.mark.parametrize("bad", ["", "nope", "a@b", "a b@example.com", "x" * 300 + "@example.com"])
def test_bad_email_addresses_are_refused(client, bad):
    assert client.post("/login", data={"email": bad}).status_code == 400 and not outbox(client)


def test_sign_in_requests_are_rate_limited(client):
    client.application.config["RATELIMIT_ENABLED"] = True
    codes = [client.post("/login", data={"email": "same@example.com"}).status_code for _ in range(7)]
    assert codes[:5] == [200] * 5 and codes[5:] == [429, 429]
    assert client.post("/login", data={"email": "other@example.com"}).status_code == 200      # per-address, not global
    assert len(outbox(client)) == 6


@pytest.mark.parametrize("target,expected", [("/account", "/account"), ("//evil.example/x", "/"), ("https://evil.example", "/"),
                                              ("/\\evil.example", "/"), ("", "/")])
def test_next_only_redirects_within_the_site(client, target, expected):
    client.post("/login", data={"email": "a@example.com", "next": target})
    path = "/" + link_from_mail(client).split("/", 3)[3].split("?")[0]
    r = client.post(path + "?next=" + target)
    assert r.status_code == 302 and r.headers["Location"].endswith(expected) and "evil" not in r.headers["Location"]


def test_logout_ends_the_session(client):
    sign_in(client, make_user())
    assert b"Sign out" in client.get("/").data
    client.post("/logout")
    assert b"Sign in" in client.get("/").data


# ---------------- CSRF ----------------

@pytest.fixture
def csrf_client(client):
    client.application.config["CSRF_ENABLED"] = True
    return client


def token_for(c):
    html = c.get("/").data.decode()
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def test_posts_without_a_csrf_token_are_refused(csrf_client):
    c = csrf_client
    assert c.post("/routes/upload", data={"gpx": (io.BytesIO(b"x"), "a.gpx")}, content_type="multipart/form-data").status_code == 403
    r = c.post("/api/routes/1/scan", json={})
    assert r.status_code == 403 and "session expired" in r.get_json()["error"]
    assert c.post("/logout").status_code == 403
    assert c.patch("/api/waypoints/1", json={}).status_code == 403 and c.delete("/api/waypoints/1").status_code == 403
    assert c.put("/api/routes/1/ride", json={}).status_code == 403


def test_a_wrong_token_is_refused_and_the_right_one_accepted(csrf_client):
    c = csrf_client
    tok = token_for(c)
    assert c.post("/logout", headers={"X-CSRF-Token": "nope"}).status_code == 403
    assert c.post("/logout", headers={"X-CSRF-Token": tok}).status_code in (302, 200)
    tok = token_for(c)
    assert c.post("/logout", data={"csrf_token": tok}).status_code in (302, 200)          # form field works too


def test_get_requests_never_need_a_token_and_the_page_embeds_one(csrf_client):
    html = csrf_client.get("/").data.decode()
    assert csrf_client.get("/privacy").status_code == 200
    assert 'name="csrf_token"' in html and 'meta name="csrf-token"' in html and "X-CSRF-Token" in html


def test_the_session_and_csrf_token_change_at_sign_in(csrf_client):
    c = csrf_client
    before = token_for(c)
    c.post("/login", data={"email": "a@example.com", "csrf_token": before})
    path = "/" + link_from_mail(c).split("/", 3)[3]
    c.post(path, data={"csrf_token": token_for(c)})
    assert token_for(c) != before                                                          # no session fixation


def test_the_webhook_is_exempt_from_csrf_because_strava_has_no_session(csrf_client):
    assert csrf_client.post("/strava/webhook", json={"object_type": "activity"}).status_code != 403


# ---------------- who can see and change a route ----------------

def test_signed_out_uploads_stay_open_like_before(client):
    upload(client)
    assert client.get("/routes/1").status_code == 200
    assert other_client(client).get("/routes/1").status_code == 200


def test_a_signed_in_upload_is_private_to_its_owner(client):
    me, you = make_user("me@example.com"), make_user("you@example.com")
    sign_in(client, me)
    upload(client)
    route = Route.query.one()
    assert route.owner_id == me.id and route.visibility == "private"
    assert client.get("/routes/1").status_code == 200 and client.get("/api/routes/1").status_code == 200
    stranger = other_client(client)
    sign_in(stranger, you)
    for url in ("/routes/1", "/api/routes/1", "/api/routes/1/waypoints", "/api/routes/1/climbs", "/api/routes/1/ride",
                "/api/routes/1/editor", "/routes/1/export", "/routes/1/export/gpx/download", "/api/routes/1/history"):
        assert stranger.get(url).status_code == 404, url
    assert other_client(client).get("/api/routes/1").status_code == 404                     # signed out too
    assert "Spin" not in stranger.get("/").data.decode() and "Spin" in client.get("/").data.decode()


def test_other_people_cannot_change_a_private_route_either(client):
    me, you = make_user("me@example.com"), make_user("you@example.com")
    sign_in(client, me)
    upload(client)
    stranger = other_client(client)
    sign_in(stranger, you)
    assert stranger.post("/routes/1/delete").status_code == 404
    assert stranger.post("/api/routes/1/edit", json={"op": "add_via", "lat": 53.1, "lon": -6.8}).status_code == 404
    assert stranger.put("/api/routes/1/ride", json={}).status_code == 404
    assert Route.query.count() == 1


def test_a_shared_route_is_visible_to_all_but_only_changeable_by_its_owner(client):
    me, you = make_user("me@example.com"), make_user("you@example.com")
    sign_in(client, me)
    upload(client)
    Route.query.update({"visibility": "club"})
    db.session.commit()
    stranger = other_client(client)
    sign_in(stranger, you)
    assert stranger.get("/routes/1").status_code == 200 and other_client(client).get("/api/routes/1").status_code == 200
    r = stranger.post("/routes/1/delete")
    assert r.status_code == 403 and Route.query.count() == 1
    assert stranger.post("/api/routes/1/edit", json={"op": "add_via", "lat": 53.1, "lon": -6.8}).status_code == 403
    assert client.post("/routes/1/delete").status_code == 302 and Route.query.count() == 0   # the owner can


def test_waypoint_climb_and_scan_endpoints_are_protected_through_their_route(client):
    from geoalchemy2.elements import WKTElement
    from app.models import Climb, ScanJob, Waypoint
    me, you = make_user("me@example.com"), make_user("you@example.com")
    sign_in(client, me)
    upload(client)
    db.session.add_all([
        ScanJob(route_id=1, route_version=1, status="done"),
        Waypoint(route_id=1, name="Tap", kind="water", s_m=100, geom=WKTElement("POINT(-6.9 53.16)", srid=4326)),
        Climb(route_id=1, s_start_m=0, s_end_m=1000, length_m=1000, gain_m=60, avg_grade=6, max_grade=8, score=6000)])
    db.session.commit()
    wp, cl = Waypoint.query.one(), Climb.query.one()
    stranger = other_client(client)
    sign_in(stranger, you)
    assert stranger.get("/api/scans/1").status_code == 404
    assert stranger.patch(f"/api/waypoints/{wp.id}", json={"included": True}).status_code == 404
    assert stranger.delete(f"/api/waypoints/{wp.id}").status_code == 404
    assert stranger.patch(f"/api/climbs/{cl.id}", json={"regroup_mode": "off"}).status_code == 404
    assert client.patch(f"/api/waypoints/{wp.id}", json={"included": True}).status_code == 200      # the owner still can


# ---------------- production safety ----------------

def test_production_refuses_to_start_without_real_secrets(monkeypatch):
    for k in ("SECRET_KEY", "TOKEN_ENCRYPTION_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app({"RLK_ENV": "production"})
    monkeypatch.setenv("SECRET_KEY", "x" * 40)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "k")
    with pytest.raises(RuntimeError, match="secure cookies"):
        create_app({"RLK_ENV": "production", "SESSION_COOKIE_SECURE": False})
