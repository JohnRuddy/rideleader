"""End to end: upload GPX -> scan (mocked Overpass) -> edit -> rescan -> bulk."""
import io

import pytest

from app.models import Waypoint
from app.poi import nearest_aeds
from tests.conftest import hilly_points, make_gpx

PTS = hilly_points()
LON_MID = PTS[100][1]


class FakeOverpass:
    def __init__(self, elements):
        self.elements, self.calls = elements, 0

    def fetch(self, bbox):
        self.calls += 1
        return self.elements


def node(i, lat, lon, **tags):
    return {"type": "node", "id": i, "lat": lat, "lon": lon, "tags": tags}


BASE = [
    node(1, 53.1625, LON_MID, amenity="cafe", name="Mid Cafe"),            # ~280 m off: inside 500
    node(2, 53.1680, LON_MID, amenity="cafe", name="Far Cafe"),            # ~890 m off: outside 500
    node(3, 53.1680, LON_MID + 0.01, emergency="defibrillator",
         **{"defibrillator:location": "outside the pharmacy"}),            # ~890 m off: inside AED 1 km
    {"type": "way", "id": 4, "center": {"lat": 53.1603, "lon": PTS[150][1]},
     "tags": {"amenity": "drinking_water"}},                               # ways use a centre point
    node(5, 53.1600, PTS[50][1], ford="yes"),                              # on route
    node(6, 53.1605, LON_MID, amenity="bench"),                            # unclassified: ignored
]


@pytest.fixture
def scanned(client):
    fake = FakeOverpass(BASE)
    client.application.config.update(QUEUE_INLINE=True, OVERPASS_CLIENT=fake)
    client.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(PTS)), "a.gpx")},
                content_type="multipart/form-data")
    r = client.post("/routes/1/scan", json={})
    assert r.status_code == 202
    return client, fake, r.get_json()["job_id"]


def wps(client):
    return {w["name"]: w for w in client.get("/api/routes/1/waypoints").get_json()}


def test_scan_finds_expected_candidates_with_distance_along_route(scanned):
    client, fake, job = scanned
    st = client.get(f"/api/scans/{job}").get_json()
    assert st["status"] == "done" and st["progress"] == 1.0
    w = wps(client)
    names = set(w)
    assert "Mid Cafe" in names and "Far Cafe" not in names
    assert any(x["kind"] == "aed" for x in w.values())
    assert any(x["kind"] == "water" for x in w.values()) and any(x["kind"] == "hazard" for x in w.values())
    cafe = w["Mid Cafe"]
    api = client.get("/api/routes/1").get_json()
    assert cafe["s_m"] == pytest.approx(api["dist_m"][100], abs=60)
    assert 250 < cafe["offset_m"] < 310 and cafe["side"] == "L"
    assert cafe["included"] is False                       # candidates start unselected
    assert st["summary"]["counts"]["cafe"] == 1


def test_adjustable_corridor_widens_search(scanned):
    client, _, _ = scanned
    client.post("/routes/1/scan", json={"corridors": {"cafe": 1000}})
    assert "Far Cafe" in wps(client)


def test_scan_input_validation(scanned):
    client, _, _ = scanned
    assert client.post("/routes/1/scan", json={"corridors": {"cafe": 5}}).status_code == 400
    assert client.post("/routes/1/scan", json={"corridors": {"nope": 100}}).status_code == 400
    assert client.post("/routes/1/scan", json={"categories": []}).status_code == 400


def test_second_scan_uses_tile_cache(scanned):
    client, fake, _ = scanned
    first = fake.calls
    assert first >= 1
    client.post("/routes/1/scan", json={})
    assert fake.calls == first


def test_rescan_keeps_selection_and_pinned_drops_stale(scanned):
    client, fake, _ = scanned
    w = wps(client)
    cafe_id = w["Mid Cafe"]["id"]
    water_id = next(x["id"] for x in w.values() if x["kind"] == "water")
    client.patch(f"/api/waypoints/{cafe_id}", json={"included": True, "name": "Club Cafe", "note": "room for 20"})
    client.patch(f"/api/waypoints/{water_id}", json={"pinned": True})

    # the OSM data changes: café and water are gone
    fake.elements = [e for e in BASE if e["id"] not in (1, 4)]
    from app.models import PoiTile
    from app import db
    PoiTile.query.delete(); db.session.commit()            # force a refetch
    client.post("/routes/1/scan", json={})
    now = wps(client)
    assert "Club Cafe" not in now                          # unpinned and no longer in OSM: removed
    assert any(x["id"] == water_id for x in now.values())  # pinned: kept

    # café returns: it is a new candidate again
    fake.elements = BASE
    PoiTile.query.delete(); db.session.commit()
    client.post("/routes/1/scan", json={})
    assert "Mid Cafe" in wps(client)


