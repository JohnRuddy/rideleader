"""Route editing engine against a fake router (no network, no database)."""
import math

import pytest

from app.editor import Region, Track, add_via, cum_dist, delete_via, move_via
from app.routemodel import haversine_m
from app.routing import BRouterProvider, RouteOptions, RoutingError

LAT = 53.16
M_LON = 111195 * math.cos(math.radians(LAT))


class FakeRouter:
    """Routes in a straight line, but 'snaps' the end points by a few metres like a real router would."""

    def __init__(self, snap_deg=0.00003, detour=0.0):
        self.calls, self.snap, self.detour = [], snap_deg, detour

    def route(self, points, options, avoid=None):
        self.calls.append((list(points), options))
        self.avoids = getattr(self, "avoids", []) + [avoid]
        (la1, lo1), (la2, lo2) = points[0], points[-1]
        d = haversine_m(la1, lo1, la2, lo2)
        n = max(2, int(d / 100) + 1)
        out = []
        for i in range(n + 1):
            t = i / n
            bump = self.detour * math.sin(math.pi * t)           # makes the routed line differ from a straight one
            out.append((lo1 + (lo2 - lo1) * t + (self.snap if i in (0, n) else 0), la1 + (la2 - la1) * t + bump, 100.0))
        return out


def straight_route(length_m=20_000, step_m=100):
    n = int(length_m / step_m) + 1
    return [(-6.9 + i * step_m / M_LON, LAT, 100.0 + i * 0.01) for i in range(n)]


def make_track(length_m=20_000):
    return Track.from_coords(straight_route(length_m))


def s_to_lonlat(s):   # a point beside the route at distance s, 150 m to the north
    return LAT + 150 / 111195, -6.9 + s / M_LON


def kinds(t):
    return [a.kind for a in t.anchors]


def legs(t):
    return [l.src for l in t.legs]


def continuous(coords, max_gap_m=400):
    return all(haversine_m(a[1], a[0], b[1], b[0]) < max_gap_m for a, b in zip(coords, coords[1:]))


# ---------------- structure ----------------

def test_new_track_is_one_original_leg_between_two_ends():
    t = make_track()
    assert kinds(t) == ["end", "end"] and legs(t) == ["original"] and len(t.coords()) == 201


def test_json_roundtrip_preserves_everything():
    t, _ = add_via(make_track(), 10_000, *s_to_lonlat(10_000), FakeRouter())
    back = Track.from_json(t.to_json(), t.coords())
    assert kinds(back) == kinds(t) and legs(back) == legs(t) and back.next_id == t.next_id
    assert [len(a.pts) for a in back.legs] == [len(a.pts) for a in t.legs]


def test_from_json_rejects_mismatched_geometry():
    t = make_track()
    with pytest.raises(ValueError):
        Track.from_json(t.to_json(), t.coords()[:-5])


# ---------------- add via ----------------

def test_dragging_the_middle_of_a_long_route_reroutes_only_nearby():
    t0 = make_track()
    t, region = add_via(t0, 10_000, *s_to_lonlat(10_000), FakeRouter())
    assert kinds(t) == ["end", "cut", "via", "cut", "end"] and legs(t) == ["original", "routed", "routed", "original"]
    # the untouched ends are bit-for-bit the original geometry
    orig = t0.coords()
    assert t.legs[0].pts[:-1] == orig[:len(t.legs[0].pts) - 1]
    assert t.legs[3].pts[1:] == orig[-(len(t.legs[3].pts) - 1):]
    assert continuous(t.coords()) and t.coords()[0] == orig[0] and t.coords()[-1][:2] == orig[-1][:2]
    # at most 1.5 km either side is replaced, and less where the new path just retraces the original line
    assert 8_500 - 5 <= region.a_old <= 10_000 and 10_000 <= region.b_old <= 11_500 + 5
    assert region.b_new > region.a_new


def test_region_remap_keeps_points_outside_and_shifts_those_after():
    r = Region(8_500, 11_500, 8_500, 12_500)           # the edit made the stretch 1 km longer
    assert r.remap(100) == 100 and r.remap(8_500) == 8_500
    assert r.remap(11_500) == 12_500 and r.remap(15_000) == 16_000
    assert r.remap(10_000) == pytest.approx(8_500 + 1500 / 3000 * 4000)
    xs = [r.remap(s) for s in range(0, 20_000, 250)]
    assert xs == sorted(xs)                              # never reorders anything


