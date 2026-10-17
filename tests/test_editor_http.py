"""Route editor through the API: edits, waypoint re-anchoring, undo/redo, rescans, failures, elevation fill."""
import io

import pytest

from app import db
from app.elevation import ElevationError
from app.models import GeomBlob, RouteState
from app.routing import RoutingError
from tests.conftest import make_gpx
from tests.test_climbs_http import climbing_points
from tests.test_editor import FakeRouter, LAT, M_LON, s_to_lonlat
from tests.test_scan_e2e import FakeOverpass, node

LON0 = -6.9


def upload(client, pts, waypoints=(), name="Edit me"):
    client.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(pts, waypoints, name)), "a.gpx")},
                content_type="multipart/form-data")


def lon_at(s):
    return LON0 + s / M_LON


def straight(n=300, step_m=100, ele=100.0):
    return [(LAT, lon_at(i * step_m), ele) for i in range(n)]


@pytest.fixture
def ed(client):
    upload(client, climbing_points())
    client.application.config["ROUTING_CLIENT"] = FakeRouter()
    return client


def route(client):
    return client.get("/api/routes/1").get_json()


def same(a, b, tol=1e-6):
    """Coordinates are stored to 7 decimals (about a centimetre), so compare with a tolerance."""
    return len(a) == len(b) and all(abs(p[0] - q[0]) < tol and abs(p[1] - q[1]) < tol for p, q in zip(a, b))


def edit(client, **body):
    return client.post("/api/routes/1/edit", json=body)


def wps(client):
    return {w["id"]: w for w in client.get("/api/routes/1/waypoints").get_json()}


# ---------------- basic edits ----------------

def test_fresh_route_has_two_ends_and_no_history(ed):
    d = ed.get("/api/routes/1/editor").get_json()
    assert [a["kind"] for a in d["anchors"]] == ["end", "end"] and d["options"] == {"avoid_busy": False, "allow_unpaved": False}
    assert d["history"]["can_undo"] is False and d["history"]["states"] == []


def test_click_adds_a_via_point_on_the_nearest_part_of_the_line(ed):
    before = route(ed)
    r = edit(ed, op="add_via", lat=LAT + 0.01, lon=lon_at(12_000))                  # ~1.1 km north of km 12
    assert r.status_code == 200
    d = r.get_json()
    assert [a["kind"] for a in d["anchors"]] == ["end", "cut", "via", "cut", "end"] and d["version"] == 2
    after = route(ed)
    assert after["length_m"] > before["length_m"] + 600                              # the detour is longer (~735 m)
    assert same(after["coords"][:100], before["coords"][:100])                       # the far ends are untouched
    assert len(after["dist_m"]) == len(after["coords"]) == len(after["ele_m"])       # all arrays stay aligned
    assert d["history"]["can_undo"] and d["history"]["undo_label"] == "Add via point"


def test_drag_uses_the_grab_position_not_the_nearest_to_the_target(ed):
    orig = route(ed)
    r = edit(ed, op="add_via", s_m=25_000, lat=LAT + 0.01, lon=lon_at(12_000))      # grab at km 25, drop up near km 12
    assert r.status_code == 200
    via = next(a for a in r.get_json()["anchors"] if a["kind"] == "via")
    assert via["lon"] == pytest.approx(lon_at(12_000), abs=1e-3)
    after = route(ed)
    # km 12 is where the point was dropped, but the reroute replaced km 23.5-26.5, so km 12 is untouched
    assert same(after["coords"][:230], orig["coords"][:230])
    assert after["length_m"] > orig["length_m"] + 5_000                              # a long way out to km 12 and back


def test_move_then_delete_a_via_point(ed):
    edit(ed, op="add_via", lat=LAT + 0.005, lon=lon_at(12_000))
    via = next(a for a in ed.get("/api/routes/1/editor").get_json()["anchors"] if a["kind"] == "via")
    mid = route(ed)["length_m"]
    r = edit(ed, op="move_via", anchor_id=via["id"], lat=LAT + 0.02, lon=via["lon"])
    assert r.status_code == 200 and r.get_json()["length_m"] > mid
    r = edit(ed, op="delete_via", anchor_id=via["id"])
    assert r.status_code == 200 and "via" not in [a["kind"] for a in r.get_json()["anchors"]]


