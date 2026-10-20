"""End to end: import a GPX, scan (mocked Overpass), edit selection, export to every device and re-parse."""
import io

import gpxpy
import pytest
from garmin_fit_sdk import Decoder, Stream

from tests.conftest import hilly_points, make_gpx
from tests.test_scan_e2e import BASE, FakeOverpass

PTS = hilly_points()


@pytest.fixture
def ready(client):
    client.application.config.update(QUEUE_INLINE=True, OVERPASS_CLIENT=FakeOverpass(BASE))
    client.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(PTS, waypoints=[("Meet here", 53.16, PTS[2][1])], name="Sunday spin")), "a.gpx")},
                content_type="multipart/form-data")
    client.post("/routes/1/scan", json={})
    for kind in ("cafe", "aed", "water", "hazard"):
        client.post("/api/routes/1/waypoints/bulk", json={"kind": kind, "action": "add_all"})
    return client


def dl(client, device, fmt=None):
    url = f"/routes/1/export/{device}/download" + (f"?fmt={fmt}" if fmt else "")
    return client.get(url)


@pytest.mark.parametrize("device", ["gpx", "wahoo", "karoo", "strava"])
def test_gpx_devices_end_to_end(ready, device):
    r = dl(ready, device)
    assert r.status_code == 200 and r.mimetype == "application/gpx+xml"
    assert f'filename="sunday-spin-{device}.gpx"' in r.headers["Content-Disposition"]
    g = gpxpy.parse(r.data.decode())
    names = [w.name for w in g.waypoints]
    assert "Mid Cafe" in names and "Meet here" in names and "Far Cafe" not in names
    lons = [w.longitude for w in g.waypoints]
    assert lons == sorted(lons)        # route runs west to east, so waypoints come out in ride order
    assert len(g.tracks[0].segments[0].points) == 200


def test_unselected_candidates_are_not_exported(ready):
    ready.post("/api/routes/1/waypoints/bulk", json={"kind": "cafe", "action": "remove_all"})
    names = [w.name for w in gpxpy.parse(dl(ready, "gpx").data.decode()).waypoints]
    assert "Mid Cafe" not in names and "Meet here" in names


def test_garmin_fit_end_to_end(ready):
    r = dl(ready, "garmin", "fit")
    assert r.mimetype == "application/vnd.ant.fit"
    assert Decoder(Stream.from_byte_array(bytearray(r.data))).check_integrity()
    m, errs = Decoder(Stream.from_byte_array(bytearray(r.data))).read(apply_scale_and_offset=True)
    assert errs == [] and len(m["record_mesgs"]) == 200
    cps = m["course_point_mesgs"]
    assert [c["distance"] for c in cps] == sorted(c["distance"] for c in cps)   # ordered along the route
    assert {"food", "first_aid", "water", "danger", "generic"} <= {c["type"] for c in cps}


def test_garmin_tcx_end_to_end(ready):
    r = dl(ready, "garmin", "tcx")
    assert r.mimetype == "application/vnd.garmin.tcx+xml" and b"<CoursePoint>" in r.data


def test_bad_device_or_format_is_400(ready):
    assert dl(ready, "nokia").status_code == 400
    assert dl(ready, "wahoo", "tcx").status_code == 400
    assert dl(ready, "wahoo", "fit").status_code == 200
    assert ready.get("/routes/99/export").status_code == 404


def test_export_page_remembers_device_and_shows_strava_handoff(ready):
    page = ready.get("/routes/1/export?device=strava")
    assert page.status_code == 200
    assert b"https://www.strava.com/routes/new" in page.data and b"subscription" in page.data
    assert b'rel="noopener noreferrer"' in page.data
    # cookie remembered: a plain visit now lands on Strava again
    assert b"Send to Strava" in ready.get("/routes/1/export").data
    # route page panel carries the device for the limit warning
    assert b'data-device-label="Strava route"' in ready.get("/routes/1").data


def test_garmin_page_warns_when_over_limit(ready):
    from app import db
    from app.models import Route, Waypoint
    from geoalchemy2.elements import WKTElement
    route = db.session.get(Route, 1)
    for i in range(201):
        route.waypoints.append(Waypoint(kind="water", name=f"W{i}", s_m=float(i), included=True, origin="osm",
                                        geom=WKTElement("POINT(-6.8 53.16)", srid=4326)))
    db.session.commit()
    page = ready.get("/routes/1/export?device=garmin").data.decode()
    assert "commonly limited to 200" in page
    assert 'data-device-limit="200"' in ready.get("/routes/1").data.decode()
