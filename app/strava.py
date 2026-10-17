"""Strava API access, read-only by construction.

The interface exposes exactly what this app is allowed to do: OAuth, list the signed-in athlete's routes,
fetch one route, export one route as GPX, revoke access, and manage the webhook subscription. There is no method
that creates or changes a route or activity, because Strava's API has no endpoint for creating routes and the
app must not automate the Strava website. "Send to Strava" is a download plus a link (see the export page).

Endpoints and scopes were checked against developers.strava.com (authentication, webhooks, reference) in
October 2026. Live use is pending: everything here is tested against fakes.
"""
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from urllib.parse import urlencode

import requests

from . import polyline

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
REVOKE_URL = "https://www.strava.com/oauth/revoke"            # recommended from 2026-06-01
LEGACY_DEAUTH_URL = "https://www.strava.com/oauth/deauthorize"  # supported until 2027-06-01
API = "https://www.strava.com/api/v3"
SCOPE = "read,read_all"        # read_all is what lets us see private routes; users may untick it, so we check what was granted


class StravaError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class StravaUnauthorized(StravaError):
    """401: the token was revoked or has expired beyond refresh."""


class StravaNotFound(StravaError):
    """404: the route no longer exists (deleted or made private on Strava)."""


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str
    expires_at: datetime
    scope: str
    athlete_id: int | None = None


@dataclass(frozen=True)
class StravaRoute:
    id: int
    name: str
    distance_m: float
    elevation_gain_m: float
    summary_polyline: str
    private: bool


class StravaClient(Protocol):
    def authorize_url(self, state: str, redirect_uri: str) -> str: ...
    def exchange_code(self, code: str) -> TokenSet: ...
    def refresh(self, refresh_token: str) -> TokenSet: ...
    def list_routes(self, access_token: str, athlete_id: int, page: int = 1, per_page: int = 30) -> list[StravaRoute]: ...
    def get_route(self, access_token: str, route_id: int) -> StravaRoute: ...
    def export_gpx(self, access_token: str, route_id: int) -> bytes: ...
    def revoke(self, access_token: str) -> None: ...
    def create_subscription(self, callback_url: str, verify_token: str) -> int: ...


def _expiry(epoch) -> datetime:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc)


def _route(d: dict) -> StravaRoute:
    return StravaRoute(id=int(d["id"]), name=d.get("name") or "Untitled route", distance_m=float(d.get("distance") or 0),
                       elevation_gain_m=float(d.get("elevation_gain") or 0),
                       summary_polyline=(d.get("map") or {}).get("summary_polyline") or "", private=bool(d.get("private")))


class HttpStravaClient:
    def __init__(self, client_id: str, client_secret: str, session=None, timeout: float = 15):
        self.client_id, self.client_secret = client_id, client_secret
        self.http, self.timeout = session or requests.Session(), timeout

    # ---- OAuth ----
    def authorize_url(self, state, redirect_uri):
        return AUTHORIZE_URL + "?" + urlencode({"client_id": self.client_id, "redirect_uri": redirect_uri,
                                                 "response_type": "code", "approval_prompt": "auto",
                                                 "scope": SCOPE, "state": state})

    def _token(self, data: dict) -> TokenSet:
        r = self.http.post(TOKEN_URL, data={"client_id": self.client_id, "client_secret": self.client_secret, **data},
                           timeout=self.timeout)
        if r.status_code in (400, 401):
            raise StravaUnauthorized("Strava didn't accept that authorisation.", r.status_code)
        self._check(r)
        b = r.json()
        return TokenSet(b["access_token"], b["refresh_token"], _expiry(b["expires_at"]), b.get("scope", ""),
                        (b.get("athlete") or {}).get("id"))

    def exchange_code(self, code):
        return self._token({"code": code, "grant_type": "authorization_code"})

    def refresh(self, refresh_token):
        """Refresh tokens rotate: the returned refresh token replaces the one sent, which stops working at once."""
        return self._token({"refresh_token": refresh_token, "grant_type": "refresh_token"})

    def revoke(self, access_token):
        try:
            r = self.http.post(REVOKE_URL, data={"token": access_token, "token_type_hint": "access_token"},
                               auth=(self.client_id, self.client_secret), timeout=self.timeout)
            if r.status_code == 200:
                return
        except requests.RequestException:
            pass
        self.http.post(LEGACY_DEAUTH_URL, data={"access_token": access_token}, timeout=self.timeout)

    # ---- reading routes (the only data we touch) ----
    def _get(self, path, access_token):
        r = self.http.get(f"{API}{path}", headers={"Authorization": f"Bearer {access_token}"}, timeout=self.timeout)
        if r.status_code == 401:
            raise StravaUnauthorized("Strava no longer accepts our access.", 401)
        if r.status_code == 404:
            raise StravaNotFound("That route is no longer on Strava.", 404)
        self._check(r)
        return r

    @staticmethod
    def _check(r):
        if r.status_code == 429:
            raise StravaError("Strava is limiting requests right now. Try again in a few minutes.", 429)
        if r.status_code >= 400:
            raise StravaError(f"Strava returned an error ({r.status_code}).", r.status_code)

    def list_routes(self, access_token, athlete_id, page=1, per_page=30):
        return [_route(d) for d in self._get(f"/athletes/{int(athlete_id)}/routes?page={int(page)}&per_page={int(per_page)}",
                                              access_token).json()]

    def get_route(self, access_token, route_id):
        return _route(self._get(f"/routes/{int(route_id)}", access_token).json())

    def export_gpx(self, access_token, route_id):
        return self._get(f"/routes/{int(route_id)}/export_gpx", access_token).content

    # ---- webhook subscription (one per app) ----
    def create_subscription(self, callback_url, verify_token):
        r = self.http.post(f"{API}/push_subscriptions", data={"client_id": self.client_id, "client_secret": self.client_secret,
                                                               "callback_url": callback_url, "verify_token": verify_token},
                           timeout=self.timeout)
        self._check(r)
        return int(r.json()["id"])


