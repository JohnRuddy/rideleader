"""The map picture behind the route on the ride sheet: the map maths, tile fetching and caching, the image routes, and the
sheet's SVG (which falls back to the outline whenever tiles can't be had)."""
import io
import re

import pytest
from PIL import Image

from app import basemap as bm
from app import ratelimit
from app.models import Route
from app.plan import Plan
from tests.test_auth_security import other_client
from tests.test_share import share, sw  # noqa: F401  (sw is a fixture)

TILE_URL = "https://tile.example.test/{z}/{x}/{y}.png"


def png(color=(120, 160, 90), size=(256, 256)):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return out.getvalue()


class FakeTiles:
    def __init__(self, mode="ok", bad=()):
        self.calls, self.mode, self.bad = [], mode, set(bad)

    def fetch(self, z, x, y):
        self.calls.append((z, x, y))
        if self.mode == "none" or (x, y) in self.bad:
            return None
        if self.mode == "garbage":
            return b"not a picture"
        if self.mode == "wrongsize":
            return png(size=(100, 100))
        return png(((x * 37) % 200 + 30, (y * 53) % 200 + 30, 120))


def webp_size(data):
    im = Image.open(io.BytesIO(data))
    assert im.format == "WEBP"
    return im.size


# ===================== the map maths =====================

def test_world_pixels_match_known_points():
    assert bm.world_px(0, 0, 0) == (128.0, 128.0)
    assert bm.world_px(0, -180, 3)[0] == 0 and bm.world_px(0, 180, 3)[0] == 256 * 8
    x1, y1 = bm.world_px(53.16, -6.9, 10)
    x2, y2 = bm.world_px(53.16, -6.9, 11)
    assert abs(x2 - 2 * x1) < 1e-6 and abs(y2 - 2 * y1) < 1e-6                  # one zoom in doubles every pixel position
    assert bm.world_px(89.9, 0, 5)[1] == bm.world_px(85.05112878, 0, 5)[1]       # the poles are clamped, never infinite


def route_pts(lat0=53.16, lon0=-6.9, dlat=0.0, dlon=0.3, n=50):
    return [lat0 + dlat * i / (n - 1) for i in range(n)], [lon0 + dlon * i / (n - 1) for i in range(n)]


def test_the_frame_holds_the_whole_route_with_a_margin_and_the_sheets_shape():
    lats, lons = route_pts(dlat=0.05, dlon=0.3)
    f = bm.frame_for(lats, lons)
    assert f.w <= 900 and abs(f.w / f.h - bm.SHEET_W / bm.SHEET_H) < 0.02
    for la, lo in zip(lats, lons):
        x, y = f.project(la, lo)
        assert 12 <= x <= bm.SHEET_W - 12 and 12 <= y <= bm.SHEET_H - 12


def test_the_zoom_is_the_sharpest_that_fits_and_is_capped():
    big = bm.frame_for(*route_pts(dlat=2.0, dlon=3.0))                      # roughly Dublin to Cork and beyond
    mid = bm.frame_for(*route_pts(dlat=0.05, dlon=0.3))
    tiny = bm.frame_for(*route_pts(dlat=0.001, dlon=0.002))
    assert big.z < mid.z <= tiny.z <= 15
    for f in (big, mid, tiny):
        assert f.w <= 900 or f.z == 3


def test_the_frame_never_needs_a_silly_number_of_tiles():
    for dlat, dlon in ((0.0, 0.3), (2.0, 3.0), (0.4, 0.1), (0.001, 0.001)):
        f = bm.frame_for(*route_pts(dlat=dlat, dlon=dlon))
        assert 1 <= len(f.tiles()) <= 36, (dlat, dlon, len(f.tiles()))


def test_the_frame_key_changes_with_the_frame_or_the_tile_source():
    f = bm.frame_for(*route_pts())
    g = bm.frame_for(*route_pts(dlon=0.4))
    assert bm.frame_key(f, TILE_URL) == bm.frame_key(f, TILE_URL)
    assert bm.frame_key(f, TILE_URL) != bm.frame_key(g, TILE_URL) != bm.frame_key(f, "https://other.test/{z}/{x}/{y}.png")


