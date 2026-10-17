"""Clubs, invite codes, shared data and sharing Strava-derived routes."""
import io
import re
from datetime import datetime, timedelta, timezone

import pytest

from app import clubs, db
from app.models import Closure, Club, ClubStop, Invite, LoginToken, Membership, Route, User
from tests.conftest import hilly_points, make_gpx
from tests.test_auth_security import link_from_mail, other_client, outbox, sign_in


@pytest.fixture
def invited(client):
    """Invite codes switched on, as in production."""
    client.application.config["INVITE_REQUIRED"] = True
    return client


def user(email, role="leader"):
    u = User(email=email, role=role)
    db.session.add(u)
    db.session.commit()
    return u


def member(u, club_id, role="member"):
    db.session.add(Membership(user_id=u.id, club_id=club_id, role=role))
    db.session.commit()


def new_club(name):
    c = Club(name=name, settings={})
    db.session.add(c)
    db.session.commit()
    return c


def request_link(c, email, code=None, **extra):
    return c.post("/login", data={"email": email, "code": code or "", **extra})


def sign_in_with_mail(c):
    link = link_from_mail(c)
    token = link.rstrip("/").split("/login/")[1].split("?")[0]
    return c.post(f"/login/{token}")


def upload(c, name="Spin"):
    return c.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(hilly_points(), name=name)), "a.gpx")},
                  content_type="multipart/form-data")


# ---------------- the sample club ----------------

def test_there_is_a_sample_club_called_velo_club_by_default(client):
    assert [c.name for c in Club.query.all()] == ["Velo Club"]


def test_the_platform_admin_is_an_admin_of_the_sample_club_after_the_migration(client):
    from app import clubs as cl
    admin = user("boss@example.com", "admin")
    club = Club.query.first()
    club.settings = {}                                # as if the migration had not run yet
    db.session.commit()
    cl.seed_default_club()
    assert Membership.query.filter_by(user_id=admin.id, club_id=club.id, role="admin").count() == 1


def test_existing_accounts_are_kept_in_by_the_migration_as_members_of_the_sample_club(client):
    from app import clubs as cl
    boss, old = user("boss@example.com", "admin"), user("old@example.com")
    club = Club.query.first()
    club.settings = {}
    Membership.query.delete()
    db.session.commit()
    cl.seed_default_club()
    assert Membership.query.filter_by(user_id=old.id, club_id=club.id, role="member").count() == 1
    assert Membership.query.filter_by(user_id=boss.id, club_id=club.id, role="admin").count() == 1
    cl.seed_default_club()                                      # runs at every start: it must not repeat or duplicate
    assert Membership.query.count() == 2


# ---------------- invite codes: creating, using, limits ----------------

def test_a_code_is_shown_once_and_only_its_hash_is_stored(client):
    club = Club.query.first()
    inv, code = clubs.create_invite(club.id, None)
    db.session.commit()
    assert re.fullmatch(r"[A-Z2-9]{5}-[A-Z2-9]{5}", code)
    assert code not in (inv.code_hash, inv.hint) and inv.code_hash == clubs.hash_code(code) and inv.hint == code[-3:]


def test_codes_work_in_any_case_with_or_without_the_dash(client):
    club = Club.query.first()
    inv, code = clubs.create_invite(club.id, None)
    db.session.commit()
    assert clubs.lookup_invite(code.lower()) is inv
    assert clubs.lookup_invite(code.replace("-", " ")) is inv
    assert clubs.lookup_invite("AAAAA-BBBBB") is None and clubs.lookup_invite("") is None


@pytest.mark.parametrize("break_it", ["expired", "revoked", "used_up"])
def test_expired_revoked_and_used_up_codes_are_refused(client, break_it):
    club = Club.query.first()
    inv, code = clubs.create_invite(club.id, None, max_uses=1)
    if break_it == "expired":
        inv.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    elif break_it == "revoked":
        clubs.revoke(inv)
    else:
        inv.uses = 1
    db.session.commit()
    assert clubs.lookup_invite(code) is None