def test_bad_edits_are_rejected_cleanly(ed):
    before = route(ed)
    assert edit(ed, op="warp").status_code == 400
    assert edit(ed, op="add_via", lat=95, lon=0).status_code == 400
    assert edit(ed, op="add_via", lat="north", lon=0).status_code == 400
    assert edit(ed, op="add_via", lat=LAT).status_code == 400
    ends = ed.get("/api/routes/1/editor").get_json()["anchors"]
    assert edit(ed, op="delete_via", anchor_id=ends[0]["id"]).status_code == 400
    assert edit(ed, op="move_via", anchor_id=999, lat=LAT, lon=0).status_code == 400
    assert route(ed)["coords"] == before["coords"]
    assert [s["label"] for s in ed.get("/api/routes/1/history").get_json()["states"]] in ([], ["Original"])


def test_a_routing_failure_changes_nothing_and_adds_no_history(ed):
    class Boom:
        def route(self, *a): raise RoutingError("Couldn't find a cycling route there.")
    ed.application.config["ROUTING_CLIENT"] = Boom()
    before = route(ed)
    r = edit(ed, op="add_via", lat=LAT + 0.01, lon=lon_at(12_000))
    assert r.status_code == 422 and "cycling route" in r.get_json()["error"]
    assert route(ed)["coords"] == before["coords"] and route(ed)["length_m"] == before["length_m"]
    assert [s["label"] for s in ed.get("/api/routes/1/history").get_json()["states"]] in ([], ["Original"])


def test_routing_options_are_remembered_and_passed_to_the_router(ed):
    r = ed.put("/api/routes/1/editor/options", json={"avoid_busy": False, "allow_unpaved": True})
    assert r.get_json()["options"] == {"avoid_busy": False, "allow_unpaved": True}
    router = ed.application.config["ROUTING_CLIENT"]
    edit(ed, op="add_via", lat=LAT + 0.01, lon=lon_at(12_000))
    assert router.calls and all(c[1].avoid_busy is False and c[1].allow_unpaved is True for c in router.calls)
    assert ed.put("/api/routes/1/editor/options", json={"avoid_busy": "maybe"}).status_code == 400


# ---------------- waypoints follow the line ----------------

def test_waypoints_shift_with_the_line_and_stay_with_their_place(client):
    upload(client, straight(), waypoints=[("Before", LAT, lon_at(5_000)), ("After", LAT, lon_at(25_000)),
                                          ("Inside", LAT + 0.0002, lon_at(10_000))])
    client.application.config["ROUTING_CLIENT"] = FakeRouter()
    old = {w["name"]: w for w in wps(client).values()}
    len0 = route(client)["length_m"]
    edit(client, op="add_via", lat=LAT + 0.01, lon=lon_at(10_000))
    delta = route(client)["length_m"] - len0
    assert delta > 600
    new = {w["name"]: w for w in wps(client).values()}
    assert new["Before"]["s_m"] == pytest.approx(old["Before"]["s_m"], abs=1)                 # before the edit: unchanged
    assert new["After"]["s_m"] == pytest.approx(old["After"]["s_m"] + delta, abs=80)          # after it: pushed along
    assert (new["After"]["lat"], new["After"]["lon"]) == (old["After"]["lat"], old["After"]["lon"])   # never moved
    assert new["Inside"]["offset_m"] > 300                                                    # now off the new line


