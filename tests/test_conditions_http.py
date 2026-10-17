"""Conditions through the API: warnings banner, weather along the route, the 10-day strip, daylight, closures, club settings."""
import io
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app import weather as w
from app.models import Closure, Route, User
from app.routing import RoutingError
from tests.conftest import hilly_points, make_gpx
from tests.test_along_route import RAIN, steps_between
from tests.test_auth_security import make_user, other_client, sign_in
from tests.test_editor import FakeRouter
from tests.test_warnings import Feed, item

UTC = timezone.utc
NOW = datetime(2026, 10, 10, 18, 0, tzinfo=UTC)          # Saturday evening
SUNDAY = "2026-10-11"


class FakeWeather:
    name = "met-eireann"

    def __init__(self, over=None, end=datetime(2026, 10, 19, 12, tzinfo=UTC), fail=False):
        self.steps = steps_between(datetime(2026, 10, 10, 0, tzinfo=UTC), end, over)
        self.fail, self.calls = fail, 0

    def fetch(self, lat, lon):
        self.calls += 1
        if self.fail:
            raise w.WeatherError("Met Éireann answered 503")
        return list(self.steps)


@pytest.fixture
def cond(client):
    cfg = client.application.config
    cfg.update(CLOCK=lambda: NOW, WEATHER_CLIENT=FakeWeather(), WARNINGS_CLIENT=Feed([]), CLOSURE_PROVIDERS=[],
               ROUTING_CLIENT=FakeRouter())
    client.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(hilly_points(), name="Sunday spin")), "a.gpx")},
                content_type="multipart/form-data")
    set_ride(client)
    return client


def set_ride(c, date=SUNDAY, start="09:00", groups=None):
    r = c.put("/api/routes/1/ride", json={"date": date, "start_time": start, "groups": groups or [
        {"name": "A", "speed_kmh": 28, "stop_min": 20}, {"name": "C", "speed_kmh": 18, "stop_min": 20}]})
    assert r.status_code == 200, r.data
    return r


def get(c, what):
    r = c.get(f"/api/routes/1/{what}")
    assert r.status_code == 200, r.data
    return r.get_json()


def use_feed(c, items=None, error=None):
    c.application.config["WARNINGS_CLIENT"] = Feed(items or [], error)


def sunday_warning(level="yellow", type_="Wind", names="Kildare", cap="w1", **kw):
    return item(cap=cap, level=level, type_=type_, regions=("EI12",), headline=f"Status {level.title()} - {type_} warning for {names}",
                onset=datetime(2026, 10, 11, 6, tzinfo=UTC), expiry=datetime(2026, 10, 11, 21, tzinfo=UTC), **kw)


# ---------------- warnings banner ----------------

def test_no_warnings_is_a_confirmed_all_clear_with_attribution(cond):
    b = get(cond, "warnings")
    assert b["status"] == "ok" and b["warnings"] == [] and b["top_level"] is None and b["checked_at"]
    assert "Met Éireann" in b["attribution"] and b["window"]["buffer_h"] == 2.0


def test_a_yellow_wind_warning_shows_as_issued_and_does_not_ask_to_postpone(cond):
    use_feed(cond, [sunday_warning()])
    b = get(cond, "warnings")
    [w_] = b["warnings"]
    assert b["top_level"] == "yellow" and not b["postpone_prompt"] and b["review_message"] is None
    assert (w_["level_title"], w_["instruction"], w_["headline"]) == ("Weather Alert", "Be Aware", "Status Yellow - Wind warning for Kildare")
    assert w_["sections"] and w_["url"] == "https://www.met.ie/warnings"


def test_an_orange_warning_asks_the_leader_to_consider_postponing_and_drafts_a_message(cond):
    use_feed(cond, [sunday_warning("orange", "Rain", "Kildare, Dublin")])
    b = get(cond, "warnings")
    assert b["postpone_prompt"] is True and b["top_level"] == "orange"
    msg = b["review_message"]
    assert "Sunday's spin (Sunday spin) is under review" in msg and "Orange Rain warning" in msg and "Kildare, Dublin" in msg
    assert "don't set off" in msg and "met.ie/warnings" in msg                          # never cancels, only asks people to wait


