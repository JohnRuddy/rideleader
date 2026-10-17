"""Sharing a ride: the public ride sheet, the WhatsApp text, the cue card PDF, and the guarantees around them."""
import io
import re
from datetime import datetime, timedelta, timezone

import pytest
from geoalchemy2.elements import WKTElement
from pypdf import PdfReader

from app import db
from app import hours as hrs
from app import ride_sheet as rs
from app import weather as w
from app.models import Closure, Ride, Route, Waypoint
from tests.conftest import make_gpx
from tests.test_auth_security import make_user, other_client, sign_in
from tests.test_climbs_http import climbing_points
from tests.test_conditions_http import FakeWeather, NOW
from tests.test_editor import FakeRouter
from tests.test_strava_flow import FakeStrava, connect
from tests.test_warnings import Feed, item

UTC = timezone.utc
SUNDAY = "2026-10-11"


class FakeHours:
    def __init__(self, fail=False):
        self.fail = fail

    def evaluate(self, items):
        if self.fail:
            raise hrs.HoursUnavailable("hours service is down")
        return [{"id": it["id"], "warnings": [], "day": [["2026-10-11T07:00:00Z", "2026-10-11T16:00:00Z", False, None]],
                 "arrivals": [{"at": a, "open": True, "unknown": False, "comment": None, "next_change": "2026-10-11T16:00:00Z",
                               "next_change_open": False, "next_open": None} for a in it["arrivals"]]} for it in items]


def add_wp(kind, name, s, offset=30.0, included=True, origin="osm", tags=None, side="L", note=None, planned=False, **kw):
    lon = -6.9 + s / 100.2 * 0.0015
    wp = Waypoint(route_id=1, kind=kind, name=name, s_m=s, offset_m=offset, side=side, included=included, origin=origin, tags=tags or {},
                  note=note, planned_stop=planned, geom=WKTElement(f"POINT({lon} 53.1603)", srid=4326), **kw)
    db.session.add(wp)
    db.session.commit()
    return wp


@pytest.fixture
def sw(client):
    cfg = client.application.config
    cfg.update(CLOCK=lambda: NOW, WEATHER_CLIENT=FakeWeather(), WARNINGS_CLIENT=Feed([]), CLOSURE_PROVIDERS=[],
               HOURS_CLIENT=FakeHours(), ROUTING_CLIENT=FakeRouter(), QUEUE_INLINE=True)
    sign_in(client, make_user("leader@example.com"))
    client.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(climbing_points(), name="Sunday spin")), "a.gpx")}, content_type="multipart/form-data")
    r = client.put("/api/routes/1/ride", json={"date": SUNDAY, "start_time": "09:00", "meeting_name": "Kildare Town Square",
                                              "leader_phone": "087 123 4567", "notes": "Bring a spare tube.",
                                              "groups": [{"name": "A", "speed_kmh": 28, "stop_min": 20}, {"name": "C", "speed_kmh": 18, "stop_min": 20}]})
    assert r.status_code == 200, r.data
    client.get("/api/routes/1/climbs")
    add_wp("cafe", "Cafe One", 12_000, tags={"opening_hours": "Mo-Su 08:00-17:00"}, planned=True, note="Room for 20")
    add_wp("aed", "Spar AED", 8_000, offset=150.0, included=False, side="R", origin="club",
           tags={"club": True, "verified_at": "2026-09-30T10:00:00+00:00"}, note="By the door")
    add_wp("pharmacy", "Lowe's Pharmacy", 4_000, included=False)
    add_wp("bailout", "Kildare Station", 15_000, offset=400.0, included=False)
    return client


def share(c):
    r = c.post("/api/routes/1/share")
    assert r.status_code == 200, r.data
    return r.get_json()


def public(c, token, path=""):
    return other_client(c).get(f"/r/{token}{path}")


def html(c, token):
    r = public(c, token)
    assert r.status_code == 200
    return r.data.decode()


# ---------------- turning sharing on and off ----------------

