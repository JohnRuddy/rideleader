"""Forecast parsing (against a real Met Éireann response), the Open-Meteo fallback, caching, and step interpretation."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import weather as w
from app.models import ForecastCache

FIXTURE = Path(__file__).parent / "fixtures" / "metie_forecast.xml"
UTC = timezone.utc
NOW = datetime(2026, 10, 9, 0, 40, tzinfo=UTC)


@pytest.fixture(scope="module")
def steps():
    return w.parse_met_xml(FIXTURE.read_bytes())


# ---------------- Met Éireann XML (real sample, fetched 2026-10-09) ----------------

def test_real_response_parses_into_one_step_per_forecast_time(steps):
    assert len(steps) == 112
    assert steps[0].t == "2026-10-09T01:00:00+00:00" and steps == sorted(steps, key=lambda s: s.dt)


def test_the_first_hour_has_the_values_in_the_file(steps):
    s = steps[0]
    assert (s.temp_c, s.wind_mps, s.wind_deg, s.gust_mps, s.cloud_pct) == (14.1, 8.4, 244.9, 13.9, 100.0)
    # precipitation and symbol come from the interval ending at this time (00:00 to 01:00)
    assert (s.precip_mm, s.precip_hours, s.precip_prob, s.symbol) == (0.1, 1.0, 25.4, "Drizzle")
    assert w.symbol_info(s.symbol)[0] == "drizzle"


def test_the_forecast_gets_coarser_further_out_as_met_eireann_documents(steps):
    assert steps[0].step_hours == 1 and steps[40].step_hours == 1
    assert steps[-1].step_hours >= 3 and max(s.step_hours for s in steps) in (3, 6)
    near, far = steps[0], steps[-1]
    assert (far.dt - near.dt) > timedelta(days=8)                          # runs about nine days out
    assert w.detail_class((steps[0].dt - NOW).total_seconds() / 3600) == "Detailed"
    assert w.detail_class((far.dt - NOW).total_seconds() / 3600) == "Outlook only"


def test_later_steps_carry_multi_hour_precipitation_and_a_rate_is_derived(steps):
    far = next(s for s in steps if s.precip_hours and s.precip_hours >= 3)
    assert far.precip_mm is not None
    assert w.rain_mm_h(far) == pytest.approx(far.precip_mm / far.precip_hours)


def test_bad_xml_is_a_weather_error_not_a_crash():
    for bad in (b"<not xml", b"<weatherdata></weatherdata>"):
        with pytest.raises(w.WeatherError):
            w.parse_met_xml(bad)


class Resp:
    def __init__(self, status=200, content=b"", body=None):
        self.status_code, self.content, self._b = status, content, body
        self.text = content.decode() if isinstance(content, bytes) else content

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))


class Session:
    def __init__(self, *resp):
        self.resp, self.calls = list(resp), []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.resp.pop(0)


def test_the_met_eireann_request_uses_the_documented_semicolon_form():
    s = Session(Resp(200, FIXTURE.read_bytes()))
    out = w.MetEireannForecast(session=s).fetch(53.16, -6.9)
    assert s.calls[0][0] == f"{w.MET_URL}?lat=53.1600;long=-6.9000" and len(out) == 112


def test_met_eireann_failures_become_weather_errors():
    with pytest.raises(w.WeatherError):
        w.MetEireannForecast(session=Session(Resp(503))).fetch(53, -6)


# ---------------- Open-Meteo fallback ----------------

OM = {"hourly": {"time": ["2026-10-09T00:00", "2026-10-09T01:00"], "temperature_2m": [14.6, 14.6], "precipitation": [0.1, 0.5],
                 "precipitation_probability": [82, 87], "wind_speed_10m": [7.2, 6.9], "wind_direction_10m": [242, 240],
                 "wind_gusts_10m": [13.8, 13.3], "cloud_cover": [100, 100], "weather_code": [51, 63], "shortwave_radiation": [0, 0]}}


def test_open_meteo_hourly_data_maps_onto_the_same_steps():
    s = Session(Resp(200, body=OM))
    out = w.OpenMeteoForecast(session=s).fetch(54.6, -5.9)
    assert s.calls[0][1]["params"]["wind_speed_unit"] == "ms"
    assert out[1].t == "2026-10-09T01:00:00+00:00" and (out[1].wind_mps, out[1].wind_deg, out[1].gust_mps) == (6.9, 240.0, 13.3)
    assert (out[1].precip_mm, out[1].precip_hours, out[1].precip_prob, out[1].step_hours) == (0.5, 1.0, 87.0, 1.0)
    assert [x.symbol for x in out] == ["Drizzle", "Rain"]


def test_open_meteo_errors_are_weather_errors():
    with pytest.raises(w.WeatherError):
        w.OpenMeteoForecast(session=Session(Resp(500))).fetch(1, 1)
    with pytest.raises(w.WeatherError):
        w.OpenMeteoForecast(session=Session(Resp(200, body={"no": "hourly"}))).fetch(1, 1)


# ---------------- provider choice and the hourly cache ----------------

class Fake:
    def __init__(self, name="fake", steps_=None, fail=False):
        self.name, self.calls, self.fail = name, [], fail
        self.steps_ = steps_ or [w.Step("2026-10-09T12:00:00+00:00", 10, 5, 200, 8, 0, 1, 0, 50, "Cloud", 100, 1.0)]

    def fetch(self, lat, lon):
        self.calls.append((lat, lon))
        if self.fail:
            raise w.WeatherError("down")
        return list(self.steps_)


def test_inside_the_republic_uses_met_eireann_elsewhere_open_meteo(client):
    met, om = Fake("met-eireann"), Fake("open-meteo")
    cfg = {"MET_PROVIDER": met, "FALLBACK_WEATHER": om}
    assert w.provider_for(53.16, -6.9, cfg) is met                     # Kildare
    assert w.provider_for(54.60, -5.93, cfg) is om                     # Belfast
    assert w.provider_for(51.5, -0.12, cfg) is om                      # London
    forced = Fake("forced")
    assert w.provider_for(54.6, -5.9, {**cfg, "WEATHER_CLIENT": forced}) is forced


def test_a_forecast_is_cached_for_an_hour_per_5km_cell(client):
    met = Fake("met-eireann")
    cfg = {"MET_PROVIDER": met}
    a = w.get_forecast(53.161, -6.901, cfg, NOW)
    b = w.get_forecast(53.172, -6.912, cfg, NOW + timedelta(minutes=30))              # same ~5 km cell, within the hour
    assert len(met.calls) == 1 and a.provider == "met-eireann" and b.fetched_at == NOW
    w.get_forecast(53.30, -6.9, cfg, NOW + timedelta(minutes=30))                     # a different cell
    assert len(met.calls) == 2
    w.get_forecast(53.161, -6.901, cfg, NOW + timedelta(minutes=61))                  # an hour on: fetch again
    assert len(met.calls) == 3
    assert ForecastCache.query.count() == 2


def test_a_recent_stale_forecast_is_used_when_the_provider_is_down_but_not_forever(client):
    met = Fake("met-eireann")
    cfg = {"MET_PROVIDER": met}
    w.get_forecast(53.16, -6.9, cfg, NOW)
    met.fail = True
    stale = w.get_forecast(53.16, -6.9, cfg, NOW + timedelta(hours=3))
    assert stale.fetched_at == NOW                                                    # honest about its age
    with pytest.raises(w.WeatherError):
        w.get_forecast(53.16, -6.9, cfg, NOW + timedelta(hours=7))
    with pytest.raises(w.WeatherError):
        w.get_forecast(53.9, -8.0, cfg, NOW)                                          # nothing cached at all


def test_the_nearest_step_and_horizon():
    steps_ = [w.Step(f"2026-10-09T{h:02d}:00:00+00:00", 10, 5, 200, None, None, None, None, None, None, None, 1.0) for h in range(10, 16)]
    fc = w.Forecast(steps_, "fake", NOW)
    st, off = fc.step_near(datetime(2026, 10, 9, 12, 20, tzinfo=UTC))
    assert st.t.startswith("2026-10-09T12") and off == pytest.approx(1 / 3)
    assert fc.horizon == datetime(2026, 10, 9, 15, tzinfo=UTC)


# ---------------- interpreting a step ----------------

def test_lead_time_bands_follow_met_eireanns_resolution():
    assert [w.detail_class(h) for h in (0, 90, 90.1, 144, 144.1, 240)] == ["Detailed", "Detailed", "Indicative", "Indicative", "Outlook only", "Outlook only"]
    assert w.UNCERTAINTY["Detailed"] == "" and "±2" in w.UNCERTAINTY["Indicative"] and "±4" in w.UNCERTAINTY["Outlook only"]


def test_wind_chill_matches_the_published_formula():
    assert w.wind_chill(-10, 30) == pytest.approx(-19.5, abs=0.2)
    assert w.wind_chill(6, 29) == pytest.approx(1.4, abs=0.2)
    assert w.wind_chill(12, 40) == 12 and w.wind_chill(5, 4.0) == 5            # outside the formula's range: unchanged
    assert w.wind_chill(None, 10) is None


@pytest.mark.parametrize("deg,name", [(0, "N"), (90, "E"), (205, "SSW"), (244.9, "WSW"), (359, "N"), (None, "")])
def test_compass(deg, name):
    assert w.compass(deg) == name


@pytest.mark.parametrize("sym,fam", [("Drizzle", "drizzle"), ("RainThunder", "thunder"), ("LightRainSun", "rain"), ("PartlyCloud", "partlycloud"),
                                      ("LightCloud", "lightcloud"), ("Cloud", "cloud"), ("Fog", "fog"), ("Sleet", "sleet"), ("Sun", "sun"),
                                      ("SnowThunder", "thunder"), (None, "unknown")])
def test_symbol_families(sym, fam):
    assert w.symbol_info(sym)[0] == fam


def test_without_a_symbol_cloud_cover_decides_the_icon():
    assert w.symbol_info(None, 95)[0] == "cloud" and w.symbol_info(None, 50)[0] == "partlycloud" and w.symbol_info(None, 5)[0] == "sun"


def test_open_meteo_weather_codes():
    assert [w.wmo_symbol(c) for c in (0, 2, 45, 53, 63, 73, 81, 95, 1234)] == ["Sun", "PartlyCloud", "Fog", "Drizzle", "Rain", "Snow", "Rain", "RainThunder", None]
