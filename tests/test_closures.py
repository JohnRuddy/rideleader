"""Closures: club-entered, OpenStreetMap roadworks, a configurable ArcGIS layer, the intersection with a route and
ride window, honest coverage, and rerouting around one."""
import math
from datetime import datetime, timedelta, timezone

import pytest
from geoalchemy2.elements import WKTElement
from shapely.geometry import LineString, Point

from app import closures as cl
from app import db
from app.editor import Track, reroute_section
from app.models import Closure, OsmClosureCache
from app.routing import BRouterProvider, RouteOptions
from tests.test_editor import FakeRouter, M_LON, LAT, straight_route
from tests.test_editor import legs as leg_kinds, kinds as anchor_kinds

UTC = timezone.utc
START = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
END = datetime(2026, 10, 11, 13, 0, tzinfo=UTC)
BBOX = (53.1, -7.1, 53.3, -6.5)


def lon_at(s):
    return -7.0 + s / M_LON


def route_arrays(length_m=20_000, step=100):
    n = int(length_m / step) + 1
    return [LAT] * n, [lon_at(i * step) for i in range(n)], [i * step for i in range(n)]


def raw(id_="x:1", geom=None, starts=None, ends=None, radius=0.0, desc="Works"):
    return cl.RawClosure(id_, id_.split(":")[0], desc, geom or Point(lon_at(5_000), LAT), starts, ends, None, radius)


# ---------------- which closures are active that day ----------------

@pytest.mark.parametrize("starts,ends,expected", [
    (None, None, True), (START - timedelta(days=3), START + timedelta(days=3), True),
    (END + timedelta(minutes=1), END + timedelta(days=1), False),           # starts after the ride
    (START - timedelta(days=3), START - timedelta(minutes=1), False),       # ended before it
    (START - timedelta(days=1), None, True), (None, END, True), (None, START - timedelta(seconds=1), False)])
def test_a_closure_counts_only_if_its_dates_overlap_the_ride(starts, ends, expected):
    assert cl._active(raw(starts=starts, ends=ends), START, END) is expected


# ---------------- intersecting with the route ----------------

def test_a_closure_on_the_route_reports_the_stretch_it_blocks():
    lats, lons, d = route_arrays()
    line = LineString([(lon_at(5_000), LAT), (lon_at(5_800), LAT)])
    [h] = cl.hits_for_route(lats, lons, d, [raw(geom=line)])
    assert h["s_start"] == pytest.approx(5_000 - cl.ROUTE_BUFFER_M, abs=30) and h["s_end"] == pytest.approx(5_800 + cl.ROUTE_BUFFER_M, abs=30)


def test_a_point_closure_uses_its_radius_and_a_far_one_is_ignored():
    lats, lons, d = route_arrays()
    near = raw("club:1", Point(lon_at(9_000), LAT + 150 / 111195), radius=200)       # 150 m off the road but 200 m wide
    far = raw("club:2", Point(lon_at(9_000), LAT + 500 / 111195), radius=100)
    hits = cl.hits_for_route(lats, lons, d, [near, far])
    assert [h["id"] for h in hits] == ["club:1"] and 8_700 < hits[0]["s_start"] < 9_000 < hits[0]["s_end"] < 9_300


def test_hits_come_back_in_ride_order_with_their_details():
    lats, lons, d = route_arrays()
    a = raw("club:a", Point(lon_at(15_000), LAT), radius=50, desc="Later")
    b = raw("osm:way/9", LineString([(lon_at(3_000), LAT), (lon_at(3_100), LAT)]), ends=END, desc="Earlier")
    hits = cl.hits_for_route(lats, lons, d, [a, b])
    assert [h["description"] for h in hits] == ["Earlier", "Later"] and hits[0]["ends_at"] == END.isoformat()
    assert hits[0]["geometry"]["type"] == "LineString"


def test_avoid_circles_cover_a_point_and_a_line():
    assert cl.avoid_circles({"_geom": Point(-7, 53), "radius_m": 0}) == [(53.0, -7.0, 40.0)]
    assert cl.avoid_circles({"_geom": Point(-7, 53), "radius_m": 120}) == [(53.0, -7.0, 120.0)]
    line = LineString([(-7.0, 53.0), (-7.0, 53.01)])                                  # ~1.1 km
    circles = cl.avoid_circles({"_geom": line, "radius_m": 0})
    assert 5 <= len(circles) <= 8 and circles[0][:2] == (53.0, -7.0) and circles[-1][1] == -7.0 and all(c[2] == 40.0 for c in circles)


# ---------------- club-entered closures ----------------

def add_closure(lon, desc, starts, ends, radius=100):
    c = Closure(geom=WKTElement(f"POINT({lon} {LAT})", srid=4326), radius_m=radius, description=desc, starts_at=starts, ends_at=ends)
    db.session.add(c)
    db.session.commit()
    return c


