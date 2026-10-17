"""Weather along a route per pace group, wind relative to travel, words, kit, flags, the 10-day strip, daylight."""
import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import along_route as ar
from app import club, pace
from app import weather as w

UTC = timezone.utc
DUB = ZoneInfo("Europe/Dublin")
SETTINGS = club.DEFAULT_SETTINGS
LAT = 53.2
M_LON = 111195 * math.cos(math.radians(LAT))


def out_and_back(km_each_way=35, step_m=500):
    """35 km east then 35 km back along the same road."""
    n = int(km_each_way * 1000 / step_m)
    lons = [-7.0 + i * step_m / M_LON for i in range(n + 1)]
    lons = lons + lons[-2::-1]
    dist = [i * step_m for i in range(len(lons))]
    return ar.Line([LAT] * len(lons), lons, dist)


def steps_between(start, end, over=None):
    """Hourly steps with a plain default, per-hour overrides via over={hour: {field: value}}."""
    out, t = [], start
    while t <= end:
        d = dict(temp_c=6.0, wind_mps=8.0, wind_deg=60.0, gust_mps=10.0, precip_mm=0.0, precip_hours=1.0, precip_prob=10.0,
                 cloud_pct=40.0, symbol="PartlyCloud", radiation=100.0, step_hours=1.0)
        d.update((over or {}).get(t.hour, {}))
        out.append(w.Step(t=t.isoformat(), **d))
        t += timedelta(hours=1)
    return out


def fc_of(steps, fetched=datetime(2026, 10, 10, 12, tzinfo=UTC), provider="met-eireann"):
    return w.Forecast(steps, provider, fetched)


RAIN = {"precip_mm": 1.5, "precip_prob": 85.0, "symbol": "Rain"}
START = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 10, 18, 0, tzinfo=UTC)
GROUPS = [pace.Group("A", 28, 20), pace.Group("C", 18, 20)]


def run(over=None, groups=None, planned=(), summits=(), fc=None, line=None, lights=False, settings=SETTINGS, start=START):
    steps = steps_between(datetime(2026, 10, 11, 6, tzinfo=UTC), datetime(2026, 10, 11, 20, tzinfo=UTC), over)
    fcst = fc or fc_of(steps)
    return ar.weather_along(line or out_and_back(), start, groups or GROUPS, list(planned), list(summits), lambda la, lo: fcst,
                            settings, NOW, "UTC", lights)


# ---------------- wind relative to the direction of travel ----------------

@pytest.mark.parametrize("wind_from,bearing,cls", [
    (90, 90, "head"), (270, 90, "tail"), (0, 90, "cross"), (180, 90, "cross"),
    (135, 90, "head"), (136, 90, "cross"), (224, 90, "cross"), (225, 90, "tail"),   # 45 and 135 degree edges, both inclusive
    (60, 270, "tail"), (60, 90, "head"), (350, 10, "head"), (10, 350, "head")])
def test_wind_class_uses_where_the_wind_comes_from_against_the_bearing(wind_from, bearing, cls):
    assert ar.wind_relation(wind_from, bearing, 20)[0] == cls


def test_headwind_component_is_signed_and_scaled():
    assert ar.wind_relation(90, 90, 20)[1] == pytest.approx(20)
    assert ar.wind_relation(270, 90, 20)[1] == pytest.approx(-20)
    assert ar.wind_relation(0, 90, 20)[1] == pytest.approx(0, abs=1e-9)
    assert ar.wind_relation(45, 90, 20)[1] == pytest.approx(20 * math.cos(math.radians(45)))
    assert ar.wind_relation(None, 90, 20) == ("cross", 0.0)


def test_bearings_on_a_straight_line_and_after_a_turn():
    line = out_and_back()
    assert line.bearing(10_000) == pytest.approx(90, abs=1) and line.bearing(50_000) == pytest.approx(270, abs=1)
    north = ar.Line([53.0 + i * 0.001 for i in range(50)], [-7.0] * 50, [i * 111.2 for i in range(50)])
    assert north.bearing(2_000) == pytest.approx(0, abs=1)


