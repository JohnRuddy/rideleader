"""A club's settings (usual spin days, default start time, safety thresholds). Clubs themselves, members and invite
codes live in clubs.py. A route with no club uses the first club's settings, as it did before there were several."""
from datetime import time

from . import db
from .models import Club

DEFAULT_SETTINGS = {
    "usual_days": [6],             # 0 = Monday ... 6 = Sunday
    "default_start": "09:00",
    "warning_buffer_h": 2.0,       # a warning counts if it overlaps the ride window plus this much either side
    "gust_kmh": 50.0,              # advisory flag thresholds (not official warnings)
    "ice_c": 2.0,
    "heavy_rain_mm_h": 4.0,
    "lights_min_before_sunset": 30,
    "long_stretch_km": 25.0,
    "timezone": "Europe/Dublin",   # the club's clock: sponsor campaign dates are read in it
    "sponsor_rotation": False,     # rotate between several eligible sponsor campaigns (fixed per ride for a week)
}


def get_club(club_id: int | None = None) -> Club:
    club = db.session.get(Club, club_id or 1)
    return club if club is not None else Club(name="", settings={})      # not saved: just defaults


def settings(club_id: int | None = None) -> dict:
    stored = {k: v for k, v in (get_club(club_id).settings or {}).items() if not k.startswith("_")}
    return {**DEFAULT_SETTINGS, **stored}


def validate(body: dict) -> dict:
    out = {}
    if "usual_days" in body:
        d = body["usual_days"]
        if not isinstance(d, list) or not all(isinstance(x, int) and 0 <= x <= 6 for x in d):
            raise ValueError("Usual spin days must be numbers from 0 (Monday) to 6 (Sunday).")
        out["usual_days"] = sorted(set(d))
    if "default_start" in body:
        try:
            t = time.fromisoformat(str(body["default_start"]))
        except ValueError:
            raise ValueError("Default start must look like 09:00.")
        out["default_start"] = t.strftime("%H:%M")
    if "timezone" in body:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(str(body["timezone"]))
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("That isn't a timezone name. Try Europe/Dublin.")
        out["timezone"] = str(body["timezone"])
    if "sponsor_rotation" in body:
        out["sponsor_rotation"] = bool(body["sponsor_rotation"])
    for key, lo, hi in (("warning_buffer_h", 0, 12), ("gust_kmh", 20, 150), ("ice_c", -10, 10),
                        ("heavy_rain_mm_h", 0.5, 30), ("lights_min_before_sunset", 0, 180), ("long_stretch_km", 5, 200)):
        if key in body:
            try:
                v = float(body[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number.")
            if not lo <= v <= hi:
                raise ValueError(f"{key} must be between {lo} and {hi}.")
            out[key] = v
    return out
