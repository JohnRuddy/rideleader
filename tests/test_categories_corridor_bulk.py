from types import SimpleNamespace as NS

import pytest

from app.bulk import apply_bulk
from app.categories import classify
from app.corridor import RouteIndex
from app.overpass import build_query, tile_bbox, tiles_for_points
from app.routemodel import haversine_m

# ---- classification ----

@pytest.mark.parametrize("tags,expected", [
    ({"amenity": "cafe"}, "cafe"),
    ({"shop": "bakery"}, "cafe"),
    ({"amenity": "restaurant", "cuisine": "coffee_shop"}, "cafe"),
    ({"amenity": "restaurant", "cuisine": "italian"}, None),
    ({"emergency": "defibrillator"}, "aed"),
    ({"amenity": "drinking_water"}, "water"),
    ({"amenity": "parking", "access": "private"}, None),
    ({"amenity": "parking"}, "parking"),
    ({"ford": "yes"}, "hazard"),
    ({"railway": "level_crossing"}, "hazard"),
    ({"historic": "boundary_stone"}, None),
    ({"historic": "castle"}, "historic"),
    ({"highway": "bus_stop"}, None),
    ({"amenity": "bench"}, None),
])
def test_classify(tags, expected):
    assert classify(tags) == expected


def test_overpass_query_covers_every_rule():
    q = build_query(tile_bbox(-69, 531))
    for needle in ['["amenity"="cafe"]', '["emergency"="defibrillator"]', '["historic"]',
                   '["amenity"="restaurant"]["cuisine"~', "out center tags"]:
        assert needle in q


def test_tiles_cover_route_and_edge_padding():
    # a point 0.005 deg from a tile edge needs the neighbour tile too (pad 0.015)
    tiles = tiles_for_points([53.1005], [-6.9], pad_deg=0.015)
    assert (-69, 530) in tiles and (-69, 531) in tiles


# ---- corridor ----

def straight_route(n=101, step=0.001):
    """West->east along 53.0N; about 66.8 m per step."""
    lons = [-7.0 + i * step for i in range(n)]
    lats = [53.0] * n
    d = [0.0]
    for i in range(1, n):
        d.append(d[-1] + haversine_m(lats[i - 1], lons[i - 1], lats[i], lons[i]))
    return lats, lons, d


def test_point_beside_route_gets_distance_offset_and_side():
    lats, lons, d = straight_route()
    idx = RouteIndex(lats, lons, d)
    north = idx.passes(53.001, -6.95, 250)   # ~111 m north; travelling east, north is left
    south = idx.passes(52.999, -6.95, 250)
    assert len(north) == len(south) == 1
    assert north[0].side == "L" and south[0].side == "R"
    assert north[0].offset_m == pytest.approx(111, abs=2)
    assert north[0].s_m == pytest.approx(d[50], abs=1)


def test_point_outside_corridor_is_not_matched():
    lats, lons, d = straight_route()
    assert RouteIndex(lats, lons, d).passes(53.004, -6.95, 250) == []   # ~445 m


def test_out_and_back_gives_two_passes_with_distinct_distances():
    lats, lons, d = straight_route()
    # return along the same road
    lats2 = lats + lats[-2::-1]
    lons2 = lons + lons[-2::-1]
    d2 = [0.0]
    for i in range(1, len(lats2)):
        d2.append(d2[-1] + haversine_m(lats2[i - 1], lons2[i - 1], lats2[i], lons2[i]))
    passes = RouteIndex(lats2, lons2, d2).passes(53.0005, -6.95, 250)
    assert len(passes) == 2
    assert passes[0].s_m == pytest.approx(d[50], abs=1)
    assert passes[1].s_m == pytest.approx(d2[-1] - d[50], abs=1)
    assert passes[0].side != passes[1].side   # same café is on the left going out, right coming back


def test_poi_at_turnaround_is_one_pass():
    lats, lons, d = straight_route()
    lats2, lons2 = lats + lats[-2::-1], lons + lons[-2::-1]
    d2 = [0.0]
    for i in range(1, len(lats2)):
        d2.append(d2[-1] + haversine_m(lats2[i - 1], lons2[i - 1], lats2[i], lons2[i]))
    assert len(RouteIndex(lats2, lons2, d2).passes(53.0003, -6.9, 250)) == 1


# ---- bulk ----

def W(kind="cafe", s=0, off=0, inc=False, pinned=False):
    return NS(kind=kind, s_m=s, offset_m=off, included=inc, pinned=pinned)


def test_add_all_and_clear_only_touch_that_category():
    a, b, c = W("cafe"), W("cafe"), W("water")
    assert apply_bulk([a, b, c], "add_all", "cafe") == 2
    assert a.included and b.included and not c.included
    assert apply_bulk([a, b, c], "remove_all", "cafe") == 2


def test_within_selects_nearby_and_deselects_far_but_not_pinned():
    near, far, pin = W(off=100, inc=True), W(off=900, inc=True), W(off=900, inc=True, pinned=True)
    apply_bulk([near, far, pin], "within", "cafe", 300)
    assert near.included and not far.included and pin.included


def test_max_n_keeps_closest_to_route():
    ws = [W(off=o, s=i, inc=True) for i, o in enumerate([400, 50, 200, 10])]
    apply_bulk(ws, "max_n", "cafe", 2)
    assert [w.included for w in ws] == [False, True, False, True]


def test_one_per_km_keeps_one_per_bin():
    ws = [W(s=100, off=80, inc=True), W(s=900, off=20, inc=True), W(s=1500, off=5, inc=True), W(s=1800, off=50, inc=True)]
    apply_bulk(ws, "one_per_km", "cafe", 1)
    assert [w.included for w in ws] == [False, True, True, False]


def test_bulk_validation():
    with pytest.raises(ValueError):
        apply_bulk([], "explode", "cafe")
    with pytest.raises(ValueError):
        apply_bulk([], "within", "cafe", 0)


def test_only_the_wanted_stop_types_are_offered_to_leaders():
    from app import categories as c
    offered = {x["key"] for x in c.public_json()}
    assert offered == {"hazard", "aed", "cafe", "water", "toilets", "bike", "fuel", "shelter", "historic", "regroup",
                       "cue_left", "cue_right", "cue_straight", "summit", "valley", "sprint"}      # the last six are placed by hand
    assert c.classify({"amenity": "fuel"}) == "fuel" and c.classify({"historic": "castle"}) == "historic"
    for tags in ({"amenity": "pharmacy"}, {"railway": "station"}, {"tourism": "viewpoint"}, {"natural": "peak"},
                 {"historic": "milestone"}):
        assert c.classify(tags) is None
    assert c.classify({"shop": "convenience"}) == "shop" and "shop" not in offered      # kept only as a regroup helper
