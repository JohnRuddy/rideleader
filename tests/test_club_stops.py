"""The club stop library: CRUD and permissions, verification, and how club stops flow into a scan ahead of OpenStreetMap."""
import io
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app import hours as hrs
from app.models import ClubStop, Route, Waypoint
from tests.conftest import hilly_points, make_gpx
from tests.test_auth_security import make_user, other_client, sign_in
from tests.test_scan_e2e import FakeOverpass, node

LAT = 53.16
LON0, STEP = -6.9, 0.0015                      # hilly_points(): 100 m a point, 200 points, due east


def lon_at(s):
    return LON0 + s / 100.2 * STEP


def body(**o):
    return {"name": "Maguire's Cafe", "kind": "cafe", "lat": LAT + 0.0003, "lon": lon_at(10_000), "hours": "Mo-Su 08:00-18:00",
            "note": "Room for 20", **o}


@pytest.fixture
def lib(client):
    sign_in(client, make_user("leader@example.com"))
    return client


def stops():
    return {s.name: s for s in ClubStop.query.order_by(ClubStop.id).all()}


def upload(c):
    c.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(hilly_points(), name="Spin")), "a.gpx")}, content_type="multipart/form-data")


def scan(c, elements=(), **cfg):
    c.application.config.update(QUEUE_INLINE=True, OVERPASS_CLIENT=FakeOverpass(list(elements)), **cfg)
    from app.models import PoiTile
    PoiTile.query.delete()
    db.session.commit()
    r = c.post("/routes/1/scan", json={})
    assert r.status_code == 202
    return {w["name"]: w for w in c.get("/api/routes/1/waypoints").get_json()}


# ---------------- sign-in required ----------------

def test_the_library_needs_a_signed_in_member(client):
    assert client.get("/club/stops").status_code == 302
    assert client.get("/api/club/stops").status_code == 401
    assert client.post("/api/club/stops", json=body()).status_code == 401
    assert client.post("/api/waypoints/1/save-to-club").status_code == 401


# ---------------- adding, listing, editing ----------------

def test_adding_a_stop_and_having_it_verified(lib):
    r = lib.post("/api/club/stops", json=body(verified=True))
    assert r.status_code == 201
    s = r.get_json()
    assert (s["name"], s["kind"], s["hours"], s["note"]) == ("Maguire's Cafe", "cafe", "Mo-Su 08:00-18:00", "Room for 20")
    assert s["verified_at"] and s["verified_by"] and abs(s["lat"] - (LAT + 0.0003)) < 1e-9
    unverified = lib.post("/api/club/stops", json=body(name="New place")).get_json()
    assert unverified["verified_at"] is None


@pytest.mark.parametrize("bad", [{"name": ""}, {"name": "x" * 201}, {"kind": "banana"}, {"lat": None}, {"lat": 95}, {"lon": 190},
                                 {"hours": "x" * 301}, {"note": "n" * 1001}, {"lat": "north"}])
def test_bad_stops_are_refused(lib, bad):
    assert lib.post("/api/club/stops", json=body(**bad)).status_code == 400 and ClubStop.query.count() == 0


def test_listing_editing_and_the_page(lib):
    a = lib.post("/api/club/stops", json=body()).get_json()
    lib.post("/api/club/stops", json=body(name="Dunne's Garage", kind="shop"))
    assert [s["name"] for s in lib.get("/api/club/stops").get_json()] == ["Maguire's Cafe", "Dunne's Garage"]      # by kind, then name
    r = lib.patch(f"/api/club/stops/{a['id']}", json={"name": "Maguire's", "hours": "Mo-Fr 09:00-17:00", "lat": 53.2, "lon": -6.8})
    assert r.status_code == 200 and r.get_json()["name"] == "Maguire's" and r.get_json()["lat"] == 53.2
    assert lib.patch(f"/api/club/stops/{a['id']}", json={"lat": 999}).status_code == 400
    assert lib.patch("/api/club/stops/999", json={"name": "x"}).status_code == 404
    page = lib.get("/club/stops")
    assert page.status_code == 200 and b"Maguire&#39;s" in page.data and b"Not verified yet" in page.data and b"Add a stop" in page.data