def test_an_orange_warning_for_another_county_is_not_shown(cond):
    use_feed(cond, [item(cap="d", level="orange", type_="Rain", regions=("EI06",), headline="Status Orange - Rain warning for Donegal",
                         onset=datetime(2026, 10, 11, 6, tzinfo=UTC), expiry=datetime(2026, 10, 11, 21, tzinfo=UTC))])
    b = get(cond, "warnings")
    assert b["warnings"] == [] and not b["postpone_prompt"] and b["status"] == "ok"


def test_an_unreachable_warnings_feed_says_so(cond):
    use_feed(cond, error="Met Éireann answered 503")
    b = get(cond, "warnings")
    assert b["status"] == "unreachable" and b["warnings"] == []
    assert b["message"] == "Couldn't check Met Éireann weather warnings. Check met.ie/warnings before you ride."


def test_changing_the_day_changes_which_warnings_apply(cond):
    use_feed(cond, [sunday_warning("red", "Wind")])
    assert get(cond, "warnings")["top_level"] == "red"
    set_ride(cond, date="2026-10-15")
    assert get(cond, "warnings")["warnings"] == []                                      # the warning ends on the 11th
    set_ride(cond, date=SUNDAY, start="22:00")
    assert get(cond, "warnings")["top_level"] == "red"                                  # the buffer reaches a late start


def test_a_slower_finish_widens_the_window_to_the_latest_group(cond):
    w_ = sunday_warning()
    w_["onset"] = datetime(2026, 10, 11, 11, 30, tzinfo=UTC).isoformat()                # a 09:00 start is 08:00 UTC (summer time)
    w_["expiry"] = datetime(2026, 10, 11, 21, tzinfo=UTC).isoformat()
    use_feed(cond, [w_])
    set_ride(cond, groups=[{"name": "A", "speed_kmh": 40, "stop_min": 0}])              # home about 08:30 UTC; the warning starts 3 h later
    assert get(cond, "warnings")["warnings"] == []
    set_ride(cond, groups=[{"name": "A", "speed_kmh": 40, "stop_min": 0}, {"name": "C", "speed_kmh": 8, "stop_min": 0}])    # slowest home ~10:30 UTC: within 2 h
    assert [x["id"] for x in get(cond, "warnings")["warnings"]] == ["w1"]


# ---------------- weather along the route ----------------

def test_weather_carries_per_group_rows_summary_kit_and_the_warnings_banner(cond):
    res = get(cond, "weather")
    assert res["available"] and [g["name"] for g in res["groups"]] == ["A", "C"]
    first = res["groups"][0]["samples"][0]
    assert first["label"] == "Start" and first["eta"] == "09:00" and first["temp_c"] == 6.0
    assert res["summary"].startswith("Dry start") and "ENE wind" in res["summary"] and res["summary"].endswith("feels like 1 °C.")
    assert res["detail"]["class"] == "Detailed" and res["updated"] == "19:00"          # fetched 18:00 UTC = 19:00 Irish summer time
    assert res["attribution"] == ["Forecast: Met Éireann (met.ie), CC BY 4.0"]
    assert "warnings" in res and res["warnings"]["status"] == "ok"                      # forecast and warnings travel together
    assert {k["item"] for k in res["kit"]} >= {"Gloves"}                                # feels as low as 1 degree
    assert res["daylight"]["sunset"]


def test_weather_changes_with_the_day_and_the_start_time(cond):
    cond.application.config["WEATHER_CLIENT"] = FakeWeather({9: RAIN})                     # 10:00 Irish time: 20 km ride, group A is at the far end about then
    assert "showers likely" in get(cond, "weather")["summary"]
    set_ride(cond, start="14:00")
    later = get(cond, "weather")
    assert later["groups"][0]["samples"][0]["eta"] == "14:00" and later["summary"].startswith("Dry start, staying dry")


