"""Getting a route onto a bike computer: the Wahoo phone flow with its short-lived QR link, and the riders' download on the shared
ride sheet."""
import re
import xml.etree.ElementTree as ET

import pytest

from app import export_send, ratelimit
from app.export import DEVICES
from app.models import Route
from tests.test_auth_security import other_client
from tests.test_share import share, sw  # noqa: F401  (sw is a fixture)


def gpx_names(data: bytes):
    root = ET.fromstring(data)
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    return [e.text for e in root.findall(".//g:wpt/g:name", ns)], len(root.findall(".//g:trkpt", ns))


# ===================== the signed phone link =====================

def test_a_token_carries_one_route_device_and_file_type(sw):
    with sw.application.app_context():
        t = export_send.make_token(7, "wahoo", "gpx")
        assert export_send.read_token(t) == (7, "wahoo", "gpx")
        assert export_send.read_token(t + "x") is None and export_send.read_token("junk") is None and export_send.read_token("") is None


def test_a_token_from_a_different_secret_or_salt_is_refused(sw):
    from itsdangerous import URLSafeTimedSerializer
    with sw.application.app_context():
        for secret, salt in (("another-secret", export_send.SALT), (sw.application.config["SECRET_KEY"], "other-salt")):
            bad = URLSafeTimedSerializer(secret, salt=salt).dumps([1, "wahoo", "gpx"])
            assert export_send.read_token(bad) is None


def test_the_link_opens_the_file_without_signing_in_and_with_safe_headers(sw):
    with sw.application.app_context():
        token = export_send.make_token(1, "wahoo", "gpx")
    r = other_client(sw).get(f"/send/{token}")
    assert r.status_code == 200 and r.mimetype == "application/gpx+xml"
    assert r.headers["Content-Disposition"] == 'attachment; filename="sunday-spin-wahoo.gpx"'
    assert r.headers["Cache-Control"] == "no-store" and r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Robots-Tag"] == "noindex, nofollow"
    names, trkpts = gpx_names(r.data)
    assert trkpts > 10 and "Cafe One" in names


def test_the_link_stops_working_after_twenty_minutes(sw, monkeypatch):
    with sw.application.app_context():
        token = export_send.make_token(1, "wahoo", "gpx")
    assert export_send.TTL == 20 * 60
    monkeypatch.setattr(export_send, "TTL", -1)
    r = other_client(sw).get(f"/send/{token}")
    assert r.status_code == 410 and "expired" in r.get_data(as_text=True) and r.headers["Cache-Control"] == "no-store"


def test_the_link_cannot_be_bent_to_fetch_anything_else(sw):
    anon = other_client(sw)
    assert anon.get("/send/not-a-token").status_code == 404
    with sw.application.app_context():
        assert anon.get(f"/send/{export_send.make_token(999, 'wahoo', 'gpx')}").status_code == 404        # a route that doesn't exist
        assert anon.get(f"/send/{export_send.make_token(1, 'nosuchdevice', 'gpx')}").status_code == 404
        assert anon.get(f"/send/{export_send.make_token(1, 'wahoo', 'tcx')}").status_code == 404         # a file type Wahoo doesn't use here


def test_the_export_page_for_wahoo_shows_the_phone_steps_and_a_scannable_code(sw):
    sw.application.config["APP_BASE_URL"] = "https://rl.example.test"
    page = sw.get("/routes/1/export?device=wahoo").get_data(as_text=True)
    assert "Get it onto your Wahoo" in page and "Send to ELEMNT" in page and "My Routes" in page
    assert "<svg" in page and "QR code: open this route on your phone" in page and 'class="send-qr only-wide"' in page
    m = re.search(r'class="send-url"[^>]*value="(https://rl\.example\.test/send/[^"]+)"', page)
    assert m
    with sw.application.app_context():
        assert export_send.read_token(m.group(1).rsplit("/", 1)[1]) == (1, "wahoo", "gpx")
    assert "20 minutes" in page and "Download GPX" in page and "Download FIT" in page
    assert "http://" not in page.split("QR code")[1].split("</svg>")[0].replace("http://www.w3.org/2000/svg", "")   # the code draws no external image


def test_the_link_in_the_export_page_really_opens_the_route(sw):
    sw.application.config["APP_BASE_URL"] = "https://rl.example.test"
    page = sw.get("/routes/1/export?device=wahoo").get_data(as_text=True)
    path = re.search(r'value="https://rl\.example\.test(/send/[^"]+)"', page).group(1)
    r = other_client(sw).get(path)
    assert r.status_code == 200 and r.mimetype == "application/gpx+xml"


def test_only_wahoo_gets_the_phone_section_for_now(sw):
    for dev in ("garmin", "karoo", "gpx", "strava"):
        page = sw.get(f"/routes/1/export?device={dev}").get_data(as_text=True)
        assert "Get it onto your Wahoo" not in page and 'class="send-qr' not in page


