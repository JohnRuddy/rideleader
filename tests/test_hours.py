"""Pace groups, arrival times, opening-hours evaluation (real opening_hours.js sidecar) and club overrides."""
import io
import os
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest
import requests

from app import hours as hrs
from app import pace
from tests.conftest import hilly_points, make_gpx
from tests.test_scan_e2e import FakeOverpass, node

DUB = ZoneInfo("Europe/Dublin")
HOURS_URL = os.environ.get("HOURS_URL", "http://hours:3000")


# ---------------- pace / ETA ----------------

def test_eta_without_stops_is_distance_over_speed():
    g = pace.Group("A", 30, 20)
    assert pace.seconds_to(15_000, g, []) == pytest.approx(1800)       # 15 km at 30 km/h = 30 min


def test_eta_includes_dwell_only_for_stops_already_passed():
    g = pace.Group("B", 24, 20)
    planned = [20_000]
    assert pace.seconds_to(20_000, g, planned) == pytest.approx(3000)          # arrives before its own stop
    assert pace.seconds_to(20_001, g, planned) == pytest.approx(3000 + 1200 + 0.15, abs=0.2)
    assert pace.seconds_to(40_000, g, [20_000, 30_000]) == pytest.approx(6000 + 2400)


def test_summary_finish_time_counts_every_stop():
    start = pace.start_instant(date(2026, 10, 11), time(9, 0), "Europe/Dublin")
    s = pace.summary(start, 60_000, pace.Group("A", 30, 15), [25_000, 45_000])
    assert s["moving_s"] == 7200 and s["stops_s"] == 1800 and s["finish_local"] == "11:30"


def test_start_instant_is_dst_correct():
    summer = pace.start_instant(date(2026, 10, 11), time(9, 0), "Europe/Dublin")
    winter = pace.start_instant(date(2026, 11, 1), time(9, 0), "Europe/Dublin")
    assert summer.utcoffset() == timedelta(hours=1) and winter.utcoffset() == timedelta(0)


def test_fmt_duration():
    assert pace.fmt_duration(3 * 3600 + 5 * 60) == "3h 05m" and pace.fmt_duration(40 * 60) == "40m"


# ---------------- club override rules ----------------

def W(**kw):
    base = dict(tags={}, club_hours=None, club_hours_at=None, osm_hours_changed_at=None)
    return NS(**{**base, **kw})


def test_effective_hours_sources():
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert hrs.effective_hours(W()) == (None, None, False)
    assert hrs.effective_hours(W(tags={"opening_hours": "Mo-Su 09:00-17:00"})) == ("Mo-Su 09:00-17:00", "osm", False)
    club = W(tags={"opening_hours": "Mo-Su 09:00-17:00"}, club_hours="Mo-Fr 10:00-16:00", club_hours_at=t0)
    assert hrs.effective_hours(club) == ("Mo-Fr 10:00-16:00", "club", False)
    # club-only hours (café missing from OSM hours) work too
    assert hrs.effective_hours(W(club_hours="Su 10:00-14:00", club_hours_at=t0))[1] == "club"


def test_newer_osm_value_beats_older_club_entry_and_is_flagged():
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    w = W(tags={"opening_hours": "Mo-Su 08:00-20:00"}, club_hours="Mo-Fr 10:00-16:00", club_hours_at=t0,
          osm_hours_changed_at=t0 + timedelta(days=3))
    assert hrs.effective_hours(w) == ("Mo-Su 08:00-20:00", "osm", True)
    # ...but a later club confirmation wins again
    w.club_hours_at = t0 + timedelta(days=5)
    assert hrs.effective_hours(w)[1:] == ("club", False)


# ---------------- classification (canned sidecar answers) ----------------

def raw(open_, nc=None, nc_open=None, unknown=False, next_open=None, comment=None):
    return {"open": open_, "unknown": unknown, "comment": comment, "next_change": nc,
            "next_change_open": nc_open, "next_open": next_open}


ARR = datetime(2026, 10, 11, 9, 21, tzinfo=DUB)                       # 08:21Z
DAY = [["2026-10-11T08:00:00Z", "2026-10-11T16:00:00Z", False, None]]  # 09:00-17:00 local