# ===================== getting tiles =====================

class FakeResp:
    def __init__(self, code=200, content=b"x"):
        self.status_code, self.content = code, content


class FakeSession:
    def __init__(self, resp=None, boom=False):
        self.resp, self.boom, self.seen = resp or FakeResp(), boom, []

    def get(self, url, headers=None, timeout=None):
        self.seen.append((url, headers, timeout))
        if self.boom:
            raise OSError("down")
        return self.resp


def test_the_http_client_names_itself_and_builds_the_address():
    s = FakeSession(FakeResp(200, png()))
    c = bm.HttpTileClient("https://{s}.tiles.test/{z}/{x}/{y}{r}.png", "RideLeaderKit/1.0 (+https://example.test)", session=s)
    assert c.fetch(7, 60, 41) == png()
    url, headers, timeout = s.seen[0]
    assert url == "https://a.tiles.test/7/60/41.png" and headers["User-Agent"].startswith("RideLeaderKit/1.0") and timeout == 3.0


def test_the_http_client_returns_nothing_for_bad_answers():
    assert bm.HttpTileClient(TILE_URL, "ua", session=FakeSession(FakeResp(404))).fetch(1, 1, 1) is None
    assert bm.HttpTileClient(TILE_URL, "ua", session=FakeSession(FakeResp(200, b""))).fetch(1, 1, 1) is None
    assert bm.HttpTileClient(TILE_URL, "ua", session=FakeSession(FakeResp(200, b"x" * (bm.MAX_TILE_BYTES + 1)))).fetch(1, 1, 1) is None
    assert bm.HttpTileClient(TILE_URL, "ua", session=FakeSession(boom=True)).fetch(1, 1, 1) is None
    with pytest.raises(ValueError):
        bm.HttpTileClient("https://tiles.test/static.png", "ua")


def test_the_client_comes_from_config_and_can_be_switched_off():
    fake = FakeTiles()
    assert bm.get_client({"TILE_CLIENT": fake}) is fake
    assert bm.get_client({"TILE_CLIENT": fake, "BASEMAP_ENABLED": False}) is None
    assert isinstance(bm.get_client({"TILE_URL": TILE_URL, "APP_BASE_URL": "https://rl.example.test"}), bm.HttpTileClient)
    assert bm.get_client({"TILE_URL": "https://broken.test/static.png"}) is None


# ===================== making and caching the picture =====================