def test_sample_points_cover_start_every_10km_stops_summits_and_finish():
    pts = ar.sample_positions(35_000, [12_300], [20_000])
    labels = {p["label"]: p["s"] for p in pts}
    assert [p["s"] for p in pts] == sorted(p["s"] for p in pts)
    assert pts[0]["label"] == "Start" and pts[-1]["label"] == "Finish" and pts[-1]["s"] == 35_000
    assert labels["Stop"] == 12_300 and labels["Summit"] == 20_000
    assert "km 10" in labels and "km 30" in labels
    assert "km 20" not in labels                                           # swallowed by the summit 0 m away
    gaps = [b["s"] - a["s"] for a, b in zip(pts, pts[1:])]
    assert min(gaps) > 100


def test_a_regular_sample_close_to_the_finish_is_dropped():
    labels = [p["label"] for p in ar.sample_positions(30_800, [], [])]
    assert labels == ["Start", "km 10", "km 20", "Finish"]


# ---------------- weather per group, at each group's own time ----------------

def test_each_group_is_looked_up_at_its_own_arrival_time_so_the_shower_hits_them_in_different_places():
    res = run({11: RAIN})
    assert res["available"] and [g["name"] for g in res["groups"]] == ["A", "C"]
    wet = {g["name"]: [r["label"] for r in g["samples"] if (r.get("rain_mm_h") or 0) >= 0.5] for g in res["groups"]}
    assert wet["A"] == ["km 50", "km 60", "Finish"]                            # the fast group reaches the far end around 11:00
    assert wet["C"] == ["km 30", "km 40"]                                      # the slow group is at km 30-40 at that time
    a = {r["label"]: r for r in res["groups"][0]["samples"]}
    assert a["Start"]["eta"] == "09:00" and a["km 10"]["eta"] == "09:21" and a["Finish"]["eta"] == "11:30"


def test_stops_add_dwell_time_to_later_arrivals_in_the_weather_too():
    plain = run(groups=[GROUPS[0]])["groups"][0]["samples"]
    stopped = run(groups=[GROUPS[0]], planned=[25_000])["groups"][0]["samples"]
    assert next(r for r in stopped if r["label"] == "Finish")["eta"] == "11:50"        # 20 minutes of coffee
    assert next(r for r in plain if r["label"] == "Finish")["eta"] == "11:30"
    assert any(r["label"] == "Stop" for r in stopped)


def test_sample_rows_carry_temperature_feels_like_wind_and_its_relation_to_travel():
    res = run()
    r = {x["label"]: x for x in res["groups"][0]["samples"]}
    out, back = r["km 10"], r["km 40"]
    assert out["temp_c"] == 6.0 and out["feels_c"] == pytest.approx(1.4, abs=0.2)
    assert out["wind_kmh"] == pytest.approx(28.8) and out["gust_kmh"] == pytest.approx(36.0)
    assert out["wind_from"] == "ENE" and out["wind_class"] == "head" and out["headwind_kmh"] > 20      # riding east into an ENE wind
    assert back["wind_class"] == "tail" and back["headwind_kmh"] < -20                                  # and home with it behind you
    assert out["icon"] and out["label_text"] and out["detail"] == "Detailed"


def test_the_map_segments_colour_the_route_by_wind_class():
    seg = run()["segments"]
    assert [s["class"] for s in seg] == ["head", "tail"] and seg[0]["s0"] == 0 and seg[-1]["s1"] == 70_000
    assert seg[0]["s1"] == seg[1]["s0"]                                       # contiguous, no gaps
    assert 30_000 <= seg[0]["s1"] <= 40_000


def test_summary_in_the_shape_the_brief_asks_for():
    s = run({11: RAIN})["summary"]
    assert s.startswith("Dry start, showers likely 10:47–11:30 near km 50–70;")
    assert "30 km/h ENE wind — headwind on the way out, tailwind home;" in s
    assert s.endswith("6 °C at the start, feels like 1 °C.")