def test_advisory_flags_are_separate_from_official_warnings(cond):
    cond.application.config["WEATHER_CLIENT"] = FakeWeather({h: {"gust_mps": 20.0, "temp_c": 1.0} for h in range(6, 20)})
    res = get(cond, "weather")
    kinds = {f["kind"] for f in res["flags"]}
    assert kinds >= {"gusts", "ice"} and all(f["advisory"] for f in res["flags"])
    assert res["warnings"]["warnings"] == []                                            # no official warning, only our advisory flags


def test_a_forecast_outage_is_reported_and_the_banner_still_comes_with_it(cond):
    cond.application.config["WEATHER_CLIENT"] = FakeWeather(fail=True)
    res = get(cond, "weather")
    assert res["available"] is False and "No forecast right now" in res["reason"] and "warnings" in res


def test_a_ride_beyond_the_forecast_says_when_to_check_back(cond):
    cond.application.config["WEATHER_CLIENT"] = FakeWeather(end=datetime(2026, 10, 13, 12, tzinfo=UTC))
    set_ride(cond, date="2026-10-16")
    res = get(cond, "weather")
    assert res["available"] is False and "Forecast not available yet" in res["reason"]


# ---------------- the 10-day strip ----------------

def test_the_day_strip(cond):
    use_feed(cond, [sunday_warning("orange", "Wind")])
    d = get(cond, "days")
    assert len(d["days"]) == 10 and d["selected"] == SUNDAY and d["start_time"] == "09:00"
    by = {x["date"]: x for x in d["days"]}
    assert by["2026-10-10"]["today"] and by["2026-10-11"]["usual"] and by["2026-10-11"]["selected"] and by["2026-10-11"]["weekend"]
    assert by["2026-10-11"]["warning"] == "orange" and by["2026-10-12"]["warning"] is None
    assert by["2026-10-11"]["available"] and by["2026-10-11"]["icon"] and by["2026-10-11"]["wind_kmh"] == 29
    assert "Met Éireann" in d["attribution"][0] and "warnings" in d                      # the banner accompanies the strip too


def test_days_past_the_forecast_stay_selectable_with_a_note(cond):
    cond.application.config["WEATHER_CLIENT"] = FakeWeather(end=datetime(2026, 10, 14, 18, tzinfo=UTC))
    d = get(cond, "days")["days"]
    far = d[-1]
    assert far["available"] is False and "Forecast not available yet" in far["note"] and far["date"] == "2026-10-19"
    assert cond.put("/api/routes/1/ride", json={"date": far["date"], "start_time": "09:00", "groups": [
        {"name": "A", "speed_kmh": 28, "stop_min": 20}]}).status_code == 200            # planning ahead is allowed


# ---------------- daylight ----------------

def test_daylight_flags_a_late_finish(cond):
    ok = get(cond, "daylight")
    assert ok["sunset"] and ok["warnings"] == [] and ok["lights_needed"] is False
    set_ride(cond, start="17:45")
    late = get(cond, "daylight")
    assert late["lights_needed"] and any("after sunset" in x or "before sunset" in x for x in late["warnings"])
    kit = {k["item"] for k in get(cond, "weather")["kit"]}
    assert "Lights (front and rear)" in kit                                             # and the kit list follows


# ---------------- closures ----------------

def add_closure(c, **over):
    body = {"lat": 53.1605, "lon": -6.75, "radius_m": 150, "description": "Culvert collapsed, road shut",
            "starts_at": "2026-10-08", "ends_at": "2026-10-20", **over}
    return c.post("/api/closures", json=body)