def test_a_code_made_for_one_email_only_works_for_that_email(client):
    club = Club.query.first()
    _, code = clubs.create_invite(club.id, None, email="Rider@Example.com")
    db.session.commit()
    assert clubs.lookup_invite(code, "rider@example.com") is not None
    assert clubs.lookup_invite(code, "someone.else@example.com") is None


def test_code_settings_are_validated(client):
    club = Club.query.first()
    for bad in ({"days": 0}, {"days": 400}, {"max_uses": 0}, {"role": "owner"}):
        with pytest.raises(ValueError):
            clubs.create_invite(club.id, None, **bad)


# ---------------- signing in ----------------

def test_a_new_person_needs_a_code_and_a_valid_one_makes_them_a_member(invited):
    club = Club.query.first()
    _, code = clubs.create_invite(club.id, None)
    db.session.commit()
    boss = user("boss@example.com", "admin")
    # no code, unknown email: the same page as anyone, and nothing is sent
    r = request_link(invited, "newbie@example.com")
    assert r.status_code == 200 and b"Check your email" in r.data and outbox(invited) == []
    # a wrong code is refused out loud
    r = request_link(invited, "newbie@example.com", "AAAAA-BBBBB")
    assert r.status_code == 400 and b"isn&#39;t valid" in r.data and outbox(invited) == []
    # the right code sends the link; using the link joins the club and signs in
    assert request_link(invited, "newbie@example.com", code).status_code == 200
    assert sign_in_with_mail(invited).status_code == 302
    u = User.query.filter_by(email="newbie@example.com").one()
    assert u.role == "leader" and Membership.query.filter_by(user_id=u.id, club_id=club.id, role="member").count() == 1
    assert Invite.query.one().uses == 1
    assert b"Sign out" in invited.get("/").data


def test_after_joining_a_member_signs_in_with_just_their_email(invited):
    club = Club.query.first()
    _, code = clubs.create_invite(club.id, None)
    db.session.commit()
    request_link(invited, "newbie@example.com", code)
    sign_in_with_mail(invited)
    again = other_client(invited)
    r = request_link(again, "newbie@example.com")
    assert r.status_code == 200 and again.application.config["MAILER"].outbox, "a returning member needs no code"


def test_the_platform_admin_signs_in_without_a_code(invited):
    user("boss@example.com", "admin")
    request_link(invited, "boss@example.com")
    assert outbox(invited) and sign_in_with_mail(invited).status_code == 302


def test_an_admin_code_makes_a_club_admin(invited):
    user("boss@example.com", "admin")
    club = Club.query.first()
    _, code = clubs.create_invite(club.id, None, role="admin")
    db.session.commit()
    request_link(invited, "lead@example.com", code)
    sign_in_with_mail(invited)
    u = User.query.filter_by(email="lead@example.com").one()
    assert clubs.is_club_admin(u, club.id) and not clubs.is_platform_admin(u)


def test_a_code_that_runs_out_between_asking_and_clicking_is_refused(invited):
    club = Club.query.first()
    inv, code = clubs.create_invite(club.id, None, max_uses=1)
    db.session.commit()
    request_link(invited, "a@example.com", code)
    link = link_from_mail(invited)
    inv.uses = 1                                      # somebody else used the last place first
    db.session.commit()
    token = link.rstrip("/").split("/login/")[1].split("?")[0]
    r = invited.post(f"/login/{token}")
    assert r.status_code == 400 and not User.query.filter_by(email="a@example.com").count()


def test_using_a_code_again_does_not_use_up_a_place(invited):
    club = Club.query.first()
    inv, code = clubs.create_invite(club.id, None)
    u = user("m@example.com")
    member(u, club.id)
    db.session.commit()
    clubs.redeem(u, inv)
    assert inv.uses == 0


