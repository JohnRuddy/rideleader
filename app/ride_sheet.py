"""The ride sheet: everything a rider or leader needs for one planned ride, assembled in one place.

It feeds the shareable page, the WhatsApp text and the printable cue card, so all three always say the same thing.
Rules it follows:
- Met Éireann warnings come first, and wherever forecast data appears the warnings appear with it (licence).
- A weather outage, a missing opening-hours service or an unreachable closures source is said out loud, never hidden.
- Only the route's own data is used: never Strava data belonging to anyone (Strava imports can't be shared at all).
- Defibrillator locations are community data and always carry that caveat.
"""
import math
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

import numpy as np
from flask import current_app
from markupsafe import Markup
from shapely.geometry import LineString

from . import categories, closures as cl, hours as hrs
from . import warnings_feed as wf
from .models import Waypoint
from .plan import Plan, clock_now

SECTION_M = 10_000.0
AED_NOTE = "Defibrillator locations come from community data (OpenStreetMap and the club) and may be incomplete or out of date."
EMERGENCY = "In an emergency call 112 or 999."
CHECKLIST = ["Spare tube", "Pump", "First-aid kit", "Lights (front and rear)", "Ride roster / who is here", "Phone charged"]


def _km(m: float) -> str:
    return f"{m / 1000:.1f}"


def _hours_client():
    return current_app.config.get("HOURS_CLIENT") or hrs.HoursClient(current_app.config["HOURS_URL"])


def _wp(w: Waypoint) -> dict:
    return {"id": w.id, "kind": w.kind, "kind_label": categories.label_for(w.kind), "name": w.name, "note": w.note,
            "s_m": w.s_m, "km": _km(w.s_m), "offset_m": w.offset_m, "side": w.side, "included": w.included, "origin": w.origin,
            "club": w.origin == "club", "verified_at": (w.tags or {}).get("verified_at"), "planned_stop": w.planned_stop,
            "reason": (w.tags or {}).get("reason"), "mode": (w.tags or {}).get("mode"), "color": categories.color_for(w.kind)}


def nearest_by_section(waypoints: list[Waypoint], kind: str, length_m: float) -> list[dict]:
    """For each 10 km section of the route, the nearest candidate of one kind (any candidate, selected for export or not)."""
    cands = [w for w in waypoints if w.kind == kind]
    out = []
    n = max(1, math.ceil(length_m / SECTION_M))
    for i in range(n):
        s0, s1 = i * SECTION_M, min((i + 1) * SECTION_M, length_m)
        mid = (s0 + s1) / 2
        best = min(cands, key=lambda w: abs(w.s_m - mid) + (w.offset_m or 0), default=None)
        within = best is not None and abs(best.s_m - mid) <= SECTION_M
        out.append({"s0": s0, "s1": s1, "label": f"km {s0 / 1000:.0f}–{s1 / 1000:.0f}", "item": _wp(best) if within else None,
                    "away_km": round(abs(best.s_m - mid) / 1000, 1) if within else None})
    return out


def _closure_data(plan: Plan) -> dict:
    try:
        out = cl.check(plan.lats, plan.lons, plan.route.dist_m, plan.start, plan.finish, cl.providers(current_app.config, plan.route.club_id))
        out.pop("_hits", None)
        return out
    except Exception as e:      # never let a failing source take the whole sheet down
        return {"hits": [], "coverage": [], "not_checked": cl.NOT_AVAILABLE, "disclaimer": cl.CLEAR_NOT_PROVEN, "all_sources_ok": False,
                "error": f"Couldn't check for closures right now ({e})."}


def _cafes(plan: Plan) -> tuple[list[dict], str | None]:
    """Opening status for the cafés people will actually stop at (selected or planned). (rows, error message)."""
    route = plan.route
    try:
        rows = hrs.evaluate_stops(route.waypoints, plan.start, plan.groups, plan.planned, plan.tz, _hours_client())
    except hrs.HoursUnavailable:
        return [], "Couldn't check opening hours right now, so café hours are shown as unknown. Ring ahead."
    keep = {w.id for w in route.waypoints if w.kind == "cafe" and (w.included or w.planned_stop)}
    return [r for r in rows if r["id"] in keep], None