def test_entering_a_closure_needs_sign_in_and_sensible_details(cond):
    assert add_closure(cond).status_code == 401
    sign_in(cond, make_user())
    r = add_closure(cond)
    assert r.status_code == 201 and r.get_json()["description"].startswith("Culvert")
    for bad in ({"description": ""}, {"lat": 95}, {"radius_m": 5}, {"ends_at": "2026-10-01"}, {"starts_at": "someday"}):
        assert add_closure(cond, **bad).status_code == 400, bad
    assert cond.post("/api/closures", json={"lat": 53}).status_code == 400


def test_a_club_closure_on_the_route_appears_with_honest_coverage(cond):
    cond.application.config["CLOSURE_PROVIDERS"] = None                                  # use the real provider list
    cond.application.config["OVERPASS_CLIENT"] = type("O", (), {"run": lambda self, q: [], "fetch": lambda self, b: []})()
    sign_in(cond, make_user())
    add_closure(cond)
    res = get(cond, "closures")
    [h] = res["hits"]
    assert h["description"].startswith("Culvert") and h["id"].startswith("club:") and h["s_start"] < h["s_end"] and "_hits" not in res
    assert [c["name"] for c in res["coverage"]] == ["club", "osm"] and res["not_checked"] and "not a guarantee" in res["disclaimer"]
    set_ride(cond, date="2026-10-25")                                                    # after the closure ends
    assert get(cond, "closures")["hits"] == []


def test_closures_can_be_removed_only_by_their_author_or_an_admin(cond):
    me, other = make_user("me@example.com"), make_user("other@example.com")
    sign_in(cond, me)
    cid = add_closure(cond).get_json()["id"]
    stranger = other_client(cond)
    sign_in(stranger, other)
    assert stranger.delete(f"/api/closures/{cid}").status_code == 403 and Closure.query.count() == 1
    other.role = "admin"
    db.session.commit()
    assert stranger.delete(f"/api/closures/{cid}").status_code == 204 and Closure.query.count() == 0
    assert cond.delete("/api/closures/999").status_code == 404


def test_the_closure_list_hides_ones_that_have_expired(cond):
    sign_in(cond, make_user())
    add_closure(cond)
    add_closure(cond, description="Old", starts_at="2026-09-01", ends_at="2026-09-05")
    assert [c["description"] for c in cond.get("/api/closures").get_json()] == ["Culvert collapsed, road shut"]


class BendRouter(FakeRouter):
    """Takes a detour north of any circle it is told to avoid."""
    def route(self, points, options, avoid=None):
        if avoid:
            lat, lon, r = avoid[0]
            points = [points[0], (lat + (r + 400) / 111195, lon), points[-1]]
        out = []
        for a, b in zip(points, points[1:]):
            out += super().route([a, b], options)[:-1]
        return out + [super().route([points[-2], points[-1]], options)[-1]]


def test_rerouting_around_a_closure_replaces_that_stretch_and_checks_the_result(cond):
    cond.application.config.update(CLOSURE_PROVIDERS=None, ROUTING_CLIENT=BendRouter(),
                                   OVERPASS_CLIENT=type("O", (), {"run": lambda self, q: []})())
    sign_in(cond, make_user())
    add_closure(cond)
    before = cond.get("/api/routes/1").get_json()
    hit = get(cond, "closures")["hits"][0]
    r = cond.post("/api/routes/1/closures/reroute", json={"closure_id": hit["id"]})
    assert r.status_code == 200
    out = r.get_json()
    assert out["still_blocked"] is False and out["length_m"] > before["length_m"] and out["version"] == 2
    assert get(cond, "closures")["hits"] == []
    assert cond.get("/api/routes/1/history").get_json()["states"][-1]["label"] == "Reroute around closure"
    cond.post("/api/routes/1/history/undo")
    assert [h["id"] for h in get(cond, "closures")["hits"]] == [hit["id"]]               # undo puts the closure back in the way