# ------------------------------------------------------------------ development mock

def _loop_points(lat0, lon0, rx, ry, n=240, wobble=0.0):
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / (n - 1)
        pts.append((lat0 + ry * math.sin(t) + wobble * math.sin(5 * t), lon0 + rx * math.cos(t) - rx, 90 + 60 * (1 + math.sin(t - 1)) + 15 * math.sin(7 * t)))
    return pts


_MOCK_ROUTES = {
    9001: ("Mock: Curragh loop", False, _loop_points(53.16, -6.80, 0.045, 0.020, wobble=0.002)),
    9002: ("Mock: Hill of Allen climb", False, _loop_points(53.19, -6.75, 0.030, 0.026, wobble=0.003)),
    9003: ("Mock: Private recovery spin", True, _loop_points(53.14, -6.85, 0.020, 0.012)),
}


def _mock_gpx(rid: int) -> bytes:
    name, _, pts = _MOCK_ROUTES[rid]
    trk = "".join(f'<trkpt lat="{la:.6f}" lon="{lo:.6f}"><ele>{e:.1f}</ele></trkpt>' for la, lo, e in pts)
    return (f'<?xml version="1.0"?><gpx version="1.1" creator="mock" xmlns="http://www.topografix.com/GPX/1/1">'
            f"<trk><name>{name}</name><trkseg>{trk}</trkseg></trk></gpx>").encode()


class MockStravaClient:
    """Development stand-in (STRAVA_MODE=mock): a pretend athlete with three routes. Never used unless asked for."""
    athlete_id = 4242

    def __init__(self):
        self.n = 0

    def _tokens(self):
        self.n += 1
        return TokenSet(f"mock-access-{self.n}", f"mock-refresh-{self.n}", datetime.now(timezone.utc) + timedelta(hours=6),
                        "read,read_all", self.athlete_id)

    def authorize_url(self, state, redirect_uri):   # loop straight back to our callback, as if the athlete had approved
        return redirect_uri + "?" + urlencode({"code": "mock-code", "state": state, "scope": "read,read_all"})

    def exchange_code(self, code):
        return self._tokens()

    def refresh(self, refresh_token):
        return self._tokens()

    def list_routes(self, access_token, athlete_id, page=1, per_page=30):
        return [self.get_route(access_token, rid) for rid in _MOCK_ROUTES] if page == 1 else []

    def get_route(self, access_token, route_id):
        if route_id not in _MOCK_ROUTES:
            raise StravaNotFound("That route is no longer on Strava.", 404)
        name, private, pts = _MOCK_ROUTES[route_id]
        gain = sum(max(0, b[2] - a[2]) for a, b in zip(pts, pts[1:]))
        dist = sum(math.hypot((b[0] - a[0]) * 111195, (b[1] - a[1]) * 66800) for a, b in zip(pts, pts[1:]))
        simple = [(p[0], p[1]) for p in pts[::6]]
        return StravaRoute(route_id, name, dist, gain, polyline.encode(simple), private)

    def export_gpx(self, access_token, route_id):
        self.get_route(access_token, route_id)
        return _mock_gpx(route_id)

    def revoke(self, access_token):
        pass

    def create_subscription(self, callback_url, verify_token):
        return 1


def get_client(config) -> StravaClient | None:
    """The client for the configured mode, or None when Strava isn't set up (the app works fine without it)."""
    if config.get("STRAVA_CLIENT"):
        return config["STRAVA_CLIENT"]
    mode = config.get("STRAVA_MODE", "off")
    if mode == "mock":
        return config.setdefault("_MOCK_STRAVA", MockStravaClient())
    if mode == "live" and config.get("STRAVA_CLIENT_ID") and config.get("STRAVA_CLIENT_SECRET"):
        return HttpStravaClient(config["STRAVA_CLIENT_ID"], config["STRAVA_CLIENT_SECRET"])
    return None
