import pytest

from app.routemodel import ascent_descent, haversine_m, locate_on_route, parse_gpx
from tests.conftest import hilly_points, make_gpx


def test_haversine_one_degree_latitude():
    assert haversine_m(53, -6, 54, -6) == pytest.approx(111_195, rel=1e-3)


def test_cumulative_distance_monotonic_and_total():
    r = parse_gpx(make_gpx(hilly_points()))
    d = [p.dist_m for p in r.points]
    assert d[0] == 0 and all(b > a for a, b in zip(d, d[1:]))
    assert r.length_m == pytest.approx(19_900, rel=0.01)  # 199 * 0.0015 deg lon at 53.16N


def test_ascent_ignores_noise_but_counts_real_climb():
    noisy = [100, 101, 100, 101.5, 100, 101, 100]
    assert ascent_descent(noisy) == (0.0, 0.0)
    up, down = ascent_descent([100, 150, 200, 150, 100])
    assert (up, down) == (100, 100)


def test_route_ascent_matches_bump():
    up, down = parse_gpx(make_gpx(hilly_points())).ascent_descent()
    assert up == pytest.approx(100, abs=5) and down == pytest.approx(100, abs=5)


def test_no_elevation_flagged():
    r = parse_gpx(make_gpx(hilly_points(with_ele=False)))
    assert not r.has_elevation


def test_duplicate_points_dropped():
    pts = [(53.0, -6.0, 1), (53.0, -6.0, 1), (53.001, -6.0, 2)]
    assert len(parse_gpx(make_gpx(pts)).points) == 2


def test_too_few_points_rejected():
    with pytest.raises(ValueError):
        parse_gpx(make_gpx([(53.0, -6.0, 1)]))


def test_waypoint_anchored_by_distance_along_route():
    pts = hilly_points()
    r = parse_gpx(make_gpx(pts, waypoints=[("Cafe", 53.1605, pts[100][1])]))
    s, off = locate_on_route(r, r.waypoints[0]["lat"], r.waypoints[0]["lon"])
    assert s == pytest.approx(r.points[100].dist_m)
    assert 40 < off < 70  # 0.0005 deg lat ~ 55 m
