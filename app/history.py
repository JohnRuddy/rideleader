"""Undo / redo for a route: snapshots of everything the leader edits.

A snapshot covers the geometry, the editor state (via points), every waypoint (so toggles, renames, pins and
bulk actions are all undoable), climbs and their regroup choices. It does not cover the ride plan (date, start
time, pace groups) or scan results arriving from the background worker.

Geometry is stored once per distinct shape (GeomBlob, gzip'd) and referenced by hash, so a stream of waypoint
toggles on a 10,000-point route costs a few KB each, not a copy of the line each time.
"""
import gzip
import hashlib
import json
from datetime import datetime

from geoalchemy2.elements import WKTElement
from geoalchemy2.shape import to_shape

from . import db
from .geometry import apply_coords, route_coords
from .models import Climb, GeomBlob, RouteState, Waypoint

MAX_STATES = 60


def _gz(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":"), sort_keys=True).encode(), compresslevel=6)


def _ungz(blob: bytes):
    return json.loads(gzip.decompress(blob))


def _iso(dt):
    return dt.isoformat() if dt else None


def _dt(s):
    return datetime.fromisoformat(s) if s else None


def _waypoint_dict(w: Waypoint) -> dict:
    p = to_shape(w.geom)
    return {"id": w.id, "kind": w.kind, "name": w.name, "note": w.note, "lon": p.x, "lat": p.y, "s_m": w.s_m,
            "offset_m": w.offset_m, "pinned": w.pinned, "included": w.included, "origin": w.origin, "tags": w.tags,
            "side": w.side, "osm_ref": w.osm_ref, "planned_stop": w.planned_stop, "club_hours": w.club_hours,
            "club_hours_at": _iso(w.club_hours_at), "osm_hours_changed_at": _iso(w.osm_hours_changed_at)}


def _climb_dict(c: Climb) -> dict:
    return {"id": c.id, "s_start_m": c.s_start_m, "s_end_m": c.s_end_m, "length_m": c.length_m, "gain_m": c.gain_m,
            "avg_grade": c.avg_grade, "max_grade": c.max_grade, "score": c.score, "category": c.category,
            "regroup_mode": c.regroup_mode, "ease_km": c.ease_km}


def _snapshot(route) -> dict:
    coords = [[round(c[0], 7), round(c[1], 7), None if c[2] is None else round(c[2], 2)] for c in route_coords(route)]
    geom_hash = hashlib.sha1(json.dumps(coords, separators=(",", ":")).encode()).hexdigest()
    if not GeomBlob.query.filter_by(route_id=route.id, hash=geom_hash).first():
        db.session.add(GeomBlob(route_id=route.id, hash=geom_hash, blob=_gz(coords)))
    return {"geom": geom_hash, "version": route.version, "climbs_version": route.climbs_version,
            "editor": route.editor, "climb_settings": route.climb_settings,
            "waypoints": [_waypoint_dict(w) for w in sorted(route.waypoints, key=lambda w: w.id)],
            "climbs": [_climb_dict(c) for c in sorted(route.climbs, key=lambda c: c.id)]}


def _digest(snap: dict) -> str:
    return hashlib.sha1(json.dumps(snap, sort_keys=True, default=str).encode()).hexdigest()


def _current(route) -> RouteState | None:
    return RouteState.query.filter_by(route_id=route.id, seq=route.state_seq).first() if route.state_seq is not None else None


def before(route) -> None:
    """Call before a mutation: the state as it is right now becomes the point undo returns to.

    Things that change without being edits (climbs detected the first time they are viewed, scan results from
    the background worker) are folded into the current state here, so undoing an edit goes back to what the
    leader was looking at, not to an older picture that never had those."""
    snap = _snapshot(route)
    if route.state_seq is None:
        db.session.add(RouteState(route_id=route.id, seq=0, label="Original", digest=_digest(snap), blob=_gz(snap)))
        route.state_seq = 0
    else:
        cur = _current(route)
        if cur is not None and cur.digest != _digest(snap):
            cur.digest, cur.blob = _digest(snap), _gz(snap)
    db.session.flush()


