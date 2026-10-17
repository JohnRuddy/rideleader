"""Detect climbs for a stored route and keep its regroup waypoints in step with the leader's choices.

Regroups are ordinary Waypoint rows (kind 'regroup', origin 'computed') so selection, pinning, the export
counter and every export format work unchanged. Their `tags` record why they exist:
  {"reason": "climb"|"descent"|"gap", "mode": "ease_back"|"stop", "climb_id": ..., ...}
"""
from geoalchemy2.elements import WKTElement
from geoalchemy2.shape import to_shape

from . import db, regroup
from .climbs import Profile
from .models import Climb, Waypoint
from .routemodel import route_arrays

DEFAULT_SETTINGS = {"min_gain_m": 40.0, "default_mode": "ease_back", "ease_km": 2.0, "gap_km": 25.0}
MODES = ("off", "ease_back", "stop")


def settings_for(route) -> dict:
    return {**DEFAULT_SETTINGS, **(route.climb_settings or {})}


def validate_settings(body: dict) -> dict:
    """Returns the cleaned settings or raises ValueError with a message for the leader."""
    out = {}
    if "min_gain_m" in body:
        v = float(body["min_gain_m"])
        if not 0 <= v <= 2000:
            raise ValueError("Regroup threshold must be between 0 and 2000 m of climbing.")
        out["min_gain_m"] = v
    if "default_mode" in body:
        if body["default_mode"] not in MODES:
            raise ValueError("Regroup mode must be off, ease_back or stop.")
        out["default_mode"] = body["default_mode"]
    if "ease_km" in body:
        v = float(body["ease_km"])
        if not 0.5 <= v <= 10:
            raise ValueError("Ease-back distance must be between 0.5 and 10 km.")
        out["ease_km"] = v
    if "gap_km" in body:
        v = float(body["gap_km"])
        if not 5 <= v <= 200:
            raise ValueError("Long-stretch distance must be between 5 and 200 km.")
        out["gap_km"] = v
    return out


def profile_for(route) -> Profile | None:
    if not route.ele_m or any(e is None for e in route.ele_m):
        return None
    lats, lons = route_arrays(route)
    return Profile(lats, lons, route.dist_m, route.ele_m)


def _candidates(route) -> list[dict]:
    """Scanned stops as plain dicts (not our own regroups)."""
    out = []
    for w in route.waypoints:
        if w.origin == "computed" or w.kind == "regroup":
            continue
        pt = to_shape(w.geom)
        out.append({"id": w.id, "kind": w.kind, "name": w.name, "tags": w.tags or {}, "s_m": w.s_m,
                    "offset_m": w.offset_m or 0.0, "lat": pt.y, "lon": pt.x})
    return out


def _regroup_rows(route, reason=None, climb_id=None):
    return [w for w in route.waypoints if w.kind == "regroup" and w.origin == "computed"
            and (reason is None or (w.tags or {}).get("reason") == reason)
            and (climb_id is None or (w.tags or {}).get("climb_id") == climb_id)]


def _add(route, spec: dict, reason: str, included: bool, climb_id: int | None = None) -> Waypoint:
    tags = {**spec["tags"], "reason": reason}
    if climb_id is not None:
        tags["climb_id"] = climb_id
    w = Waypoint(kind="regroup", name=spec["name"][:200], note=spec["note"], s_m=spec["s_m"], offset_m=spec["offset_m"],
                 origin="computed", included=included, tags=tags,
                 geom=WKTElement(f"POINT({spec['lon']} {spec['lat']})", srid=4326))
    route.waypoints.append(w)
    return w