def test_a_reroute_that_cannot_avoid_the_closure_says_so(cond):
    cond.application.config.update(CLOSURE_PROVIDERS=None, ROUTING_CLIENT=FakeRouter(),
                                   OVERPASS_CLIENT=type("O", (), {"run": lambda self, q: []})())
    sign_in(cond, make_user())
    add_closure(cond)
    hit = get(cond, "closures")["hits"][0]
    out = cond.post("/api/routes/1/closures/reroute", json={"closure_id": hit["id"]}).get_json()
    assert out["still_blocked"] is True                                                  # a straight-line router ignores the avoid area


def test_reroute_errors(cond):
    cond.application.config.update(CLOSURE_PROVIDERS=None, OVERPASS_CLIENT=type("O", (), {"run": lambda self, q: []})())
    sign_in(cond, make_user())
    assert cond.post("/api/routes/1/closures/reroute", json={"closure_id": "club:999"}).status_code == 404
    add_closure(cond)
    hit = get(cond, "closures")["hits"][0]

    class Boom:
        def route(self, *a, **k): raise RoutingError("No cycling route avoids that.")
    cond.application.config["ROUTING_CLIENT"] = Boom()
    r = cond.post("/api/routes/1/closures/reroute", json={"closure_id": hit["id"]})
    assert r.status_code == 422 and "avoids" in r.get_json()["error"]


# ---------------- club settings ----------------

def test_club_settings_start_with_defaults_and_only_an_admin_can_change_them(cond):
    admin, member = make_user("admin@example.com"), make_user("member@example.com")
    admin.role = "admin"
    db.session.commit()
    sign_in(cond, member)
    assert cond.get("/api/club").get_json()["settings"]["usual_days"] == [6]
    assert cond.put("/api/club", json={"usual_days": [5]}).status_code == 403
    boss = other_client(cond)
    sign_in(boss, admin)
    r = boss.put("/api/club", json={"name": "Kildare Wheelers", "usual_days": [5, 6], "gust_kmh": 45, "warning_buffer_h": 3})
    assert r.status_code == 200 and r.get_json()["name"] == "Kildare Wheelers" and r.get_json()["settings"]["gust_kmh"] == 45
    for bad in ({"usual_days": [9]}, {"default_start": "late"}, {"gust_kmh": 5}, {"ice_c": "cold"}):
        assert boss.put("/api/club", json=bad).status_code == 400, bad
    days = {d["date"]: d for d in get(cond, "days")["days"]}
    assert days["2026-10-10"]["usual"] and days["2026-10-11"]["usual"]                   # Saturday now too
    assert get(cond, "warnings")["window"]["buffer_h"] == 3.0


def test_the_first_person_to_sign_in_becomes_the_club_admin(client):
    first = client.application.test_client()
    client.application.config["MAILER"].outbox.clear()
    for email, c in (("first@example.com", first), ("second@example.com", client.application.test_client())):
        c.post("/login", data={"email": email})
        link = __import__("re").search(r"https?://\S+/login/\S+", client.application.config["MAILER"].outbox[-1]["body"]).group(0)
        c.post("/" + link.split("/", 3)[3])
    assert User.query.filter_by(email="first@example.com").one().role == "admin"
    assert User.query.filter_by(email="second@example.com").one().role == "leader"


def test_conditions_endpoints_respect_route_privacy(client):
    cfg = client.application.config
    cfg.update(CLOCK=lambda: NOW, WEATHER_CLIENT=FakeWeather(), WARNINGS_CLIENT=Feed([]), CLOSURE_PROVIDERS=[])
    me, you = make_user("me@example.com"), make_user("you@example.com")
    sign_in(client, me)
    client.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(hilly_points())), "a.gpx")}, content_type="multipart/form-data")
    stranger = other_client(client)
    sign_in(stranger, you)
    for what in ("warnings", "weather", "days", "daylight", "closures"):
        assert stranger.get(f"/api/routes/1/{what}").status_code == 404, what
        assert client.get(f"/api/routes/1/{what}").status_code == 200, what