def test_classify_open_closes_soon_closed_and_unknown():
    assert hrs.classify(ARR, raw(True, "2026-10-11T16:00:00Z", False), DAY, DUB)["status"] == "open"
    soon = hrs.classify(ARR, raw(True, "2026-10-11T08:45:00Z", False), DAY, DUB)
    assert soon["status"] == "closes_soon" and soon["closes_at"] == "09:45"
    # exactly 30 minutes counts as "within 30"; 31 does not
    assert hrs.classify(ARR, raw(True, "2026-10-11T08:51:00Z", False), DAY, DUB)["status"] == "closes_soon"
    assert hrs.classify(ARR, raw(True, "2026-10-11T08:52:00Z", False), DAY, DUB)["status"] == "open"
    closed = hrs.classify(ARR, raw(False, next_open="2026-10-11T09:30:00Z"), DAY, DUB)
    assert closed["status"] == "closed" and closed["opens_at"] == "10:30"
    gone = hrs.classify(ARR, raw(False, next_open="2026-10-12T08:00:00Z"), [], DUB)
    assert gone["status"] == "closed_today" and gone["opens_at"].endswith("09:00")
    assert hrs.classify(ARR, raw(False, unknown=True), DAY, DUB)["status"] == "unknown"
    assert hrs.classify(ARR, None, None, DUB)["label"] == "Hours unknown"


def test_classify_reports_todays_hours_in_local_time():
    out = hrs.classify(ARR, raw(True, "2026-10-11T16:00:00Z", False), DAY, DUB)
    assert out["hours_today"] == ["09:00–17:00"] and out["eta"] == "09:21"


def test_alternatives_pick_nearest_open_stop_per_group():
    def stop(i, s, *statuses):
        return {"id": i, "name": f"C{i}", "s_m": s, "alternatives": {},
                "groups": [{"status": st} for st in statuses]}
    stops = [stop(1, 10_000, "closed", "open"), stop(2, 12_000, "open", "open"), stop(3, 30_000, "open", "closed_today")]
    hrs._suggest_alternatives(stops, [pace.Group("A", 28, 20), pace.Group("B", 24, 20)])
    assert stops[0]["alternatives"] == {"A": {"id": 2, "name": "C2", "s_m": 12_000}}
    assert stops[2]["alternatives"] == {"B": {"id": 2, "name": "C2", "s_m": 12_000}}
    assert stops[1]["alternatives"] == {}


# ---------------- the real opening_hours.js sidecar ----------------

@pytest.fixture(scope="module")
def sidecar():
    try:
        requests.get(f"{HOURS_URL}/health", timeout=3).raise_for_status()
    except requests.RequestException:
        pytest.skip("hours sidecar not reachable (run via docker compose)")
    return hrs.HoursClient(HOURS_URL)


def judge(sidecar, rules, local, lat=53.16, lon=-6.9):
    """Classify `rules` for an arrival at the given local Dublin datetime."""
    arrival = local.replace(tzinfo=DUB)
    day_from, day_to = hrs._day_bounds(arrival.date(), DUB)
    res = sidecar.evaluate([{"id": 1, "rules": rules, "lat": lat, "lon": lon, "country_code": "ie", "tz": "Europe/Dublin",
                             "day_from": day_from, "day_to": day_to,
                             "arrivals": [arrival.astimezone(timezone.utc).isoformat()]}])[0]
    assert "error" not in res, res
    return hrs.classify(arrival, res["arrivals"][0], res["day"], DUB)


@pytest.mark.parametrize("when,expected", [
    (datetime(2026, 10, 27, 11, 0), "open"),          # ordinary Tuesday
    (datetime(2026, 10, 26, 11, 0), "closed_today"),  # October bank holiday Monday
    (datetime(2026, 12, 25, 11, 0), "closed_today"),  # Christmas Day
    (datetime(2026, 3, 17, 12, 0), "closed_today"),   # St Patrick's Day
    (datetime(2026, 8, 3, 12, 0), "closed_today"),    # August bank holiday
    (datetime(2026, 5, 4, 12, 0), "closed_today"),    # May bank holiday
])
def test_irish_public_holidays(sidecar, when, expected):
    assert judge(sidecar, "Mo-Su 09:00-17:00; PH off", when)["status"] == expected


def test_seasonal_rules(sidecar):
    rules = "Apr-Sep Mo-Su 09:00-18:00; Oct-Mar Mo-Fr 09:00-16:00"
    assert judge(sidecar, rules, datetime(2026, 7, 12, 11, 0))["status"] == "open"          # summer Sunday
    assert judge(sidecar, rules, datetime(2026, 12, 6, 11, 0))["status"] == "closed_today"  # winter Sunday
    assert judge(sidecar, rules, datetime(2026, 12, 7, 11, 0))["status"] == "open"          # winter Monday


def test_closed_before_opening_reports_opening_time(sidecar):
    out = judge(sidecar, "Mo-Su 10:30-17:00", datetime(2026, 10, 11, 9, 21))
    assert out["status"] == "closed" and out["opens_at"] == "10:30" and out["hours_today"] == ["10:30–17:00"]