def test_sharing_needs_sign_in_and_gives_a_long_stable_link(sw):
    assert other_client(sw).post("/api/routes/1/share").status_code == 404           # a private route doesn't admit it exists
    anon = other_client(sw)
    anon.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(climbing_points(), name="Open route")), "a.gpx")}, content_type="multipart/form-data")
    assert anon.post("/api/routes/2/share").status_code == 401                         # an open route: sharing still needs a sign-in
    a = share(sw)
    assert len(a["token"]) >= 20 and a["url"].endswith(f"/r/{a['token']}")
    assert share(sw)["token"] == a["token"]                                  # idempotent: the same link
    st = sw.get("/api/routes/1/share").get_json()
    assert st == {"shared": True, "url": a["url"], "can_share": True}


def test_switching_sharing_off_kills_the_link(sw):
    token = share(sw)["token"]
    assert public(sw, token).status_code == 200
    assert sw.delete("/api/routes/1/share").status_code == 204
    assert public(sw, token).status_code == 404
    assert sw.get("/api/routes/1/share").get_json()["shared"] is False
    new = share(sw)["token"]
    assert new != token and public(sw, token).status_code == 404 and public(sw, new).status_code == 200


def test_unknown_or_short_tokens_are_404(sw):
    share(sw)
    for t in ("nope", "x" * 5, "A" * 22, "../etc/passwd"):
        assert public(sw, t).status_code == 404


def test_the_route_stays_private_while_the_link_works(sw):
    token = share(sw)["token"]
    stranger = other_client(sw)
    assert stranger.get("/routes/1").status_code == 404 and stranger.get("/api/routes/1").status_code == 404
    assert stranger.get(f"/r/{token}").status_code == 200
    assert "Sunday spin" not in stranger.get("/").data.decode()                 # and it isn't in anyone's list


# ---------------- Strava data can never be shared ----------------

def test_a_route_imported_from_strava_cannot_be_shared_even_by_its_owner(client):
    fake = FakeStrava()
    client.application.config.update(STRAVA_CLIENT=fake, CLOCK=lambda: NOW, WEATHER_CLIENT=FakeWeather(), WARNINGS_CLIENT=Feed([]),
                                     CLOSURE_PROVIDERS=[], HOURS_CLIENT=FakeHours())
    connect(client)
    client.get("/strava/routes")
    client.post("/strava/routes/101/import")
    r = client.post("/api/routes/1/share")
    assert r.status_code == 409 and "came from your Strava" in r.get_json()["error"] and "Share a copy" in r.get_json()["error"]
    assert Ride.query.filter(Ride.share_token.isnot(None)).count() == 0
    assert client.get("/api/routes/1/share").get_json()["can_share"] is False


def test_even_a_forced_token_on_a_strava_route_serves_nothing(client):
    fake = FakeStrava()
    client.application.config.update(STRAVA_CLIENT=fake, CLOCK=lambda: NOW, WEATHER_CLIENT=FakeWeather(), WARNINGS_CLIENT=Feed([]), CLOSURE_PROVIDERS=[])
    connect(client)
    client.get("/strava/routes")
    client.post("/strava/routes/101/import")
    from app.plan import get_or_create_ride
    ride = get_or_create_ride(db.session.get(Route, 1))
    ride.share_token = "forcedtoken0123456789abc"
    db.session.commit()
    for suffix in ("", "/summary.txt", "/cue-card.pdf"):
        assert other_client(client).get(f"/r/forcedtoken0123456789abc{suffix}").status_code == 404


def test_the_leaders_shared_copy_of_a_strava_route_is_shareable_and_carries_no_strava_data(client):
    fake = FakeStrava()
    client.application.config.update(STRAVA_CLIENT=fake, CLOCK=lambda: NOW, WEATHER_CLIENT=FakeWeather(), WARNINGS_CLIENT=Feed([]),
                                     CLOSURE_PROVIDERS=[], HOURS_CLIENT=FakeHours())
    connect(client)
    client.get("/strava/routes")
    client.post("/strava/routes/101/import")
    client.post("/routes/1/publish", data={"confirm_own": "yes"})
    copy = Route.query.filter_by(source="gpx").one()
    r = client.post(f"/api/routes/{copy.id}/share")
    assert r.status_code == 200
    page = other_client(client).get(f"/r/{r.get_json()['token']}").data.decode()
    assert "Curragh loop" in page and "Strava" not in page


