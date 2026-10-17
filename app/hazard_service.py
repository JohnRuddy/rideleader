"""Hazards that live on the route itself: busy main roads, unpaved stretches, steep sections.

They are ordinary waypoints (kind 'hazard', origin 'computed') so the map, panel, pinning and export all work on them.
They start unselected for export; the ride sheet lists every hazard whether or not it is exported."""
from geoalchemy2.elements import WKTElement

from . import along_route as ar
from . import db, roadinfo
from .climb_service import profile_for
from .models import Waypoint
from .routemodel import route_arrays

REASONS = ("busy", "unpaved", "steep")


def _clear(route) -> None:
    for w in list(route.waypoints):
        if w.kind == "hazard" and w.origin == "computed" and (w.tags or {}).get("reason") in REASONS and not w.pinned:
            db.session.delete(w)


def _add(route, line, s, name, note, reason, tags) -> None:
    lat, lon = line.point(s)
    route.waypoints.append(Waypoint(kind="hazard", name=name[:200], note=note, s_m=s, offset_m=0.0, origin="computed",
                                    included=False, geom=WKTElement(f"POINT({lon} {lat})", srid=4326),
                                    tags={"reason": reason, **tags}))


def sync(route, summary: dict | None) -> dict:
    """Recreate this route's computed hazards from the road summary and the elevation profile. Pinned ones stay."""
    lats, lons = route_arrays(route)
    line = ar.Line(lats, lons, route.dist_m)
    _clear(route)
    counts = {"busy": 0, "unpaved": 0, "steep": 0}
    for r in (summary or {}).get("busy", []):
        label = r.get("ref") or "main road"
        _add(route, line, r["s0"], f"Busy road: {label}", f"Main road ({r.get('highway')}) for {(r['s1'] - r['s0']) / 1000:.1f} km. Ride single file and keep to the left.",
             "busy", {"s_end": r["s1"], "highway": r.get("highway")})
        counts["busy"] += 1
    for r in (summary or {}).get("unpaved", []):
        _add(route, line, r["s0"], "Unpaved section", f"Unpaved ({r.get('surface') or 'surface not mapped'}) for {(r['s1'] - r['s0']):.0f} m.",
             "unpaved", {"s_end": r["s1"], "surface": r.get("surface")})
        counts["unpaved"] += 1
    profile = profile_for(route)
    if profile is not None:
        for st in roadinfo.steep_sections(profile):
            word = "climb" if st["kind"] == "climb" else "descent"
            _add(route, line, st["s0"], f"Steep {word} {abs(st['grade']):.0f}%",
                 f"{abs(st['grade']):.0f}% for {(st['s1'] - st['s0']):.0f} m" + (": take care braking." if word == "descent" else "."),
                 "steep", {"s_end": st["s1"], "grade": st["grade"]})
            counts["steep"] += 1
    db.session.flush()
    return counts


def scan_roads(route, overpass) -> dict | None:
    """Fetch OSM roads along the route, summarise them, store the summary on the route and refresh the hazards.
    Best effort: an Overpass hiccup leaves the previous summary and hazards alone."""
    lats, lons = route_arrays(route)
    try:
        ways = roadinfo.fetch_ways(overpass, lats, lons)
    except Exception:
        return None
    summary = roadinfo.summarise(lats, lons, route.dist_m, ways)
    route.road_summary = summary
    sync(route, summary)
    return summary
