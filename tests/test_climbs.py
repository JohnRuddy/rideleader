"""Climb/descent detection and regroup placement on synthetic profiles (no database)."""
import math

import numpy as np
import pytest

from app.climbs import Profile, category_for
from app.regroup import descent_ends, ease_back, long_stretch, stop

LAT = 53.16
M_PER_DEG_LON = 111195 * math.cos(math.radians(LAT))


def profile(ele_fn, length=20_000, spacing=50, bend_at=None):
    """Straight west->east road (optionally turning north at bend_at metres) with elevation ele_fn(s)."""
    n = int(length / spacing) + 1
    dist = [i * spacing for i in range(n)]
    lats, lons = [], []
    for s in dist:
        if bend_at is not None and s > bend_at:
            lats.append(LAT + (s - bend_at) / 111195)
            lons.append(-6.9 + bend_at / M_PER_DEG_LON)
        else:
            lats.append(LAT)
            lons.append(-6.9 + s / M_PER_DEG_LON)
    return Profile(lats, lons, dist, [ele_fn(s) for s in dist])


def ramp(points):
    """Piecewise-linear elevation from [(s, ele), ...]."""
    xs, ys = zip(*points)
    return lambda s: float(np.interp(s, xs, ys))


# ---------------- detection ----------------

def test_flat_route_has_no_climbs():
    p = profile(lambda s: 100.0)
    assert p.climbs() == [] and p.descents() == []


def test_single_clean_climb_metrics():
    p = profile(ramp([(0, 100), (5000, 100), (7000, 220), (20000, 220)]))        # 2 km at 6%
    [c] = p.climbs()
    assert c.s_start == pytest.approx(5000, abs=120) and c.s_end == pytest.approx(7000, abs=60)
    assert c.gain_m == pytest.approx(120, abs=8) and c.avg_grade == pytest.approx(6.0, abs=0.5)
    assert c.length_m == pytest.approx(2000, abs=150)
    assert c.max_grade >= c.avg_grade
    assert c.category == "4"                                                      # 2000 m x 6% = 12000


def test_long_steep_climb_gets_higher_category():
    p = profile(ramp([(0, 100), (2000, 100), (7000, 500), (20000, 500)]))        # 5 km at 8% = 40000
    [c] = p.climbs()
    assert c.category == "2" and c.score == pytest.approx(40_000, rel=0.08)


def test_category_bands():
    assert [category_for(x) for x in (7999, 8000, 16000, 32000, 64000, 80000)] == [None, "4", "3", "2", "1", "HC"]


def test_gps_noise_does_not_split_or_inflate_a_climb():
    base = ramp([(0, 100), (5000, 100), (7000, 220), (20000, 220)])
    p = profile(lambda s: base(s) + 1.5 * math.sin(s / 17.0) + 1.0 * math.sin(s / 5.3))
    [c] = p.climbs()
    assert c.avg_grade == pytest.approx(6.0, abs=0.7) and c.gain_m == pytest.approx(120, abs=12)
    assert c.s_end == pytest.approx(7000, abs=120)          # not dragged along the noisy plateau


def test_small_dip_is_ridden_through_but_a_big_one_splits_the_climb():
    small = ramp([(0, 100), (2000, 100), (3000, 160), (3200, 155), (4200, 215), (20000, 215)])    # 5 m dip
    assert len(profile(small).climbs()) == 1
    big = ramp([(0, 100), (2000, 100), (3000, 160), (3300, 142), (4300, 202), (20000, 202)])      # 18 m dip
    assert len(profile(big).climbs()) == 2


def test_gentle_drag_and_short_bump_are_not_climbs():
    assert profile(ramp([(0, 100), (20000, 150)])).climbs() == []                 # 1%: below the 2.5% floor
    assert profile(ramp([(0, 100), (5000, 100), (5150, 115), (20000, 115)])).climbs() == []   # 150 m long


def test_max_gradient_picks_out_the_steep_pinch():
    p = profile(ramp([(0, 100), (2000, 100), (3000, 140), (3200, 164), (4200, 204), (20000, 204)]))   # 4% / 12% / 4%
    [c] = p.climbs()
    assert 10 < c.max_grade < 13 and 4.0 < c.avg_grade < 6.0      # smoothing widens each end by ~60 m


def test_flat_lead_in_is_trimmed():
    p = profile(ramp([(0, 100), (3000, 100), (4500, 190), (20000, 190)]))         # long flat, then 6%
    [c] = p.climbs()
    assert c.s_start > 2700 and c.length_m < 2000


def test_two_separate_climbs_in_order():
    p = profile(ramp([(0, 100), (2000, 100), (4000, 200), (6000, 100), (8000, 100), (10000, 220), (20000, 220)]))
    cs = p.climbs()
    assert len(cs) == 2 and cs[0].s_end < cs[1].s_start


def test_descent_detected_and_not_a_climb():
    p = profile(ramp([(0, 300), (4000, 300), (6000, 160), (20000, 160)]))        # 2 km at -7%
    assert p.climbs() == []
    [d] = p.descents()
    assert d.avg_grade == pytest.approx(7.0, abs=0.7) and d.s_end == pytest.approx(6000, abs=100)


def test_grade_and_bend_lookups():
    p = profile(ramp([(0, 100), (10000, 200), (20000, 200)]), bend_at=6000)
    assert p.grade_at(2000) == pytest.approx(1.0, abs=0.1)                                  # the bend changes position only, not the gradient
    assert p.bend_at(3000) < 2 and p.bend_at(6000) == pytest.approx(90, abs=8)


