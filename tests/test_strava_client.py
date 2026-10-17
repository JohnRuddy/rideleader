"""The Strava client (HTTP faked), polyline codec, token encryption, the dev mock, and two compliance guards."""
import ast
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from app import crypto, polyline
from app.routemodel import parse_gpx
from app.strava import (AUTHORIZE_URL, HttpStravaClient, MockStravaClient, StravaError, StravaNotFound,
                        StravaUnauthorized, get_client)

APP_DIR = Path(__file__).resolve().parent.parent / "app"


class Resp:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code, self._body, self.content = status, body, content

    def json(self):
        return self._body


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)

    def post(self, url, **kw):
        return self._next("POST", url, **kw)


def http_client(*responses):
    s = Session(*responses)
    return HttpStravaClient("1234", "shh", session=s), s


TOKEN = {"access_token": "A1", "refresh_token": "R1", "expires_at": 1_900_000_000, "scope": "read,read_all",
         "athlete": {"id": 777}}


# ---------------- polyline ----------------

def test_polyline_matches_googles_documented_example():
    pts = polyline.decode("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    assert pts == [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)]
    assert polyline.encode(pts) == "_p~iF~ps|U_ulLnnqC_mqNvxq`@"


def test_polyline_roundtrip_with_negatives_and_precision():
    pts = [(53.16, -6.9), (53.1612, -6.8987), (53.15, -6.91), (-33.8688, 151.2093)]
    back = polyline.decode(polyline.encode(pts))
    assert all(abs(a[0] - b[0]) < 1e-5 and abs(a[1] - b[1]) < 1e-5 for a, b in zip(pts, back))


# ---------------- OAuth ----------------

def test_authorize_url_has_everything_strava_requires():
    c, _ = http_client()
    u = urlparse(c.authorize_url("STATE123", "https://club.example/strava/callback"))
    q = parse_qs(u.query)
    assert f"{u.scheme}://{u.netloc}{u.path}" == AUTHORIZE_URL
    assert q["client_id"] == ["1234"] and q["response_type"] == ["code"] and q["state"] == ["STATE123"]
    assert q["redirect_uri"] == ["https://club.example/strava/callback"]
    assert q["scope"] == ["read,read_all"] and q["approval_prompt"] == ["auto"]


def test_code_exchange_and_refresh_requests():
    c, s = http_client(Resp(200, TOKEN), Resp(200, {**TOKEN, "access_token": "A2", "refresh_token": "R2", "athlete": None}))
    t = c.exchange_code("CODE")
    assert (t.access_token, t.refresh_token, t.athlete_id, t.scope) == ("A1", "R1", 777, "read,read_all")
    assert t.expires_at == datetime.fromtimestamp(1_900_000_000, tz=timezone.utc)
    assert s.calls[0][2]["data"] == {"client_id": "1234", "client_secret": "shh", "code": "CODE", "grant_type": "authorization_code"}
    r = c.refresh("R1")
    assert (r.access_token, r.refresh_token) == ("A2", "R2")                      # the rotated refresh token comes back
    assert s.calls[1][2]["data"]["grant_type"] == "refresh_token" and s.calls[1][2]["data"]["refresh_token"] == "R1"


def test_bad_grants_and_rate_limits_are_distinguished():
    with pytest.raises(StravaUnauthorized):
        http_client(Resp(400, {}))[0].refresh("dead")
    with pytest.raises(StravaUnauthorized):
        http_client(Resp(401, {}))[0].exchange_code("x")
    with pytest.raises(StravaError) as e:
        http_client(Resp(429, {}))[0].exchange_code("x")
    assert e.value.status == 429 and not isinstance(e.value, StravaUnauthorized)


def test_revoke_uses_the_new_endpoint_with_basic_auth_then_falls_back():
    c, s = http_client(Resp(200))
    c.revoke("A1")
    method, url, kw = s.calls[0]
    assert url.endswith("/oauth/revoke") and kw["auth"] == ("1234", "shh") and kw["data"]["token"] == "A1"
    c, s = http_client(Resp(500), Resp(200))
    c.revoke("A1")
    assert [x[1].rsplit("/", 1)[1] for x in s.calls] == ["revoke", "deauthorize"] and s.calls[1][2]["data"] == {"access_token": "A1"}


# ---------------- reading routes ----------------

ROUTE = {"id": 55, "name": "Sunday loop", "distance": 61234.5, "elevation_gain": 640.2, "private": True,
         "map": {"summary_polyline": "abc"}}


def test_list_get_and_export_parse_and_send_the_bearer_token():
    c, s = http_client(Resp(200, [ROUTE]), Resp(200, ROUTE), Resp(200, content=b"<gpx/>"))
    [r] = c.list_routes("TOK", 777, page=2, per_page=10)
    assert (r.id, r.name, r.distance_m, r.elevation_gain_m, r.summary_polyline, r.private) == (55, "Sunday loop", 61234.5, 640.2, "abc", True)
    assert c.get_route("TOK", 55).id == 55 and c.export_gpx("TOK", 55) == b"<gpx/>"
    urls = [x[1] for x in s.calls]
    assert urls[0].endswith("/athletes/777/routes?page=2&per_page=10") and urls[1].endswith("/routes/55") and urls[2].endswith("/routes/55/export_gpx")
    assert all(x[2]["headers"] == {"Authorization": "Bearer TOK"} for x in s.calls)


def test_missing_and_revoked_are_told_apart():
    with pytest.raises(StravaNotFound):
        http_client(Resp(404))[0].get_route("T", 1)
    with pytest.raises(StravaUnauthorized):
        http_client(Resp(401))[0].list_routes("T", 1)
    with pytest.raises(StravaError) as e:
        http_client(Resp(429))[0].export_gpx("T", 1)
    assert e.value.status == 429


def test_ids_are_forced_to_integers_in_urls():
    c, s = http_client(Resp(200, []))
    with pytest.raises(ValueError):
        c.list_routes("T", "777/../../oauth")
    assert not s.calls


def test_the_client_is_read_only_by_construction():
    """No method can create, change or delete anything on Strava; the only POSTs are OAuth and the webhook subscription."""
    public = {n for n in dir(HttpStravaClient) if not n.startswith("_")}
    assert public == {"authorize_url", "exchange_code", "refresh", "revoke", "list_routes", "get_route", "export_gpx",
                      "create_subscription"}
    c, s = http_client(Resp(200, TOKEN), Resp(200, TOKEN), Resp(200), Resp(200, [ROUTE]), Resp(200, ROUTE), Resp(200, content=b"x"),
                  Resp(200, {"id": 9}))
    c.exchange_code("c"); c.refresh("r"); c.revoke("a"); c.list_routes("a", 1); c.get_route("a", 1); c.export_gpx("a", 1)
    c.create_subscription("https://club.example/strava/webhook", "v")
    for method, url, _ in s.calls:
        path = urlparse(url).path
        if method == "GET":
            assert re.fullmatch(r"/api/v3/(athletes/\d+/routes|routes/\d+(/export_gpx)?)", path), path
        else:
            assert path in ("/oauth/token", "/oauth/revoke", "/api/v3/push_subscriptions"), path


# ---------------- dev mock and client selection ----------------

def test_mock_client_behaves_like_a_tiny_strava():
    m = MockStravaClient()
    routes = m.list_routes("t", m.athlete_id)
    assert len(routes) == 3 and any(r.private for r in routes) and all(polyline.decode(r.summary_polyline) for r in routes)
    parsed = parse_gpx(m.export_gpx("t", routes[0].id))
    assert parsed.length_m > 5_000 and parsed.has_elevation
    with pytest.raises(StravaNotFound):
        m.get_route("t", 1)
    assert m.refresh("x").refresh_token != m.refresh("x").refresh_token                 # rotates like the real thing
    cb = urlparse(m.authorize_url("S", "http://localhost/strava/callback"))
    assert parse_qs(cb.query)["state"] == ["S"] and cb.path == "/strava/callback"


def test_client_selection_by_mode():
    assert get_client({"STRAVA_MODE": "off"}) is None
    assert isinstance(get_client({"STRAVA_MODE": "mock"}), MockStravaClient)
    assert get_client({"STRAVA_MODE": "live"}) is None                                  # live without credentials: off
    assert isinstance(get_client({"STRAVA_MODE": "live", "STRAVA_CLIENT_ID": "1", "STRAVA_CLIENT_SECRET": "s"}), HttpStravaClient)
    sentinel = object()
    assert get_client({"STRAVA_CLIENT": sentinel, "STRAVA_MODE": "off"}) is sentinel


# ---------------- token encryption ----------------

def test_tokens_are_encrypted_and_roundtrip(client):
    secret = "strava-access-token-abc123"
    enc = crypto.encrypt(secret)
    assert secret not in enc and crypto.decrypt(enc) == secret and crypto.encrypt(secret) != enc   # random IV each time


def test_key_rotation_keeps_old_tokens_readable_and_wrong_keys_fail(client):
    cfg = client.application.config
    old, new = crypto.generate_key(), crypto.generate_key()
    cfg["TOKEN_ENCRYPTION_KEY"] = old
    enc = crypto.encrypt("tok")
    cfg["TOKEN_ENCRYPTION_KEY"] = f"{new},{old}"                                         # rotate: new first, old kept for reading
    assert crypto.decrypt(enc) == "tok" and crypto.decrypt(crypto.encrypt("tok2")) == "tok2"
    cfg["TOKEN_ENCRYPTION_KEY"] = new                                                    # old key retired
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(enc)


# ---------------- compliance guards ----------------

FORBIDDEN = {"anthropic", "openai", "langchain", "llama_index", "transformers", "torch", "tensorflow", "sklearn",
             "cohere", "mistralai", "ollama", "google.generativeai", "vertexai", "sentence_transformers", "litellm"}


def _imports(path: Path) -> set[str]:
    out = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_no_ai_or_ml_library_is_used_anywhere_in_the_app():
    """Strava data must never reach an AI/ML system, so the app doesn't import one at all."""
    for f in APP_DIR.rglob("*.py"):
        bad = {m for m in _imports(f) if m.split(".")[0] in FORBIDDEN or m in FORBIDDEN}
        assert not bad, f"{f.name} imports {bad}"
    reqs = (APP_DIR.parent / "requirements.txt").read_text().lower()
    assert not any(re.search(rf"^{re.escape(n)}\b", reqs, re.M) for n in ("anthropic", "openai", "langchain", "transformers", "torch"))


def test_strava_modules_do_not_touch_the_network_directly_except_the_client():
    """Strava traffic goes through app/strava.py only, so the read-only guarantee above covers all of it."""
    for name in ("strava_service.py", "strava_views.py", "account.py", "scheduler.py"):
        assert "requests" not in _imports(APP_DIR / name), name
