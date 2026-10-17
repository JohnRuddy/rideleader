"""The club: one per deployment for now. Holds the settings leaders share (usual spin days, default start time,
safety thresholds). Everyone signed in is treated as a member; the first person to sign in becomes admin."""
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
}


def get_club() -> Club:
    club = db.session.get(Club, 1)
    if club is None:
        club = Club(id=1, name="", settings={})
        db.session.add(club)
        db.session.commit()
    return club


def settings() -> dict:
    return {**DEFAULT_SETTINGS, **(get_club().settings or {})}


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