def test_club_closures_respect_their_dates(client):
    add_closure(lon_at(5_000), "Bridge closed", START - timedelta(days=1), START + timedelta(days=2))
    add_closure(lon_at(6_000), "Ended last week", START - timedelta(days=10), START - timedelta(days=7))
    add_closure(lon_at(7_000), "Starts next month", START + timedelta(days=30), START + timedelta(days=40))
    res = cl.ClubClosures().fetch(BBOX, START, END)
    assert [i.description for i in res.items] == ["Bridge closed"] and res.ok and res.items[0].id.startswith("club:")


# ---------------- OpenStreetMap roadworks ----------------

class FakeOverpass:
    def __init__(self, elements=None, fail=False):
        self.elements, self.fail, self.queries = elements or [], fail, []

    def run(self, query):
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("Overpass busy")
        return self.elements


def way(id_, tags, a=(5_000, 5_400)):
    return {"type": "way", "id": id_, "tags": tags, "geometry": [{"lat": LAT, "lon": lon_at(a[0])}, {"lat": LAT, "lon": lon_at(a[1])}]}


def test_the_osm_query_asks_for_construction_and_closed_roads_with_geometry():
    q = cl.OsmRoadworks.query(BBOX)
    for needle in ('["highway"="construction"]', '["access"="no"]', '["bicycle"="no"]', '["vehicle"="no"]', "out geom tags", "(53.10000,-7.10000,53.30000,-6.50000)"):
        assert needle in q


def test_osm_ways_become_closures_with_a_plain_description(client):
    op = FakeOverpass([way(1, {"highway": "construction", "construction": "residential", "name": "Mill Lane"}),
                       way(2, {"highway": "service", "access": "no"}, (6_000, 6_200)),
                       way(3, {"highway": "residential", "bicycle": "no", "name": "Canal Road"}, (7_000, 7_300))])
    res = cl.OsmRoadworks(op).fetch(BBOX, START, END)
    got = {i.id: i.description for i in res.items}
    assert got == {"osm:way/1": "Road under construction: residential (Mill Lane)", "osm:way/2": "Closed to vehicles (access=no)",
                   "osm:way/3": "Closed to bicycles (Canal Road)"}
    assert all(i.url.startswith("https://www.openstreetmap.org/way/") for i in res.items)


def test_a_road_closed_to_cars_but_open_to_bikes_is_not_a_closure(client):
    op = FakeOverpass([way(1, {"highway": "residential", "access": "no", "bicycle": "yes"}),
                       way(2, {"highway": "path", "vehicle": "no", "bicycle": "designated"})])
    assert cl.OsmRoadworks(op).fetch(BBOX, START, END).items == []


def test_osm_end_dates_are_read_and_respected(client):
    op = FakeOverpass([way(1, {"highway": "construction", "end_date": "2026-10-05"}),
                       way(2, {"highway": "construction", "end_date": "2026-12-01"}),
                       way(3, {"highway": "construction"})])
    res = cl.OsmRoadworks(op).fetch(BBOX, START, END)
    assert {i.id for i in res.items} == {"osm:way/2", "osm:way/3"}                    # way 1 finished before the ride


def test_osm_results_are_cached_for_hours_and_survive_an_outage(client):
    op = FakeOverpass([way(1, {"highway": "construction"})])
    prov = cl.OsmRoadworks(op)
    prov.fetch(BBOX, START, END)
    prov.fetch(BBOX, START, END)
    assert len(op.queries) == 1 and OsmClosureCache.query.count() == 1
    OsmClosureCache.query.update({"fetched_at": datetime.now(UTC) - timedelta(hours=7)}); db.session.commit()
    op.fail = True
    res = prov.fetch(BBOX, START, END)
    assert res.ok and len(res.items) == 1                                            # stale data beats none


def test_an_osm_outage_with_nothing_cached_is_reported(client):
    res = cl.OsmRoadworks(FakeOverpass(fail=True)).fetch(BBOX, START, END)
    assert res.ok is False and "Overpass busy" in res.error and res.items == []


# ---------------- a configurable ArcGIS layer ----------------