def test_dragging_near_the_start_has_no_leading_original_leg():
    t, _ = add_via(make_track(), 600, *s_to_lonlat(600), FakeRouter())
    assert kinds(t)[0] == "end" and "via" in kinds(t) and legs(t)[-1] == "original" and "routed" in legs(t)
    assert continuous(t.coords())


def test_dragging_a_short_route_replaces_the_whole_thing():
    t, region = add_via(make_track(2_500), 1_200, *s_to_lonlat(1_200), FakeRouter())
    assert "via" in kinds(t) and legs(t).count("routed") == 2 and continuous(t.coords())
    assert region.a_old < 300 and region.b_old > 2_200          # a short route is replaced all but the retraced ends


def test_router_is_asked_for_each_side_with_the_chosen_options():
    r = FakeRouter()
    add_via(make_track(), 10_000, *s_to_lonlat(10_000), r, RouteOptions(avoid_busy=False, allow_unpaved=True))
    assert len(r.calls) == 2 and all(c[1] == RouteOptions(False, True) for c in r.calls)
    assert r.calls[0][0][-1] == pytest.approx(s_to_lonlat(10_000))          # first leg ends at the dragged point
    assert r.calls[1][0][0][1] != r.calls[0][0][0][1]                        # second leg starts at the snapped via


def test_via_anchor_sits_on_the_snapped_position_not_the_click():
    t, _ = add_via(make_track(), 10_000, *s_to_lonlat(10_000), FakeRouter(snap_deg=0.0004))
    via = next(a for a in t.anchors if a.kind == "via")
    assert via.lon == pytest.approx(s_to_lonlat(10_000)[1] + 0.0004, abs=1e-7)


def test_second_drag_on_a_routed_leg_splits_it_without_new_cuts():
    t, _ = add_via(make_track(), 10_000, *s_to_lonlat(10_000), FakeRouter())
    s_mid = (t.leg_ranges()[2][0] + t.leg_ranges()[2][1]) / 2                 # inside the second routed leg
    t2, _ = add_via(t, s_mid, *s_to_lonlat(s_mid), FakeRouter())
    assert kinds(t2) == ["end", "cut", "via", "via", "cut", "end"]
    assert legs(t2) == ["original", "routed", "routed", "routed", "original"] and continuous(t2.coords())


def test_router_failure_leaves_the_track_untouched():
    class Boom:
        def route(self, *a): raise RoutingError("no road")
    t = make_track()
    before = t.to_json()
    with pytest.raises(RoutingError):
        add_via(t, 10_000, *s_to_lonlat(10_000), Boom())
    assert t.to_json() == before and legs(t) == ["original"]


# ---------------- move / delete ----------------

def test_move_via_reroutes_both_neighbours_and_keeps_the_id():
    t, _ = add_via(make_track(), 10_000, *s_to_lonlat(10_000), FakeRouter())
    via = next(a for a in t.anchors if a.kind == "via")
    la, lo = LAT + 0.01, via.lon
    t2, region = move_via(t, via.id, la, lo, FakeRouter())
    v2 = next(a for a in t2.anchors if a.id == via.id)
    assert v2.lat == pytest.approx(la, abs=1e-3) and legs(t2) == legs(t)
    assert cum_dist(t2.coords())[-1] > cum_dist(t.coords())[-1]               # the detour is longer
    assert region.b_new > region.b_old - (region.b_old - region.a_old) + 3000


def test_cannot_move_or_delete_ends_or_cuts():
    t, _ = add_via(make_track(), 10_000, *s_to_lonlat(10_000), FakeRouter())
    for a in t.anchors:
        if a.kind != "via":
            with pytest.raises(ValueError):
                move_via(t, a.id, LAT, -6.9, FakeRouter())
            with pytest.raises(ValueError):
                delete_via(t, a.id, FakeRouter())
    with pytest.raises(ValueError):
        move_via(t, 9999, LAT, -6.9, FakeRouter())


def test_delete_via_merges_into_one_routed_leg_between_the_cuts():
    t, _ = add_via(make_track(), 10_000, *s_to_lonlat(10_000), FakeRouter())
    via = next(a for a in t.anchors if a.kind == "via")
    t2, _ = delete_via(t, via.id, FakeRouter())
    assert kinds(t2) == ["end", "cut", "cut", "end"] and legs(t2) == ["original", "routed", "original"]
    assert continuous(t2.coords())


