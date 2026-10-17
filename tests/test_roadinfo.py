"""Road types and surfaces along a route, and the hazards derived from them and from the elevation profile."""
import io
import math

import pytest
from geoalchemy2.elements import WKTElement

from app import db, hazard_service, roadinfo
from app.climbs import Profile
from app.models import Waypoint
from tests.conftest import make_gpx
from tests.test_climbs_http import LAT, LON0, STEP, climbing_points

M_LON = 111195 * math.cos(math.radians(LAT))


def lon_at(s):
    return -7.0 + s / M_LON


def route_arrays(length=5000, step=50):
    n = int(length / step) + 1
    return [LAT] * n, [lon_at(i * step) for i in range(n)], [i * step for i in range(n)]


def way(id_, tags, a, b, offset_m=0.0):
    off = offset_m / 111195
    return {"type": "way", "id": id_, "tags": tags,
            "geometry": [{"lat": LAT + off, "lon": lon_at(a)}, {"lat": LAT + off, "lon": lon_at(b)}]}


# ---------------- classification ----------------

@pytest.mark.parametrize("tags,expected", [
    ({"highway": "primary"}, "main"), ({"highway": "trunk_link"}, "main"), ({"highway": "secondary"}, "local"),
    ({"highway": "residential"}, "local"), ({"highway": "unclassified", "surface": "asphalt"}, "local"),
    ({"highway": "residential", "surface": "gravel"}, "unpaved"), ({"highway": "service", "surface": "dirt"}, "unpaved"),
    ({"highway": "track"}, "unpaved"), ({"highway": "track", "surface": "asphalt"}, "local"),
    ({"highway": "path"}, "unpaved"), ({"highway": "primary", "surface": "gravel"}, "unpaved"),
])
def test_classify(tags, expected):
    assert roadinfo.classify(tags) == expected


# ---------------- querying ----------------

def test_queries_ask_for_highways_around_the_line_in_chunks():
    lats = [LAT + 0.01 * math.sin(i / 3) for i in range(600)]          # a wiggly line so simplification keeps many points
    lons = [-7.0 + i * 0.003 for i in range(600)]
    qs = roadinfo.around_queries(lats, lons)
    assert len(qs) >= 2
    for q in qs:
        assert q.startswith("[out:json]") and "way(around:25," in q and '["highway"]' in q and q.endswith("out geom tags;")
        assert len(q.split("around:25,")[1].split(")")[0].split(",")) <= 2 * 120


class Op:
    def __init__(self, ways=None, fail=False):
        self.ways, self.fail, self.queries = ways or [], fail, []

    def run(self, q):
        self.queries.append(q)
        if self.fail:
            raise RuntimeError("busy")
        return self.ways


def test_fetched_roads_are_cached(client):
    lats, lons, _ = route_arrays()
    op = Op([way(1, {"highway": "residential"}, 0, 5000)])
    first = roadinfo.fetch_ways(op, lats, lons)
    asked = len(op.queries)
    assert asked >= 1
    roadinfo.fetch_ways(op, lats, lons)
    assert [w["id"] for w in first] == [1] and len(op.queries) == asked          # second time: no network


def test_roads_are_asked_for_by_tile_and_only_those_near_the_route_are_kept(client):
    lats, lons, _ = route_arrays()
    qs = roadinfo.tile_queries(lats, lons)
    assert qs and all(q.startswith("[out:json]") and 'way["highway"](' in q and q.endswith("out geom tags;") for q in qs)
    near = way(1, {"highway": "residential"}, 0, 5000)
    far = {"type": "way", "id": 2, "tags": {"highway": "primary"},
           "geometry": [{"lat": LAT + 0.05, "lon": -7.0}, {"lat": LAT + 0.05, "lon": -6.99}]}
    got = roadinfo.fetch_ways(Op([near, far]), lats, lons)
    assert [w["id"] for w in got] == [1]


# ---------------- matching the route to roads ----------------