# ---------------- what the public page contains ----------------

def test_the_public_page_has_everything_a_rider_needs(sw):
    token = share(sw)["token"]
    r = public(sw, token)
    page = r.data.decode()
    assert r.headers["Cache-Control"] == "no-cache" and r.headers["X-Robots-Tag"] == "noindex, nofollow" and r.headers["Referrer-Policy"] == "no-referrer"
    for needle in ("Sunday spin", "Sunday 11 October 2026", "Kildare Town Square", "09:00", "Pace groups", "Back about", "Stops and regroups",
                   "Cafe One", "Room for 20", "planned stop", "Climbs", "Hazards and closures", "What was checked", "not a guarantee",
                   "Weather", "If something goes wrong", "call 112 or 999", "Nearest defibrillator by section",
                   "Ways home", "Kildare Station", "Bring a spare tube.", "087 123 4567", "Cue card (PDF)", "Copy summary for WhatsApp",
                   "© OpenStreetMap contributors", "Met Éireann"):
        assert needle in page, needle
    assert "<svg" in page and 'class="sheet-map"' in page and 'class="sheet-profile"' in page                # inline, no tiles or scripts to fetch
    assert "maplibre" not in page and page.count("<script src=") == 0


def test_warnings_come_first_on_the_page(sw):
    token = share(sw)["token"]
    page = html(sw, token)
    assert page.index("Met Éireann weather warnings") < page.index("Pace groups") < page.index("Weather</h2>")
    assert page.index("No Met Éireann weather warnings for this ride.") < page.index("Sunday spin</h1>")


def test_a_warning_is_shown_as_issued_and_only_the_leader_sees_the_postpone_prompt(sw):
    sw.application.config["WARNINGS_CLIENT"] = Feed([item(cap="o1", level="orange", type_="Rain", regions=("EI12",),
        headline="Status Orange - Rain warning for Kildare", onset=datetime(2026, 10, 11, 6, tzinfo=UTC), expiry=datetime(2026, 10, 11, 21, tzinfo=UTC))])
    token = share(sw)["token"]
    pub = html(sw, token)
    assert "ORANGE: Weather Warning, Be Prepared" in pub and "Status Orange - Rain warning for Kildare" in pub and "Strong winds." in pub
    assert "Consider postponing" not in pub
    assert pub.index("Status Orange - Rain warning") < pub.index("Sunday spin</h1>")
    mine = sw.get("/routes/1/sheet").data.decode()
    assert "Consider postponing or changing the route" in mine and "Nothing is cancelled automatically" in mine


def test_an_unreachable_warnings_feed_is_said_out_loud(sw):
    sw.application.config["WARNINGS_CLIENT"] = Feed(error="Met Éireann answered 503")
    token = share(sw)["token"]
    page = html(sw, token)
    assert "Couldn&#39;t check Met Éireann weather warnings" in page or "Couldn't check Met Éireann weather warnings" in page
    assert "No Met Éireann weather warnings for this ride." not in page


def test_café_opening_status_per_group_is_shown(sw):
    page = html(sw, share(sw)["token"])
    assert "Group A arrives" in page and "Open on arrival" in page and "Mo-Su 08:00-17:00" in page and "OpenStreetMap hours" in page


def test_if_the_hours_service_is_down_the_sheet_says_hours_are_unknown_not_open(sw):
    sw.application.config["HOURS_CLIENT"] = FakeHours(fail=True)
    page = html(sw, share(sw)["token"])
    assert "Couldn&#39;t check opening hours right now" in page or "Couldn't check opening hours right now" in page
    assert "Open on arrival" not in page and "Cafe One" in page                          # the stop is still listed


def test_a_weather_outage_is_reported_and_the_warnings_still_show(sw):
    sw.application.config["WEATHER_CLIENT"] = FakeWeather(fail=True)
    page = html(sw, share(sw)["token"])
    assert "No forecast right now" in page and "Met Éireann weather warnings" in page


def test_the_nearest_defibrillator_by_section_with_its_caveat_and_club_badge(sw):
    page = html(sw, share(sw)["token"])
    assert "km 0–10:</strong>" in page and "Spar AED at km 8.0, 150 m right" in page and "club verified 2026-09-30" in page
    assert "By the door" in page and "may be incomplete or out of date" in page
    assert "pharmacy" not in page.lower()            # pharmacies are no longer one of the stop types