@pytest.mark.parametrize("over,expect", [
    ({h: RAIN for h in range(6, 21)}, "Wet throughout"),
    ({9: RAIN}, "Wet start, staying dry after"),
    ({}, "Dry start, staying dry after"),
])
def test_summary_rain_phrasing(over, expect):
    start = START + timedelta(minutes=29) if "Wet start" in expect else START       # 09:29: the start rounds to 09:00, km 10 (09:50) to 10:00
    assert run(over, start=start)["summary"].startswith(expect)


def test_rain_at_the_start_and_again_a_little_later_is_reported_as_showers():
    s = run({9: RAIN})["summary"]                                                  # 09:00 and 09:21 both fall in the wet hour
    assert s.startswith("Wet start, showers likely 09:21 near km 10;")


def test_summary_wind_patterns():
    tail_out = run({h: {"wind_deg": 240.0} for h in range(6, 21)})["summary"]
    assert "tailwind on the way out, headwind home" in tail_out
    cross = run({h: {"wind_deg": 0.0} for h in range(6, 21)})["summary"]
    assert "mixed, with crosswinds" in cross
    calm = run({h: {"wind_mps": 1.0, "gust_mps": 2.0} for h in range(6, 21)})["summary"]
    assert "; 5 km/h ENE wind" in calm                                           # 1 m/s = 3.6 km/h, rounded to the nearest 5


def test_a_ride_beyond_the_forecast_says_when_to_check_back():
    short = fc_of(steps_between(datetime(2026, 10, 10, 12, tzinfo=UTC), datetime(2026, 10, 10, 22, tzinfo=UTC)))     # ends the evening before the ride
    res = ar.weather_along(out_and_back(), START, GROUPS, [], [], lambda a, b: short, SETTINGS, NOW, "UTC")
    assert res["available"] is False and "Forecast not available yet" in res["reason"] and "Check back from 2026-10-10" in res["reason"]
    assert res["summary"] is None and res["kit"] == []


def test_a_provider_outage_is_reported_not_hidden():
    def boom(a, b):
        raise w.WeatherError("Met Éireann answered 503")
    res = ar.weather_along(out_and_back(), START, GROUPS, [], [], boom, SETTINGS, NOW, "UTC")
    assert res["available"] is False and "No forecast right now" in res["reason"] and "503" in res["reason"]


def test_lead_time_sets_the_detail_class_and_the_uncertainty_note():
    near = run()
    assert near["detail"] == {"class": "Detailed", "note": ""}
    far_now = START - timedelta(days=5)
    res = ar.weather_along(out_and_back(), START, GROUPS, [], [], lambda a, b: fc_of(steps_between(START - timedelta(hours=3), START + timedelta(hours=8))),
                           SETTINGS, far_now, "UTC")
    assert res["detail"]["class"] == "Indicative" and "±2" in res["detail"]["note"]
    far = ar.weather_along(out_and_back(), START, GROUPS, [], [], lambda a, b: fc_of(steps_between(START - timedelta(hours=3), START + timedelta(hours=8))),
                           SETTINGS, START - timedelta(days=8), "UTC")
    assert far["detail"]["class"] == "Outlook only"


def test_attribution_and_update_time_accompany_the_forecast():
    res = run()
    assert res["attribution"] == ["Forecast: Met Éireann (met.ie), CC BY 4.0"] and res["updated"] == "12:00"


# ---------------- kit and flags are simple, stated rules ----------------

def row(**kw):
    base = dict(temp_c=12, feels_c=12, gust_kmh=20, rain_mm_h=0, rain_prob=5, family="cloud", radiation=50, s_m=10_000, eta="10:00")
    base.update(kw)
    return base


def items(rows, lights=False):
    return {k["item"] for k in ar.kit_suggestions(rows, lights, SETTINGS)}