def build(route, plan: Plan | None = None, public: bool = False) -> dict:
    plan = plan or Plan(route)
    now = clock_now()
    tz = ZoneInfo(plan.tz)
    ride = plan.ride
    wps = sorted(route.waypoints, key=lambda w: w.s_m)
    banner = plan.banner()
    weather_res = plan.weather()
    day = weather_res.get("daylight") or plan.daylight()
    cafes, cafe_error = _cafes(plan)
    closure = _closure_data(plan)
    stops = [_wp(w) for w in wps if (w.included or w.planned_stop) and w.kind != "hazard"]
    hazards = [_wp(w) for w in wps if w.kind == "hazard"]
    start_lat, start_lon = plan.line.point(0)
    meet_lat = ride.meeting_lat if ride.meeting_lat is not None else start_lat
    meet_lon = ride.meeting_lon if ride.meeting_lon is not None else start_lon
    groups = []
    for g, summ in zip(plan.groups, plan.summaries):
        fin = datetime.fromisoformat(summ["finish"]).astimezone(tz)
        groups.append({"name": g.name, "speed_kmh": g.speed_kmh, "stop_min": g.stop_min, "ride_time": _dur(summ["total_s"]),
                       "finish": fin.strftime("%H:%M"), "moving": _dur(summ["moving_s"])})
    sheet = {
        "title": route.title, "date_long": plan.start.strftime("%A %d %B %Y"), "date_iso": ride.date.isoformat(),
        "start_time": plan.start.strftime("%H:%M"), "tz": plan.tz, "public": public,
        "meeting": {"name": ride.meeting_name or "Route start", "lat": meet_lat, "lon": meet_lon,
                    "map_url": f"https://www.openstreetmap.org/?mlat={meet_lat:.5f}&mlon={meet_lon:.5f}#map=17/{meet_lat:.5f}/{meet_lon:.5f}"},
        "leader_phone": ride.leader_phone, "notes": ride.notes,
        "distance_km": round(route.length_m / 1000, 1), "ascent_m": None if route.ascent_m is None else round(route.ascent_m),
        "descent_m": None if route.descent_m is None else round(route.descent_m), "groups": groups,
        "banner": banner, "weather": weather_res, "daylight": day,
        "climbs": [{"s_start": c.s_start_m, "s_end": c.s_end_m, "km": f"{c.s_start_m / 1000:.1f}–{c.s_end_m / 1000:.1f}", "length_km": round(c.length_m / 1000, 1),
                    "gain_m": round(c.gain_m), "avg": round(c.avg_grade, 1), "max": round(c.max_grade, 1), "category": c.category,
                    "regroup": c.regroup_mode} for c in route.climbs],
        "stops": stops, "cafes": cafes, "cafe_error": cafe_error, "hazards": hazards, "closures": closure,
        "aed_sections": nearest_by_section(wps, "aed", route.length_m),
        "bailouts": [_wp(w) for w in wps if w.kind == "bailout"], "aed_note": AED_NOTE, "emergency": EMERGENCY,
        "road_summary": route.road_summary, "checklist": [] if public else CHECKLIST,
        "generated_at": now.astimezone(tz).strftime("%a %d %b %H:%M"), "generated_iso": now.isoformat(),
        "attribution": ["Map and stop data © OpenStreetMap contributors (ODbL)", wf.ATTRIBUTION]
                       + weather_res.get("attribution", []) + ["County boundaries: Tailte Éireann (CC BY 4.0)"],
    }
    sheet["map_svg"] = map_svg(plan, stops, hazards, closure["hits"], banner["warnings"])
    sheet["profile_svg"] = profile_svg(plan, sheet["climbs"], stops)
    return sheet


