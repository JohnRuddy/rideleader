"""Climbs and regroups through the HTTP API: detection, per-climb modes, settings, moving, rescans and export."""
import io

import numpy as np
import pytest
from garmin_fit_sdk import Decoder, Stream

from app.categories import classify
from app.overpass import build_query, query_version, tile_id
from tests.conftest import make_gpx
from tests.test_scan_e2e import FakeOverpass, node

LAT, LON0, STEP = 53.16, -6.9, 0.0015            # ~100 m per point, west to east


def climbing_points(n=300, with_ele=True):
    ele = np.interp
    xs = [0, 5000, 7000, 7600, 15000, 18300, 21000, 31000]
    ys = [100, 100, 220, 184, 184, 284, 164, 164]
    pts = []
    for i in range(n):
        s = i * 100.2
        pts.append((LAT, LON0 + i * STEP, float(ele(s, xs, ys)) if with_ele else None))
    return pts


def upload(client, pts, name="Hilly spin"):
    client.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(pts, name=name)), "a.gpx")},
                content_type="multipart/form-data")


@pytest.fixture
def hilly(client):
    upload(client, climbing_points())
    return client


def climbs(client):
    return client.get("/api/routes/1/climbs").get_json()


def regroups(client):
    return [w for w in client.get("/api/routes/1/waypoints").get_json() if w["kind"] == "regroup"]


def test_lazy_detection_finds_both_climbs_with_metrics_and_default_regroups(hilly):
    data = climbs(hilly)
    assert data["has_elevation"] and len(data["climbs"]) == 2
    one, two = data["climbs"]
    assert one["s_end_m"] == pytest.approx(7000, abs=250) and one["gain_m"] == pytest.approx(120, abs=12)
    assert one["avg_grade"] == pytest.approx(6.0, abs=0.8) and one["category"] == "4"
    assert two["s_end_m"] == pytest.approx(18300, abs=250) and two["gain_m"] == pytest.approx(100, abs=12)
    assert {c["regroup_mode"] for c in data["climbs"]} == {"ease_back"}      # both above the 40 m default threshold
    eb = [w for w in regroups(hilly) if w["reason"] == "climb"]
    assert [w["name"] for w in eb] == ["Ease back 2km", "Ease back 2km"] and all(w["included"] for w in eb)
    assert eb[0]["s_m"] == pytest.approx(one["s_end_m"], abs=1)               # at the summit
    assert "Ease back 2 km — regroup" == eb[0]["note"]


def test_descent_end_suggestion_is_offered_but_not_selected(hilly):
    climbs(hilly)
    sug = [w for w in regroups(hilly) if w["reason"] == "descent"]
    assert len(sug) == 1 and sug[0]["included"] is False
    assert sug[0]["s_m"] == pytest.approx(21000, abs=300) and "descent" in sug[0]["note"]


def test_route_without_elevation_reports_it(client):
    upload(client, climbing_points(with_ele=False))
    d = climbs(client)
    assert d["has_elevation"] is False and d["climbs"] == []
    assert client.post("/api/routes/1/climbs/detect").status_code == 400


def test_switch_a_climb_to_a_stop_then_off(hilly):
    c1 = climbs(hilly)["climbs"][0]
    r = hilly.patch(f"/api/climbs/{c1['id']}", json={"regroup_mode": "stop"}).get_json()
    assert r["warning"] is None and next(c for c in r["climbs"] if c["id"] == c1["id"])["regroup_mode"] == "stop"
    stop = next(w for w in regroups(hilly) if w["climb_id"] == c1["id"])
    assert stop["name"] == "Regroup" and 7000 < stop["s_m"] <= 9400 and "Drag it" in stop["note"]
    hilly.patch(f"/api/climbs/{c1['id']}", json={"regroup_mode": "off"})
    assert not [w for w in regroups(hilly) if w["climb_id"] == c1["id"]]
    assert len([w for w in regroups(hilly) if w["reason"] == "climb"]) == 1   # the other climb is untouched


def test_stop_uses_a_scanned_lay_by_after_the_summit(hilly):
    lon_at = lambda s: LON0 + s / 100.2 * STEP
    hilly.application.config.update(QUEUE_INLINE=True, OVERPASS_CLIENT=FakeOverpass([
        node(1, LAT + 0.0003, lon_at(8300), amenity="parking", parking="layby", name="Layby"),
        node(2, LAT + 0.0003, lon_at(7900), amenity="parking", name="Hotel car park"),
        node(3, LAT + 0.0003, lon_at(7200), amenity="parking", name="On the descent")]))
    hilly.post("/routes/1/scan", json={})
    c1 = climbs(hilly)["climbs"][0]
    r = hilly.patch(f"/api/climbs/{c1['id']}", json={"regroup_mode": "stop"})
    stop = next(w for w in regroups(hilly) if w["climb_id"] == c1["id"])
    assert stop["name"] == "Regroup Layby" and stop["s_m"] == pytest.approx(8300, abs=120) and stop["included"]
    assert r.get_json()["warning"] is None


