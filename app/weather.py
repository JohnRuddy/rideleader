"""Forecast providers behind one interface (WeatherProvider), and an hour-long cache per ~5 km grid cell.

Met Éireann's point forecast (http://openaccess.pf.api.met.ie/metno-wdb2ts/locationforecast) is used inside the
Republic: hourly out to ~90 h from the HARMONIE model, then 3- to 6-hourly from ECMWF out to ~240 h. Elsewhere
(including Northern Ireland, which the Met Éireann point forecast isn't meant for) Open-Meteo is the fallback.

Licence note: Met Éireann requires its warnings to be shown wherever its forecast data is shown publicly. Every
page that shows a forecast here also shows the warnings banner (see conditions_views and the shared ride page).
"""
import logging
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from xml.etree import ElementTree as ET

import requests

from . import counties, db
from .models import ForecastCache

log = logging.getLogger("rlk.weather")

MET_URL = "http://openaccess.pf.api.met.ie/metno-wdb2ts/locationforecast"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_TTL = timedelta(hours=1)
GRID_DEG = 0.05                     # ~5 km: forecasts are not finer than this, and it keeps API calls few
ATTRIBUTION = {"met-eireann": "Forecast: Met Éireann (met.ie), CC BY 4.0",
               "open-meteo": "Forecast: Open-Meteo.com (CC BY 4.0), used outside the Republic of Ireland"}


class WeatherError(RuntimeError):
    pass


@dataclass
class Step:
    t: str                      # ISO UTC of this forecast time
    temp_c: float | None
    wind_mps: float | None
    wind_deg: float | None      # direction the wind blows FROM
    gust_mps: float | None
    precip_mm: float | None     # over the interval ending at (or around) t
    precip_hours: float | None
    precip_prob: float | None   # percent
    cloud_pct: float | None
    symbol: str | None
    radiation: float | None
    step_hours: float           # spacing of this part of the forecast: 1 near, 3-6 far out

    @property
    def dt(self) -> datetime:
        return datetime.fromisoformat(self.t)


class WeatherProvider(Protocol):
    name: str

    def fetch(self, lat: float, lon: float) -> list[Step]: ...


def _f(node, attr):
    try:
        return float(node.get(attr)) if node is not None and node.get(attr) is not None else None
    except ValueError:
        return None


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class MetEireannForecast:
    name = "met-eireann"

    def __init__(self, url: str = MET_URL, session=None, timeout: float = 15):
        self.url, self.session, self.timeout = url, session or requests.Session(), timeout

    def fetch(self, lat, lon):
        try:
            # the API wants a semicolon between the parameters, so it can't go through a params dict
            r = self.session.get(f"{self.url}?lat={lat:.4f};long={lon:.4f}", timeout=self.timeout)
        except requests.RequestException as e:
            raise WeatherError(f"couldn't reach Met Éireann: {e}") from e
        if r.status_code != 200:
            raise WeatherError(f"Met Éireann answered {r.status_code}")
        return parse_met_xml(r.content if isinstance(r.content, (bytes, str)) else r.text)


