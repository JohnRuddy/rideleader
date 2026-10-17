"""Regroup proposals. Pure functions: they return plain dicts; the service layer persists them.

Candidates are dicts describing scanned stops: id, kind, name, tags, s_m, offset_m, lat, lon.
What we can and cannot judge: gradient and bends come from the route's own geometry and elevation; whether a
spot is on a fast road or a blind junction would need road-network data we do not load, so those are not checked.
"""
from .climbs import Profile, Segment

STOP_WINDOW = (200.0, 2000.0)    # metres after the summit, per the "first 1-2 km" rule
EXTENDED_LIMIT_M = 5000.0        # how far we will look if the descent runs on past 2 km
MAX_OFFSET_M = 100.0             # a regroup spot must be right by the road
NO_DESCENT_GRADE = -3.0          # steeper than this (downhill) and the group is still descending
FLAT_GRADE = -1.5
MAX_BEND_DEG = 45.0              # sharper than this over 100 m: not a place to stand around

# Preference order from the brief: lay-bys, car parks, (junction wide spots: not detectable), shop/church forecourts.
TIERS = ("layby", "parking", "shop", "fuel", "worship")


def _tier(c: dict) -> str | None:
    if c["kind"] == "parking":
        return "layby" if c["tags"].get("parking") == "layby" else "parking"
    return c["kind"] if c["kind"] in ("shop", "fuel", "worship") else None


def _safe(profile: Profile, s: float, grade_limit: float = NO_DESCENT_GRADE) -> bool:
    return profile.grade_at(s) > grade_limit and profile.bend_at(s) <= MAX_BEND_DEG


def ease_back(profile: Profile, climb: Segment, km: float) -> dict:
    """Soft-pedal section after the summit: a course point at the summit telling the group to ease off."""
    lat, lon = profile.point_at(climb.s_end)
    km_txt = f"{km:g}"
    return {"name": f"Ease back {km_txt}km", "note": f"Ease back {km_txt} km — regroup", "s_m": climb.s_end,
            "offset_m": 0.0, "lat": lat, "lon": lon, "tags": {"mode": "ease_back", "ease_km": km}}


def stop(profile: Profile, climb: Segment, candidates: list[dict]) -> dict | None:
    """A regroup spot 1-2 km after the summit, off the descent and not on a sharp bend; None if none is safe."""
    lo, hi = climb.s_end + STOP_WINDOW[0], min(climb.s_end + STOP_WINDOW[1], profile.length)
    target = climb.s_end + 1000.0
    near = [c for c in candidates if lo <= c["s_m"] <= hi and c["offset_m"] <= MAX_OFFSET_M
            and _tier(c) and _safe(profile, c["s_m"])]
    if near:
        best = min(near, key=lambda c: (TIERS.index(_tier(c)), abs(c["s_m"] - target)))
        place = best["name"] if best["name"] and best["name"] != "Parking" else None
        return {"name": f"Regroup {place}" if place else "Regroup", "s_m": best["s_m"], "offset_m": best["offset_m"],
                "lat": best["lat"], "lon": best["lon"],
                "note": f"Regroup after the climb at {place or _tier(best)}",
                "tags": {"mode": "stop", "place": place, "source": _tier(best)}}
    # Nothing mapped: the flattest, straightest stretch near the target, widening the search if the descent runs on.
    for limit, grade, reach in ((hi, NO_DESCENT_GRADE, "open road"), (min(climb.s_end + EXTENDED_LIMIT_M, profile.length),
                                                                       FLAT_GRADE, "after a long descent")):
        s = lo
        pool = []
        while s <= limit:
            if _safe(profile, s, grade):
                pool.append(s)
            s += 20.0
        if pool:
            s = min(pool, key=lambda x: (abs(profile.grade_at(x)), abs(x - target)))
            lat, lon = profile.point_at(s)
            return {"name": "Regroup", "s_m": s, "offset_m": 0.0, "lat": lat, "lon": lon,
                    "note": f"No mapped stop nearby; this is a flat, straight stretch ({reach}). Drag it to a better spot.",
                    "tags": {"mode": "stop", "place": None, "source": "route"}}
    return None


def descent_ends(profile: Profile, candidates: list[dict]) -> list[dict]:
    """Regroup suggestions at the bottom of fast descents, preferring a mapped spot just past the bottom."""
    out = []
    for d in profile.descents():
        s = d.s_end
        spot = min((c for c in candidates if s <= c["s_m"] <= s + 800 and c["offset_m"] <= MAX_OFFSET_M and _tier(c)
                    and _safe(profile, c["s_m"], FLAT_GRADE)), key=lambda c: c["s_m"] - s, default=None)
        lat, lon = (spot["lat"], spot["lon"]) if spot else profile.point_at(s)
        place = spot["name"] if spot and spot["name"] != "Parking" else None
        out.append({"name": f"Regroup {place}" if place else "Regroup (descent)",
                    "note": f"End of a {d.length_m / 1000:.1f} km descent ({d.avg_grade:.0f}% average): regroup here",
                    "s_m": spot["s_m"] if spot else s, "offset_m": spot["offset_m"] if spot else 0.0, "lat": lat, "lon": lon,
                    "tags": {"mode": "stop", "reason": "descent", "place": place}})
    return out


STRETCH_KINDS = ("shop", "fuel", "cafe", "parking", "toilets", "worship", "bailout", "shelter")


def long_stretch(profile: Profile, anchors_s: list[float], candidates: list[dict], gap_km: float = 25.0) -> list[dict]:
    """Where there is more than gap_km between stops/regroups, suggest one in the middle at a mapped spot
    (a village shop, car park, church...)."""
    pts = sorted({0.0, profile.length, *anchors_s})
    out = []
    for a, b in zip(pts, pts[1:]):
        if b - a <= gap_km * 1000:
            continue
        mid = (a + b) / 2
        pool = [c for c in candidates if c["kind"] in STRETCH_KINDS and c["offset_m"] <= MAX_OFFSET_M
                and abs(c["s_m"] - mid) <= (b - a) * 0.25 and _safe(profile, c["s_m"], FLAT_GRADE)]
        if pool:
            c = min(pool, key=lambda c: abs(c["s_m"] - mid))
            place = c["name"] if c["name"] else None
            out.append({"name": f"Regroup {place}" if place else "Regroup", "s_m": c["s_m"], "offset_m": c["offset_m"],
                        "lat": c["lat"], "lon": c["lon"],
                        "note": f"{(b - a) / 1000:.0f} km between stops: regroup here",
                        "tags": {"mode": "stop", "reason": "gap", "place": place}})
    return out
