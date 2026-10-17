"""RoutingProvider interface and the BRouter implementation.

Everything above this module talks to `RoutingProvider.route(points, options)`, so BRouter can be swapped for
GraphHopper / OpenRouteService / a self-hosted instance by writing one class.
"""
import time
from dataclasses import dataclass
from typing import Protocol, Sequence

import requests

Coord = tuple[float, float, float | None]   # lon, lat, ele (metres or None)


class RoutingError(RuntimeError):
    """Raised with a message that is safe to show to a ride leader."""


@dataclass(frozen=True)
class RouteOptions:
    avoid_busy: bool = False      # off by default: an edit should stay on the roads the leader chose; opt in to quieter ones
    allow_unpaved: bool = False   # road-bike default: stay on tarmac

    def profile(self) -> str:
        """BRouter profile. Road bike by default; the 'low traffic' variant avoids busy roads."""
        if self.allow_unpaved:
            return "trekking"     # balanced profile that will use gravel and tracks
        return "fastbike-lowtraffic" if self.avoid_busy else "fastbike"

    def to_json(self) -> dict:
        return {"avoid_busy": self.avoid_busy, "allow_unpaved": self.allow_unpaved}

    @staticmethod
    def from_json(d: dict | None) -> "RouteOptions":
        d = d or {}
        return RouteOptions(bool(d.get("avoid_busy", False)), bool(d.get("allow_unpaved", False)))


class RoutingProvider(Protocol):
    def route(self, points: Sequence[tuple[float, float]], options: RouteOptions,
              avoid: Sequence[tuple[float, float, float]] | None = None) -> list[Coord]:
        """Snap a path through `points` ((lat, lon) pairs, in order) to roads. Returns lon/lat/ele coordinates.
        `avoid` is a list of (lat, lon, radius_m) circles the route must stay out of (closures)."""


class BRouterProvider:
    """BRouter HTTP API (https://github.com/abrensch/brouter). The public brouter.de instance is shared and
    throttled: fine for development, but self-host (BROUTER_URL) before a club relies on it."""

    def __init__(self, url: str, min_interval: float = 1.0, timeout: float = 30, session=None, sleep=time.sleep):
        self.url, self.min_interval, self.timeout = url, min_interval, timeout
        self.session, self._sleep, self._last = session or requests.Session(), sleep, 0.0

    def route(self, points, options: RouteOptions, avoid=None) -> list[Coord]:
        if len(points) < 2:
            raise RoutingError("Need at least two points to route between.")
        lonlats = "|".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)
        params = {"lonlats": lonlats, "profile": options.profile(), "alternativeidx": 0, "format": "geojson"}
        if avoid:
            params["nogos"] = "|".join(f"{lon:.6f},{lat:.6f},{radius:.0f}" for lat, lon, radius in avoid)
        last_status = None
        for attempt in range(1, 4):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.session.get(self.url, params=params, timeout=self.timeout)
            except requests.RequestException as e:
                raise RoutingError("The routing service didn't answer. Try again in a moment.") from e
            last_status = r.status_code
            if r.status_code in (429, 502, 503, 504):
                self._sleep(2 * attempt)
                continue
            break
        else:
            raise RoutingError("The routing service is busy. Try again in a moment.")
        if last_status != 200:
            # BRouter answers with plain text; 403/500 on the public instance mean "can't route this"
            raise RoutingError("Couldn't find a cycling route there. Try a point nearer a road, "
                               "or switch off 'avoid busy roads'.")
        try:
            feat = r.json()["features"][0]["geometry"]["coordinates"]
        except (ValueError, KeyError, IndexError) as e:
            raise RoutingError("The routing service sent something unexpected.") from e
        out = [(float(c[0]), float(c[1]), float(c[2]) if len(c) > 2 else None) for c in feat]
        if len(out) < 2:
            raise RoutingError("Couldn't find a cycling route there.")
        return out