def _dur(seconds: float) -> str:
    m = round(seconds / 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


# ------------------------------------------------------------------ lightweight inline SVG (no tiles, no JavaScript)

def _slice(plan: Plan, s0: float, s1: float, step: float = 200.0):
    ss = list(np.arange(max(s0, 0), min(s1, plan.line.length), step)) + [min(s1, plan.line.length)]
    return [plan.line.point(float(s)) for s in ss]


def map_svg(plan: Plan, stops: list[dict], hazards: list[dict], closure_hits: list[dict], warnings: list[dict],
            w: int = 360, h: int = 260, pad: int = 14) -> Markup:
    lats, lons = plan.lats, plan.lons
    line = LineString(list(zip(lons, lats))).simplify(0.0003)
    pts = list(line.coords)[:900]
    lat0 = float(np.mean(lats))
    kx = math.cos(math.radians(lat0))
    xs = [(lo) * kx for lo, la in pts]
    ys = [-la for lo, la in pts]
    span_x, span_y = (max(xs) - min(xs)) or 1e-6, (max(ys) - min(ys)) or 1e-6
    scale = min((w - 2 * pad) / span_x, (h - 2 * pad) / span_y)
    ox, oy = (w - span_x * scale) / 2 - min(xs) * scale, (h - span_y * scale) / 2 - min(ys) * scale

    def xy(lat, lon):
        return (lon * kx) * scale + ox, (-lat) * scale + oy

    def path(points, **attrs):
        d = "M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in (xy(la, lo) for la, lo in points))
        return f'<path d="{d}" fill="none" ' + " ".join(f'{k.replace("_", "-")}="{v}"' for k, v in attrs.items()) + "/>"

    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Map of the route" xmlns="http://www.w3.org/2000/svg" class="sheet-map">',
           f'<rect width="{w}" height="{h}" fill="#f2f2f7" rx="12"/>']
    level_color = {"yellow": "#ffcc00", "orange": "#ff9500", "red": "#d70015"}
    for wn in warnings:
        for a, b in wn.get("sections", []):
            out.append(path(_slice(plan, a, b), stroke=level_color[wn["level"]], stroke_width=11, stroke_opacity=0.5, stroke_linecap="round"))
    out.append(path([(la, lo) for lo, la in pts], stroke="#0071e3", stroke_width=3.5, stroke_linejoin="round", stroke_linecap="round"))
    for c in closure_hits:
        out.append(path(_slice(plan, c["s_start"], c["s_end"], 50), stroke="#d70015", stroke_width=6, stroke_dasharray="4 3"))
    for hz in hazards:
        x, y = xy(*plan.line.point(hz["s_m"]))
        out.append(f'<polygon points="{x:.1f},{y - 6:.1f} {x - 5.5:.1f},{y + 4:.1f} {x + 5.5:.1f},{y + 4:.1f}" fill="#ff9500" stroke="#fff"><title>{escape(hz["name"])}</title></polygon>')
    for s in stops:
        x, y = xy(*plan.line.point(s["s_m"]))
        if s["kind"] == "regroup":
            out.append(f'<rect x="{x - 6:.1f}" y="{y - 6:.1f}" width="12" height="12" rx="3" fill="#0a84ff" stroke="#fff"><title>{escape(s["name"])}</title></rect>'
                       f'<text x="{x:.1f}" y="{y + 3.5:.1f}" font-size="9" font-weight="700" text-anchor="middle" fill="#fff">R</text>')
        else:
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{s["color"]}" stroke="#fff" stroke-width="1.5"><title>{escape(s["name"])} (km {s["km"]})</title></circle>')
    for label, s, color in (("S", 0, "#34c759"), ("F", plan.line.length, "#1d1d1f")):
        x, y = xy(*plan.line.point(s))
        out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="8" fill="{color}" stroke="#fff" stroke-width="2"/>'
                   f'<text x="{x:.1f}" y="{y + 3.5:.1f}" font-size="10" font-weight="700" text-anchor="middle" fill="#fff">{label}</text>')
    out.append("</svg>")
    return Markup("".join(out))