def test_failed_code_guessing_is_throttled(invited):
    invited.application.config["RATELIMIT_ENABLED"] = True
    statuses = [request_link(invited, f"x{i}@example.com", f"AAAAA-BBBB{i}").status_code for i in range(15)]
    assert 429 in statuses


def test_someone_removed_from_every_club_is_signed_out_on_their_next_request(invited):
    club = Club.query.first()
    u = user("gone@example.com")
    member(u, club.id)
    other = user("boss@example.com", "admin")
    sign_in(invited, u)
    assert b"Sign out" in invited.get("/").data
    Membership.query.filter_by(user_id=u.id).delete()
    db.session.commit()
    assert b"Sign out" not in invited.get("/").data


# ---------------- who may manage what ----------------

def test_only_the_platform_admin_can_create_a_club(client):
    boss, lead = user("boss@example.com", "admin"), user("lead@example.com")
    member(lead, Club.query.first().id, "admin")
    c2 = other_client(client)
    sign_in(c2, lead)
    assert c2.post("/clubs", data={"name": "Kildare Wheelers"}).status_code == 403
    sign_in(client, boss)
    r = client.post("/clubs", data={"name": "Kildare Wheelers"})
    assert r.status_code == 302
    made = Club.query.filter_by(name="Kildare Wheelers").one()
    assert Membership.query.filter_by(user_id=boss.id, club_id=made.id, role="admin").count() == 1
    assert client.post("/clubs", data={"name": "kildare wheelers"}).status_code == 302
    assert Club.query.filter_by(name="Kildare Wheelers").count() == 1                 # names are unique


def test_a_club_admin_can_invite_and_a_plain_member_cannot(client):
    club = Club.query.first()
    admin, plain = user("a@example.com"), user("p@example.com")
    member(admin, club.id, "admin")
    member(plain, club.id)
    sign_in(client, admin)
    r = client.post(f"/clubs/{club.id}/invites", data={"role": "member", "days": "30", "label": "Spring"}, follow_redirects=True)
    assert r.status_code == 200 and re.search(rb"[A-Z2-9]{5}-[A-Z2-9]{5}", r.data)
    assert Invite.query.filter_by(club_id=club.id).count() == 1
    c2 = other_client(client)
    sign_in(c2, plain)
    assert c2.post(f"/clubs/{club.id}/invites", data={"role": "member", "days": "30"}).status_code == 403


def test_the_new_code_is_shown_once_and_not_again(client):
    club = Club.query.first()
    admin = user("a@example.com")
    member(admin, club.id, "admin")
    sign_in(client, admin)
    first = client.post(f"/clubs/{club.id}/invites", data={"days": "30"}, follow_redirects=True).data
    code = re.search(rb"[A-Z2-9]{5}-[A-Z2-9]{5}", first).group(0)
    assert code in first
    assert code not in client.get(f"/clubs/{club.id}").data


def test_people_outside_a_club_cannot_see_its_page(client):
    club = new_club("Other Club")
    stranger = user("s@example.com")
    member(stranger, Club.query.first().id)
    sign_in(client, stranger)
    assert client.get(f"/clubs/{club.id}").status_code == 404
    assert client.post(f"/clubs/{club.id}/invites", data={"days": "30"}).status_code == 404


def test_a_club_always_keeps_one_admin(client):
    club = Club.query.first()
    only = user("only@example.com")
    member(only, club.id, "admin")
    sign_in(client, only)
    client.post(f"/clubs/{club.id}/members/{only.id}/role", data={"role": "member"})
    client.post(f"/clubs/{club.id}/members/{only.id}/remove")
    assert Membership.query.filter_by(user_id=only.id, club_id=club.id, role="admin").count() == 1
    other = user("two@example.com")
    member(other, club.id)
    client.post(f"/clubs/{club.id}/members/{other.id}/role", data={"role": "admin"})
    client.post(f"/clubs/{club.id}/members/{only.id}/role", data={"role": "member"})
    assert Membership.query.filter_by(user_id=only.id, club_id=club.id, role="member").count() == 1