def test_verifying_sets_the_date_and_who(lib):
    a = lib.post("/api/club/stops", json=body()).get_json()
    assert a["verified_at"] is None
    v = lib.post(f"/api/club/stops/{a['id']}/verify").get_json()
    assert v["verified_at"] and v["verified_by"]
    first = v["verified_at"]
    later = lib.post(f"/api/club/stops/{a['id']}/verify").get_json()
    assert later["verified_at"] >= first and lib.post("/api/club/stops/999/verify").status_code == 404


def test_only_the_author_or_an_admin_can_remove_a_stop(lib):
    a = lib.post("/api/club/stops", json=body()).get_json()
    other = make_user("other@example.com")
    c2 = other_client(lib)
    sign_in(c2, other)
    assert c2.delete(f"/api/club/stops/{a['id']}").status_code == 403 and ClubStop.query.count() == 1
    assert c2.post(f"/api/club/stops/{a['id']}/verify").status_code == 200                # anyone in the club can re-verify
    other.role = "admin"
    db.session.commit()
    assert c2.delete(f"/api/club/stops/{a['id']}").status_code == 204 and ClubStop.query.count() == 0
    assert lib.delete(f"/api/club/stops/{a['id']}").status_code == 404


# ---------------- club stops in a scan ----------------

def test_a_club_stop_beside_the_route_becomes_a_badged_unselected_candidate(lib):
    upload(lib)
    lib.post("/api/club/stops", json=body(verified=True))
    got = scan(lib)
    w = got["Maguire's Cafe"]
    assert w["club"] is True and w["verified_at"] and w["kind"] == "cafe" and w["included"] is False and w["note"] == "Room for 20"
    row = Waypoint.query.filter_by(name="Maguire's Cafe").one()
    assert row.origin == "club" and row.osm_ref == f"club/{ClubStop.query.one().id}" and row.club_stop_id == ClubStop.query.one().id
    assert abs(w["s_m"] - 10_000) < 150 and 20 < w["offset_m"] < 50 and w["side"] == "L"


def test_a_stop_far_from_the_route_or_deleted_or_a_different_kind_is_not_added(lib):
    upload(lib)
    lib.post("/api/club/stops", json=body(name="Too far", lat=LAT + 0.02))                  # ~2 km north: outside a café's 500 m
    gone = lib.post("/api/club/stops", json=body(name="Removed")).get_json()
    lib.delete(f"/api/club/stops/{gone['id']}")
    lib.post("/api/club/stops", json=body(name="A tap", kind="water", lat=LAT + 0.0030))    # ~330 m: outside water's 250 m
    got = scan(lib)
    assert got == {}


def test_a_club_stop_replaces_the_openstreetmap_copy_of_the_same_place_only(lib):
    upload(lib)
    lib.post("/api/club/stops", json=body(name="Maguire's Cafe"))
    here = (LAT + 0.0003, lon_at(10_000))
    got = scan(lib, elements=[node(1, here[0] + 0.00015, here[1], amenity="cafe", name="Maguires (OSM)"),                  # ~17 m: the same café
                              node(2, here[0], lon_at(10_300), amenity="cafe", name="Other Cafe"),                          # 300 m on: a different one
                              node(3, here[0], here[1], amenity="drinking_water", name="Tap by the cafe")])                 # same spot, different kind
    assert set(got) == {"Maguire's Cafe", "Other Cafe", "Tap by the cafe"}
    assert got["Maguire's Cafe"]["club"] and not got["Other Cafe"]["club"] and not got["Tap by the cafe"]["club"]


def test_club_hours_override_openstreetmap_hours_for_the_same_cafe(lib):
    upload(lib)
    lib.post("/api/club/stops", json=body(hours="Mo-Su 08:00-18:00", verified=True))
    scan(lib, elements=[node(1, LAT + 0.0003, lon_at(10_000), amenity="cafe", name="Maguires (OSM)", opening_hours="Mo-Fr 09:00-12:00")])
    row = Waypoint.query.filter_by(kind="cafe").one()
    assert row.origin == "club" and row.club_hours == "Mo-Su 08:00-18:00" and row.club_hours_at is not None
    rules, source, conflict = hrs.effective_hours(row)
    assert (rules, source, conflict) == ("Mo-Su 08:00-18:00", "club", False)


