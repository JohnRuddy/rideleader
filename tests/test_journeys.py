"""The key journeys, end to end, plus the plain-language guide and the phone basics every page must have."""
import io
import re
from datetime import datetime, timedelta, timezone

import pytest

from app import clubs, db
from app.models import Club, Membership, Route, User
from tests.conftest import hilly_points, make_gpx
from tests.test_auth_security import link_from_mail, other_client, outbox, sign_in


def user(email, role="leader"):
    u = User(email=email, role=role)
    db.session.add(u)
    db.session.commit()
    return u


def member(u, club_id, role="member"):
    db.session.add(Membership(user_id=u.id, club_id=club_id, role=role))
    db.session.commit()


def upload(c, name="Sunday spin"):
    return c.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(hilly_points(), name=name)), "a.gpx")},
                  content_type="multipart/form-data")


# ---------------- the guide ----------------

def test_the_guide_is_public_and_explains_the_workings(client):
    r = client.get("/how-it-works")
    assert r.status_code == 200
    page = r.data.decode()
    for needle in ("A ride in four steps", "What the checks mean", "Pace groups and arrival times", "Clubs and invite codes",
                   "Strava", "invite code", "average moving speed", "never calls a ride", "112 or 999"):
        assert needle in page, needle


def test_the_guide_quotes_the_real_default_limits(client):
    from app import club
    page = client.get("/how-it-works").data.decode()
    d = club.DEFAULT_SETTINGS
    assert f"{d['gust_kmh']:g} km/h" in page and f"{d['ice_c']:g}" in page and f"{d['lights_min_before_sunset']} minutes" in page


def test_the_guide_is_linked_from_every_kind_of_page(client):
    assert 'href="/how-it-works"' in client.get("/welcome").data.decode()
    assert 'href="/how-it-works"' in client.get("/login").data.decode()
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    assert 'href="/how-it-works"' in client.get("/").data.decode()


# ---------------- phone basics on every kind of page ----------------

@pytest.mark.parametrize("path", ["/welcome", "/login", "/how-it-works", "/privacy"])
def test_public_pages_have_the_phone_basics(client, path):
    html = client.get(path).data.decode()
    assert 'name="viewport" content="width=device-width, initial-scale=1' in html
    assert '<html lang="en">' in html
    assert 'class="skip"' in html and 'id="main"' in html                      # keyboard users can skip the menu
    assert html.count("<h1") == 1                                              # one main heading per page
    for img in re.findall(r"<img\b[^>]*>", html):
        assert 'alt="' in img, img                                             # every image says what it is


