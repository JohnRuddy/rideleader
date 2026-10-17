"""Conditions along a route for a given day and start time: weather at points along it (using each pace group's own
arrival time there), wind relative to the direction of travel, summary, kit and safety flags, the 10-day strip, and
the daylight check. All deterministic rules; no machine learning anywhere.
"""
import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

import numpy as np

from . import pace
from .sun import sun_times
from .weather import (FAMILY, Forecast, UNCERTAINTY, WeatherError, compass, detail_class, rain_mm_h, symbol_info, wind_chill)

SAMPLE_SPACING_M = 10_000
MERGE_WITHIN_M = 1_500
KMH = 3.6
# worst weather first, for picking one icon for a stretch of time
SEVERITY = ["thunder", "snow", "sleet", "rain", "shower", "drizzle", "fog", "cloud", "partlycloud", "lightcloud", "sun", "clear", "unknown"]


# ------------------------------------------------------------------ geometry helpers

class Line:
    """Route geometry for lookups: positions and travel bearing at a distance along the route."""

    def __init__(self, lats, lons, dist_m):
        self.lat, self.lon, self.dist = np.asarray(lats, float), np.asarray(lons, float), np.asarray(dist_m, float)
        self.length = float(self.dist[-1])

    def point(self, s: float) -> tuple[float, float]:
        s = min(max(s, 0.0), self.length)
        return float(np.interp(s, self.dist, self.lat)), float(np.interp(s, self.dist, self.lon))

    def bearing(self, s: float, span: float = 150.0) -> float:
        a, b = self.point(max(0.0, s - span)), self.point(min(self.length, s + span))
        dy = b[0] - a[0]
        dx = (b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
        return (math.degrees(math.atan2(dx, dy)) + 360) % 360


def wind_relation(wind_from_deg: float | None, bearing: float, wind_kmh: float | None) -> tuple[str, float]:
    """('head'|'cross'|'tail', headwind component in km/h; negative = tailwind).

    Wind direction is where the wind comes FROM, so wind from straight ahead (the same bearing you are riding) is a
    headwind. Within 45° of that is 'head', within 45° of the opposite is 'tail', otherwise 'cross'."""
    if wind_from_deg is None or wind_kmh is None:
        return "cross", 0.0
    rel = ((wind_from_deg - bearing + 540) % 360) - 180
    cls = "head" if abs(rel) <= 45 else "tail" if abs(rel) >= 135 else "cross"
    return cls, wind_kmh * math.cos(math.radians(rel))


def sample_positions(length_m: float, planned_stops_s: list[float], summits_s: list[float],
                     spacing: float = SAMPLE_SPACING_M) -> list[dict]:
    """Where to look at the weather: the start, every ~10 km, each planned stop, each summit, and the finish."""
    labelled = [{"s": 0.0, "label": "Start"}, {"s": length_m, "label": "Finish"}]
    labelled += [{"s": s, "label": "Stop"} for s in planned_stops_s]
    labelled += [{"s": s, "label": "Summit"} for s in summits_s]
    regular = [{"s": float(s), "label": f"km {s / 1000:.0f}"} for s in np.arange(spacing, length_m, spacing)]
    keep = [r for r in regular if all(abs(r["s"] - l["s"]) > MERGE_WITHIN_M for l in labelled)]
    out = sorted(labelled + keep, key=lambda x: x["s"])
    dedup = []
    for p in out:
        if dedup and abs(p["s"] - dedup[-1]["s"]) < 1.0:
            if p["label"] in ("Start", "Finish") or dedup[-1]["label"].startswith("km"):
                dedup[-1] = p
            continue
        dedup.append(p)
    return dedup


# ------------------------------------------------------------------ weather at the sample points

ForecastFn = Callable[[float, float], Forecast]


def _sample_row(line: Line, p: dict, group: pace.Group, start: datetime, planned_s: list[float], fc: Forecast,
                now: datetime, tz: ZoneInfo) -> dict:
    lat, lon = line.point(p["s"])
    eta = pace.eta(start, p["s"], group, planned_s)
    step, off_h = fc.step_near(eta.astimezone(timezone.utc))
    lead_h = (eta - now).total_seconds() / 3600
    beyond = eta.astimezone(timezone.utc) > fc.horizon + timedelta(hours=max(step.step_hours, 1))
    row = {"s_m": p["s"], "label": p["label"], "lat": lat, "lon": lon, "eta": eta.astimezone(tz).strftime("%H:%M"),
           "eta_iso": eta.isoformat(), "available": not beyond, "detail": detail_class(max(lead_h, 0)),
           "step_hours": step.step_hours}
    if beyond:
        return row
    wind_kmh = None if step.wind_mps is None else step.wind_mps * KMH
    gust_kmh = None if step.gust_mps is None else step.gust_mps * KMH
    bearing = line.bearing(p["s"])
    cls, head = wind_relation(step.wind_deg, bearing, wind_kmh)
    fam, icon, label = symbol_info(step.symbol, step.cloud_pct)
    mm_h = rain_mm_h(step)
    row.update({
        "temp_c": step.temp_c, "feels_c": None if step.temp_c is None else wind_chill(step.temp_c, wind_kmh or 0),
        "wind_kmh": wind_kmh, "gust_kmh": gust_kmh, "wind_from_deg": step.wind_deg, "wind_from": compass(step.wind_deg),
        "wind_class": cls, "headwind_kmh": head, "bearing": bearing, "rain_mm_h": mm_h, "rain_prob": step.precip_prob,
        "cloud_pct": step.cloud_pct, "radiation": step.radiation, "family": fam, "icon": icon, "label_text": label,
        "symbol": step.symbol, "forecast_step": step.t})
    return row


def _is_wet(r: dict) -> bool:
    mm, prob = r.get("rain_mm_h") or 0, r.get("rain_prob") or 0
    return mm >= 0.5 or (mm >= 0.1 and prob >= 50)


def weather_along(line: Line, start: datetime, groups: list[pace.Group], planned_s: list[float], summits_s: list[float],
                  forecast_fn: ForecastFn, settings: dict, now: datetime, tz_name: str = "Europe/Dublin",
                  lights_needed: bool = False) -> dict:
    tz = ZoneInfo(tz_name)
    positions = sample_positions(line.length, planned_s, summits_s)
    forecasts: dict[tuple, Forecast] = {}
    unavailable_reason = None
    per_group = []
    try:
        for g in groups:
            rows = []
            for p in positions:
                lat, lon = line.point(p["s"])
                key = (round(lat / 0.05), round(lon / 0.05))
                if key not in forecasts:
                    forecasts[key] = forecast_fn(lat, lon)
                rows.append(_sample_row(line, p, g, start, planned_s, forecasts[key], now, tz))
            per_group.append({"name": g.name, "samples": rows})
    except WeatherError as e:
        return {"available": False, "reason": f"No forecast right now: {e}", "groups": [], "segments": [], "summary": None,
                "kit": [], "flags": [], "attribution": []}
    primary = [r for r in per_group[0]["samples"]]
    ok_rows = [r for grp in per_group for r in grp["samples"] if r["available"]]
    if not ok_rows:
        horizon = min(f.horizon for f in forecasts.values())
        spare = (horizon - min(f.fetched_at for f in forecasts.values())).days
        unavail = (start.astimezone(tz).date() - timedelta(days=max(spare, 1))).isoformat()
        return {"available": False, "reason": f"Forecast not available yet. Check back from {unavail}.", "check_back_from": unavail,
                "groups": per_group, "segments": [], "summary": None, "kit": [], "flags": [], "attribution": _attrib(forecasts)}
    detail = max((r["detail"] for r in ok_rows), key=["Detailed", "Indicative", "Outlook only"].index)
    return {
        "available": True, "groups": per_group, "segments": wind_segments(primary, line.length),
        "summary": summary_text(primary, tz) if all(r["available"] for r in primary) else None,
        "kit": kit_suggestions(ok_rows, lights_needed, settings), "flags": safety_flags(ok_rows, settings, tz),
        "detail": {"class": detail, "note": UNCERTAINTY[detail]},
        "updated": min(f.fetched_at for f in forecasts.values()).astimezone(tz).strftime("%H:%M"),
        "updated_iso": min(f.fetched_at for f in forecasts.values()).isoformat(),
        "attribution": _attrib(forecasts)}


def _attrib(forecasts) -> list[str]:
    return sorted({f.attribution for f in forecasts.values() if f.attribution})


def wind_segments(rows: list[dict], length_m: float) -> list[dict]:
    """Route stretches coloured by head/cross/tail wind: each sample speaks for the half-way points either side."""
    rows = [r for r in rows if r["available"]]
    out = []
    for i, r in enumerate(rows):
        a = 0.0 if i == 0 else (rows[i - 1]["s_m"] + r["s_m"]) / 2
        b = length_m if i == len(rows) - 1 else (r["s_m"] + rows[i + 1]["s_m"]) / 2
        if out and out[-1]["class"] == r["wind_class"]:
            out[-1]["s1"] = b
        else:
            out.append({"s0": a, "s1": b, "class": r["wind_class"], "wind_kmh": r["wind_kmh"], "from": r["wind_from"]})
    return out


# ------------------------------------------------------------------ words

def summary_text(rows: list[dict], tz: ZoneInfo) -> str:
    """One-line ride summary: 'Dry start, showers likely 11:00–12:00 near km 45–60; 25–35 km/h SW wind — headwind on the
    way out, tailwind home; 6 °C at the start, feels like 2 °C.'"""
    first = rows[0]
    wet = [r for r in rows if _is_wet(r)]
    if len(wet) == len(rows):
        rain = "Wet throughout"
    else:
        start = "Wet start" if _is_wet(first) else "Dry start"
        if wet and not (len(wet) == 1 and wet[0] is first):
            showers = [r for r in wet if r is not first] or wet
            t0 = min(datetime.fromisoformat(r["eta_iso"]) for r in showers).astimezone(tz).strftime("%H:%M")
            t1 = max(datetime.fromisoformat(r["eta_iso"]) for r in showers).astimezone(tz).strftime("%H:%M")
            k0, k1 = min(r["s_m"] for r in showers) / 1000, max(r["s_m"] for r in showers) / 1000
            when = t0 if t0 == t1 else f"{t0}–{t1}"
            where = f"km {k0:.0f}" if round(k0) == round(k1) else f"km {k0:.0f}–{k1:.0f}"
            rain = f"{start}, showers likely {when} near {where}"
        else:
            rain = f"{start}, staying dry after"
    speeds = [r["wind_kmh"] for r in rows if r["wind_kmh"] is not None]
    lo, hi = (round(min(speeds) / 5) * 5, round(max(speeds) / 5) * 5) if speeds else (0, 0)
    dirs = sorted(r["wind_from"] for r in rows if r.get("wind_from"))
    common = max(set(dirs), key=dirs.count) if dirs else ""
    amount = f"{hi} km/h" if lo == hi else f"{lo}–{hi} km/h"
    half = max(1, len(rows) // 2)
    first_half = np.mean([r["headwind_kmh"] for r in rows[:half]])
    second_half = np.mean([r["headwind_kmh"] for r in rows[half:]] or [0.0])
    overall = np.mean([r["headwind_kmh"] for r in rows])
    if first_half > 5 and second_half < -5:
        pattern = "headwind on the way out, tailwind home"
    elif first_half < -5 and second_half > 5:
        pattern = "tailwind on the way out, headwind home"
    elif overall > 5:
        pattern = "mostly headwind"
    elif overall < -5:
        pattern = "mostly tailwind"
    else:
        pattern = "mixed, with crosswinds"
    wind = f"{amount} {common} wind — {pattern}".replace("  ", " ")
    t, f = first["temp_c"], first["feels_c"]
    temp = f"{t:.0f} °C at the start" + (f", feels like {f:.0f} °C" if f is not None and abs(f - t) >= 1 else "")
    return f"{rain}; {wind}; {temp}."


def kit_suggestions(rows: list[dict], lights_needed: bool, settings: dict) -> list[dict]:
    """Simple rules, not a model. Each suggestion says why."""
    out = []

    def add(item, why):
        out.append({"item": item, "why": why})

    temps = [r["temp_c"] for r in rows if r.get("temp_c") is not None]
    feels = [r["feels_c"] for r in rows if r.get("feels_c") is not None]
    gusts = [r["gust_kmh"] for r in rows if r.get("gust_kmh") is not None]
    wet = any(_is_wet(r) for r in rows) or max([r.get("rain_prob") or 0 for r in rows] + [0]) >= 60
    if wet:
        add("Rain jacket", "rain is likely on the ride")
        if temps and min(temps) <= 8:
            add("Overshoes", "wet and cold: keeps feet usable")
    if feels and min(feels) <= 8:
        add("Gloves", f"feels as low as {min(feels):.0f} °C")
    if feels and min(feels) <= 4:
        add("Thermal base layer and warm jacket", f"feels as low as {min(feels):.0f} °C")
    if gusts and max(gusts) >= 40:
        add("Windproof layer", f"gusts up to {max(gusts):.0f} km/h")
    if lights_needed or any(r.get("family") == "fog" for r in rows):
        add("Lights (front and rear)", "daylight is short" if lights_needed else "fog or low visibility forecast")
    sunny = [r for r in rows if (r.get("radiation") or 0) >= 400 or (r.get("radiation") is None and r.get("family") in ("sun", "lightcloud") and (r.get("temp_c") or 0) >= 15)]
    if sunny:
        add("Sunscreen", "strong sun forecast")
    return out


def safety_flags(rows: list[dict], settings: dict, tz: ZoneInfo) -> list[dict]:
    """Advisory flags against the club's thresholds. They are NOT official warnings and say so by being shown separately."""
    flags = []

    def worst(kind, key, test, text):
        cand = [r for r in rows if r.get(key) is not None and test(r[key])]
        if cand:
            r = max(cand, key=lambda r: abs(r[key]))
            flags.append({"kind": kind, "text": text(r), "s_m": r["s_m"], "advisory": True})

    worst("gusts", "gust_kmh", lambda v: v > settings["gust_kmh"],
          lambda r: f"Gusts up to {r['gust_kmh']:.0f} km/h near km {r['s_m'] / 1000:.0f} (limit {settings['gust_kmh']:.0f})")
    cold = [r for r in rows if r.get("temp_c") is not None and r["temp_c"] <= settings["ice_c"]]
    if cold:
        r = min(cold, key=lambda r: r["temp_c"])
        flags.append({"kind": "ice", "s_m": r["s_m"], "advisory": True,
                      "text": f"{r['temp_c']:.0f} °C near km {r['s_m'] / 1000:.0f} at {r['eta']}: ice risk, especially early on and on shaded descents"})
    heavy = [r for r in rows if (r.get("rain_mm_h") or 0) >= settings["heavy_rain_mm_h"]]
    if heavy:
        r = max(heavy, key=lambda r: r["rain_mm_h"])
        flags.append({"kind": "rain", "s_m": r["s_m"], "advisory": True,
                      "text": f"Heavy rain ({r['rain_mm_h']:.1f} mm/h) near km {r['s_m'] / 1000:.0f} at {r['eta']}"})
    fog = [r for r in rows if r.get("family") == "fog"]
    if fog:
        r = fog[0]
        flags.append({"kind": "fog", "s_m": r["s_m"], "advisory": True,
                      "text": f"Fog or low visibility near km {r['s_m'] / 1000:.0f} at {r['eta']}"})
    return flags


# ------------------------------------------------------------------ daylight

def daylight(day: date, lat: float, lon: float, finishes: dict[str, datetime], start: datetime, min_before_sunset: float,
             tz_name: str = "Europe/Dublin") -> dict:
    tz = ZoneInfo(tz_name)
    times = sun_times(day, lat, lon, tz_name)
    if times is None:
        return {"sunrise": None, "sunset": None, "warnings": [], "lights_needed": False}
    rise, sset = times
    notes, lights = [], False
    if start < rise:
        notes.append(f"Starts before sunrise ({rise.strftime('%H:%M')}): lights on and take care in the dark.")
        lights = True
    for name, fin in finishes.items():
        gap = (sset - fin).total_seconds() / 60
        if gap < 0:
            notes.append(f"Group {name} is due back at {fin.astimezone(tz).strftime('%H:%M')}, after sunset ({sset.strftime('%H:%M')}): lights needed.")
            lights = True
        elif gap < min_before_sunset:
            notes.append(f"Group {name} is due back at {fin.astimezone(tz).strftime('%H:%M')}, {gap:.0f} min before sunset ({sset.strftime('%H:%M')}): lights needed.")
            lights = True
    return {"sunrise": rise.strftime("%H:%M"), "sunset": sset.strftime("%H:%M"), "warnings": notes, "lights_needed": lights}


# ------------------------------------------------------------------ the 10-day strip

def _worst_family(families: list[str]) -> str:
    return min(families or ["unknown"], key=lambda f: SEVERITY.index(f) if f in SEVERITY else len(SEVERITY))


def day_strip(mid: tuple[float, float], start_time: time, tz_name: str, usual_days: list[int], duration_h: float,
              forecast_fn: ForecastFn, warning_level: Callable[[datetime, datetime], str | None], today: date,
              now: datetime, n: int = 10) -> list[dict]:
    """One badge per day for the next n days: icon, max wind, rain chance, and a dot if a warning overlaps."""
    tz = ZoneInfo(tz_name)
    try:
        fc = forecast_fn(*mid)
    except WeatherError:
        fc = None
    out = []
    for i in range(n):
        d = today + timedelta(days=i)
        start = datetime.combine(d, start_time, tzinfo=tz)
        end = start + timedelta(hours=duration_h)
        day = {"date": d.isoformat(), "weekday": d.strftime("%a"), "day": d.day, "today": i == 0, "weekend": d.weekday() >= 5,
               "usual": d.weekday() in usual_days, "start": start.isoformat(), "available": False, "icon": "·", "label": "",
               "wind_kmh": None, "rain_pct": None, "temp_c": None, "warning": warning_level(start, end), "note": ""}
        if fc is None:
            day["note"] = "No forecast right now"
        elif start.astimezone(timezone.utc) > fc.horizon:
            back = d - timedelta(days=max((fc.horizon - fc.fetched_at).days, 1))
            day["note"] = f"Forecast not available yet. Check back from {back.strftime('%a %d %b')}."
        else:
            steps = [s for s in fc.steps if start.astimezone(timezone.utc) - timedelta(hours=1) <= s.dt <= end.astimezone(timezone.utc) + timedelta(hours=1)]
            steps = steps or [fc.step_near(start.astimezone(timezone.utc))[0]]
            fam = _worst_family([symbol_info(s.symbol, s.cloud_pct)[0] for s in steps])
            _, icon, label = next((f for f in FAMILY if f[0] == fam), ("", "·", "No forecast"))
            winds = [max(s.wind_mps or 0, s.gust_mps or 0) * KMH for s in steps if s.wind_mps is not None]
            sustained = [s.wind_mps * KMH for s in steps if s.wind_mps is not None]
            lead = (start - now).total_seconds() / 3600
            day.update({"available": True, "icon": icon, "label": label, "wind_kmh": round(max(sustained)) if sustained else None,
                        "gust_kmh": round(max(winds)) if winds else None,
                        "rain_pct": round(max([s.precip_prob or 0 for s in steps] + [0])),
                        "temp_c": round(np.mean([s.temp_c for s in steps if s.temp_c is not None])) if any(s.temp_c is not None for s in steps) else None,
                        "detail": detail_class(max(lead, 0))})
        out.append(day)
    return out