def test_kit_rules():
    assert items([row()]) == set()
    assert "Rain jacket" in items([row(rain_mm_h=1.0, rain_prob=70)]) and "Rain jacket" in items([row(rain_prob=60)])
    assert "Overshoes" in items([row(rain_mm_h=1.0, rain_prob=70, temp_c=7)]) and "Overshoes" not in items([row(rain_mm_h=1.0, rain_prob=70, temp_c=10)])
    assert "Gloves" in items([row(feels_c=8)]) and "Gloves" not in items([row(feels_c=9)])
    assert "Thermal base layer and warm jacket" in items([row(feels_c=4)])
    assert "Windproof layer" in items([row(gust_kmh=40)]) and "Windproof layer" not in items([row(gust_kmh=39)])
    assert "Lights (front and rear)" in items([row()], lights=True) and "Lights (front and rear)" in items([row(family="fog")])
    assert "Sunscreen" in items([row(radiation=450, family="sun")]) and "Sunscreen" not in items([row(radiation=300)])
    assert all(k["why"] for k in ar.kit_suggestions([row(feels_c=2, rain_prob=80, rain_mm_h=1)], True, SETTINGS))


def flags(rows, **settings):
    return {f["kind"]: f for f in ar.safety_flags(rows, {**SETTINGS, **settings}, UTC)}


def test_safety_flags_use_the_club_thresholds_and_are_advisory():
    assert flags([row()]) == {}
    assert "gusts" not in flags([row(gust_kmh=50.0)]) and "gusts" in flags([row(gust_kmh=50.1)])
    assert "gusts" in flags([row(gust_kmh=45)], gust_kmh=40)                           # configurable per club
    assert "ice" in flags([row(temp_c=2)]) and "ice" not in flags([row(temp_c=2.1)])
    assert "rain" in flags([row(rain_mm_h=4.0)]) and "rain" not in flags([row(rain_mm_h=3.9)])
    assert "fog" in flags([row(family="fog")])
    f = flags([row(gust_kmh=70, s_m=42_000, eta="10:30")])["gusts"]
    assert f["advisory"] is True and "70 km/h" in f["text"] and "km 42" in f["text"]


def test_worst_value_is_the_one_reported():
    f = flags([row(gust_kmh=60, s_m=10_000), row(gust_kmh=80, s_m=30_000), row(gust_kmh=55, s_m=50_000)])["gusts"]
    assert f["s_m"] == 30_000 and "80 km/h" in f["text"]


# ---------------- the 10-day strip ----------------

def strip(fc, warning=lambda a, b: None, today=date(2026, 10, 9), usual=(6,), start=time(9, 0), now=datetime(2026, 10, 9, 0, 40, tzinfo=UTC)):
    return ar.day_strip((53.2, -6.9), start, "UTC", list(usual), 4.0, lambda a, b: fc, warning, today, now)


def week_forecast(horizon=datetime(2026, 10, 18, 12, tzinfo=UTC), over=None):
    return fc_of(steps_between(datetime(2026, 10, 9, 0, tzinfo=UTC), horizon, over), fetched=datetime(2026, 10, 9, 0, 40, tzinfo=UTC))


def test_strip_has_ten_days_with_today_weekends_and_usual_spin_days_marked():
    days = strip(week_forecast())
    assert len(days) == 10 and days[0]["date"] == "2026-10-09" and days[0]["today"] and not any(d["today"] for d in days[1:])
    assert [d["weekday"] for d in days[:3]] == ["Fri", "Sat", "Sun"]
    assert [d["weekend"] for d in days[:4]] == [False, True, True, False]
    assert [d["date"] for d in days if d["usual"]] == ["2026-10-11", "2026-10-18"]
    assert strip(week_forecast(), usual=(2, 6))[5]["usual"] is True                    # Wednesday the 14th as well