def test_the_menu_collapses_for_signed_in_people_and_marks_the_current_page(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    html = client.get("/").data.decode()
    assert '<details class="menu"' in html and "Sign out" in html
    assert re.search(r'<a href="/"[^>]*aria-current="page"[^>]*>My routes</a>', html)
    assert "Velo Club" in html                                                  # the club you are acting in


def test_the_public_ride_sheet_has_plain_links_and_no_menu_script(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    upload(client)
    token = client.post(f"/api/routes/{Route.query.one().id}/share").get_json()["token"]
    html = other_client(client).get(f"/r/{token}").data.decode()
    assert '<details class="menu"' not in html and 'href="/how-it-works"' in html


# ---------------- journey: a new member joins ----------------

def test_a_new_member_sees_which_club_they_are_joining_and_then_a_welcome(client):
    client.application.config["INVITE_REQUIRED"] = True
    user("boss@example.com", "admin")
    club = Club.query.first()
    _, code = clubs.create_invite(club.id, None)
    db.session.commit()
    client.post("/login", data={"email": "new@example.com", "code": code})
    link = link_from_mail(client)
    page = client.get(link).data.decode()
    assert "Join Velo Club" in page and "new@example.com" in page and "Join and sign in" in page
    token = link.rstrip("/").split("/login/")[1].split("?")[0]
    assert client.post(f"/login/{token}").status_code == 302
    home = client.get("/").data.decode()
    assert "Welcome to Velo Club" in home and "Get your first ride ready" in home
    assert "Bring a route" in home and 'href="/how-it-works"' in home


def test_the_welcome_goes_away_once_there_is_a_route(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    assert "Get your first ride ready" in client.get("/").data.decode()
    upload(client)
    assert "Get your first ride ready" not in client.get("/").data.decode()


def test_the_login_page_asks_for_the_invite_code_in_plain_words(client):
    page = client.get("/login?code=ABCDE-FGH23").data.decode()
    assert "Invite code" in page and 'value="ABCDE-FGH23"' in page and "First time here?" in page


# ---------------- journey: a leader plans and shares ----------------

def test_the_route_page_says_what_to_do_next(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    upload(client)
    rid = Route.query.one().id
    page = client.get(f"/routes/{rid}").data.decode()
    assert 'class="next-step"' in page and "Find stops" in page and 'data-goto="stops"' in page
    # once stops have been found the prompt moves on to sharing
    from app.models import Waypoint
    from geoalchemy2.elements import WKTElement
    db.session.add(Waypoint(route_id=rid, kind="cafe", name="Cafe", s_m=1000, origin="osm", geom=WKTElement("POINT(-6.9 53.16)", srid=4326)))
    db.session.commit()
    page = client.get(f"/routes/{rid}").data.decode()
    assert 'data-goto="share"' in page and "Share it" in page
    client.post(f"/api/routes/{rid}/share")
    assert 'class="next-step"' not in client.get(f"/routes/{rid}").data.decode()


def test_other_members_are_not_nagged_with_the_leaders_next_step(client):
    a, b = user("a@example.com"), user("b@example.com")
    for u in (a, b):
        member(u, Club.query.first().id)
    sign_in(client, a)
    upload(client)
    cb = other_client(client)
    sign_in(cb, b)
    assert 'class="next-step"' not in cb.get(f"/routes/{Route.query.one().id}").data.decode()


def test_the_phone_share_button_is_on_the_route_page_and_the_ride_sheet(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    upload(client)
    rid = Route.query.one().id
    assert 'id="share-native"' in client.get(f"/routes/{rid}").data.decode()
    token = client.post(f"/api/routes/{rid}/share").get_json()["token"]
    sheet = other_client(client).get(f"/r/{token}").data.decode()
    assert 'id="share-native"' in sheet and "navigator.share" in sheet


# ---------------- journey: a rider opens the link on a phone ----------------

def test_the_ride_sheet_shows_pace_groups_in_a_speed_roundel(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    upload(client)
    token = client.post(f"/api/routes/{Route.query.one().id}/share").get_json()["token"]
    html = other_client(client).get(f"/r/{token}").data.decode()
    assert html.count('class="roundel sm"') == 3 and 'aria-label="Group A, 28 kilometres an hour"' in html


def test_a_rider_can_add_the_ride_to_their_calendar_without_signing_in(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    upload(client, "Naas, loop; with commas")
    rid = Route.query.one().id
    client.put(f"/api/routes/{rid}/ride", json={"date": "2026-11-01", "start_time": "09:30", "meeting_name": "Naas Harbour",
                                                 "notes": "Mudguards on, please", "groups": [{"name": "A", "speed_kmh": 27, "stop_min": 15}]})
    token = client.post(f"/api/routes/{rid}/share").get_json()["token"]
    r = other_client(client).get(f"/r/{token}/ride.ics")
    assert r.status_code == 200 and r.mimetype == "text/calendar" and "attachment" in r.headers["Content-Disposition"]
    body = r.data.decode()
    assert body.startswith("BEGIN:VCALENDAR\r\n") and body.rstrip().endswith("END:VCALENDAR")
    assert "SUMMARY:Naas\\, loop\\; with commas" in body and "LOCATION:Naas Harbour" in body
    assert "Mudguards on\\, please" in body and f"/r/{token}" in body
    assert "DTSTART:20261101T093000Z" in body                                  # 09:30 in Dublin in November is UTC
    start = re.search(r"DTSTART:(\d{8}T\d{6}Z)", body).group(1)
    end = re.search(r"DTEND:(\d{8}T\d{6}Z)", body).group(1)
    assert end > start
    assert other_client(client).get("/r/not-a-real-token-123456/ride.ics").status_code == 404


def test_the_ride_sheet_links_to_the_calendar_file(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    upload(client)
    token = client.post(f"/api/routes/{Route.query.one().id}/share").get_json()["token"]
    assert f'href="/r/{token}/ride.ics"' in other_client(client).get(f"/r/{token}").data.decode()


# ---------------- journey: a club admin invites people ----------------

def test_an_admin_can_send_the_invite_on_whatsapp_straight_from_the_new_code(client):
    club = Club.query.first()
    admin = user("a@example.com")
    member(admin, club.id, "admin")
    sign_in(client, admin)
    page = client.post(f"/clubs/{club.id}/invites", data={"days": "30"}, follow_redirects=True).data.decode()
    assert "https://wa.me/?text=" in page and "Send on WhatsApp" in page and 'id="invite-native"' in page
    code = re.search(r"[A-Z2-9]{5}-[A-Z2-9]{5}", page).group(0)
    assert code.replace("-", "") in page.replace("-", "").replace("%2D", "") or code in page


# ---------------- journey: the site admin sets up a club ----------------

def test_the_site_admin_sets_up_a_club_and_is_taken_straight_to_inviting(client):
    boss = user("boss@example.com", "admin")
    sign_in(client, boss)
    r = client.post("/clubs", data={"name": "Kildare Wheelers"}, follow_redirects=True)
    page = r.data.decode()
    assert "Kildare Wheelers is set up" in page and "Invite people" in page and "Make an invite code" in page
