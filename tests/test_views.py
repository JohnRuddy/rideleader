import io

from tests.conftest import hilly_points, make_gpx


def upload(client, data, filename="spin.gpx"):
    return client.post("/routes/upload", data={"gpx": (io.BytesIO(data), filename)},
                       content_type="multipart/form-data")


def test_upload_show_and_api_roundtrip(client):
    pts = hilly_points()
    r = upload(client, make_gpx(pts, waypoints=[("Cafe", 53.1605, pts[100][1])]))
    assert r.status_code == 302
    page = client.get(r.headers["Location"])
    assert page.status_code == 200 and b"Test spin" in page.data

    api = client.get("/api/routes/1").get_json()
    assert len(api["coords"]) == len(api["dist_m"]) == len(api["ele_m"]) == 200
    assert api["ascent_m"] > 90
    assert api["waypoints"][0]["name"] == "Cafe"
    assert api["waypoints"][0]["s_m"] == api["dist_m"][100]
    assert client.get("/").data.count(b"Test spin") == 1


def test_route_without_elevation(client):
    upload(client, make_gpx(hilly_points(with_ele=False)))
    api = client.get("/api/routes/1").get_json()
    assert api["ele_m"] is None and api["ascent_m"] is None


def test_rejects_non_gpx_and_garbage(client):
    assert upload(client, b"hello", "x.txt").status_code == 400
    assert upload(client, b"not xml at all", "x.gpx").status_code == 400
    assert client.post("/routes/upload").status_code == 400


def test_delete(client):
    upload(client, make_gpx(hilly_points()))
    assert client.post("/routes/1/delete").status_code == 302
    assert client.get("/routes/1").status_code == 404


def test_a_leader_can_place_a_cue_by_hand_and_it_is_selected_pinned_and_undoable(client):
    pts = hilly_points()
    upload(client, make_gpx(pts))
    api = client.get("/api/routes/1").get_json()
    lon, lat = api["coords"][60]
    r = client.post("/api/routes/1/waypoints", json={"kind": "cue_left", "name": "Left at the church", "lat": lat + 0.0002, "lon": lon})
    assert r.status_code == 201
    w = r.get_json()
    assert (w["kind"], w["name"], w["included"], w["pinned"], w["origin"]) == ("cue_left", "Left at the church", True, True, "manual")
    assert abs(w["s_m"] - api["dist_m"][60]) < 60 and w["offset_m"] < 40
    assert any(x["id"] == w["id"] for x in client.get("/api/routes/1/waypoints").get_json())
    # a bare click still gets a sensible name; nonsense is refused
    assert client.post("/api/routes/1/waypoints", json={"kind": "summit", "lat": lat, "lon": lon}).get_json()["name"] == "Summit"
    assert client.post("/api/routes/1/waypoints", json={"kind": "pharmacy", "lat": lat, "lon": lon}).status_code == 400
    assert client.post("/api/routes/1/waypoints", json={"kind": "generic", "lat": 0, "lon": 0}).status_code == 400
    assert client.post("/api/routes/1/waypoints", json={"kind": "generic"}).status_code == 400