def test_ease_back_distance_is_configurable_and_validated(hilly):
    c1 = climbs(hilly)["climbs"][0]
    hilly.patch(f"/api/climbs/{c1['id']}", json={"ease_km": 1.5})
    assert next(w for w in regroups(hilly) if w["climb_id"] == c1["id"])["name"] == "Ease back 1.5km"
    for bad in ({"ease_km": 0.1}, {"ease_km": "far"}, {"regroup_mode": "sprint"}):
        assert hilly.patch(f"/api/climbs/{c1['id']}", json=bad).status_code == 400


def test_threshold_setting_drops_regroups_for_smaller_climbs(hilly):
    climbs(hilly)
    r = hilly.put("/api/routes/1/climb-settings", json={"min_gain_m": 110})        # only the 120 m climb qualifies
    modes = [c["regroup_mode"] for c in r.get_json()["climbs"]]
    assert modes == ["ease_back", "off"]
    assert len([w for w in regroups(hilly) if w["reason"] == "climb"]) == 1
    r = hilly.put("/api/routes/1/climb-settings", json={"min_gain_m": 0, "default_mode": "stop", "ease_km": 3})
    assert [c["regroup_mode"] for c in r.get_json()["climbs"]] == ["stop", "stop"]
    for bad in ({"min_gain_m": -5}, {"default_mode": "wave"}, {"ease_km": 99}, {"gap_km": 1}, {"min_gain_m": "lots"}):
        assert hilly.put("/api/routes/1/climb-settings", json=bad).status_code == 400


def test_moving_a_regroup_updates_distance_pins_it_and_survives_redetect(hilly):
    climbs(hilly)
    eb = next(w for w in regroups(hilly) if w["reason"] == "climb")
    target_s = eb["s_m"] + 600
    lon = LON0 + target_s / 100.2 * STEP
    r = hilly.patch(f"/api/waypoints/{eb['id']}", json={"lat": LAT + 0.0002, "lon": lon})
    moved = r.get_json()
    assert r.status_code == 200 and moved["s_m"] == pytest.approx(target_s, abs=60) and moved["pinned"]
    assert 10 < moved["offset_m"] < 40
    hilly.post("/api/routes/1/climbs/detect")
    assert any(w["id"] == eb["id"] for w in regroups(hilly))                  # pinned: kept through re-detect


def test_move_validation(hilly):
    climbs(hilly)
    wid = regroups(hilly)[0]["id"]
    assert hilly.patch(f"/api/waypoints/{wid}", json={"lat": LAT}).status_code == 400
    assert hilly.patch(f"/api/waypoints/{wid}", json={"lat": 120, "lon": 0}).status_code == 400
    far = hilly.patch(f"/api/waypoints/{wid}", json={"lat": LAT + 0.2, "lon": LON0})
    assert far.status_code == 400 and "3 km" in far.get_json()["error"]


def test_scan_does_not_remove_computed_regroups(hilly):
    climbs(hilly)
    before = len(regroups(hilly))
    hilly.application.config.update(QUEUE_INLINE=True, OVERPASS_CLIENT=FakeOverpass([]))
    hilly.post("/routes/1/scan", json={})
    assert len(regroups(hilly)) == before


def test_regroups_are_exported_as_course_points_with_short_names(hilly):
    climbs(hilly)
    r = hilly.get("/routes/1/export/garmin/download?fmt=fit")
    m, errs = Decoder(Stream.from_byte_array(bytearray(r.data))).read(apply_scale_and_offset=True)
    cps = m["course_point_mesgs"]
    assert errs == [] and [c["name"] for c in cps] == ["Ease back 2km", "Ease back 2km"]
    assert {c["type"] for c in cps} == {"meeting_spot"} and all(len(c["name"]) <= 15 for c in cps)
    # an unselected suggestion stays out of the file
    assert all(c["distance"] < 20_000 for c in cps)                           # the descent-end suggestion (~21 km) isn't in
    gpx = hilly.get("/routes/1/export/gpx/download").data.decode()
    assert "Ease back 2km" in gpx and "Ease back 2 km — regroup" in gpx


def test_regroups_category_is_listed_but_never_scanned(hilly):
    page = hilly.get("/routes/1").data.decode()
    assert "&#34;key&#34;: &#34;regroup&#34;" in page or '"key": "regroup"' in page
    assert hilly.post("/routes/1/scan", json={"corridors": {"regroup": 100}}).status_code == 400
    assert hilly.post("/routes/1/scan", json={"categories": ["regroup"]}).status_code == 400


# ---------------- church category and tile cache versioning ----------------

def test_churches_are_a_scanned_category():
    assert classify({"amenity": "place_of_worship", "name": "St Brigid's"}) == "worship"
    assert '["amenity"="place_of_worship"]' in build_query((53.0, -7.0, 53.1, -6.9))


def test_tile_ids_carry_a_query_version_so_new_categories_refetch():
    v = query_version()
    assert len(v) == 6 and tile_id(-69, 531) == f"0.1:-69:531:{v}"
