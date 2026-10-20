"""A real map behind the route on the ride sheet.

The sheet's SVG used to draw the route on a blank card. This module makes the picture the route sits on: the OpenStreetMap
tiles that cover it, stitched into one image, lightened so the route and stops stand out.

Design rules:

* The rider's browser only ever talks to us. Our server fetches the tiles, so no rider address or cookie goes to a tile
  server, there is no script on the page, and the image works offline once the sheet has been opened (the service worker
  keeps everything under /r/).
* The picture and the SVG overlay share one projection (Web Mercator) and one frame, so the route lands exactly on the roads.
* Tiles are cached for 14 days (OpenStreetMap's tile policy asks for at least 7), requests identify the app, concurrency is
  small, and a finished picture is cached too. For heavy use point TILE_URL at a tile provider.
* If tiles can't be had, the page still works: the image simply isn't there and the old outline shows.
"""
from __future__ import annotations

import hashlib
import io
import math
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout, as_completed
from dataclasses import dataclass

TILE = 256
MAX_LAT = 85.05112878
TILE_TTL = 14 * 86400
MAP_TTL = 14 * 86400
FAIL_TTL = 120                      # remember a bad or missing tile briefly so a broken server isn't hammered
PARTIAL_TTL = 60                    # a picture with holes is served, but not kept: it heals on the next view
MAX_TILE_BYTES = 150_000
FILL = (236, 236, 241)
ATTRIBUTION = "© OpenStreetMap contributors"
SHEET_W, SHEET_H = 360, 260         # the SVG's own coordinate box; the frame has the same shape


# ------------------------------------------------------------------ the map maths

def world_px(lat: float, lon: float, z: int) -> tuple[float, float]:
    """Pixel position of a point on the whole-world Web Mercator map at zoom z."""
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    n = TILE * (2 ** z)
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