def record(route, label: str) -> bool:
    """Call after a mutation is committed. Returns False if nothing actually changed."""
    before_missing = route.state_seq is None
    if before_missing:  # a mutation path forgot before(); better a baseline-less history than none
        route.state_seq = -1
    snap = _snapshot(route)
    digest = _digest(snap)
    cur = _current(route)
    if cur is not None and cur.digest == digest:
        return False
    RouteState.query.filter(RouteState.route_id == route.id, RouteState.seq > route.state_seq).delete()  # drop redo branch
    seq = route.state_seq + 1
    db.session.add(RouteState(route_id=route.id, seq=seq, label=label[:120], digest=digest, blob=_gz(snap)))
    route.state_seq = seq
    _trim(route)
    db.session.commit()
    return True


def _trim(route) -> None:
    RouteState.query.filter(RouteState.route_id == route.id, RouteState.seq <= route.state_seq - MAX_STATES).delete()
    db.session.flush()
    keep = set()
    for st in RouteState.query.filter_by(route_id=route.id).all():
        keep.add(_ungz(st.blob)["geom"])
    for g in GeomBlob.query.filter_by(route_id=route.id).all():
        if g.hash not in keep:
            db.session.delete(g)


def _restore(route, snap: dict) -> None:
    geom = GeomBlob.query.filter_by(route_id=route.id, hash=snap["geom"]).first()
    apply_coords(route, [tuple(c) for c in _ungz(geom.blob)])
    route.version, route.climbs_version = snap["version"], snap["climbs_version"]
    route.editor, route.climb_settings = snap["editor"], snap["climb_settings"]
    route.waypoints = []
    route.climbs = []
    db.session.flush()
    for c in snap["climbs"]:
        route.climbs.append(Climb(**c))
    for w in snap["waypoints"]:
        w = dict(w)
        lon, lat = w.pop("lon"), w.pop("lat")
        w["club_hours_at"], w["osm_hours_changed_at"] = _dt(w["club_hours_at"]), _dt(w["osm_hours_changed_at"])
        route.waypoints.append(Waypoint(geom=WKTElement(f"POINT({lon} {lat})", srid=4326), **w))


def goto(route, seq: int) -> dict:
    st = RouteState.query.filter_by(route_id=route.id, seq=seq).first()
    if st is None:
        raise ValueError("That step is no longer in the history.")
    _restore(route, _ungz(st.blob))
    route.state_seq = seq
    db.session.commit()
    return info(route)


def undo(route) -> dict:
    if route.state_seq is None or route.state_seq <= _min_seq(route):
        raise ValueError("Nothing to undo.")
    return goto(route, route.state_seq - 1)


def redo(route) -> dict:
    nxt = RouteState.query.filter_by(route_id=route.id, seq=(route.state_seq if route.state_seq is not None else -1) + 1).first()
    if route.state_seq is None or nxt is None:
        raise ValueError("Nothing to redo.")
    return goto(route, nxt.seq)


def _min_seq(route) -> int:
    first = RouteState.query.filter_by(route_id=route.id).order_by(RouteState.seq).first()
    return first.seq if first else 0


def info(route) -> dict:
    states = RouteState.query.filter_by(route_id=route.id).order_by(RouteState.seq).all()
    cur = route.state_seq
    return {"cursor": cur, "can_undo": bool(states) and cur is not None and cur > states[0].seq,
            "can_redo": bool(states) and cur is not None and cur < states[-1].seq,
            "undo_label": next((s.label for s in states if s.seq == cur), None) if cur is not None and states and cur > states[0].seq else None,
            "redo_label": next((s.label for s in states if cur is not None and s.seq == cur + 1), None),
            "states": [{"seq": s.seq, "label": s.label} for s in states]}