def test_sections_pick_the_nearest_and_admit_when_there_is_none(sw):
    secs = rs.nearest_by_section(sorted(db.session.get(Route, 1).waypoints, key=lambda x: x.s_m), "aed", 29_900)
    assert [s["label"] for s in secs] == ["km 0–10", "km 10–20", "km 20–30"]
    assert secs[0]["item"]["name"] == "Spar AED" and secs[1]["item"]["name"] == "Spar AED" and secs[1]["away_km"] == 7.0 and secs[2]["item"] is None


def test_hazards_and_closures_are_listed_with_dates_and_an_honest_disclaimer(sw):
    add_wp("hazard", "Cattle grid", 6_000, included=False, tags={"reason": None})
    db.session.add(Closure(geom=WKTElement("POINT(-6.78 53.16)", srid=4326), radius_m=150, description="Culvert collapsed",
                           starts_at=datetime(2026, 10, 8, tzinfo=UTC), ends_at=datetime(2026, 10, 20, tzinfo=UTC)))
    db.session.commit()
    sw.application.config["CLOSURE_PROVIDERS"] = None
    sw.application.config["OVERPASS_CLIENT"] = type("O", (), {"run": lambda self, q: []})()
    page = html(sw, share(sw)["token"])
    assert "Cattle grid" in page and "Culvert collapsed" in page and "until 2026-10-20" in page
    assert "not a guarantee" in page and "TII national road events" in page


def test_everything_user_supplied_is_escaped(sw):
    add_wp("cafe", "<script>alert(1)</script>", 20_000, note="<img src=x onerror=alert(2)>")
    Route.query.update({"title": "<b>Bold</b> route"})
    ride = db.session.get(Route, 1).ride
    ride.notes = "<iframe src=//evil.example></iframe>"
    ride.meeting_name = "<u>square</u>"
    db.session.commit()
    page = html(sw, share(sw)["token"])
    for raw in ("<script>alert(1)", "<img src=x", "<iframe", "<u>square", "<b>Bold</b>"):
        assert raw not in page, raw
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "&lt;b&gt;Bold&lt;/b&gt;" in page


def test_the_leaders_copy_has_the_checklist_and_the_link_but_the_public_one_does_not(sw):
    token = share(sw)["token"]
    mine = sw.get("/routes/1/sheet").data.decode()
    assert "Leader&#39;s checklist" in mine or "Leader's checklist" in mine
    for item_ in ("Spare tube", "Pump", "First-aid kit", "Lights (front and rear)", "Ride roster"):
        assert item_ in mine
    assert f"/r/{token}" in mine
    pub = html(sw, token)
    assert "checklist" not in pub.lower()


def test_the_leaders_preview_follows_route_privacy(sw):
    assert other_client(sw).get("/routes/1/sheet").status_code == 404
    assert other_client(sw).get("/routes/1/cue-card.pdf").status_code == 404
    stranger = other_client(sw)
    sign_in(stranger, make_user("stranger@example.com"))
    assert stranger.get("/routes/1/sheet").status_code == 404 and sw.get("/routes/1/sheet").status_code == 200


def test_leader_details_are_validated(sw):
    base = {"date": SUNDAY, "start_time": "09:00", "groups": [{"name": "A", "speed_kmh": 28, "stop_min": 20}]}
    assert sw.put("/api/routes/1/ride", json={**base, "leader_phone": "call me maybe"}).status_code == 400
    assert sw.put("/api/routes/1/ride", json={**base, "meeting_lat": 95, "meeting_lon": 0}).status_code == 400
    assert sw.put("/api/routes/1/ride", json={**base, "notes": "n" * 1001}).status_code == 400
    assert sw.put("/api/routes/1/ride", json={**base, "meeting_name": "m" * 121}).status_code == 400
    ok = sw.put("/api/routes/1/ride", json={**base, "leader_phone": "+353 87 123 4567", "meeting_lat": 53.158, "meeting_lon": -6.909, "meeting_name": ""})
    assert ok.status_code == 200 and ok.get_json()["leader_phone"] == "+353 87 123 4567" and ok.get_json()["meeting_name"] is None