def test_out_and_back_waypoint_keeps_its_pass_when_the_return_leg_is_edited(client):
    out = straight(201)
    back = [(LAT, lon_at(i * 100), 100.0) for i in range(199, -1, -1)]
    upload(client, out + back)
    client.application.config.update(QUEUE_INLINE=True, ROUTING_CLIENT=FakeRouter(),
                                     OVERPASS_CLIENT=FakeOverpass([node(1, LAT + 0.0003, lon_at(5_000), amenity="cafe", name="Shared Cafe")]))
    client.post("/routes/1/scan", json={})
    cafes = sorted((w for w in wps(client).values() if w["name"] == "Shared Cafe"), key=lambda w: w["s_m"])
    assert len(cafes) == 2 and cafes[0]["s_m"] == pytest.approx(5_000, abs=100) and cafes[1]["s_m"] == pytest.approx(35_000, abs=100)
    len0 = route(client)["length_m"]
    r = edit(client, op="add_via", s_m=30_000, lat=LAT + 0.01, lon=lon_at(10_000))          # edit the way home only
    assert r.status_code == 200
    delta = route(client)["length_m"] - len0
    new = sorted((w for w in wps(client).values() if w["name"] == "Shared Cafe"), key=lambda w: w["s_m"])
    assert len(new) == 2 and new[0]["s_m"] == pytest.approx(5_000, abs=100)                  # out pass untouched
    assert new[1]["s_m"] == pytest.approx(35_000 + delta, abs=150)                           # return pass moved along


def test_climb_choices_survive_an_edit_elsewhere(ed):
    ed.get("/api/routes/1/climbs")
    c1 = ed.get("/api/routes/1/climbs").get_json()["climbs"][0]
    ed.patch(f"/api/climbs/{c1['id']}", json={"regroup_mode": "off"})
    edit(ed, op="add_via", lat=LAT + 0.01, lon=lon_at(26_000))                               # far from both climbs
    modes = [c["regroup_mode"] for c in ed.get("/api/routes/1/climbs").get_json()["climbs"]]
    assert modes[0] == "off" and len(modes) >= 1


def test_pinned_regroup_is_not_duplicated_after_an_edit(ed):
    ed.get("/api/routes/1/climbs")
    eb = next(w for w in wps(ed).values() if w["reason"] == "climb")
    ed.patch(f"/api/waypoints/{eb['id']}", json={"pinned": True})
    edit(ed, op="add_via", lat=LAT + 0.01, lon=lon_at(26_000))
    same = [w for w in wps(ed).values() if w["reason"] == "climb" and abs(w["s_m"] - eb["s_m"]) < 1500]
    assert len(same) == 1 and same[0]["id"] == eb["id"]


# ---------------- background rescan ----------------

def test_edit_triggers_a_background_rescan_only_after_a_scan_was_made(ed):
    assert edit(ed, op="add_via", lat=LAT + 0.01, lon=lon_at(12_000)).get_json()["scan_job_id"] is None
    fake = FakeOverpass([node(1, LAT + 0.0003, lon_at(20_000), amenity="cafe", name="Far Cafe")])
    ed.application.config.update(QUEUE_INLINE=True, OVERPASS_CLIENT=fake)
    ed.post("/routes/1/scan", json={})
    ed.put("/api/routes/1/editor/options", json={"avoid_busy": True})
    r = edit(ed, op="add_via", lat=LAT + 0.01, lon=lon_at(6_000))
    job = r.get_json()["scan_job_id"]
    assert job and ed.get(f"/api/scans/{job}").get_json()["status"] == "done"


def test_rescan_after_an_edit_keeps_pinned_waypoints(ed):
    fake = FakeOverpass([node(1, LAT + 0.0003, lon_at(20_000), amenity="cafe", name="Pinned Cafe")])
    ed.application.config.update(QUEUE_INLINE=True, OVERPASS_CLIENT=fake)
    ed.post("/routes/1/scan", json={})
    cid = next(i for i, w in wps(ed).items() if w["name"] == "Pinned Cafe")
    ed.patch(f"/api/waypoints/{cid}", json={"pinned": True, "included": True})
    fake.elements = []                                                     # OSM no longer lists it
    from app.models import PoiTile
    PoiTile.query.delete(); db.session.commit()
    edit(ed, op="add_via", lat=LAT + 0.01, lon=lon_at(6_000))
    assert cid in wps(ed) and wps(ed)[cid]["included"] and wps(ed)[cid]["pinned"]


# ---------------- undo / redo ----------------