@dataclass(frozen=True)
class Frame:
    """The part of the world map the sheet shows: zoom, top-left pixel, and size in pixels (whole numbers, so the picture
    and the overlay line up exactly)."""
    z: int
    x0: int
    y0: int
    w: int
    h: int

    def project(self, lat: float, lon: float, w: float = SHEET_W, h: float = SHEET_H) -> tuple[float, float]:
        """Where a point lands in a w x h box that shows this frame."""
        px, py = world_px(lat, lon, self.z)
        return (px - self.x0) / self.w * w, (py - self.y0) / self.h * h

    def tiles(self) -> list[tuple[int, int]]:
        n = 2 ** self.z
        tx0, tx1 = self.x0 // TILE, (self.x0 + self.w - 1) // TILE
        ty0, ty1 = max(0, self.y0 // TILE), min(n - 1, (self.y0 + self.h - 1) // TILE)
        return [(tx % n, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]


def frame_for(lats, lons, *, aspect: float = SHEET_W / SHEET_H, max_w: int = 900, pad: float = 0.09, margin_px: int = 24,
              z_max: int = 15, z_min: int = 3) -> Frame:
    """The sharpest zoom at which the whole route fits in a picture at most `max_w` pixels wide, with a margin round it."""
    lat_lo, lat_hi, lon_lo, lon_hi = min(lats), max(lats), min(lons), max(lons)
    pick = None
    for z in range(z_max, z_min - 1, -1):
        xa, ya = world_px(lat_hi, lon_lo, z)
        xb, yb = world_px(lat_lo, lon_hi, z)
        span_x, span_y = abs(xb - xa), abs(yb - ya)
        need_w = span_x * (1 + 2 * pad) + 2 * margin_px
        need_h = span_y * (1 + 2 * pad) + 2 * margin_px
        fw = max(need_w, need_h * aspect)
        pick = (z, (xa + xb) / 2, (ya + yb) / 2, fw)
        if fw <= max_w:
            break
    z, cx, cy, fw = pick
    w = int(math.ceil(fw))
    h = int(math.ceil(w / aspect))
    return Frame(z=z, x0=int(round(cx - w / 2)), y0=int(round(cy - h / 2)), w=w, h=h)


def frame_for_plan(plan, **kw) -> Frame:
    return frame_for(plan.lats, plan.lons, **kw)


def frame_key(frame: Frame, tile_url: str) -> str:
    """A short fingerprint of the picture: changes if the route's frame or the tile source does (it goes in the image URL)."""
    raw = f"{tile_url}|{frame.z}|{frame.x0}|{frame.y0}|{frame.w}|{frame.h}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


# ------------------------------------------------------------------ getting tiles

class HttpTileClient:
    """Fetches one tile from a standard {z}/{x}/{y} server. Returns the image bytes, or None for anything that isn't a good tile."""

    def __init__(self, url_template: str, user_agent: str, timeout: float = 3.0, session=None):
        if not all(k in url_template for k in ("{z}", "{x}", "{y}")):
            raise ValueError("TILE_URL needs {z}, {x} and {y}")
        self.template, self.timeout = url_template, timeout
        self.headers = {"User-Agent": user_agent, "Accept": "image/png,image/jpeg,image/webp;q=0.9"}
        self.session = session

    def _session(self):
        if self.session is None:
            import requests
            self.session = requests.Session()
        return self.session

    def fetch(self, z: int, x: int, y: int) -> bytes | None:
        url = self.template.format(z=z, x=x, y=y, s="a", r="")
        try:
            r = self._session().get(url, headers=self.headers, timeout=self.timeout)
        except Exception:
            return None
        if getattr(r, "status_code", 0) != 200:
            return None
        data = r.content or b""
        return data if 0 < len(data) <= MAX_TILE_BYTES else None


def get_client(config):
    """The tile client: a test's fake, else the configured server. None when the map is switched off."""
    if not config.get("BASEMAP_ENABLED", True):
        return None
    if config.get("TILE_CLIENT") is not None:
        return config["TILE_CLIENT"]
    base = (config.get("APP_BASE_URL") or "").rstrip("/")
    ua = f"RideLeaderKit/1.0 (+{base or 'https://rideleader.cyclingtools.cc'})"
    try:
        return HttpTileClient(config["TILE_URL"], ua)
    except ValueError:
        return None


# ------------------------------------------------------------------ a small byte cache (Redis if there is one)

class ByteCache:
    """Redis in production (shared and kept across restarts); in memory otherwise or when Redis is down, so a Redis outage
    only means tiles are fetched again."""

    def __init__(self, url: str | None = None, cap: int = 600):
        self.mem: OrderedDict[str, tuple[float, bytes]] = OrderedDict()
        self.cap, self.r = cap, None
        if url:
            try:
                from redis import Redis
                self.r = Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
            except Exception:
                self.r = None

    def get(self, key: str) -> bytes | None:
        if self.r is not None:
            try:
                v = self.r.get(key)
                if v is not None:
                    return v
            except Exception:
                pass
        item = self.mem.get(key)
        if item and item[0] > time.time():
            self.mem.move_to_end(key)
            return item[1]
        self.mem.pop(key, None)
        return None

    def set(self, key: str, value: bytes, ttl: int) -> None:
        if self.r is not None:
            try:
                self.r.setex(key, ttl, value)
                return
            except Exception:
                pass
        self.mem[key] = (time.time() + ttl, value)
        self.mem.move_to_end(key)
        while len(self.mem) > self.cap:
            self.mem.popitem(last=False)


def get_cache(app) -> ByteCache:
    ext = app.extensions
    if "basemap_cache" not in ext:
        url = None if app.config.get("TESTING") else app.config.get("REDIS_URL")
        ext["basemap_cache"] = ByteCache(url)
    return ext["basemap_cache"]


# ------------------------------------------------------------------ making the picture

def _valid_tile(data: bytes | None):
    """A decoded 256 px tile, or None if the bytes aren't one."""
    if not data:
        return None
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        im.load()
        return im.convert("RGB") if im.size == (TILE, TILE) else None
    except Exception:
        return None


def render(frame: Frame, client, cache: ByteCache, tile_url: str, *, budget_s: float = 9.0, workers: int = 4):
    """The picture for a frame as (WebP bytes or None, complete). Cached: tiles for 14 days, the finished picture too."""
    from PIL import Image, ImageEnhance

    fk = frame_key(frame, tile_url)
    hit = cache.get("basemap:" + fk)
    if hit:
        return hit, True
    part = cache.get("basemap-partial:" + fk)
    if part:
        return part, False
    coords = frame.tiles()
    tiles: dict[tuple[int, int], object] = {}
    todo = []
    for (x, y) in coords:
        ck = f"tile:{hashlib.sha1(tile_url.encode()).hexdigest()[:8]}:{frame.z}:{x}:{y}"
        got = cache.get(ck)
        if got == b"":
            continue                                        # known bad for a moment: leave the hole
        im = _valid_tile(got) if got else None
        if im is not None:
            tiles[(x, y)] = im
        else:
            todo.append((x, y, ck))

    def one(item):
        x, y, ck = item
        data = client.fetch(frame.z, x, y)
        im = _valid_tile(data)
        cache.set(ck, data if im is not None else b"", TILE_TTL if im is not None else FAIL_TTL)
        return (x, y), im

    if todo:
        ex = ThreadPoolExecutor(max_workers=workers)
        futs = [ex.submit(one, t) for t in todo]
        try:
            for f in as_completed(futs, timeout=budget_s):
                pos, im = f.result()
                if im is not None:
                    tiles[pos] = im
        except FutureTimeout:
            pass
        finally:
            ex.shutdown(wait=False, cancel_futures=True)
    if not tiles:
        return None, False
    n = 2 ** frame.z
    tx0, ty0 = frame.x0 // TILE, max(0, frame.y0 // TILE)
    tx1 = (frame.x0 + frame.w - 1) // TILE
    ty1 = min(n - 1, (frame.y0 + frame.h - 1) // TILE)
    canvas = Image.new("RGB", ((tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE), FILL)
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            im = tiles.get((tx % n, ty))
            if im is not None:
                canvas.paste(im, ((tx - tx0) * TILE, (ty - ty0) * TILE))
    ox, oy = frame.x0 - tx0 * TILE, frame.y0 - ty0 * TILE
    crop = canvas.crop((ox, oy, ox + frame.w, oy + frame.h))
    # Lighten and calm the colours, so the route, stops and hazards drawn over it stand out (and it prints well).
    crop = ImageEnhance.Color(crop).enhance(0.8)
    crop = Image.blend(crop, Image.new("RGB", crop.size, (255, 255, 255)), 0.28)
    out = io.BytesIO()
    crop.save(out, "WEBP", quality=78, method=4)
    data = out.getvalue()
    complete = len(tiles) == len(coords)
    cache.set(("basemap:" if complete else "basemap-partial:") + fk, data, MAP_TTL if complete else PARTIAL_TTL)
    return data, complete