def test_the_picture_is_the_size_of_the_frame_lighter_than_the_tiles_and_cached():
    lats, lons = route_pts(dlat=0.05, dlon=0.3)
    f = bm.frame_for(lats, lons)
    fake, cache = FakeTiles(), bm.ByteCache()
    data, complete = bm.render(f, fake, cache, TILE_URL)
    assert complete and webp_size(data) == (f.w, f.h) and len(fake.calls) == len(f.tiles())
    px = Image.open(io.BytesIO(data)).convert("RGB").getpixel((f.w // 2, f.h // 2))
    assert min(px) > 100                                                        # washed out: the route will stand out on it
    n = len(fake.calls)
    again, complete2 = bm.render(f, fake, cache, TILE_URL)
    assert again == data and complete2 and len(fake.calls) == n                 # served from the finished-picture cache


def test_a_neighbouring_route_reuses_cached_tiles():
    f1 = bm.frame_for(*route_pts(dlat=0.02, dlon=0.2))
    f2 = bm.frame_for(*route_pts(lon0=-6.89, dlat=0.02, dlon=0.2))
    fake, cache = FakeTiles(), bm.ByteCache()
    bm.render(f1, fake, cache, TILE_URL)
    first = len(fake.calls)
    bm.render(f2, fake, cache, TILE_URL)
    shared = set(f1.tiles()) & set(f2.tiles())
    assert shared and len(fake.calls) - first == len(set(f2.tiles()) - set(f1.tiles())) if f1.z == f2.z else True


def test_a_picture_with_a_hole_is_served_but_marked_incomplete_and_not_kept_long():
    f = bm.frame_for(*route_pts(dlat=0.05, dlon=0.3))
    hole = f.tiles()[0]
    fake, cache = FakeTiles(bad=[hole]), bm.ByteCache()
    data, complete = bm.render(f, fake, cache, TILE_URL)
    assert data and not complete and webp_size(data) == (f.w, f.h)
    n = len(fake.calls)
    data2, complete2 = bm.render(f, fake, cache, TILE_URL)
    assert data2 == data and not complete2 and len(fake.calls) == n             # remembered briefly: no hammering a broken tile
    assert cache.get("basemap:" + bm.frame_key(f, TILE_URL)) is None            # and never stored as the finished picture


def test_no_tiles_at_all_gives_no_picture_and_a_missing_tile_is_not_asked_for_again_straight_away():
    f = bm.frame_for(*route_pts(dlat=0.05, dlon=0.3))
    fake, cache = FakeTiles("none"), bm.ByteCache()
    assert bm.render(f, fake, cache, TILE_URL) == (None, False)
    n = len(fake.calls)
    assert bm.render(f, fake, cache, TILE_URL) == (None, False) and len(fake.calls) == n


@pytest.mark.parametrize("mode", ["garbage", "wrongsize"])
def test_bytes_that_are_not_a_proper_tile_are_never_used(mode):
    f = bm.frame_for(*route_pts(dlat=0.05, dlon=0.3))
    assert bm.render(f, FakeTiles(mode), bm.ByteCache(), TILE_URL) == (None, False)


def test_the_byte_cache_expires_and_stays_small():
    c = bm.ByteCache(cap=3)
    for i in range(5):
        c.set(f"k{i}", b"v", 100)
    assert c.get("k0") is None and c.get("k4") == b"v"
    c.set("old", b"v", -1)
    assert c.get("old") is None


# ===================== the image routes =====================

@pytest.fixture
def tiles(sw):  # noqa: F811
    fake = FakeTiles()
    sw.application.config.update(TILE_CLIENT=fake, TILE_URL=TILE_URL, BASEMAP_ENABLED=True, RATELIMIT_ENABLED=False)
    sw.fake = fake
    return sw


def test_a_shared_ride_serves_its_map_picture_with_safe_headers_and_caches_it(tiles):
    token = share(tiles)["token"]
    anon = other_client(tiles)
    r = anon.get(f"/r/{token}/map.webp")
    assert r.status_code == 200 and r.mimetype == "image/webp" and webp_size(r.data)[0] > 300
    assert r.headers["Cache-Control"] == "public, max-age=86400" and r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Robots-Tag"] == "noindex, nofollow" and r.headers["Referrer-Policy"] == "no-referrer"
    n = len(tiles.fake.calls)
    assert n > 0
    assert anon.get(f"/r/{token}/map.webp").data == r.data and len(tiles.fake.calls) == n


def test_the_map_picture_is_only_for_people_with_the_link(tiles):
    token = share(tiles)["token"]
    anon = other_client(tiles)
    assert anon.get("/r/short/map.webp").status_code == 404
    assert anon.get("/r/" + "x" * 22 + "/map.webp").status_code == 404
    assert anon.get("/routes/1/map.webp").status_code == 404                     # the leader's own address needs access to the route
    assert tiles.get("/routes/1/map.webp").status_code == 200
    route = Route.query.first()
    route.source = "strava"                                                      # Strava routes are never shown as they are
    from app import db
    db.session.commit()
    assert anon.get(f"/r/{token}/map.webp").status_code == 404


def test_the_sheet_points_at_the_picture_and_overlays_the_route_on_it(tiles):
    token = share(tiles)["token"]
    page = other_client(tiles).get(f"/r/{token}").get_data(as_text=True)
    route = Route.query.first()
    frame = bm.frame_for_plan(Plan(route))
    v = bm.frame_key(frame, TILE_URL)
    assert f'<image href="/r/{token}/map.webp?v={v}"' in page
    svg = page[page.index('<svg viewBox="0 0 360 260"'):page.index("</svg>", page.index('class="sheet-map"')) + 6]
    assert "© OpenStreetMap contributors" in svg and "OpenStreetMap background" in svg
    assert "tile.openstreetmap.org" not in page and "tile.example.test" not in page          # the rider's browser never talks to a tile server
    assert page.count("<script src=") == 0
    m = re.search(r'<circle cx="([\d.]+)" cy="([\d.]+)" r="8" fill="#34c759"', svg)
    la, lo = Plan(route).line.point(0)
    ex, ey = frame.project(la, lo)
    assert abs(float(m.group(1)) - ex) < 0.11 and abs(float(m.group(2)) - ey) < 0.11          # the start lands on the picture's road
    assert 0 <= ex <= 360 and 0 <= ey <= 260


def test_the_leaders_preview_uses_its_own_picture_address(tiles):
    share(tiles)
    page = tiles.get("/routes/1/sheet").get_data(as_text=True)
    assert '<image href="/routes/1/map.webp?v=' in page


def test_when_tiles_cannot_be_had_the_picture_says_so_and_the_sheet_still_works(tiles):
    tiles.application.config["TILE_CLIENT"] = FakeTiles("none")
    token = share(tiles)["token"]
    anon = other_client(tiles)
    r = anon.get(f"/r/{token}/map.webp")
    assert r.status_code == 503 and r.headers["Retry-After"] == "30" and "no-store" in r.headers["Cache-Control"]
    page = anon.get(f"/r/{token}")
    assert page.status_code == 200 and 'class="sheet-map"' in page.get_data(as_text=True)   # the outline is still there


def test_switching_the_map_off_returns_the_plain_outline(tiles):
    token = share(tiles)["token"]
    tiles.application.config["BASEMAP_ENABLED"] = False
    anon = other_client(tiles)
    assert anon.get(f"/r/{token}/map.webp").status_code == 404
    page = anon.get(f"/r/{token}").get_data(as_text=True)
    assert 'class="sheet-map"' in page and "<image" not in page and "map.webp" not in page


def test_building_pictures_is_rate_limited_but_cached_ones_are_not(tiles, monkeypatch):
    token = share(tiles)["token"]
    anon = other_client(tiles)
    monkeypatch.setattr(ratelimit, "allow", lambda *a, **k: False)
    assert anon.get(f"/r/{token}/map.webp").status_code == 429
    monkeypatch.setattr(ratelimit, "allow", lambda *a, **k: True)
    assert anon.get(f"/r/{token}/map.webp").status_code == 200
    monkeypatch.setattr(ratelimit, "allow", lambda *a, **k: False)
    assert anon.get(f"/r/{token}/map.webp").status_code == 200                      # already built: no limit needed


def test_the_text_summary_and_the_pdf_never_trigger_tile_fetches(tiles):
    token = share(tiles)["token"]
    anon = other_client(tiles)
    assert anon.get(f"/r/{token}/summary.txt").status_code == 200
    assert anon.get(f"/r/{token}/cue-card.pdf").status_code == 200
    assert tiles.fake.calls == []


def test_changing_the_tile_source_changes_the_picture_address_so_caches_refresh(tiles):
    token = share(tiles)["token"]
    anon = other_client(tiles)
    a = re.search(r"map\.webp\?v=(\w+)", anon.get(f"/r/{token}").get_data(as_text=True)).group(1)
    tiles.application.config["TILE_URL"] = "https://another.example.test/{z}/{x}/{y}.png"
    b = re.search(r"map\.webp\?v=(\w+)", anon.get(f"/r/{token}").get_data(as_text=True)).group(1)
    assert a != b


def test_the_service_worker_keeps_the_picture_for_offline_use():
    from app.share_views import SW_JS
    assert "startsWith('/r/')" in SW_JS and "caches.match" in SW_JS             # everything under /r/, so /r/<token>/map.webp too


def test_the_privacy_page_says_where_the_map_comes_from(client):
    page = client.get("/privacy").get_data(as_text=True)
    assert "OpenStreetMap map tiles" in page and "no map server sees your address" in page