def test_undo_and_redo_a_geometry_edit(ed):
    orig = route(ed)
    edit(ed, op="add_via", lat=LAT + 0.01, lon=lon_at(12_000))
    edited = route(ed)
    r = ed.post("/api/routes/1/history/undo")
    assert r.status_code == 200 and r.get_json()["history"]["can_redo"] is True
    back = route(ed)
    assert same(back["coords"], orig["coords"]) and back["length_m"] == pytest.approx(orig["length_m"], rel=1e-4)
    assert [a["kind"] for a in r.get_json()["anchors"]] == ["end", "end"]
    r = ed.post("/api/routes/1/history/redo")
    assert route(ed)["length_m"] == pytest.approx(edited["length_m"], rel=1e-6) and r.get_json()["history"]["can_undo"]
    assert ed.post("/api/routes/1/history/redo").status_code == 409                    # nothing further to redo


def test_a_new_edit_after_undo_drops_the_redo_branch(ed):
    edit(ed, op="add_via", lat=LAT + 0.01, lon=lon_at(12_000))
    ed.post("/api/routes/1/history/undo")
    edit(ed, op="add_via", lat=LAT + 0.01, lon=lon_at(22_000))
    assert ed.get("/api/routes/1/history").get_json()["can_redo"] is False
    assert ed.post("/api/routes/1/history/redo").status_code == 409


def test_nothing_to_undo_on_a_fresh_route(ed):
    assert ed.post("/api/routes/1/history/undo").status_code == 409


def test_waypoint_changes_are_undoable_with_ids_preserved(client):
    upload(client, straight(), waypoints=[("Meet", LAT, lon_at(1_000))])
    wid = next(iter(wps(client)))
    client.patch(f"/api/waypoints/{wid}", json={"included": False})
    client.patch(f"/api/waypoints/{wid}", json={"name": "Meeting point", "note": "by the church"})
    client.patch(f"/api/waypoints/{wid}", json={"pinned": True})
    client.delete(f"/api/waypoints/{wid}")
    assert wid not in wps(client)
    labels = [s["label"] for s in client.get("/api/routes/1/history").get_json()["states"]]
    assert labels == ["Original", "Remove Meet", "Rename to Meeting point", "Pin Meeting point", "Delete Meeting point"]
    client.post("/api/routes/1/history/undo")                                            # un-delete
    assert wps(client)[wid]["name"] == "Meeting point" and wps(client)[wid]["pinned"] is True
    client.post("/api/routes/1/history/undo")                                            # un-pin
    assert wps(client)[wid]["pinned"] is False and wps(client)[wid]["note"] == "by the church"
    client.post("/api/routes/1/history/undo")
    client.post("/api/routes/1/history/undo")
    assert wps(client)[wid]["name"] == "Meet" and wps(client)[wid]["included"] is True   # back to the original


def test_bulk_actions_and_climb_choices_are_undoable(ed):
    fake = FakeOverpass([node(i, LAT + 0.0003, lon_at(2_000 + i * 3_000), amenity="drinking_water") for i in range(1, 5)])
    ed.application.config.update(QUEUE_INLINE=True, OVERPASS_CLIENT=fake)
    ed.post("/routes/1/scan", json={})
    ed.post("/api/routes/1/waypoints/bulk", json={"kind": "water", "action": "add_all"})
    assert sum(w["included"] for w in wps(ed).values() if w["kind"] == "water") == 4
    ed.post("/api/routes/1/history/undo")
    assert sum(w["included"] for w in wps(ed).values() if w["kind"] == "water") == 0

    c1 = ed.get("/api/routes/1/climbs").get_json()["climbs"][0]
    ed.patch(f"/api/climbs/{c1['id']}", json={"regroup_mode": "stop"})
    assert next(c for c in ed.get("/api/routes/1/climbs").get_json()["climbs"] if c["id"] == c1["id"])["regroup_mode"] == "stop"
    ed.post("/api/routes/1/history/undo")
    after = ed.get("/api/routes/1/climbs").get_json()["climbs"]
    assert next(c for c in after if c["id"] == c1["id"])["regroup_mode"] == "ease_back"
    assert any(w["name"].startswith("Ease back") and w["climb_id"] == c1["id"] for w in wps(ed).values())