def parse_met_xml(xml) -> list[Step]:
    """Met Éireann XML -> steps. 'Point' nodes (from == to) carry temperature, wind, cloud; 'interval' nodes carry
    precipitation and the weather symbol for a span. Each point is paired with the interval that ends at it (or
    contains it), which is how the file lays out hourly data and its coarser later models."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise WeatherError("Met Éireann sent a forecast we couldn't read") from e
    points: dict[datetime, dict] = {}
    intervals: list[tuple[datetime, datetime, dict]] = []
    for tnode in root.iter("time"):
        loc = tnode.find("location")
        if loc is None:
            continue
        try:
            a, b = _t(tnode.get("from")), _t(tnode.get("to"))
        except (TypeError, ValueError):
            continue
        if a == b:
            points[a] = {"temp": _f(loc.find("temperature"), "value"), "wind": _f(loc.find("windSpeed"), "mps"),
                         "deg": _f(loc.find("windDirection"), "deg"), "gust": _f(loc.find("windGust"), "mps"),
                         "cloud": _f(loc.find("cloudiness"), "percent"), "rad": _f(loc.find("globalRadiation"), "value")}
        else:
            p, sym = loc.find("precipitation"), loc.find("symbol")
            intervals.append((a, b, {"mm": _f(p, "value"), "prob": _f(p, "probability"),
                                     "symbol": sym.get("id") if sym is not None else None}))
    if not points:
        raise WeatherError("Met Éireann's forecast had no data points")
    times = sorted(points)
    out = []
    for i, t in enumerate(times):
        nxt = times[i + 1] if i + 1 < len(times) else None
        prev = times[i - 1] if i else None
        gap = ((nxt - t) if nxt else (t - prev) if prev else timedelta(hours=1)).total_seconds() / 3600
        ending = [iv for iv in intervals if iv[1] == t]
        containing = [iv for iv in intervals if iv[0] <= t <= iv[1]]
        iv = min(ending or containing, key=lambda x: x[1] - x[0], default=None)
        d = points[t]
        out.append(Step(t=t.isoformat(), temp_c=d["temp"], wind_mps=d["wind"], wind_deg=d["deg"], gust_mps=d["gust"],
                        precip_mm=iv[2]["mm"] if iv else None, precip_hours=(iv[1] - iv[0]).total_seconds() / 3600 if iv else None,
                        precip_prob=iv[2]["prob"] if iv else None, cloud_pct=d["cloud"], radiation=d["rad"],
                        symbol=iv[2]["symbol"] if iv else None, step_hours=gap))
    return out


_WMO = [(range(0, 1), "Sun"), (range(1, 2), "LightCloud"), (range(2, 3), "PartlyCloud"), (range(3, 4), "Cloud"),
        (range(45, 49), "Fog"), (range(51, 58), "Drizzle"), (range(61, 66), "Rain"), (range(66, 68), "Sleet"),
        (range(71, 78), "Snow"), (range(80, 83), "Rain"), (range(85, 87), "Snow"), (range(95, 100), "RainThunder")]


def wmo_symbol(code) -> str | None:
    return next((name for r, name in _WMO if code is not None and int(code) in r), None)


class OpenMeteoForecast:
    name = "open-meteo"

    def __init__(self, url: str = OPEN_METEO_URL, session=None, timeout: float = 15):
        self.url, self.session, self.timeout = url, session or requests.Session(), timeout

    def fetch(self, lat, lon):
        try:
            r = self.session.get(self.url, timeout=self.timeout, params={
                "latitude": f"{lat:.4f}", "longitude": f"{lon:.4f}", "wind_speed_unit": "ms", "timezone": "UTC", "forecast_days": 16,
                "hourly": "temperature_2m,precipitation,precipitation_probability,wind_speed_10m,wind_direction_10m,"
                          "wind_gusts_10m,cloud_cover,weather_code,shortwave_radiation"})
            r.raise_for_status()
            h = r.json()["hourly"]
        except (requests.RequestException, KeyError, ValueError) as e:
            raise WeatherError(f"couldn't get an Open-Meteo forecast: {e}") from e
        out = []
        for i, ts in enumerate(h["time"]):
            def g(k):
                v = h.get(k, [None] * len(h["time"]))[i]
                return None if v is None else float(v)
            out.append(Step(t=datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).isoformat(), temp_c=g("temperature_2m"),
                            wind_mps=g("wind_speed_10m"), wind_deg=g("wind_direction_10m"), gust_mps=g("wind_gusts_10m"),
                            precip_mm=g("precipitation"), precip_hours=1.0, precip_prob=g("precipitation_probability"),
                            cloud_pct=g("cloud_cover"), symbol=wmo_symbol(g("weather_code")), radiation=g("shortwave_radiation"),
                            step_hours=1.0))
        return out


@dataclass
class Forecast:
    steps: list[Step]
    provider: str
    fetched_at: datetime

    @property
    def horizon(self) -> datetime:
        return self.steps[-1].dt

    @property
    def attribution(self) -> str:
        return ATTRIBUTION.get(self.provider, "")

    def step_near(self, t: datetime) -> tuple[Step, float]:
        """The forecast step closest to t, and how many hours away it is."""
        best = min(self.steps, key=lambda s: abs((s.dt - t).total_seconds()))
        return best, abs((best.dt - t).total_seconds()) / 3600


def _cell(v: float) -> float:
    return round(round(v / GRID_DEG) * GRID_DEG, 4)


def provider_for(lat: float, lon: float, config) -> WeatherProvider:
    if config.get("WEATHER_CLIENT"):
        return config["WEATHER_CLIENT"]
    if counties.in_ireland(lat, lon):
        return config.get("MET_PROVIDER") or MetEireannForecast()
    return config.get("FALLBACK_WEATHER") or OpenMeteoForecast()


def get_forecast(lat: float, lon: float, config, now: datetime | None = None) -> Forecast:
    """Forecast for the grid cell containing the point, from cache if under an hour old."""
    now = now or datetime.now(timezone.utc)
    clat, clon = _cell(lat), _cell(lon)
    provider = provider_for(clat, clon, config)
    key = f"{provider.name}:{clat}:{clon}"
    row = db.session.get(ForecastCache, key)
    if row is not None and now - row.fetched_at < CACHE_TTL:
        return Forecast([Step(**s) for s in row.payload["steps"]], provider.name, row.fetched_at)
    try:
        steps = provider.fetch(clat, clon)
    except WeatherError:
        if row is not None and now - row.fetched_at < timedelta(hours=6):     # a stale forecast beats none, and says so
            return Forecast([Step(**s) for s in row.payload["steps"]], provider.name, row.fetched_at)
        raise
    if row is None:
        row = ForecastCache(key=key, payload={}, fetched_at=now)
        db.session.add(row)
    row.payload, row.fetched_at = {"steps": [asdict(s) for s in steps]}, now
    db.session.commit()
    return Forecast(steps, provider.name, now)


# ------------------------------------------------------------------ interpreting a step

def detail_class(lead_hours: float) -> str:
    """How much to trust it, from Met Éireann's own resolution bands: hourly to 90 h, 3-hourly to 144 h, 6-hourly after."""
    if lead_hours <= 90:
        return "Detailed"
    return "Indicative" if lead_hours <= 144 else "Outlook only"