def test_removing_a_member_cuts_their_access(client):
    club = Club.query.first()
    admin, gone = user("a@example.com"), user("g@example.com")
    member(admin, club.id, "admin")
    member(gone, club.id)
    sign_in(client, admin)
    assert client.post(f"/clubs/{club.id}/members/{gone.id}/remove").status_code == 302
    assert Membership.query.filter_by(user_id=gone.id).count() == 0


def test_revoking_a_code_stops_it_working(client):
    club = Club.query.first()
    admin = user("a@example.com")
    member(admin, club.id, "admin")
    inv, code = clubs.create_invite(club.id, admin.id)
    db.session.commit()
    sign_in(client, admin)
    assert client.post(f"/clubs/{club.id}/invites/{inv.id}/revoke").status_code == 302
    assert clubs.lookup_invite(code) is None


def test_deleting_a_club_needs_the_site_admin_and_its_name(client):
    boss, lead = user("boss@example.com", "admin"), user("lead@example.com")
    club = new_club("Doomed")
    member(lead, club.id, "admin")
    c2 = other_client(client)
    sign_in(c2, lead)
    assert c2.post(f"/clubs/{club.id}/delete", data={"confirm_name": "Doomed"}).status_code == 403
    sign_in(client, boss)
    client.post(f"/clubs/{club.id}/delete", data={"confirm_name": "nope"})
    assert db.session.get(Club, club.id) is not None
    client.post(f"/clubs/{club.id}/delete", data={"confirm_name": "Doomed"})
    db.session.expire_all()
    assert db.session.get(Club, club.id) is None


# ---------------- sharing within a club ----------------

def test_routes_are_shared_with_the_club_and_nobody_else(client):
    a, b, outsider = user("a@example.com"), user("b@example.com"), user("o@example.com")
    velo, other = Club.query.first(), new_club("Other Club")
    member(a, velo.id)
    member(b, velo.id)
    member(outsider, other.id)
    sign_in(client, a)
    upload(client, "Sunday spin")
    route = Route.query.one()
    assert route.club_id == velo.id and route.owner_id == a.id and route.visibility == "club"
    cb, co = other_client(client), other_client(client)
    sign_in(cb, b)
    sign_in(co, outsider)
    assert b"Sunday spin" in cb.get("/").data and cb.get(f"/routes/{route.id}").status_code == 200
    assert b"Sunday spin" not in co.get("/").data and co.get(f"/routes/{route.id}").status_code == 404
    assert cb.delete(f"/api/routes/{route.id}/share").status_code in (204, 403), "a member can see but not change it"
    assert cb.post(f"/api/routes/{route.id}/delete").status_code != 200


def test_a_private_route_stays_with_its_owner(client):
    a, b = user("a@example.com"), user("b@example.com")
    velo = Club.query.first()
    member(a, velo.id)
    member(b, velo.id)
    sign_in(client, a)
    upload(client, "Secret spin")
    Route.query.one().visibility = "private"
    db.session.commit()
    cb = other_client(client)
    sign_in(cb, b)
    assert b"Secret spin" not in cb.get("/").data and cb.get(f"/routes/{Route.query.one().id}").status_code == 404


def test_stops_are_shared_inside_a_club_and_hidden_from_others(client):
    a, b, outsider = user("a@example.com"), user("b@example.com"), user("o@example.com")
    velo, other = Club.query.first(), new_club("Other Club")
    member(a, velo.id)
    member(b, velo.id)
    member(outsider, other.id)
    sign_in(client, a)
    r = client.post("/api/club/stops", json={"name": "Velo cafe", "kind": "cafe", "lat": 53.16, "lon": -6.9})
    assert r.status_code == 201 and ClubStop.query.one().club_id == velo.id
    cb, co = other_client(client), other_client(client)
    sign_in(cb, b)
    sign_in(co, outsider)
    assert [s["name"] for s in cb.get("/api/club/stops").get_json()] == ["Velo cafe"]
    assert co.get("/api/club/stops").get_json() == []
    stop_id = ClubStop.query.one().id
    assert co.post(f"/api/club/stops/{stop_id}/verify").status_code == 404
    assert co.delete(f"/api/club/stops/{stop_id}").status_code == 404