def test_each_day_has_an_icon_wind_and_rain_chance_for_the_ride_window():
    d = strip(week_forecast(over={}))[2]
    assert d["available"] and d["icon"] and d["wind_kmh"] == 29 and d["gust_kmh"] == 36 and d["rain_pct"] == 10 and d["temp_c"] == 6
    wet = strip(week_forecast(over={11: {**RAIN, "wind_mps": 14.0}}))[2]               # rain at 11:00 falls inside 09:00-13:00
    assert wet["label"] == "Rain" and wet["rain_pct"] == 85 and wet["wind_kmh"] == 50
    dry_window = strip(week_forecast(over={15: RAIN}))[2]                              # rain at 15:00 is after the ride
    assert dry_window["label"] != "Rain" and dry_window["rain_pct"] == 10


def test_the_worst_weather_in_the_window_picks_the_icon():
    d = strip(week_forecast(over={10: {"symbol": "Sun", "cloud_pct": 5.0}, 11: {"symbol": "RainThunder"}, 12: {"symbol": "Cloud"}}))[2]
    assert d["label"] == "Thunderstorms"


def test_days_past_the_forecast_say_when_to_check_back():
    days = strip(week_forecast(horizon=datetime(2026, 10, 14, 18, tzinfo=UTC)))
    assert [d["available"] for d in days] == [True] * 6 + [False] * 4
    assert days[6]["note"] == "Forecast not available yet. Check back from Sat 10 Oct." and days[6]["icon"] == "·"
    assert days[9]["date"] == "2026-10-18"                                           # still selectable for planning


def test_a_warning_overlapping_a_day_puts_a_dot_on_it():
    def warn(start, end):
        return "orange" if start.date() == date(2026, 10, 11) else None
    days = strip(week_forecast(), warning=warn)
    assert [d["warning"] for d in days] == [None, None, "orange"] + [None] * 7


def test_no_forecast_at_all_still_gives_ten_selectable_days():
    def boom(a, b):
        raise w.WeatherError("down")
    days = ar.day_strip((53.2, -6.9), time(9, 0), "UTC", [6], 4.0, boom, lambda a, b: None, date(2026, 10, 9), NOW)
    assert len(days) == 10 and all(not d["available"] and "No forecast" in d["note"] for d in days)


# ---------------- daylight ----------------

OCT12_SUNRISE, OCT12_SUNSET = "07:46", "18:36"        # Dublin, from an independent source (see test_geo_sun)


def lt(h, m=0, day=12):
    return datetime(2026, 10, day, h, m, tzinfo=DUB)


def daylight(finishes, start=lt(9)):
    return ar.daylight(date(2026, 10, 12), 53.35, -6.26, finishes, start, 30, "Europe/Dublin")


def test_daylight_reports_sunrise_and_sunset_in_local_time():
    d = daylight({"A": lt(14)})
    assert d["sunrise"] in ("07:45", "07:46", "07:47") and d["sunset"] in ("18:35", "18:36", "18:37")
    assert d["warnings"] == [] and d["lights_needed"] is False


def test_finishing_within_30_minutes_of_sunset_warns_that_lights_are_needed():
    d = daylight({"A": lt(14), "C": lt(18, 15)})
    assert d["lights_needed"] and len(d["warnings"]) == 1 and "Group C" in d["warnings"][0] and "before sunset" in d["warnings"][0]
    assert daylight({"A": lt(18, 0)})["warnings"] == []                              # 36 minutes of daylight left: fine


def test_finishing_after_sunset_and_starting_before_sunrise_both_warn():
    d = daylight({"B": lt(19, 10)})
    assert "after sunset" in d["warnings"][0] and d["lights_needed"]
    d = daylight({"A": lt(12)}, start=lt(7, 0))
    assert "before sunrise" in d["warnings"][0] and d["lights_needed"]


def test_no_daylight_warning_when_there_is_no_sunrise():
    d = ar.daylight(date(2026, 12, 21), 78.0, 15.0, {"A": lt(12)}, lt(9), 30, "UTC")
    assert d == {"sunrise": None, "sunset": None, "warnings": [], "lights_needed": False}