def test_deleting_the_only_via_of_a_short_route_leaves_a_clean_routed_route():
    t, _ = add_via(make_track(2_500), 1_200, *s_to_lonlat(1_200), FakeRouter())
    via = next(a for a in t.anchors if a.kind == "via")
    t2, _ = delete_via(t, via.id, FakeRouter())
    assert "via" not in kinds(t2) and continuous(t2.coords()) and t2.coords()[0] == t.coords()[0]


def test_cut_anchors_between_routed_legs_are_merged_away():
    t, _ = add_via(make_track(6_000), 3_000, *s_to_lonlat(3_000), FakeRouter())    # cuts at 1.5 km / 4.5 km
    s = t.leg_ranges()[1][0] + 100                                                  # drag inside the first routed leg
    t2, _ = add_via(t, s, *s_to_lonlat(s), FakeRouter())
    for i, a in enumerate(t2.anchors):                                             # every surviving cut borders an original leg
        if a.kind == "cut":
            assert "original" in (t2.legs[i - 1].src, t2.legs[i].src)


# ---------------- the BRouter adapter (HTTP faked) ----------------

class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.responses.pop(0)


GOOD = Resp(200, {"features": [{"geometry": {"coordinates": [[-6.9, 53.16, 101.0], [-6.8, 53.2, 82.5]]}}]})


def brouter(*responses):
    return BRouterProvider("http://b/brouter", min_interval=0, session=FakeSession(*responses), sleep=lambda s: None)


def test_brouter_request_shape_and_parsing():
    b = brouter(GOOD)
    out = b.route([(53.16, -6.9), (53.2, -6.8)], RouteOptions(avoid_busy=True))
    params = b.session.calls[0][1]
    assert params["lonlats"] == "-6.900000,53.160000|-6.800000,53.200000" and params["format"] == "geojson"
    assert params["profile"] == "fastbike-lowtraffic"
    assert out == [(-6.9, 53.16, 101.0), (-6.8, 53.2, 82.5)]


@pytest.mark.parametrize("opts,profile", [
    (RouteOptions(True, False), "fastbike-lowtraffic"), (RouteOptions(False, False), "fastbike"),
    (RouteOptions(True, True), "trekking"), (RouteOptions(False, True), "trekking")])
def test_profile_selection(opts, profile):
    assert opts.profile() == profile


def test_brouter_retries_when_busy_then_succeeds():
    b = brouter(Resp(503), Resp(429), GOOD)
    assert len(b.route([(53.16, -6.9), (53.2, -6.8)], RouteOptions())) == 2 and len(b.session.calls) == 3


def test_brouter_gives_up_with_a_leader_friendly_message():
    with pytest.raises(RoutingError, match="busy"):
        brouter(Resp(503), Resp(503), Resp(503)).route([(53.16, -6.9), (53.2, -6.8)], RouteOptions())
    with pytest.raises(RoutingError, match="Couldn't find a cycling route"):
        brouter(Resp(403)).route([(53.16, -6.9), (53.2, -6.8)], RouteOptions())
    with pytest.raises(RoutingError):
        brouter(Resp(200, {"nope": 1})).route([(53.16, -6.9), (53.2, -6.8)], RouteOptions())
    with pytest.raises(RoutingError):
        brouter(GOOD).route([(53.16, -6.9)], RouteOptions())


# ---------------- staying on the leader's own line ----------------

def test_a_nudge_that_the_router_answers_with_the_original_road_changes_almost_nothing():
    """A drag that lands on the same road: the new path coincides with the original, so the original's own points stay."""
    t0 = make_track()
    t, region = add_via(t0, 10_000, LAT, -6.9 + 10_000 / M_LON, FakeRouter(detour=0.0))     # dropped right on the line
    replaced = region.b_old - region.a_old
    assert replaced < 1_000                                   # nothing like the full 3 km window
    orig = t0.coords()
    assert t.legs[0].pts[:-1] == orig[:len(t.legs[0].pts) - 1]  # everything before is the original, bit for bit


def test_quieter_roads_are_opt_in():
    assert RouteOptions().avoid_busy is False and RouteOptions().profile() == "fastbike"
    assert RouteOptions(avoid_busy=True).profile() == "fastbike-lowtraffic"
    assert RouteOptions.from_json({}).avoid_busy is False
