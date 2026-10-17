"""The club's own stop library: favourite cafés, regroup spots, hazards and verified AEDs, with notes and a last-verified date.

Club stops are suggested first and carry a club badge. A club café's hours override OpenStreetMap's (the club confirmed
them); a club stop near an OpenStreetMap one of the same kind replaces it."""
from datetime import datetime, timezone

from geoalchemy2.shape import to_shape

from . import categories, db
from .models import ClubStop, Waypoint
from .routemodel import haversine_m

DUPLICATE_M = 40.0      # an OSM stop this close to a club stop of the same kind is the same place


def stop_json(s: ClubStop) -> dict:
    p = to_shape(s.geom)
    return {"id": s.id, "kind": s.kind, "kind_label": categories.label_for(s.kind), "name": s.name, "note": s.note, "hours": s.hours,
            "lat": p.y, "lon": p.x, "verified_at": s.last_verified_at.isoformat() if s.last_verified_at else None,
            "verified_by": s.verified_by, "active": s.active, "created_at": s.created_at.isoformat() if s.created_at else None}


def validate(body: dict, partial: bool = False) -> dict:
    from geoalchemy2.elements import WKTElement
    out = {}
    if "kind" in body or not partial:
        kind = body.get("kind")
        if kind not in categories.BY_KEY or kind == "regroup" and False:
            raise ValueError("Pick what kind of stop this is.")
        out["kind"] = kind
    if "name" in body or not partial:
        name = str(body.get("name", "")).strip()
        if not name or len(name) > 200:
            raise ValueError("Give the stop a name (up to 200 characters).")
        out["name"] = name
    if "note" in body:
        out["note"] = (str(body["note"]).strip() or None)
        if out["note"] and len(out["note"]) > 1000:
            raise ValueError("Keep the note under 1000 characters.")
    if "hours" in body:
        out["hours"] = (str(body["hours"] or "").strip() or None)
        if out["hours"] and len(out["hours"]) > 300:
            raise ValueError("Those opening hours are too long.")
    if "lat" in body or "lon" in body or not partial:
        try:
            lat, lon = float(body["lat"]), float(body["lon"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("A stop needs a latitude and longitude.")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("That position isn't on Earth.")
        out["geom"] = WKTElement(f"POINT({lon} {lat})", srid=4326)
    return out


def save_from_waypoint(w: Waypoint, user_id: int) -> ClubStop:
    """Copy a stop the leader has already found (and perhaps corrected) into the library, verified now."""
    from geoalchemy2.elements import WKTElement
    p = to_shape(w.geom)
    now = datetime.now(timezone.utc)
    stop = ClubStop(kind=w.kind if w.kind in categories.BY_KEY else "generic", name=w.name, note=w.note,
                    hours=(w.club_hours or (w.tags or {}).get("opening_hours")), geom=WKTElement(f"POINT({p.x} {p.y})", srid=4326),
                    verified_by=user_id, last_verified_at=now, active=True, created_by=user_id)
    db.session.add(stop)
    db.session.flush()
    w.club_stop_id = stop.id
    return stop


def club_pois(enabled: set[str]) -> list[dict]:
    """Active library stops as scan candidates (same shape as normalised OSM elements, plus the club's own details)."""
    out = []
    for s in ClubStop.query.filter_by(active=True).all():
        if s.kind not in enabled and s.kind != "generic":
            continue
        p = to_shape(s.geom)
        out.append({"ref": f"club/{s.id}", "lat": p.y, "lon": p.x, "kind": s.kind, "origin": "club", "club_stop_id": s.id,
                    "name": s.name, "note": s.note, "hours": s.hours, "verified_at": s.last_verified_at,
                    "tags": {"club": True, "club_stop_id": s.id, "verified_at": s.last_verified_at.isoformat() if s.last_verified_at else None,
                             **({"opening_hours": s.hours} if s.hours else {})}})
    return out


def drop_osm_duplicates(osm: list[dict], club: list[dict]) -> list[dict]:
    """The club's version of a place wins over OpenStreetMap's: remove OSM candidates of the same kind right on top of one."""
    if not club:
        return osm
    return [p for p in osm if not any(c["kind"] == p["kind"] and haversine_m(c["lat"], c["lon"], p["lat"], p["lon"]) <= DUPLICATE_M for c in club)]