def test_rescan_preserves_edits_when_element_unchanged(scanned):
    client, _, _ = scanned
    cid = wps(client)["Mid Cafe"]["id"]
    client.patch(f"/api/waypoints/{cid}", json={"included": True, "name": "Club Cafe", "note": "card only"})
    client.post("/routes/1/scan", json={})
    now = wps(client)["Club Cafe"]
    assert now["id"] == cid and now["included"] and now["note"] == "card only"


def test_gpx_waypoints_untouched_by_scan(client):
    client.application.config.update(QUEUE_INLINE=True, OVERPASS_CLIENT=FakeOverpass([]))
    client.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(PTS, waypoints=[("Meet here", 53.16, PTS[3][1])])), "a.gpx")},
                content_type="multipart/form-data")
    client.post("/routes/1/scan", json={})
    assert "Meet here" in wps(client)


def test_bulk_endpoint_and_counter_source(scanned):
    client, _, _ = scanned
    r = client.post("/api/routes/1/waypoints/bulk", json={"kind": "cafe", "action": "add_all"})
    assert r.status_code == 200 and r.get_json()["changed"] == 1
    assert sum(w["included"] for w in r.get_json()["waypoints"]) == 1
    assert client.post("/api/routes/1/waypoints/bulk", json={"kind": "cafe", "action": "bad"}).status_code == 400


def test_patch_validation_and_delete(scanned):
    client, _, _ = scanned
    wid = wps(client)["Mid Cafe"]["id"]
    assert client.patch(f"/api/waypoints/{wid}", json={"name": "  "}).status_code == 400
    assert client.patch(f"/api/waypoints/{wid}", json={"included": "yes"}).status_code == 400
    assert client.patch(f"/api/waypoints/{wid}", json={"kind": "nonsense"}).status_code == 400
    assert client.delete(f"/api/waypoints/{wid}").status_code == 204
    assert "Mid Cafe" not in wps(client)


def test_nearest_aed_ranking(scanned):
    client, _, _ = scanned
    from app.models import Route
    from app import db
    route = db.session.get(Route, 1)
    best = nearest_aeds(route.waypoints, 5000)
    assert len(best) == 1 and best[0].kind == "aed"
    assert best[0].tags["defibrillator:location"] == "outside the pharmacy"


def test_failed_scan_reports_error(client):
    class Boom:
        def fetch(self, bbox): raise RuntimeError("Overpass busy")
    client.application.config.update(QUEUE_INLINE=True, OVERPASS_CLIENT=Boom())
    client.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(PTS)), "a.gpx")},
                content_type="multipart/form-data")
    job = client.post("/routes/1/scan", json={}).get_json()["job_id"]
    st = client.get(f"/api/scans/{job}").get_json()
    assert st["status"] == "failed" and "Overpass busy" in st["message"]


def test_tiles_are_fetched_in_parallel_when_the_server_is_ours(client):
    """A server we run ourselves can take several tiles at once; results and the cache behave exactly as before."""
    import threading
    from datetime import timedelta

    from app.poi import fetch_elements

    class Many:
        workers = 4

        def __init__(self):
            self.lock, self.boxes, self.live, self.peak = threading.Lock(), [], 0, 0

        def fetch(self, bbox):
            import time
            with self.lock:
                self.boxes.append(bbox); self.live += 1; self.peak = max(self.peak, self.live)
            time.sleep(0.05)
            with self.lock:
                self.live -= 1
            return [node(len(self.boxes), bbox[0], bbox[1], amenity="cafe")]

    tiles = {(0, 0), (1, 0), (2, 0), (3, 0), (0, 1), (1, 1)}
    seen = []
    c = Many()
    out = fetch_elements(c, tiles, timedelta(hours=1), lambda f, m: seen.append(round(f, 2)))
    assert len(out) == 6 and len(set(map(tuple, c.boxes))) == 6 and c.peak > 1
    assert seen[-1] == 1.0
    again = Many()
    assert len(fetch_elements(again, tiles, timedelta(hours=1))) == 6 and again.boxes == []     # cached: no network

    class Broken(Many):
        def fetch(self, bbox):
            raise RuntimeError("Overpass busy")

    with pytest.raises(RuntimeError):
        fetch_elements(Broken(), {(10, 10), (11, 10)}, timedelta(hours=1))