def test_percentages_and_busy_and_unpaved_stretches():
    lats, lons, d = route_arrays(5000)
    ways = [way(1, {"highway": "primary", "ref": "N7"}, 0, 1500), way(2, {"highway": "residential"}, 1500, 3500),
            way(3, {"highway": "track", "surface": "gravel"}, 3500, 4000)]
    s = roadinfo.summarise(lats, lons, d, ways)
    assert s["main_pct"] == pytest.approx(30, abs=3) and s["local_pct"] == pytest.approx(40, abs=3)
    assert s["unpaved_pct"] == pytest.approx(10, abs=3) and s["unknown_pct"] == pytest.approx(20, abs=3)
    assert s["ways_seen"] == 3
    [busy] = s["busy"]
    assert busy["ref"] == "N7" and busy["s0"] == pytest.approx(0, abs=50) and busy["s1"] == pytest.approx(1500, abs=60)
    [un] = s["unpaved"]
    assert un["surface"] == "gravel" and un["s0"] == pytest.approx(3500, abs=60) and un["s1"] == pytest.approx(4000, abs=60)


def test_a_road_too_far_from_the_line_is_not_matched():
    lats, lons, d = route_arrays(2000)
    near = roadinfo.summarise(lats, lons, d, [way(1, {"highway": "primary"}, 0, 2000, offset_m=20)])
    far = roadinfo.summarise(lats, lons, d, [way(1, {"highway": "primary"}, 0, 2000, offset_m=60)])
    assert near["main_pct"] > 90 and far["main_pct"] == 0 and far["unknown_pct"] == 100


def test_short_busy_and_unpaved_stretches_are_not_flagged():
    lats, lons, d = route_arrays(3000)
    s = roadinfo.summarise(lats, lons, d, [way(1, {"highway": "primary"}, 500, 700), way(2, {"highway": "track"}, 1500, 1560)])
    assert s["busy"] == [] and s["unpaved"] == [] and s["main_pct"] > 0


def test_no_roads_at_all_is_all_unknown_never_guessed():
    lats, lons, d = route_arrays(2000)
    s = roadinfo.summarise(lats, lons, d, [])
    assert (s["main_pct"], s["local_pct"], s["unpaved_pct"], s["unknown_pct"]) == (0, 0, 0, 100)


# ---------------- steep sections ----------------

def profile(points, length=3000):
    lats = [LAT] * 61
    lons = [lon_at(i * length / 60) for i in range(61)]
    d = [i * length / 60 for i in range(61)]
    import numpy as np
    xs, ys = zip(*points)
    return Profile(lats, lons, d, [float(np.interp(x, xs, ys)) for x in d])


def test_steep_climbs_and_descents_are_found():
    p = profile([(0, 100), (1000, 100), (1400, 148), (2000, 148), (2300, 112), (3000, 112)])     # 12% up over 400 m, 12% down over 300 m
    got = roadinfo.steep_sections(p)
    assert [g["kind"] for g in got] == ["climb", "descent"]
    assert 9 < got[0]["grade"] < 13 and -13 < got[1]["grade"] < -9
    assert got[0]["s0"] < 1200 < got[0]["s1"] and got[1]["s0"] < 2150 < got[1]["s1"]


def test_ordinary_hills_and_very_short_ramps_are_not_steep():
    assert roadinfo.steep_sections(profile([(0, 100), (3000, 300)])) == []                       # 6.7% all the way
    assert roadinfo.steep_sections(profile([(0, 100), (1500, 100), (1560, 110), (3000, 110)])) == []   # 17% for 60 m


# ---------------- hazards on the route ----------------

def upload(client):
    client.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(climbing_points(), name="Hazards")), "a.gpx")}, content_type="multipart/form-data")
    from app.models import Route
    return db.session.get(Route, 1)


def hazards(route):
    return sorted(((w.name, w.tags["reason"]) for w in route.waypoints if w.kind == "hazard" and w.origin == "computed"))


