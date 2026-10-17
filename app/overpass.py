"""Overpass client: one query per 0.1 degree tile, all categories at once, cached by the caller."""
import hashlib
import math
import time

import requests

from .categories import CATEGORIES

TILE_DEG = 0.1
USER_AGENT = "RideLeaderKit/0.1 (club ride planning; contact: set OVERPASS_CONTACT)"


def _selectors() -> list[str]:
    return sorted({r.overpass() for c in CATEGORIES for r in c.rules})


def query_version() -> str:
    """Short hash of the tag selectors. Cached tiles are keyed by it, so adding a category refetches
    instead of silently serving tiles that never contained it."""
    return hashlib.sha1("|".join(_selectors()).encode()).hexdigest()[:6]


def tile_id(ix: int, iy: int) -> str:
    return f"{TILE_DEG}:{ix}:{iy}:{query_version()}"


def tile_bbox(ix: int, iy: int) -> tuple[float, float, float, float]:
    """(south, west, north, east)"""
    return (iy * TILE_DEG, ix * TILE_DEG, (iy + 1) * TILE_DEG, (ix + 1) * TILE_DEG)


def tiles_for_points(lats, lons, pad_deg: float = 0.015) -> set[tuple[int, int]]:
    """Tiles touched by any route point, padded so a POI just across a tile edge is still found.

    Assumes consecutive points are closer than a tile (true for snapped/recorded tracks).
    """
    out = set()
    for la, lo in zip(lats, lons):
        for ix in range(math.floor((lo - pad_deg) / TILE_DEG), math.floor((lo + pad_deg) / TILE_DEG) + 1):
            for iy in range(math.floor((la - pad_deg) / TILE_DEG), math.floor((la + pad_deg) / TILE_DEG) + 1):
                out.add((ix, iy))
    return out


def build_query(bbox, timeout: int = 60) -> str:
    s, w, n, e = bbox
    body = "".join(f"nwr{sel}({s},{w},{n},{e});" for sel in _selectors())
    return f"[out:json][timeout:{timeout}];({body});out center tags;"


class OverpassError(RuntimeError):
    pass


DOWN_FOR = 120          # seconds to stop trying after Overpass fails to answer at all
_down_until: dict[str, float] = {}     # url -> monotonic time; shared by every client in this process


class OverpassClient:
    def __init__(self, url: str, min_interval: float = 1.0, timeout: float | tuple = 90, session=None, sleep=time.sleep,
                 retries: int = 3):
        self.url, self.min_interval, self.timeout, self.retries = url, min_interval, timeout, retries
        self.workers = 1      # tiles fetched at once; raised only for a server we run ourselves
        self.session = session or requests.Session()
        self._sleep, self._last = sleep, 0.0

    def fetch(self, bbox) -> list[dict]:
        return self.run(build_query(bbox))

    def run(self, query: str) -> list[dict]:
        """POST any Overpass query, with the same pacing and retries; returns the elements."""
        if _down_until.get(self.url, 0) > time.monotonic():     # a page waiting 90 s for a dead server is worse than "couldn't check"
            raise OverpassError("Overpass isn't answering; not retrying for a couple of minutes")
        for attempt in range(1, self.retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.session.post(self.url, data={"data": query}, headers={"User-Agent": USER_AGENT},
                                      timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as e:
                _down_until[self.url] = time.monotonic() + DOWN_FOR
                raise OverpassError(f"Overpass didn't answer ({type(e).__name__})") from e
            if r.status_code in (429, 502, 503, 504):  # busy: back off and retry
                self._sleep(5 * attempt)
                continue
            r.raise_for_status()
            body = r.json()
            if "runtime error" in str(body.get("remark", "")):
                raise OverpassError(body["remark"])
            return body.get("elements", [])
        raise OverpassError(f"Overpass busy (last status {r.status_code}); try again shortly")


def get_client(app_config, interactive: bool = False) -> OverpassClient:
    """interactive: a person is waiting on a page, so give up quickly (the background scan keeps the patient settings)."""
    if interactive:
        return OverpassClient(app_config["OVERPASS_URL"], min_interval=app_config["OVERPASS_MIN_INTERVAL"], timeout=(3, 15), retries=1)   # (connect, read)
    c = OverpassClient(app_config["OVERPASS_URL"], min_interval=app_config["OVERPASS_MIN_INTERVAL"])
    c.workers = max(1, int(app_config.get("OVERPASS_WORKERS", 1)))
    return c