class Resp:
    def __init__(self, body=None, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


class Sess:
    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.resp


def epoch(dt):
    return int(dt.timestamp() * 1000)


GEOJSON = {"features": [
    {"geometry": {"type": "LineString", "coordinates": [[lon_at(5_000), LAT], [lon_at(5_500), LAT]]},
     "properties": {"OBJECTID": 7, "Reason": "Water main repair", "From": epoch(START - timedelta(days=1)), "To": epoch(START + timedelta(days=1)), "Link": "https://c.ie/7"}},
    {"geometry": {"type": "Point", "coordinates": [lon_at(9_000), LAT]},
     "properties": {"OBJECTID": 8, "Reason": "Old closure", "From": epoch(START - timedelta(days=9)), "To": epoch(START - timedelta(days=5))}},
    {"geometry": None, "properties": {"OBJECTID": 9}}]}
MAPPING = {"id": "OBJECTID", "description": "Reason", "start": "From", "end": "To", "url": "Link"}


def test_an_arcgis_layer_is_queried_by_envelope_and_mapped_by_config():
    s = Sess(Resp(GEOJSON))
    res = cl.ArcGisClosures("council", "Example council closures", "https://x/FeatureServer/0/", MAPPING, session=s).fetch(BBOX, START, END)
    url, kw = s.calls[0]
    assert url == "https://x/FeatureServer/0/query" and kw["params"]["geometry"] == "-7.1,53.1,-6.5,53.3" and kw["params"]["f"] == "geojson"
    assert [(i.id, i.description, i.url) for i in res.items] == [("council:7", "Water main repair", "https://c.ie/7")]   # the old and the geometry-less skipped
    assert res.items[0].starts_at < START < res.items[0].ends_at


def test_an_arcgis_failure_is_reported_not_swallowed():
    res = cl.ArcGisClosures("c", "C", "https://x/0", MAPPING, session=Sess(Resp(status=500))).fetch(BBOX, START, END)
    assert res.ok is False and res.items == []
    res = cl.ArcGisClosures("c", "C", "https://x/0", MAPPING, session=Sess(Resp({"error": "no"}))).fetch(BBOX, START, END)
    assert res.ok is False


# ---------------- the whole check ----------------

class Stub:
    def __init__(self, name, items=None, fail=False):
        self.name, self.label, self.coverage, self.items, self.fail = name, name.title(), f"{name} coverage", items or [], fail

    def fetch(self, bbox, start, end):
        if self.fail:
            raise RuntimeError("source exploded")
        return cl.SourceResult(self.name, self.label, self.coverage, self.items, fetched_at=datetime(2026, 10, 10, 8, tzinfo=UTC))


def test_check_lists_coverage_what_is_not_checked_and_never_claims_clear():
    lats, lons, d = route_arrays()
    out = cl.check(lats, lons, d, START, END, [Stub("club"), Stub("osm", [raw("osm:way/1", Point(lon_at(5_000), LAT), radius=30)])])
    assert [c["name"] for c in out["coverage"]] == ["club", "osm"] and out["coverage"][1]["found"] == 1 and out["coverage"][0]["checked_at"]
    assert len(out["hits"]) == 1 and "_geom" not in out["hits"][0] and out["all_sources_ok"] is True
    assert any("TII" in x for x in out["not_checked"]) and any("TrafficWatchNI" in x for x in out["not_checked"])
    assert out["disclaimer"] == "No closures found in the sources above. That is not a guarantee the road is clear."


def test_one_failing_source_does_not_hide_the_others_and_is_flagged():
    lats, lons, d = route_arrays()
    out = cl.check(lats, lons, d, START, END, [Stub("club", [raw("club:1", Point(lon_at(5_000), LAT), radius=30)]), Stub("osm", fail=True)])
    assert len(out["hits"]) == 1 and out["all_sources_ok"] is False
    bad = next(c for c in out["coverage"] if c["name"] == "osm")
    assert bad["ok"] is False and "exploded" in bad["error"]


# ---------------- rerouting around a closure ----------------

def long_track(length=20_000):
    return Track.from_coords(straight_route(length))


def test_reroute_section_replaces_only_that_stretch_and_tells_the_router_what_to_avoid():
    router = FakeRouter()
    circles = [(LAT, lon_at(10_000), 60.0)]
    t, region = reroute_section(long_track(), 9_700, 10_300, router, avoid=circles)
    assert leg_kinds(t) == ["original", "routed", "original"] and anchor_kinds(t) == ["end", "cut", "cut", "end"]
    assert router.avoids == [circles] and len(router.calls) == 1
    assert region.a_old == pytest.approx(9_700, abs=5) and region.b_old == pytest.approx(10_300, abs=5)


def test_reroute_section_inside_a_routed_leg_keeps_it_one_leg():
    t, _ = reroute_section(long_track(), 9_000, 11_000, FakeRouter())
    sec = (t.leg_ranges()[1][0] + t.leg_ranges()[1][1]) / 2
    t2, _ = reroute_section(t, sec - 200, sec + 200, FakeRouter(), avoid=[(LAT, lon_at(sec), 50.0)])
    assert leg_kinds(t2) == ["original", "routed", "original"] and anchor_kinds(t2) == ["end", "cut", "cut", "end"]


def test_reroute_section_at_the_start_of_a_route_needs_no_leading_cut():
    t, _ = reroute_section(long_track(), 0, 400, FakeRouter())
    assert leg_kinds(t) == ["routed", "original"] and anchor_kinds(t) == ["end", "cut", "end"]


def test_an_empty_stretch_is_refused():
    with pytest.raises(ValueError):
        reroute_section(long_track(), 5_000, 5_000.5, FakeRouter())


def test_brouter_gets_the_avoid_circles_as_nogos():
    class R:
        status_code = 200
        def json(self): return {"features": [{"geometry": {"coordinates": [[-7.0, 53.0, 1.0], [-6.9, 53.1, 2.0]]}}]}

    class S:
        def __init__(self): self.params = None
        def get(self, url, params=None, timeout=None):
            self.params = params
            return R()

    s = S()
    BRouterProvider("http://b", min_interval=0, session=s, sleep=lambda x: None).route(
        [(53.0, -7.0), (53.1, -6.9)], RouteOptions(), avoid=[(53.05, -6.95, 60.0), (53.06, -6.96, 40.4)])
    assert s.params["nogos"] == "-6.950000,53.050000,60|-6.960000,53.060000,40"
    s2 = S()
    BRouterProvider("http://b", min_interval=0, session=s2, sleep=lambda x: None).route([(53.0, -7.0), (53.1, -6.9)], RouteOptions())
    assert "nogos" not in s2.params


def test_a_dead_overpass_is_given_up_on_quickly_and_not_retried_straight_away():
    import requests
    from app import overpass as op

    class Dead:
        calls = 0
        def post(self, *a, **k):
            Dead.calls += 1
            raise requests.ConnectTimeout("no answer")

    op._down_until.clear()
    c = op.OverpassClient("http://dead.invalid/api", min_interval=0, session=Dead(), sleep=lambda s: None)
    for _ in range(3):
        with pytest.raises(op.OverpassError):
            c.run("x")
    assert Dead.calls == 1          # the first failure marks it down; the next two don't wait on the network at all
    op._down_until.clear()


def test_a_private_driveway_that_only_crosses_the_route_is_not_a_closure_but_a_closed_road_along_it_is():
    from shapely.geometry import LineString
    lats = [53.0] * 200
    lons = [-7.0 + i * 0.0003 for i in range(200)]           # ~ 4 km due east
    d = [i * 20.0 for i in range(200)]
    kx = 111_195 * math.cos(math.radians(53.0))
    cross = cl.RawClosure("osm:way/1", "osm", "Closed to vehicles (access=no)",
                          LineString([(-6.97, 52.9995), (-6.97, 53.0005)]), None, None, None)       # runs north-south across it
    along = cl.RawClosure("osm:way/2", "osm", "Closed to vehicles (access=no)",
                          LineString([(-6.97, 53.0001), (-6.95, 53.0001)]), None, None, None)       # runs beside it for ~1.3 km
    works = cl.RawClosure("osm:way/3", "osm", "Road under construction",
                          LineString([(-6.96, 52.9995), (-6.96, 53.0005)]), None, None, None)       # crossing, but roadworks count
    hits = cl.hits_for_route(lats, lons, d, [cross, along, works])
    assert {h["id"] for h in hits} == {"osm:way/2", "osm:way/3"}


def test_a_loop_that_meets_the_same_closed_road_twice_gets_two_short_reports_not_one_long_one():
    from shapely.geometry import LineString
    # north 1 km beside the road, east 3 km, north, west 3 km, then south 1 km beside the same road: two passes, ~9 km apart
    pts = [(-7.0, 53.0 + j * 0.0003) for j in range(0, 31)] + [(-7.0 + i * 0.0003, 53.009) for i in range(1, 101)]         + [(-6.97, 53.009 + j * 0.0003) for j in range(1, 31)] + [(-6.97 - i * 0.0003, 53.018) for i in range(1, 101)]         + [(-7.0, 53.018 - j * 0.0003) for j in range(1, 31)]
    lats, lons = [p[1] for p in pts], [p[0] for p in pts]
    d = [0.0]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        d.append(d[-1] + math.hypot((x1 - x0) * 111_195 * math.cos(math.radians(53.0)), (y1 - y0) * 111_195))
    road = cl.RawClosure("osm:way/9", "osm", "Closed to vehicles (access=no)",
                         LineString([(-7.0003, 52.9990), (-7.0003, 53.0200)]), None, None, None)
    hits = cl.hits_for_route(lats, lons, d, [road])
    assert len(hits) == 2 and all(h["s_end"] - h["s_start"] < 3_500 for h in hits)
    assert hits[0]["id"] == "osm:way/9#1" and hits[1]["id"] == "osm:way/9#2"
