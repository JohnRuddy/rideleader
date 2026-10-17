"""County layer, Met Éireann region codes, and sunrise/sunset."""
from datetime import date, timedelta

import pytest

from app import counties
from app.models import County
from app.sun import sun_times

DUBLIN = (53.35, -6.26)


def test_all_26_counties_load_with_a_region_code_each(client):
    assert County.query.count() == 26
    assert all(c.code for c in County.query.all())
    assert {c.code for c in County.query.all()} == set(counties.FIPS)


def test_codes_confirmed_from_live_met_eireann_warnings(client):
    for code, name in counties.CONFIRMED_PAIRS.items():
        assert County.query.filter_by(code=code).one().name == name


@pytest.mark.parametrize("lat,lon,name", [(53.158, -6.909, "Kildare"), (53.35, -6.26, "Dublin"), (51.90, -8.47, "Cork"),
                                           (53.27, -9.05, "Galway"), (54.95, -7.73, "Donegal")])
def test_county_lookup(client, lat, lon, name):
    assert counties.county_at(lat, lon)["name"] == name


def test_places_outside_the_republic_are_not_in_a_county(client):
    assert counties.county_at(54.60, -5.93) is None                  # Belfast
    assert counties.county_at(53.40, -5.00) is None                  # Irish Sea
    assert not counties.in_ireland(51.5, -0.12)                      # London


def test_a_route_reports_its_counties_in_order_with_the_stretches_inside_each(client):
    # west to east along 53.30N: Kildare/Meath/Dublin territory
    n = 300
    lons = [-7.0 + i * 0.002 for i in range(n)]
    lats = [53.30] * n
    dist = [i * 133.0 for i in range(n)]
    found = counties.route_counties(lats, lons, dist)
    assert len(found) >= 2
    starts = [c["sections"][0][0] for c in found]
    assert starts == sorted(starts) and found[0]["sections"][0][0] == 0
    covered = sum(b - a for c in found for a, b in c["sections"])
    assert covered == pytest.approx(dist[-1], rel=0.05)              # every stretch is in exactly one county


def test_region_codes_resolve_to_names_both_ways():
    assert counties.FIPS["EI12"] == "Kildare" and counties.CODE_BY_NAME["kildare"] == "EI12"
    assert counties.name_key("Co. Tipperary ") == "cotipperary" and counties.name_key("TIPPERARY") == "tipperary"


# ---------------- sun ----------------

def mins(dt):
    return dt.hour * 60 + dt.minute


@pytest.mark.parametrize("day,rise,sset", [            # published by Open-Meteo for Dublin (53.35, -6.26), in local time
    (date(2026, 10, 12), "07:46", "18:36"), (date(2026, 10, 14), "07:49", "18:31"), (date(2026, 10, 16), "07:53", "18:26")])
def test_times_agree_with_an_independent_source_to_within_two_minutes(day, rise, sset):
    r, s_ = sun_times(day, *DUBLIN)
    hm = lambda t: int(t[:2]) * 60 + int(t[3:])
    assert abs(mins(r) - hm(rise)) <= 2 and abs(mins(s_) - hm(sset)) <= 2


def test_times_come_back_in_local_time_with_the_clock_change_applied():
    rise_oct, _ = sun_times(date(2026, 10, 24), *DUBLIN)           # still summer time
    rise_nov, _ = sun_times(date(2026, 10, 26), *DUBLIN)           # clocks went back on the 25th
    assert rise_oct.utcoffset() == timedelta(hours=1) and rise_nov.utcoffset() == timedelta(0)
    assert rise_nov.hour < rise_oct.hour or mins(rise_nov) < mins(rise_oct) - 40


def test_day_length_swings_through_the_year():
    def length(d):
        r, s = sun_times(d, *DUBLIN)
        return (s - r).total_seconds() / 3600
    assert 7.0 < length(date(2026, 12, 21)) < 7.5 and 16.5 < length(date(2026, 6, 21)) < 17.2
    assert 11.8 < length(date(2026, 3, 20)) < 12.5


def test_no_sunrise_in_the_polar_night():
    assert sun_times(date(2026, 12, 21), 78.0, 15.0, "UTC") is None