def test_without_a_phone_number_none_is_shown(sw):
    sw.put("/api/routes/1/ride", json={"date": SUNDAY, "start_time": "09:00", "leader_phone": "", "groups": [{"name": "A", "speed_kmh": 28, "stop_min": 20}]})
    assert "Ride leader:" not in html(sw, share(sw)["token"])


# ---------------- WhatsApp text ----------------

def test_the_whatsapp_summary_is_short_plain_and_warnings_first(sw):
    sw.application.config["WARNINGS_CLIENT"] = Feed([item(cap="y1", level="yellow", type_="Wind", regions=("EI12",),
        onset=datetime(2026, 10, 11, 6, tzinfo=UTC), expiry=datetime(2026, 10, 11, 21, tzinfo=UTC))])
    token = share(sw)["token"]
    r = public(sw, token, "/summary.txt")
    text = r.data.decode()
    assert r.status_code == 200 and r.mimetype == "text/plain" and "<" not in text
    lines = text.splitlines()
    assert lines[0].startswith("⚠️ Met Éireann Yellow Wind warning") and "Sunday spin" in lines[1]
    for needle in ("Meet: Kildare Town Square at 09:00", "km ·", "Back by: A ", "☕ Cafe One", "Leader: 087 123 4567", f"/r/{token}"):
        assert needle in text, needle
    assert len(text) < 1300
    mine = sw.get("/routes/1/summary.txt").data.decode()
    assert "Meet: Kildare Town Square" in mine


def test_the_summary_admits_when_warnings_could_not_be_checked(sw):
    sw.application.config["WARNINGS_CLIENT"] = Feed(error="down")
    text = public(sw, share(sw)["token"], "/summary.txt").data.decode()
    assert text.splitlines()[0].startswith("⚠️ Couldn't check Met Éireann warnings")


# ---------------- the cue card PDF ----------------

def pdf(sw, token=None):
    r = public(sw, token, "/cue-card.pdf") if token else sw.get("/routes/1/cue-card.pdf")
    assert r.status_code == 200 and r.mimetype == "application/pdf" and r.data.startswith(b"%PDF")
    return PdfReader(io.BytesIO(r.data)), r


def text_of(reader):
    return "\n".join(p.extract_text() for p in reader.pages)


def test_the_cue_card_is_a_readable_a5_page_with_the_essentials(sw):
    reader, resp = pdf(sw, share(sw)["token"])
    box = reader.pages[0].mediabox
    assert (round(float(box.width)), round(float(box.height))) == (420, 595)                      # A5 in points
    t = text_of(reader)
    for needle in ("Sunday spin", "Sunday 11 October 2026", "Kildare Town Square", "09:00", "EMERGENCY", "112 or 999", "087 123 4567",
                   "Cafe One", "CLIMB", "km", "Spar AED", "club verified", "community data", "Bring a spare tube."):
        assert needle in t, needle
    assert 'filename="sunday-spin-cue-card.pdf"' in resp.headers["Content-Disposition"]


def test_warnings_are_the_first_thing_on_the_card(sw):
    sw.application.config["WARNINGS_CLIENT"] = Feed([item(cap="r1", level="red", type_="Wind", regions=("EI12",),
        headline="Status Red - Wind warning for Kildare", onset=datetime(2026, 10, 11, 6, tzinfo=UTC), expiry=datetime(2026, 10, 11, 21, tzinfo=UTC))])
    t = text_of(pdf(sw, share(sw)["token"])[0])
    assert t.index("RED WIND WARNING") < t.index("EMERGENCY") and t.index("RED WIND WARNING") < t.index("Back by")
    assert "Status Red - Wind warning for Kildare" in t


def test_the_card_spills_onto_more_pages_when_there_is_a_lot_to_list(sw):
    for i in range(70):
        add_wp("hazard", f"Hazard number {i}", 100 + i * 400, included=False)
    reader, _ = pdf(sw, share(sw)["token"])
    assert len(reader.pages) >= 2 and "Hazard number 69" in text_of(reader)
    assert all(round(float(p.mediabox.width)) == 420 for p in reader.pages)


