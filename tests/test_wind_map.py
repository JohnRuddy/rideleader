"""The map on the ride sheet is coloured by the wind along the route (head, cross, tail), with arrows for the wind's direction and
drops where rain is likely, and it falls back to the plain line when there is no forecast to use."""
import math
import re
from datetime import timedelta


from app import ride_sheet as rs
from tests.test_auth_security import other_client
from tests.test_conditions_http import FakeWeather, NOW
from tests.test_share import share, sw  # noqa: F401  (sw is a fixture)

HEAD, CROSS, TAIL = rs.WIND_COLORS["head"], rs.WIND_COLORS["cross"], rs.WIND_COLORS["tail"]
ALL_HOURS = range(24)
# The test route runs east (bearing about 90). Wind "from" 60 is ahead of the rider, 240 is behind, 150 is from the side.
FROM_AHEAD, FROM_BEHIND, FROM_SIDE = 60.0, 240.0, 150.0


def wind(deg, kmh_mps=8.0):
    return {h: {"wind_deg": deg, "wind_mps": kmh_mps} for h in ALL_HOURS}


def page_with(sw, over=None, **cfg):  # noqa: F811
    sw.application.config["WEATHER_CLIENT"] = FakeWeather(over=over, **cfg)
    token = share(sw)["token"]
    return other_client(sw).get(f"/r/{token}").get_data(as_text=True)


def svg_of(page):
    i = page.index('class="sheet-map"')
    start = page.rfind("<svg", 0, i)
    return page[start:page.index("</svg>", i) + 6]


def arrows(svg):
    return re.findall(r'<g transform="translate\(([\d.]+) ([\d.]+)\)"><title>Wind[^<]*</title>.*?rotate\((\d+)\)', svg)


# ===================== the colours =====================

def test_a_headwind_colours_the_route_red_and_the_arrows_point_downwind(sw):
    page = page_with(sw, wind(FROM_AHEAD))
    svg = svg_of(page)
    assert f'stroke="{HEAD}"' in svg and f'stroke="{TAIL}"' not in svg and f'stroke="{CROSS}"' not in svg
    assert "Map of the route, coloured by wind" in svg
    a = arrows(svg)
    assert a and all(int(r) == 240 for _, _, r in a)                       # from 60 degrees, so blowing towards 240
    assert "Headwind" in page and "from the" in page and 'class="sw head"' in page


def test_a_tailwind_is_blue(sw):
    page = page_with(sw, wind(FROM_BEHIND))
    svg = svg_of(page)
    assert f'stroke="{TAIL}"' in svg and f'stroke="{HEAD}"' not in svg
    assert all(int(r) == 60 for _, _, r in arrows(svg)) and 'class="sw tail"' in page and "Tailwind" in page


def test_a_crosswind_is_amber(sw):
    page = page_with(sw, wind(FROM_SIDE))
    svg = svg_of(page)
    assert f'stroke="{CROSS}"' in svg and f'stroke="{HEAD}"' not in svg and 'class="sw cross"' in page and "Crosswind" in page


def test_a_changing_wind_gives_stretches_in_order_and_a_key_for_each(sw):
    over = {h: {"wind_deg": FROM_AHEAD} for h in range(0, 9)} | {h: {"wind_deg": FROM_BEHIND} for h in range(9, 24)}
    page = page_with(sw, over)
    svg = svg_of(page)
    assert f'stroke="{HEAD}"' in svg and f'stroke="{TAIL}"' in svg
    assert page.index("Headwind</strong> km") < page.index("Tailwind</strong> km")
    assert 'class="sw head"' in page and 'class="sw tail"' in page and 'class="sw cross"' not in page


def test_the_wind_is_also_written_out_so_colour_is_never_the_only_clue(sw):
    page = page_with(sw, wind(FROM_AHEAD))
    m = re.search(r"<strong>Headwind</strong> km (\d+)–(\d+), about (\d+) km/h from the (\w+)", page)
    assert m and int(m.group(1)) == 0 and int(m.group(3)) in range(27, 31) and m.group(4) == "ENE"
    assert "Wind for group A" in page and "Arrows point the way the wind is blowing" in page
    assert 'aria-label="Wind colours"' in page


# ===================== rain, forecast gaps and honesty =====================

def test_no_rain_drops_on_a_dry_ride(sw):
    dry = page_with(sw, wind(FROM_AHEAD))
    assert "Rain likely here" not in dry and "blue drops" not in dry


def test_rain_drops_mark_where_rain_is_likely(sw):
    rain = {h: {"wind_deg": FROM_AHEAD, "precip_mm": 1.5, "precip_prob": 85.0, "symbol": "Rain"} for h in ALL_HOURS}
    wet = page_with(sw, rain)
    assert "Rain likely here" in svg_of(wet) and "blue drops mark where rain is likely" in wet