def test_smoothing_keeps_endpoints_and_range():
    p = profile(lambda s: 100 + (s > 10000) * 50.0)
    assert p.ele[0] == pytest.approx(100, abs=1) and p.ele[-1] == pytest.approx(150, abs=1)


# ---------------- regroup placement ----------------

CLIMB_ELE = [(0, 100), (2000, 100), (4000, 220), (30000, 220)]                    # summit at 4 km, then flat


def cand(kind, s, name=None, off=20.0, **tags):
    return {"id": s, "kind": kind, "name": name, "tags": tags, "s_m": s, "offset_m": off, "lat": LAT, "lon": -6.9}


def test_ease_back_is_a_course_point_at_the_summit_with_short_name():
    p = profile(ramp(CLIMB_ELE))
    [c] = p.climbs()
    r = ease_back(p, c, 2.0)
    assert r["s_m"] == c.s_end and r["name"] == "Ease back 2km" and len(r["name"]) <= 15
    assert r["note"] == "Ease back 2 km — regroup" and r["tags"]["mode"] == "ease_back"
    assert ease_back(p, c, 1.5)["name"] == "Ease back 1.5km"


def test_stop_prefers_layby_then_car_park_then_shop_then_church():
    p = profile(ramp(CLIMB_ELE))
    [c] = p.climbs()
    s = c.s_end
    pool = [cand("worship", s + 900, "St Brigid's"), cand("shop", s + 800, "Spar"),
            cand("parking", s + 1500, "Car park"), cand("parking", s + 1900, None, parking="layby")]
    assert stop(p, c, pool)["tags"]["source"] == "layby"
    assert stop(p, c, pool[:3])["tags"]["source"] == "parking"
    assert stop(p, c, pool[:2])["tags"]["source"] == "shop"
    got = stop(p, c, pool[:1])
    assert got["tags"]["source"] == "worship" and got["name"] == "Regroup St Brigid's"


def test_stop_closest_to_one_km_wins_within_a_tier():
    p = profile(ramp(CLIMB_ELE))
    [c] = p.climbs()
    a, b = cand("parking", c.s_end + 400, "Near"), cand("parking", c.s_end + 1100, "Mid")
    assert stop(p, c, [a, b])["tags"]["place"] == "Mid"


def test_stop_ignores_candidates_off_road_outside_window_or_on_the_descent():
    # summit at 4 km, then a 1.2 km descent at -6%, then flat
    p = profile(ramp([(0, 100), (2000, 100), (4000, 220), (5200, 148), (30000, 148)]))
    [c] = p.climbs()
    s = c.s_end
    bad = [cand("parking", s + 500, "On descent"), cand("parking", s + 300, "Too far off", off=400),
           cand("parking", s + 2600, "Too far along")]
    out = stop(p, c, bad)
    assert out["tags"]["source"] == "route"                                        # nothing valid mapped: computed spot
    assert out["s_m"] > s + 1100                                                   # and not on the descent
    ok = stop(p, c, bad + [cand("shop", s + 1500, "Centra")])
    assert ok["tags"]["place"] == "Centra"


def test_stop_avoids_sharp_bends():
    p = profile(ramp(CLIMB_ELE), bend_at=5000)                                     # right-angle corner 1 km past summit
    [c] = p.climbs()
    out = stop(p, c, [cand("parking", 5000, "At the corner"), cand("parking", c.s_end + 1700, "Past it")])
    assert out["tags"]["place"] == "Past it"


def test_stop_widens_search_when_descent_runs_past_two_km_and_gives_up_if_it_never_ends():
    long_descent = profile(ramp([(0, 100), (2000, 100), (4000, 220), (7500, 10), (30000, 10)]))   # -6% for 3.5 km
    [c] = long_descent.climbs()
    out = stop(long_descent, c, [])
    assert out is not None and out["s_m"] >= 7400 and "long descent" in out["note"]
    never = profile(ramp([(0, 100), (2000, 100), (4000, 220), (14000, -380)]), length=14000)     # -5% forever
    [c2] = never.climbs()
    assert stop(never, c2, []) is None


def test_descent_end_suggestion_prefers_a_mapped_spot_just_past_the_bottom():
    p = profile(ramp([(0, 300), (4000, 300), (6000, 160), (30000, 160)]))
    [sug] = descent_ends(p, [])
    assert sug["tags"]["reason"] == "descent" and sug["s_m"] == pytest.approx(6000, abs=100)
    [near] = descent_ends(p, [cand("shop", 6300, "Londis")])
    assert near["name"] == "Regroup Londis" and near["s_m"] == 6300


def test_long_stretch_suggests_a_mapped_spot_in_the_middle_only_when_needed():
    p = profile(lambda s: 100.0, length=60_000)
    cands = [cand("shop", 29_000, "Village shop"), cand("parking", 5_000, "Far away car park")]
    [sug] = long_stretch(p, [], cands, gap_km=25)
    assert sug["name"] == "Regroup Village shop" and sug["tags"]["reason"] == "gap"
    assert long_stretch(p, [30_000], cands, gap_km=25) == []                      # stretches now 30 km each: needs spots
    assert long_stretch(p, [], cands, gap_km=70) == []                            # short enough: nothing to suggest