SUMMARY = {"busy": [{"s0": 3000.0, "s1": 4200.0, "ref": "N7", "highway": "primary", "surface": None}],
           "unpaved": [{"s0": 9000.0, "s1": 9400.0, "ref": "", "highway": "track", "surface": "gravel"}]}


def test_hazards_become_unselected_waypoints_you_can_pin_or_export(client):
    route = upload(client)
    counts = hazard_service.sync(route, SUMMARY)
    db.session.commit()
    assert counts == {"busy": 1, "unpaved": 1, "steep": 0}
    assert hazards(route) == [("Busy road: N7", "busy"), ("Unpaved section", "unpaved")]
    busy = next(w for w in route.waypoints if w.tags["reason"] == "busy")
    assert busy.included is False and busy.s_m == 3000.0 and "1.2 km" in busy.note and busy.tags["s_end"] == 4200.0


def test_syncing_again_replaces_hazards_but_keeps_pinned_ones(client):
    route = upload(client)
    hazard_service.sync(route, SUMMARY)
    pinned = next(w for w in route.waypoints if w.tags["reason"] == "unpaved")
    pinned.pinned = True
    db.session.commit()
    hazard_service.sync(route, {"busy": [], "unpaved": []})
    db.session.commit()
    assert hazards(route) == [("Unpaved section", "unpaved")]
    hazard_service.sync(route, {"busy": [{"s0": 100.0, "s1": 900.0, "ref": "R410", "highway": "primary"}], "unpaved": []})
    db.session.commit()
    assert hazards(route) == [("Busy road: R410", "busy"), ("Unpaved section", "unpaved")]            # no duplicates


def test_scanning_the_roads_stores_the_summary_and_the_hazards(client):
    route = upload(client)
    # the climbing route runs 30 km along 53.16N from -6.9; place the ways along it
    ways = [{"type": "way", "id": 1, "tags": {"highway": "primary", "ref": "N7"},
             "geometry": [{"lat": LAT, "lon": LON0}, {"lat": LAT, "lon": LON0 + 20 * STEP}]},
            {"type": "way", "id": 2, "tags": {"highway": "residential"},
             "geometry": [{"lat": LAT, "lon": LON0 + 20 * STEP}, {"lat": LAT, "lon": LON0 + 299 * STEP}]}]
    op = Op(ways)
    summary = hazard_service.scan_roads(route, op)
    db.session.commit()
    assert summary["main_pct"] > 5 and summary["local_pct"] > 80 and route.road_summary == summary
    assert ("Busy road: N7", "busy") in hazards(route)


def test_an_overpass_failure_keeps_the_old_summary_and_hazards(client):
    route = upload(client)
    hazard_service.sync(route, SUMMARY)
    route.road_summary = {"main_pct": 12.0, "busy": [], "unpaved": []}
    db.session.commit()
    assert hazard_service.scan_roads(route, Op(fail=True)) is None
    assert route.road_summary["main_pct"] == 12.0 and len(hazards(route)) == 2


def test_steep_hazards_are_created_from_the_profile_when_there_is_elevation(client):
    def ele(i):                      # flat, then a 500 m wall at 12%, then flat again
        if i < 100:
            return 100.0
        return 100.0 + 12.0 * (i - 100) if i < 105 else 160.0
    pts = [(LAT, LON0 + i * STEP, ele(i)) for i in range(200)]
    client.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(pts, name="Wall")), "a.gpx")}, content_type="multipart/form-data")
    from app.models import Route
    route = db.session.get(Route, 1)
    hazard_service.sync(route, None)
    db.session.commit()
    steep = [w for w in route.waypoints if w.kind == "hazard" and w.tags["reason"] == "steep"]
    assert len(steep) == 1 and steep[0].name.startswith("Steep climb") and steep[0].included is False
    assert 9_500 < steep[0].s_m < 10_300 and steep[0].tags["grade"] > 9