def test_the_library_is_the_authority_on_rescans_but_the_leaders_choices_survive(lib):
    upload(lib)
    s = lib.post("/api/club/stops", json=body()).get_json()
    wid = scan(lib)["Maguire's Cafe"]["id"]
    lib.patch(f"/api/waypoints/{wid}", json={"included": True, "planned_stop": True})
    lib.patch(f"/api/club/stops/{s['id']}", json={"name": "Maguire's (renamed)", "note": "Now with a ramp"})
    got = scan(lib)
    w = got["Maguire's (renamed)"]
    assert w["id"] == wid and w["included"] is True and w["planned_stop"] is True and w["note"] == "Now with a ramp"
    lib.delete(f"/api/club/stops/{s['id']}")
    assert scan(lib) == {}                                                                 # removed from the library, gone from the candidates


def test_a_pinned_club_waypoint_survives_its_stop_being_removed(lib):
    upload(lib)
    s = lib.post("/api/club/stops", json=body()).get_json()
    wid = scan(lib)["Maguire's Cafe"]["id"]
    lib.patch(f"/api/waypoints/{wid}", json={"pinned": True})
    lib.delete(f"/api/club/stops/{s['id']}")
    assert "Maguire's Cafe" in scan(lib)


def test_a_verified_club_defibrillator_is_a_normal_aed_candidate_with_its_date(lib):
    upload(lib)
    lib.post("/api/club/stops", json=body(name="Clubhouse AED", kind="aed", hours="", note="Green box by the door, code 1234", lat=LAT + 0.004, verified=True))
    got = scan(lib)
    w = got["Clubhouse AED"]
    assert w["kind"] == "aed" and w["club"] and w["verified_at"] and 400 < w["offset_m"] < 500          # inside the AED's 1 km corridor
    from app.poi import nearest_aeds
    route = db.session.get(Route, 1)
    assert nearest_aeds(route.waypoints, 10_000)[0].name == "Clubhouse AED"


# ---------------- saving what a leader has found ----------------

def test_saving_a_found_stop_to_the_library(lib):
    upload(lib)
    wid = scan(lib, elements=[node(1, LAT + 0.0003, lon_at(10_000), amenity="cafe", name="Found Cafe", opening_hours="Mo-Fr 09:00-12:00")])["Found Cafe"]["id"]
    row = db.session.get(Waypoint, wid)
    row.club_hours, row.club_hours_at = "Mo-Su 07:30-17:00", datetime.now(timezone.utc)      # the leader corrected the hours
    db.session.commit()
    r = lib.post(f"/api/waypoints/{wid}/save-to-club")
    assert r.status_code == 201
    s = r.get_json()
    assert s["name"] == "Found Cafe" and s["hours"] == "Mo-Su 07:30-17:00" and s["verified_at"] and s["kind"] == "cafe"
    db.session.refresh(row)
    assert row.origin == "club" and row.club_stop_id == s["id"] and row.osm_ref == f"club/{s['id']}"
    assert lib.post(f"/api/waypoints/{wid}/save-to-club").status_code == 409
    again = scan(lib)["Found Cafe"]                                                            # next scan comes from the library now
    assert again["club"] is True and again["id"] == wid


def test_saving_uses_the_openstreetmap_hours_when_the_leader_made_no_correction(lib):
    upload(lib)
    wid = scan(lib, elements=[node(1, LAT + 0.0003, lon_at(10_000), amenity="cafe", name="Plain Cafe", opening_hours="Mo-Fr 09:00-12:00")])["Plain Cafe"]["id"]
    assert lib.post(f"/api/waypoints/{wid}/save-to-club").get_json()["hours"] == "Mo-Fr 09:00-12:00"


def test_computed_waypoints_cant_be_saved_and_strangers_cant_touch_a_private_route(lib):
    upload(lib)
    lib.get("/api/routes/1/climbs")
    regroup = Waypoint.query.filter_by(kind="regroup").first()
    if regroup is None:
        from geoalchemy2.elements import WKTElement
        regroup = Waypoint(route_id=1, kind="regroup", name="R", s_m=5, origin="computed", geom=WKTElement("POINT(-6.9 53.16)", srid=4326))
        db.session.add(regroup); db.session.commit()
    assert lib.post(f"/api/waypoints/{regroup.id}/save-to-club").status_code == 400
    stranger = other_client(lib)
    sign_in(stranger, make_user("stranger@example.com"))
    assert stranger.post(f"/api/waypoints/{regroup.id}/save-to-club").status_code == 404