def apply_climb_regroup(route, climb: Climb, profile: Profile) -> str | None:
    """(Re)create the regroup waypoint for one climb from its mode. Returns a warning for the leader, if any."""
    for w in _regroup_rows(route, "climb", climb.id):
        db.session.delete(w)
    if climb.regroup_mode == "off":
        return None
    seg = _segment(climb)
    if climb.regroup_mode == "ease_back":
        spec = regroup.ease_back(profile, seg, climb.ease_km)
    else:
        spec = regroup.stop(profile, seg, _candidates(route))
        if spec is None:
            climb.regroup_mode = "off"
            return ("No safe regroup spot within 5 km of the summit (the descent doesn't ease off). "
                    "Use an ease-back instead, or add a stop by hand.")
    if any(w.pinned and abs(w.s_m - spec["s_m"]) < 1500 for w in _regroup_rows(route, "climb")):
        return None                     # the leader already placed (and pinned) one here
    _add(route, spec, "climb", True, climb.id)
    return None


def _segment(c: Climb):
    from .climbs import Segment
    return Segment(c.s_start_m, c.s_end_m, c.length_m, c.gain_m, c.avg_grade, c.max_grade, c.score, c.category)


def suggest_extras(route, profile: Profile, gap_km: float) -> int:
    """Descent-end and long-stretch suggestions: unselected candidates under the Regroups group."""
    for reason in ("descent", "gap"):
        for w in _regroup_rows(route, reason):
            if not w.pinned:
                db.session.delete(w)
    cands = _candidates(route)
    n = 0
    for spec in regroup.descent_ends(profile, cands):
        _add(route, spec, "descent", False)
        n += 1
    db.session.flush()
    anchors = [w.s_m for w in route.waypoints if w.included and (w.kind == "regroup" or w.planned_stop)]
    for spec in regroup.long_stretch(profile, anchors, cands, gap_km):
        _add(route, spec, "gap", False)
        n += 1
    return n


def detect(route, carry: list[tuple[float, str, float]] | None = None) -> dict:
    """Re-detect climbs and rebuild regroup proposals. Pinned regroups are kept.

    `carry` is [(summit distance on the new line, mode, ease_km)] from before a route edit, so a leader's
    per-climb choices survive: a climb whose summit is within 400 m of a carried one inherits its choice."""
    profile = profile_for(route)
    if profile is None:
        raise ValueError("This route has no elevation data, so climbs can't be detected.")
    cfg = settings_for(route)
    for c in list(route.climbs):
        db.session.delete(c)
    for w in _regroup_rows(route, "climb"):
        if not w.pinned:
            db.session.delete(w)
    db.session.flush()
    route.climbs = []
    warnings = []
    for seg in profile.climbs():
        mode = cfg["default_mode"] if seg.gain_m >= cfg["min_gain_m"] else "off"
        ease = cfg["ease_km"]
        kept = min((x for x in (carry or []) if abs(x[0] - seg.s_end) <= 400), key=lambda x: abs(x[0] - seg.s_end), default=None)
        if kept:
            mode, ease = kept[1], kept[2]
        c = Climb(s_start_m=seg.s_start, s_end_m=seg.s_end, length_m=seg.length_m, gain_m=seg.gain_m,
                  avg_grade=seg.avg_grade, max_grade=seg.max_grade, score=seg.score, category=seg.category,
                  regroup_mode=mode, ease_km=ease)
        route.climbs.append(c)
        db.session.flush()
        if mode != "off":
            w = apply_climb_regroup(route, c, profile)
            if w:
                warnings.append(f"Km {c.s_end_m / 1000:.1f}: {w}")
    extras = suggest_extras(route, profile, cfg["gap_km"])
    route.climbs_version = route.version
    db.session.commit()
    return {"detected": len(route.climbs), "suggestions": extras, "warnings": warnings}


def climb_json(c: Climb) -> dict:
    return {"id": c.id, "s_start_m": c.s_start_m, "s_end_m": c.s_end_m, "length_m": c.length_m, "gain_m": c.gain_m,
            "avg_grade": round(c.avg_grade, 1), "max_grade": round(c.max_grade, 1), "score": round(c.score),
            "category": c.category, "regroup_mode": c.regroup_mode, "ease_km": c.ease_km}