def test_goto_jumps_to_any_step_and_rejects_unknown_ones(ed):
    edit(ed, op="add_via", lat=LAT + 0.01, lon=lon_at(8_000))
    edit(ed, op="add_via", lat=LAT + 0.01, lon=lon_at(22_000))
    hist = ed.get("/api/routes/1/history").get_json()
    assert [s["label"] for s in hist["states"]] == ["Original", "Add via point", "Add via point"]
    r = ed.post("/api/routes/1/history/goto", json={"seq": 0})
    assert r.status_code == 200 and [a["kind"] for a in r.get_json()["anchors"]] == ["end", "end"]
    assert ed.post("/api/routes/1/history/goto", json={"seq": 2}).status_code == 200
    assert ed.post("/api/routes/1/history/goto", json={"seq": 99}).status_code == 409
    assert ed.post("/api/routes/1/history/goto", json={}).status_code == 400


def test_history_is_capped_and_geometry_is_stored_once_per_shape(ed):
    ed.application.config.update(QUEUE_INLINE=True, OVERPASS_CLIENT=FakeOverpass(
        [node(1, LAT + 0.0003, lon_at(3_000), amenity="drinking_water", name="Tap")]))
    ed.post("/routes/1/scan", json={})
    tap = next(i for i, w in wps(ed).items() if w["name"] == "Tap")
    for i in range(70):
        ed.patch(f"/api/waypoints/{tap}", json={"included": i % 2 == 0})
    states = RouteState.query.filter_by(route_id=1).all()
    assert len(states) <= 61
    assert GeomBlob.query.filter_by(route_id=1).count() == 1            # 70 waypoint edits, one copy of the line
    assert ed.get("/api/routes/1/history").get_json()["can_undo"] is True


def test_toggling_to_the_same_value_does_not_create_a_history_step(ed):
    fake = FakeOverpass([node(1, LAT + 0.0003, lon_at(3_000), amenity="drinking_water", name="Tap")])
    ed.application.config.update(QUEUE_INLINE=True, OVERPASS_CLIENT=fake)
    ed.post("/routes/1/scan", json={})
    tap = next(i for i, w in wps(ed).items() if w["name"] == "Tap")
    ed.patch(f"/api/waypoints/{tap}", json={"included": True})
    n = len(ed.get("/api/routes/1/history").get_json()["states"])
    ed.patch(f"/api/waypoints/{tap}", json={"included": True})
    assert len(ed.get("/api/routes/1/history").get_json()["states"]) == n


# ---------------- elevation fill ----------------

class Hills:
    def lookup(self, points):
        return [100 + (p[1] + 7) * 10_000 for p in points]                # rises to the east


def test_upload_without_elevation_is_filled_from_the_elevation_service(client):
    client.application.config["ELEVATION_CLIENT"] = Hills()
    upload(client, [(LAT, lon_at(i * 100), None) for i in range(300)])
    d = route(client)
    assert d["ele_m"] is not None and len(d["ele_m"]) == 300 and d["ele_m"][-1] > d["ele_m"][0]
    assert d["ascent_m"] and d["ascent_m"] > 50


def test_upload_survives_an_elevation_service_outage(client):
    upload(client, [(LAT, lon_at(i * 100), None) for i in range(300)])      # the default stub raises ElevationError
    d = route(client)
    assert d["ele_m"] is None and d["ascent_m"] is None


def test_editing_a_route_without_elevation_fills_it_when_the_service_is_back(client):
    upload(client, [(LAT, lon_at(i * 100), None) for i in range(300)])
    assert route(client)["ele_m"] is None
    client.application.config.update(ROUTING_CLIENT=FakeRouter(), ELEVATION_CLIENT=Hills())
    r = edit(client, op="add_via", lat=LAT + 0.01, lon=lon_at(15_000))
    assert r.status_code == 200 and route(client)["ele_m"] is not None