def profile_svg(plan: Plan, climbs: list[dict], stops: list[dict], w: int = 360, h: int = 130) -> Markup | None:
    r = plan.route
    if not r.ele_m:
        return None
    d = np.asarray(r.dist_m, float)
    e = np.asarray(r.ele_m, float)
    idx = np.unique(np.linspace(0, len(d) - 1, min(len(d), 400)).astype(int))
    d, e = d[idx], e[idx]
    lo, hi = float(e.min()), float(e.max())
    span = max(hi - lo, 20.0)
    left, right, top, bottom = 34, 8, 8, 20
    X = lambda s: left + s / d[-1] * (w - left - right)
    Y = lambda v: h - bottom - (v - lo + span * 0.05) / (span * 1.1) * (h - top - bottom)
    area = "M" + "L".join(f"{X(s):.1f} {Y(v):.1f}" for s, v in zip(d, e)) + f"L{X(d[-1]):.1f} {h - bottom}L{X(0):.1f} {h - bottom}Z"
    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="Elevation profile" xmlns="http://www.w3.org/2000/svg" class="sheet-profile">',
           f'<rect width="{w}" height="{h}" fill="#f2f2f7" rx="12"/>']
    for c in climbs:
        out.append(f'<rect x="{X(c["s_start"]):.1f}" y="{top}" width="{max(2, X(c["s_end"]) - X(c["s_start"])):.1f}" height="{h - top - bottom}" fill="#ff3b30" opacity="0.18"/>')
    out.append(f'<path d="{area}" fill="#0071e3" fill-opacity="0.25" stroke="#0071e3" stroke-width="1.5"/>')
    step = 5000 if d[-1] > 20000 else 2000
    for s in np.arange(0, d[-1] + 1, step):
        out.append(f'<text x="{X(s):.1f}" y="{h - 5}" font-size="9" text-anchor="middle" fill="#6e6e73">{s / 1000:.0f}</text>')
    out.append(f'<text x="{left - 4}" y="{Y(hi) + 3:.1f}" font-size="9" text-anchor="end" fill="#6e6e73">{hi:.0f}m</text>'
               f'<text x="{left - 4}" y="{Y(lo) + 3:.1f}" font-size="9" text-anchor="end" fill="#6e6e73">{lo:.0f}m</text>')
    for s in stops:
        if s["kind"] in ("regroup", "cafe") or s["planned_stop"]:
            x = X(s["s_m"])
            out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{top}" y2="{h - bottom}" stroke="{"#0a84ff" if s["kind"] == "regroup" else "#a2845e"}" stroke-width="1.5" stroke-dasharray="3 2"/>'
                       f'<text x="{x:.1f}" y="{top + 8}" font-size="9" font-weight="700" text-anchor="middle" fill="{"#0a84ff" if s["kind"] == "regroup" else "#a2845e"}">{"R" if s["kind"] == "regroup" else "☕"}</text>')
    out.append("</svg>")
    return Markup("".join(out))


# ------------------------------------------------------------------ the WhatsApp text

def summary_text(s: dict, url: str | None = None) -> str:
    """A short plain-text summary for a group chat. Warnings first; nothing here is longer than it needs to be."""
    lines = []
    for w in s["banner"]["warnings"][:2]:
        lines.append(f"⚠️ Met Éireann {w['level'].title()} {w['type']} warning ({', '.join(w['counties'])}), {w['valid_from']} to {w['valid_to']}")
    if s["banner"]["status"] == "unreachable":
        lines.append("⚠️ Couldn't check Met Éireann warnings: see met.ie/warnings before you ride")
    lines.append(f"🚴 {s['title']}: {s['date_long']}")
    if s.get("notes"):
        lines.append("💬 " + " ".join(str(s["notes"]).split()))
    lines.append(f"Meet: {s['meeting']['name']} at {s['start_time']}")
    climb = f" · {s['ascent_m']} m climbing" if s["ascent_m"] is not None else ""
    lines.append(f"{s['distance_km']} km{climb}")
    lines.append("Back by: " + " · ".join(f"{g['name']} {g['finish']}" for g in s["groups"]))
    cafes = [c for c in s["cafes"]]
    for c in cafes[:2]:
        first = c["groups"][0] if c["groups"] else None
        lines.append(f"☕ {c['name']} (km {c['s_m'] / 1000:.0f})" + (f": {first['label'].lower()}" if first else ""))
    for st in s["stops"]:
        if st["kind"] == "regroup":
            lines.append(f"🔁 Regroup at km {st['km']}: {st['name']}")
            break
    wx = s["weather"]
    if wx.get("available") and wx.get("summary"):
        lines.append("🌦 " + wx["summary"])
    elif not wx.get("available"):
        lines.append("🌦 " + (wx.get("reason") or "No forecast yet"))
    for w in s["daylight"].get("warnings", [])[:1]:
        lines.append("🔦 " + w)
    if s["closures"]["hits"]:
        lines.append("⛔ Closure on the route: " + s["closures"]["hits"][0]["description"])
    if s["leader_phone"]:
        lines.append(f"Leader: {s['leader_phone']}")
    if url:
        lines.append(f"Full plan: {url}")
    return "\n".join(lines)