def test_a_club_admin_can_remove_a_stop_someone_else_added(client):
    velo = Club.query.first()
    admin, adder = user("a@example.com"), user("b@example.com")
    member(admin, velo.id, "admin")
    member(adder, velo.id)
    ca = other_client(client)
    sign_in(ca, adder)
    ca.post("/api/club/stops", json={"name": "Cafe", "kind": "cafe", "lat": 53.16, "lon": -6.9})
    sign_in(client, admin)
    assert client.delete(f"/api/club/stops/{ClubStop.query.one().id}").status_code == 204


def test_closures_are_per_club(client):
    a, outsider = user("a@example.com"), user("o@example.com")
    velo, other = Club.query.first(), new_club("Other Club")
    member(a, velo.id)
    member(outsider, other.id)
    sign_in(client, a)
    now = datetime.now(timezone.utc)
    r = client.post("/api/closures", json={"lat": 53.16, "lon": -6.9, "radius_m": 100, "description": "Bridge out",
                                           "starts_at": now.isoformat(), "ends_at": (now + timedelta(days=3)).isoformat()})
    assert r.status_code == 201 and Closure.query.one().club_id == velo.id
    co = other_client(client)
    sign_in(co, outsider)
    assert co.get("/api/closures").get_json() == [] and len(client.get("/api/closures").get_json()) == 1


def test_club_settings_belong_to_the_club_and_its_admin_can_change_them(client):
    velo = Club.query.first()
    admin, plain = user("a@example.com"), user("p@example.com")
    member(admin, velo.id, "admin")
    member(plain, velo.id)
    sign_in(client, admin)
    assert client.put("/api/club", json={"gust_kmh": 40}).status_code == 200
    assert client.get("/api/club").get_json()["settings"]["gust_kmh"] == 40
    assert not any(k.startswith("_") for k in client.get("/api/club").get_json()["settings"])
    cp = other_client(client)
    sign_in(cp, plain)
    assert cp.put("/api/club", json={"gust_kmh": 90}).status_code == 403


def test_the_switcher_only_offers_clubs_you_belong_to(client):
    a = user("a@example.com")
    velo, other = Club.query.first(), new_club("Other Club")
    member(a, velo.id)
    sign_in(client, a)
    client.post("/clubs/switch", data={"club_id": other.id})
    with client.session_transaction() as s:
        assert s.get("club_id") != other.id
    assert b"Other Club" not in client.get("/clubs").data


# ---------------- the share link ----------------

def test_sharing_a_strava_route_makes_a_processed_copy_after_confirmation(client):
    from app.models import Ride
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    upload(client, "Naas loop")
    src = Route.query.one()
    src.source, src.strava_route_id = "strava", 4242
    src.strava_expires_at = datetime.now(timezone.utc) + timedelta(days=5)
    db.session.commit()
    r = client.post(f"/api/routes/{src.id}/share")
    assert r.status_code == 409 and r.get_json()["needs_confirm"] and "my own route" in r.get_json()["consent_text"]
    assert Route.query.count() == 1
    r = client.post(f"/api/routes/{src.id}/share", json={"confirm_own": True})
    assert r.status_code == 200
    data = r.get_json()
    assert data["copied"] and data["route_id"] != src.id
    copy = db.session.get(Route, data["route_id"])
    assert copy.source == "gpx" and copy.strava_route_id is None and copy.strava_expires_at is None   # detached: no 7-day purge
    assert copy.club_id == src.club_id and copy.owner_id == a.id and copy.ride.share_token == data["token"]
    # the link opens for anyone, signed in or not, and the original Strava import is still never shown
    anon = other_client(client)
    assert anon.get(f"/r/{data['token']}").status_code == 200
    assert anon.get(f"/routes/{src.id}").status_code == 404