def test_awkward_characters_do_not_break_the_card(sw):
    add_wp("cafe", "Ó Briain's ☕ café 日本", 21_000, note="Emoji 🚴 and <tags> & ampersands")
    t = text_of(pdf(sw, share(sw)["token"])[0])
    assert "Briain" in t and "<tags>" in t and "&" in t


def test_the_leaders_card_works_without_a_share_link(sw):
    t = text_of(pdf(sw)[0])
    assert "Sunday spin" in t


# ---------------- offline friendliness ----------------

def test_the_service_worker_is_network_first_and_scoped_to_ride_pages(sw):
    r = other_client(sw).get("/r/sw.js")
    assert r.status_code == 200 and "javascript" in r.mimetype
    js = r.data.decode()
    assert "fetch(e.request).then" in js and "caches.match" in js and "startsWith('/r/')" in js
    page = html(sw, share(sw)["token"])
    assert "serviceWorker.register('/r/sw.js', { scope: '/r/' })" in page
    assert 'id="offline"' in page and "may be out of date" in page and "met.ie/warnings" in page


def test_the_leaders_own_preview_does_not_register_a_service_worker(sw):
    assert "serviceWorker.register" not in sw.get("/routes/1/sheet").data.decode()


# ---------------- the user's own data export covers what they entered for sharing ----------------

def test_the_data_export_includes_ride_details_club_stops_and_closures_the_user_added(sw):
    import json
    from app.models import ClubStop
    sw.post("/api/club/stops", json={"name": "My cafe", "kind": "cafe", "lat": 53.16, "lon": -6.9, "hours": "Mo-Su 09:00-17:00", "note": "friendly"})
    sw.post("/api/closures", json={"lat": 53.1605, "lon": -6.75, "radius_m": 100, "description": "Bridge shut", "starts_at": "2026-10-08", "ends_at": "2026-10-20"})
    token = share(sw)["token"]
    data = json.loads(sw.get("/account/export.json").data)
    ride = data["routes"][0]["ride"]
    assert ride["leader_phone"] == "087 123 4567" and ride["meeting_point"] == "Kildare Town Square" and ride["notes"] == "Bring a spare tube."
    assert ride["share_link_active"] is True and [g["name"] for g in ride["pace_groups"]] == ["A", "C"]
    assert token not in json.dumps(data)                                                  # the secret link itself is not copied around
    assert [c["name"] for c in data["club_stops_you_added"]] == ["My cafe"] and data["club_stops_you_added"][0]["hours"] == "Mo-Su 09:00-17:00"
    assert [c["description"] for c in data["closures_you_entered"]] == ["Bridge shut"]
    other = other_client(sw)
    sign_in(other, make_user("someone@else.com"))
    theirs = json.loads(other.get("/account/export.json").data)
    assert theirs["club_stops_you_added"] == [] and theirs["closures_you_entered"] == [] and theirs["routes"] == []


def test_deleting_an_account_removes_their_shared_links_but_leaves_club_stops_unattributed(sw):
    from app.models import ClubStop, User
    sw.post("/api/club/stops", json={"name": "Club cafe", "kind": "cafe", "lat": 53.16, "lon": -6.9})
    token = share(sw)["token"]
    assert public(sw, token).status_code == 200
    assert sw.post("/account/delete", data={"confirm_email": "leader@example.com"}).status_code == 200
    assert public(sw, token).status_code == 404                                           # the ride and its link are gone
    stop = ClubStop.query.one()
    assert stop.name == "Club cafe" and stop.created_by is None                           # the club keeps its stop, no longer tied to the person


# ---------------- ready to ride? / renaming ----------------

def test_readiness_lists_what_a_leader_checks_and_never_calls_an_unchecked_thing_fine(sw):
    r = sw.get("/api/routes/1/readiness")
    assert r.status_code == 200
    d = r.get_json()
    by = {i["key"]: i for i in d["items"]}
    assert {"day", "meet", "warnings", "weather", "closures", "stops"} <= set(by)
    assert by["meet"]["status"] == "ok" and "Kildare Town Square" in by["meet"]["text"]
    assert by["warnings"]["status"] == "ok" and by["day"]["status"] == "ok"
    assert by["stops"]["status"] == "ok" and "ticked" in by["stops"]["text"]
    assert by["aed"]["status"] == "warn"                                  # a stop is ticked but no defibrillator is
    assert d["status"] == "warn" and d["headline"].startswith("Nearly there")
    assert [i["status"] for i in d["items"]] == sorted((i["status"] for i in d["items"]), key=["bad", "warn", "unknown", "ok"].index)