UNCERTAINTY = {"Detailed": "", "Indicative": "Expect ±2 °C and ±5 km/h on these figures.",
               "Outlook only": "Expect ±4 °C and ±10 km/h or more; use as a rough guide only."}

FAMILY = [("thunder", "⛈", "Thunderstorms"), ("snow", "🌨", "Snow"), ("sleet", "🌨", "Sleet"), ("fog", "🌫", "Fog"),
          ("drizzle", "🌦", "Drizzle"), ("rain", "🌧", "Rain"), ("shower", "🌦", "Showers"), ("partlycloud", "⛅", "Partly cloudy"),
          ("lightcloud", "🌤", "Mostly sunny"), ("cloud", "☁️", "Cloudy"), ("sun", "☀️", "Sunny"), ("clear", "☀️", "Clear")]


def symbol_info(symbol: str | None, cloud_pct: float | None = None) -> tuple[str, str, str]:
    """(family, icon, label) for a Met Éireann / Open-Meteo symbol id."""
    s = re.sub(r"[^a-z]", "", (symbol or "").lower())
    for key, icon, label in FAMILY:
        if key in s:
            return key, icon, label
    if cloud_pct is not None:
        return ("cloud", "☁️", "Cloudy") if cloud_pct > 70 else ("partlycloud", "⛅", "Partly cloudy") if cloud_pct > 30 else ("sun", "☀️", "Sunny")
    return "unknown", "·", "No forecast"


def wet(step: Step) -> bool:
    return bool((step.precip_prob or 0) >= 50 and (rain_mm_h(step) or 0) >= 0.1) or (rain_mm_h(step) or 0) >= 0.5


def rain_mm_h(step: Step) -> float | None:
    if step.precip_mm is None:
        return None
    return step.precip_mm / (step.precip_hours or 1.0)


def wind_chill(temp_c: float, wind_kmh: float) -> float:
    """Environment Canada / NWS wind chill, valid at or below 10 °C with wind above 4.8 km/h. Uses the wind speed,
    not the rider's own speed, so it errs on the mild side for a moving cyclist."""
    if temp_c is None or wind_kmh is None or temp_c > 10 or wind_kmh <= 4.8:
        return temp_c
    v = wind_kmh ** 0.16
    return 13.12 + 0.6215 * temp_c - 11.37 * v + 0.3965 * temp_c * v


COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def compass(deg: float | None) -> str:
    return "" if deg is None else COMPASS[int((deg % 360) / 22.5 + 0.5) % 16]