def test_without_a_forecast_the_route_is_the_plain_blue_line_and_says_nothing_about_wind(sw):
    page = page_with(sw, None, fail=True)
    svg = svg_of(page)
    assert 'stroke="#0071e3"' in svg and f'stroke="{HEAD}"' not in svg and "coloured by wind" not in svg and not arrows(svg)
    assert "windkey" not in page and "Wind for group" not in page


def test_a_rough_forecast_is_labelled_as_a_guide_only(sw):
    sw.application.config["CLOCK"] = lambda: NOW - timedelta(days=5)
    page = page_with(sw, wind(FROM_AHEAD))
    low = page.lower()
    assert "windkey" in page and ("indicative forecast, so a guide only" in low or "outlook only forecast, so a guide only" in low)


def test_the_wind_follows_the_first_pace_groups_times(sw):
    page = page_with(sw, wind(FROM_AHEAD))
    assert "Wind for group A" in page and "Wind for group C" not in page


# ===================== the arrows and drops stay readable =====================

def test_arrows_sit_beside_the_route_inside_the_map(sw):
    svg = svg_of(page_with(sw, wind(FROM_AHEAD)))
    for x, y, _ in arrows(svg):
        assert 12 <= float(x) <= 348 and 12 <= float(y) <= 248


def test_the_three_wind_colours_are_clearly_apart():
    def rgb(h):
        return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))
    cols = list(rs.WIND_COLORS.values())
    for i in range(3):
        for j in range(i + 1, 3):
            a, b = rgb(cols[i]), rgb(cols[j])
            assert math.dist(a, b) > 110, (cols[i], cols[j])


def test_the_wind_works_with_the_map_picture_and_in_the_leaders_preview(sw):
    page = page_with(sw, wind(FROM_AHEAD))
    svg = svg_of(page)
    assert "<image" in svg and "OpenStreetMap background" in svg and f'stroke="{HEAD}"' in svg
    leader = sw.get("/routes/1/sheet").get_data(as_text=True)
    assert f'stroke="{HEAD}"' in svg_of(leader) and "Wind for group A" in leader


# ===================== the wind view itself =====================

def sample(s_m, cls, kmh=20.0, frm="SW", deg=225.0, wet=False, available=True):
    return {"s_m": s_m, "available": available, "wind_class": cls, "wind_kmh": kmh, "wind_from": frm, "wind_from_deg": deg,
            "rain_mm_h": 1.0 if wet else 0.0, "rain_prob": 80.0 if wet else 0.0, "eta": "09:00"}


class FakePlan:
    class line:
        length = 30_000.0


def res(rows, name="A", available=True, detail="Detailed"):
    return {"available": available, "groups": [{"name": name, "samples": rows}], "detail": {"class": detail}}


def test_the_wind_view_merges_stretches_averages_speed_and_names_the_direction():
    rows = [sample(0, "head", 20, "SW"), sample(10_000, "head", 30, "SW"), sample(20_000, "tail", 10, "NE"), sample(30_000, "tail", 14, "NE")]
    v = rs._wind_view(FakePlan, res(rows))
    assert [s["class"] for s in v["stretches"]] == ["head", "tail"]
    head = v["stretches"][0]
    assert head["kmh"] == 25 and head["from"] == "SW" and head["km"].startswith("0–") and v["group"] == "A"
    assert [p["class"] for p in v["present"]] == ["head", "tail"]                       # always head, cross, tail order
    assert not v["wet"]


def test_the_wind_view_is_empty_when_there_is_nothing_to_show():
    assert rs._wind_view(FakePlan, res([], available=True)) is None
    assert rs._wind_view(FakePlan, res([sample(0, "head")], available=False)) is None
    assert rs._wind_view(FakePlan, {"available": True, "groups": []}) is None
    assert rs._wind_view(FakePlan, res([sample(0, "head", available=False)])) is None


def test_rain_is_noticed_and_the_marks_are_thinned_on_long_routes():
    rows = [sample(i * 10_000, "head", wet=(i == 7)) for i in range(40)]
    v = rs._wind_view(type("P", (), {"line": type("L", (), {"length": 400_000.0})})(), res(rows))
    assert v["wet"]
    class L:
        length = 400_000.0
        def point(self, s):
            return 53.0 + s / 4_000_000, -7.0 + s / 4_000_000
    plan = type("P", (), {"line": L()})()
    marks = rs._wind_marks(plan, v, lambda la, lo: ((lo + 7) * 4000, 260 - (la - 53) * 4000), 360, 260)
    assert 8 <= len(marks) <= 40 and any("Rain likely" in m for m in marks)               # about 14 arrows plus the wet point


def test_nothing_changes_for_the_text_summary_or_the_pdf(sw):
    sw.application.config["WEATHER_CLIENT"] = FakeWeather(over=wind(FROM_AHEAD))
    token = share(sw)["token"]
    anon = other_client(sw)
    assert anon.get(f"/r/{token}/cue-card.pdf").status_code == 200
    assert "windkey" not in anon.get(f"/r/{token}/summary.txt").get_data(as_text=True)