def test_the_ride_plan_and_notes_travel_with_the_copy(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    upload(client, "Naas loop")
    src = Route.query.one()
    client.put(f"/api/routes/{src.id}/ride", json={"date": "2026-11-01", "start_time": "09:30", "meeting_name": "Naas Harbour",
                                                    "notes": "Mudguards on please", "groups": [{"name": "A", "speed_kmh": 27, "stop_min": 15}]})
    src = db.session.get(Route, src.id)
    src.source, src.strava_route_id = "strava", 4242
    src.strava_expires_at = datetime.now(timezone.utc) + timedelta(days=5)
    db.session.commit()
    data = client.post(f"/api/routes/{src.id}/share", json={"confirm_own": True}).get_json()
    copy = db.session.get(Route, data["route_id"])
    assert copy.ride.notes == "Mudguards on please" and copy.ride.meeting_name == "Naas Harbour"
    page = other_client(client).get(f"/r/{data['token']}").data.decode()
    assert "Mudguards on please" in page


def test_a_share_link_works_for_anyone_who_has_it(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    upload(client, "Open spin")
    rid = Route.query.one().id
    token = client.post(f"/api/routes/{rid}/share").get_json()["token"]
    assert other_client(client).get(f"/r/{token}").status_code == 200
    assert other_client(client).get("/r/not-a-real-token-123456").status_code == 404
    client.delete(f"/api/routes/{rid}/share")
    assert other_client(client).get(f"/r/{token}").status_code == 404                 # the leader can still switch it off


def test_the_club_admin_command_prints_a_working_code(client):
    runner = client.application.test_cli_runner()
    r = runner.invoke(args=["club-invite", "Velo Club", "--role", "admin"])
    assert r.exit_code == 0
    code = re.search(r"[A-Z2-9]{5}-[A-Z2-9]{5}", r.output).group(0)
    assert clubs.lookup_invite(code).role == "admin"


# ---------------- production behaviour of the product page ----------------

def test_the_landing_page_says_how_to_get_in(client):
    client.application.config["RLK_ENV"] = "production"
    page = client.get("/").data.decode()
    assert "invite code" in page.lower()
    assert "Plan the ride." in page


def test_a_leader_can_switch_a_route_between_club_and_private(client):
    a, b = user("a@example.com"), user("b@example.com")
    velo = Club.query.first()
    member(a, velo.id)
    member(b, velo.id)
    sign_in(client, a)
    upload(client, "Switch spin")
    rid = Route.query.one().id
    cb = other_client(client)
    sign_in(cb, b)
    assert cb.get(f"/routes/{rid}").status_code == 200                     # club by default once you belong to one
    assert client.put(f"/api/routes/{rid}/visibility", json={"visibility": "private"}).status_code == 200
    assert cb.get(f"/routes/{rid}").status_code == 404
    assert client.put(f"/api/routes/{rid}/visibility", json={"visibility": "club"}).status_code == 200
    assert cb.get(f"/routes/{rid}").status_code == 200
    assert cb.put(f"/api/routes/{rid}/visibility", json={"visibility": "private"}).status_code == 403    # not theirs
    assert client.put(f"/api/routes/{rid}/visibility", json={"visibility": "nonsense"}).status_code == 400


def test_a_strava_import_cannot_be_made_club_visible(client):
    a = user("a@example.com")
    member(a, Club.query.first().id)
    sign_in(client, a)
    upload(client, "From strava")
    r = Route.query.one()
    r.source, r.strava_route_id, r.visibility = "strava", 7, "private"
    r.strava_expires_at = datetime.now(timezone.utc) + timedelta(days=3)
    db.session.commit()
    assert client.put(f"/api/routes/{r.id}/visibility", json={"visibility": "club"}).status_code == 409