def test_wahoo_offers_gpx_first_and_fit_as_the_second_choice():
    assert [f.key for f in DEVICES["wahoo"].formats] == ["gpx", "fit"]
    assert "ELEMNT ACE" in DEVICES["wahoo"].advice[0] and "Strava" in DEVICES["wahoo"].advice[1]


# ===================== riders' download from the shared sheet =====================

@pytest.mark.parametrize("name,mime", [("wahoo.gpx", "application/gpx+xml"), ("karoo.gpx", "application/gpx+xml"),
                                       ("gpx.gpx", "application/gpx+xml"), ("garmin.fit", "application/vnd.ant.fit"),
                                       ("wahoo.fit", "application/vnd.ant.fit"), ("garmin.tcx", "application/vnd.garmin.tcx+xml")])
def test_anyone_with_the_ride_link_can_download_the_route_for_their_device(sw, name, mime):
    token = share(sw)["token"]
    r = other_client(sw).get(f"/r/{token}/route/{name}")
    assert r.status_code == 200 and r.mimetype == mime
    assert f'filename="sunday-spin-{name.split(".")[0]}.{name.split(".")[1]}"' in r.headers["Content-Disposition"]
    assert r.headers["Cache-Control"] == "no-store" and r.headers["Referrer-Policy"] == "no-referrer"
    if name.endswith(".fit"):
        assert r.data[8:12] == b".FIT"
    elif name.endswith(".gpx"):
        names, pts = gpx_names(r.data)
        assert pts > 10 and "Cafe One" in names


def test_the_downloaded_file_never_carries_the_leaders_phone_number_or_notes(sw):
    token = share(sw)["token"]
    data = other_client(sw).get(f"/r/{token}/route/wahoo.gpx").data
    assert b"087 123 4567" not in data and b"Bring a spare tube" not in data


def test_the_route_file_needs_a_real_device_a_matching_extension_and_a_shared_ride(sw):
    token = share(sw)["token"]
    anon = other_client(sw)
    for bad in ("nosuch.gpx", "wahoo.tcx", "karoo.fit", "wahoo", "wahoo.", ".gpx", "wahoo.gpx.exe"):
        assert anon.get(f"/r/{token}/route/{bad}").status_code == 404, bad
    assert anon.get("/r/short/route/wahoo.gpx").status_code == 404
    assert anon.get("/r/" + "x" * 22 + "/route/wahoo.gpx").status_code == 404


def test_strava_routes_are_never_downloadable_through_a_share_link(sw):
    token = share(sw)["token"]
    from app import db
    Route.query.first().source = "strava"
    db.session.commit()
    assert other_client(sw).get(f"/r/{token}/route/wahoo.gpx").status_code == 404


def test_the_leaders_own_preview_download_needs_access_to_the_route(sw):
    assert sw.get("/routes/1/route/wahoo.gpx").status_code == 200
    assert other_client(sw).get("/routes/1/route/wahoo.gpx").status_code == 404


def test_downloads_are_rate_limited(sw, monkeypatch):
    token = share(sw)["token"]
    monkeypatch.setattr(ratelimit, "allow", lambda *a, **k: False)
    r = other_client(sw).get(f"/r/{token}/route/wahoo.gpx")
    assert r.status_code == 429 and r.headers["Retry-After"] == "60"


def test_the_sheet_offers_the_downloads_without_any_script(sw):
    token = share(sw)["token"]
    page = other_client(sw).get(f"/r/{token}").get_data(as_text=True)
    assert "Send the route to my bike computer" in page and "<details" in page
    for name in ("wahoo.gpx", "garmin.fit", "karoo.gpx", "gpx.gpx"):
        assert f'href="/r/{token}/route/{name}"' in page
    assert "Send to ELEMNT" in page and page.count("<script src=") == 0
    leader = sw.get("/routes/1/sheet").get_data(as_text=True)
    assert 'href="/routes/1/route/wahoo.gpx"' in leader


def test_the_pdf_and_text_summary_are_unchanged_by_all_this(sw):
    token = share(sw)["token"]
    anon = other_client(sw)
    assert anon.get(f"/r/{token}/cue-card.pdf").status_code == 200 and anon.get(f"/r/{token}/summary.txt").status_code == 200
    assert "bike computer" not in anon.get(f"/r/{token}/summary.txt").get_data(as_text=True)


def test_the_qr_helper_draws_an_inline_svg_with_a_text_alternative(sw):
    with sw.application.app_context():
        svg = str(export_send.qr_svg("https://rl.example.test/send/abc", "QR code: test"))
    assert svg.startswith("<svg") and "QR code: test" in svg and "https://rl.example.test/send/abc" in svg and "<script" not in svg
    assert export_send.slug("Sunday Spin – Kildare Loop!") == "sunday-spin-kildare-loop" and export_send.slug("???") == "route"