def test_a_dead_warnings_feed_or_a_past_date_is_not_reported_as_ready(sw):
    sw.application.config["WARNINGS_CLIENT"] = Feed(error="down")
    d = sw.get("/api/routes/1/readiness").get_json()
    assert {i["key"]: i for i in d["items"]}["warnings"]["status"] == "unknown"
    sw.application.config["WARNINGS_CLIENT"] = Feed([])
    sw.application.config["CLOCK"] = lambda: NOW + timedelta(days=9)            # the planned Sunday is now behind us
    d = sw.get("/api/routes/1/readiness").get_json()
    day = {i["key"]: i for i in d["items"]}["day"]
    assert day["status"] == "bad" and "already passed" in day["text"] and d["status"] == "bad"


def test_a_route_can_be_renamed_but_not_to_nothing(sw):
    assert sw.patch("/api/routes/1", json={"title": "  Sunday: Curragh loop  "}).get_json()["title"] == "Sunday: Curragh loop"
    assert "Sunday: Curragh loop" in sw.get("/routes/1").data.decode()
    assert sw.patch("/api/routes/1", json={"title": "   "}).status_code == 400


# ---------------- a word from the leader ----------------

def test_the_leaders_word_comes_first_on_the_sheet_the_whatsapp_text_and_the_card(sw):
    word = "Mudguards on please. Nobody gets left behind: we regroup at the top of every climb."
    r = sw.put("/api/routes/1/ride", json={"date": SUNDAY, "start_time": "09:00", "meeting_name": "Kildare Town Square", "notes": word,
                                          "groups": [{"name": "A", "speed_kmh": 28, "stop_min": 20}]})
    assert r.status_code == 200
    page = html(sw, share(sw)["token"])
    assert "A word from your ride leader" in page and word in page
    assert page.index("A word from your ride leader") < page.index("Pace groups")          # above the detail, not buried
    assert 'sheet-notes' not in page and page.count(word) == 2     # the box, and the hidden WhatsApp text; not repeated in the stats block
    text = sw.get("/routes/1/summary.txt").data.decode()
    lines = text.splitlines()
    assert any(l.startswith("💬 ") and "Mudguards on please" in l for l in lines)
    assert lines.index(next(l for l in lines if l.startswith("💬"))) < lines.index(next(l for l in lines if l.startswith("Meet:")))
    from pypdf import PdfReader
    import io as _io
    pdf = PdfReader(_io.BytesIO(sw.get("/routes/1/cue-card.pdf").data))
    first_page = pdf.pages[0].extract_text()
    assert "FROM YOUR LEADER" in first_page and "Nobody gets left behind" in first_page


def test_no_word_means_no_empty_box_and_markup_in_the_word_is_inert(sw):
    sw.put("/api/routes/1/ride", json={"date": SUNDAY, "start_time": "09:00", "meeting_name": "Kildare Town Square", "notes": "",
                                      "groups": [{"name": "A", "speed_kmh": 28, "stop_min": 20}]})
    page = html(sw, share(sw)["token"])
    assert "A word from your ride leader" not in page
    assert not any(l.startswith("💬") for l in sw.get("/routes/1/summary.txt").data.decode().splitlines())


def test_the_cyql_panel_offers_the_ride_details_and_the_gpx_and_promises_nothing_is_sent(sw):
    page = sw.get("/routes/1").data.decode()
    assert 'id="cyql"' in page and "Create this ride in Cyql" in page and "Nothing is sent to Cyql from this page" in page
    assert "/routes/1/export/gpx/download" in page and "/routes/1/summary.txt" in page
    gpx = sw.get("/routes/1/export/gpx/download?fmt=gpx")
    assert gpx.status_code == 200 and b"<gpx" in gpx.data