def test_closes_within_30_minutes(sidecar):
    assert judge(sidecar, "Mo-Su 09:00-12:00", datetime(2026, 10, 11, 11, 45))["status"] == "closes_soon"
    assert judge(sidecar, "Mo-Su 09:00-12:00", datetime(2026, 10, 11, 11, 20))["status"] == "open"


def test_timezone_summer_time_is_applied(sidecar):
    # 09:30 local, rules open from 09:00: open in IST (Oct 11) and in GMT (Nov 1) alike, because we convert via tz
    assert judge(sidecar, "Mo-Su 09:00-17:00", datetime(2026, 10, 11, 9, 30))["status"] == "open"
    assert judge(sidecar, "Mo-Su 09:00-17:00", datetime(2026, 11, 1, 8, 30))["status"] == "closed"
    assert judge(sidecar, "Mo-Su 09:00-17:00", datetime(2026, 10, 11, 8, 30))["status"] == "closed"


def test_unknown_and_invalid_rules(sidecar):
    assert judge(sidecar, "by appointment", datetime(2026, 10, 11, 9, 30))["status"] == "unknown"
    res = sidecar.evaluate([{"id": 7, "rules": "Mo-Su 25:99 banana", "lat": 53, "lon": -7, "arrivals": []}])[0]
    assert res["id"] == 7 and res["error"]


# ---------------- full HTTP flow ----------------

PTS = hilly_points()


def elements():
    lon = lambda i: PTS[i][1]
    return [
        node(1, 53.1625, lon(100), amenity="cafe", name="Open Cafe", opening_hours="Mo-Su 09:00-17:00"),
        node(2, 53.1625, lon(120), amenity="cafe", name="Weekday Cafe", opening_hours="Mo-Sa 09:00-17:00"),
        node(3, 53.1625, lon(80), amenity="cafe", name="Early Closer", opening_hours="Mo-Su 09:00-09:40"),
        node(4, 53.1625, lon(140), amenity="cafe", name="No Hours Cafe"),
    ]


@pytest.fixture
def flow(client):
    try:
        requests.get(f"{HOURS_URL}/health", timeout=3).raise_for_status()
    except requests.RequestException:
        pytest.skip("hours sidecar not reachable")
    client.application.config.update(QUEUE_INLINE=True, OVERPASS_CLIENT=FakeOverpass(elements()), HOURS_URL=HOURS_URL)
    client.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(PTS)), "a.gpx")}, content_type="multipart/form-data")
    client.post("/routes/1/scan", json={})
    r = client.put("/api/routes/1/ride", json={"date": "2026-10-11", "start_time": "09:00", "groups": [
        {"name": "A", "speed_kmh": 28, "stop_min": 20}, {"name": "C", "speed_kmh": 18, "stop_min": 30}]})
    assert r.status_code == 200
    return client


def stops(client):
    r = client.get("/api/routes/1/stops-status")
    assert r.status_code == 200
    return {s["name"]: s for s in r.get_json()["stops"]}


def statuses(s):
    return [g["status"] for g in s["groups"]]


def test_ride_defaults_and_summary(client):
    client.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(PTS)), "a.gpx")}, content_type="multipart/form-data")
    r = client.get("/api/routes/1/ride").get_json()
    assert [g["name"] for g in r["groups"]] == ["A", "B", "C"] and r["start_time"] == "09:00"
    assert date.fromisoformat(r["date"]).weekday() == 6                      # defaults to a Sunday
    a, c = r["summary"][0], r["summary"][2]
    assert a["moving_s"] == pytest.approx(19_900 / (28 / 3.6), rel=0.01) and c["total_s"] > a["total_s"]


def test_ride_validation(client):
    client.post("/routes/upload", data={"gpx": (io.BytesIO(make_gpx(PTS)), "a.gpx")}, content_type="multipart/form-data")
    ok = {"date": "2026-10-11", "start_time": "09:00", "groups": [{"name": "A", "speed_kmh": 25, "stop_min": 10}]}
    assert client.put("/api/routes/1/ride", json=ok).status_code == 200
    for bad in ({**ok, "date": "soon"}, {**ok, "start_time": "9am"}, {**ok, "groups": []},
                {**ok, "groups": [{"name": "A", "speed_kmh": 5, "stop_min": 10}]},
                {**ok, "groups": [{"name": "A", "speed_kmh": 25, "stop_min": 10}, {"name": "A", "speed_kmh": 20, "stop_min": 0}]},
                {**ok, "groups": [{"name": "", "speed_kmh": 25, "stop_min": 10}]}):
        assert client.put("/api/routes/1/ride", json=bad).status_code == 400


def test_open_hours_status_per_group_on_ride_day(flow):
    s = stops(flow)
    assert statuses(s["Open Cafe"]) == ["open", "open"]
    assert statuses(s["Weekday Cafe"]) == ["closed_today", "closed_today"]          # Sunday
    assert statuses(s["No Hours Cafe"]) == ["unknown", "unknown"]
    assert s["Open Cafe"]["groups"][0]["eta"] < s["Open Cafe"]["groups"][1]["eta"]   # A arrives before C
    assert s["Open Cafe"]["groups"][0]["hours_today"] == ["09:00–17:00"]
    early = s["Early Closer"]["groups"][0]                                              # A arrives ~09:17, café closes 09:40
    assert early["status"] == "closes_soon" and early["closes_at"] == "09:40" and early["eta"] == "09:17"
    assert s["Weekday Cafe"]["alternatives"]["A"]["name"] == "Open Cafe"              # nearest open alternative


def test_changing_the_day_changes_the_answer(flow):
    flow.put("/api/routes/1/ride", json={"date": "2026-10-12", "start_time": "09:00", "groups": [
        {"name": "A", "speed_kmh": 28, "stop_min": 20}]})                              # Monday, bank-holiday-free
    assert statuses(stops(flow)["Weekday Cafe"]) == ["open"]
    flow.put("/api/routes/1/ride", json={"date": "2026-10-26", "start_time": "09:00", "groups": [
        {"name": "A", "speed_kmh": 28, "stop_min": 20}]})
    flow.patch(f"/api/waypoints/{stops(flow)['Open Cafe']['id']}", json={"club_hours": "Mo-Su 09:00-17:00; PH off"})
    assert statuses(stops(flow)["Open Cafe"]) == ["closed_today"]                      # Irish bank holiday Monday


def test_planned_stop_pushes_back_later_arrivals(flow):
    before = stops(flow)["No Hours Cafe"]["groups"][0]["eta"]
    first = stops(flow)["Early Closer"]["id"]
    assert flow.patch(f"/api/waypoints/{first}", json={"planned_stop": True}).status_code == 200
    after = stops(flow)["No Hours Cafe"]["groups"][0]["eta"]
    assert after > before                                                              # 20 minute dwell added
    ride = flow.get("/api/routes/1/ride").get_json()
    assert ride["planned_stops"] == 1 and ride["summary"][0]["stops_s"] == 1200


def test_club_hours_validated_and_override_osm(flow):
    s = stops(flow)["Early Closer"]
    bad = flow.patch(f"/api/waypoints/{s['id']}", json={"club_hours": "gibberish ;;"})
    assert bad.status_code == 400 and "Not valid" in bad.get_json()["error"]
    good = flow.patch(f"/api/waypoints/{s['id']}", json={"club_hours": "Mo-Su 08:00-18:00", "note": "room for 20"})
    assert good.status_code == 200 and good.get_json()["club_hours"] == "Mo-Su 08:00-18:00"
    now = stops(flow)["Early Closer"]
    assert now["hours_source"] == "club" and statuses(now) == ["open", "open"]
    # clearing the club hours falls back to OSM
    flow.patch(f"/api/waypoints/{s['id']}", json={"club_hours": ""})
    assert stops(flow)["Early Closer"]["hours_source"] == "osm"


def test_rescan_with_changed_osm_hours_beats_older_club_entry(flow):
    s = stops(flow)["Open Cafe"]
    flow.patch(f"/api/waypoints/{s['id']}", json={"club_hours": "Mo-Su 10:00-16:00"})
    els = elements()
    els[0]["tags"]["opening_hours"] = "Mo-Su 07:00-19:00"                                # someone updated OSM
    from app import db
    from app.models import PoiTile
    flow.application.config["OVERPASS_CLIENT"] = FakeOverpass(els)
    PoiTile.query.delete(); db.session.commit()
    flow.post("/routes/1/scan", json={})
    now = stops(flow)["Open Cafe"]
    assert now["hours_source"] == "osm" and now["osm_changed_since_club"] is True and now["hours"] == "Mo-Su 07:00-19:00"


def test_only_open_bulk_action(flow):
    r = flow.post("/api/routes/1/waypoints/bulk", json={"kind": "cafe", "action": "only_open"})
    assert r.status_code == 200
    chosen = {w["name"] for w in r.get_json()["waypoints"] if w["included"] and w["kind"] == "cafe"}
    assert chosen == {"Open Cafe"}      # open for both groups; closing-soon, closed and unknown are not selected


def test_hours_service_down_says_so_instead_of_guessing(flow):
    flow.application.config["HOURS_URL"] = "http://127.0.0.1:9"          # nothing listening
    r = flow.get("/api/routes/1/stops-status")
    assert r.status_code == 503 and r.get_json()["stops"] == [] and "opening hours" in r.get_json()["error"]
    assert flow.post("/api/routes/1/waypoints/bulk", json={"kind": "cafe", "action": "only_open"}).status_code == 503
