"""POI scan: Overpass tiles -> classified points -> corridor passes -> Waypoint candidates."""
from datetime import datetime, timedelta, timezone

from geoalchemy2.elements import WKTElement

from . import club_stops, db
from .categories import DEFAULT_CORRIDORS, SCANNABLE, classify, label_for
from .corridor import RouteIndex
from .models import PoiTile, Waypoint
from .overpass import tile_bbox, tile_id, tiles_for_points

RESCAN_MATCH_M = 500  # same OSM element within this distance along route = same candidate


def fetch_elements(client, tiles, ttl: timedelta, progress=lambda f, msg: None) -> list[dict]:
    """Elements for all tiles, using the tile cache; only missing/expired tiles hit Overpass."""
    now = datetime.now(timezone.utc)
    tiles = sorted(tiles)
    rows = {t: db.session.get(PoiTile, tile_id(*t)) for t in tiles}
    todo = [t for t in tiles if rows[t] is None or rows[t].expires_at <= now]
    done = len(tiles) - len(todo)
    if todo:
        progress(done / len(tiles), f"Searching {len(todo)} of {len(tiles)} areas")

    def store(t, payload):                       # database work stays on the calling thread
        row = rows[t]
        if row is None:
            row = rows[t] = PoiTile(id=tile_id(*t))
            db.session.add(row)
        row.payload, row.fetched_at, row.expires_at = payload, now, now + ttl
        db.session.commit()

    workers = max(1, min(int(getattr(client, "workers", 1) or 1), len(todo)))
    if workers == 1:
        for t in todo:
            store(t, client.fetch(tile_bbox(*t)))
            done += 1
            progress(done / len(tiles), f"Searched area {done} of {len(tiles)}")
    else:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(client.fetch, tile_bbox(*t)): t for t in todo}
            for f in as_completed(futures):
                store(futures[f], f.result())     # a failed tile raises here, as it did before
                done += 1
                progress(done / len(tiles), f"Searched area {done} of {len(tiles)}")
    elements = []
    for t in tiles:
        elements += rows[t].payload
    return elements


def normalise(elements) -> list[dict]:
    """Deduplicate across tiles, take a point for ways/relations, attach a category."""
    seen, out = set(), []
    for el in elements:
        ref = f"{el['type']}/{el['id']}"
        tags = el.get("tags") or {}
        if ref in seen or not tags:
            continue
        pt = el if "lat" in el else el.get("center")
        key = classify(tags)
        if pt is None or key is None:
            continue
        seen.add(ref)
        out.append({"ref": ref, "lat": pt["lat"], "lon": pt["lon"], "tags": tags, "kind": key})
    return out


def find_hits(route, pois, corridors: dict[str, float], enabled: set[str]) -> list[dict]:
    from .routemodel import route_arrays  # local import: avoids a models <-> poi cycle at import time
    lats, lons = route_arrays(route)
    index = RouteIndex(lats, lons, route.dist_m)
    hits = []
    for p in pois:
        if p["kind"] not in enabled and p.get("origin") != "club":
            continue
        for ps in index.passes(p["lat"], p["lon"], corridors.get(p["kind"], DEFAULT_CORRIDORS.get(p["kind"], 300))):
            hits.append({**p, "s_m": ps.s_m, "offset_m": ps.offset_m, "side": ps.side})
    return sorted(hits, key=lambda h: h["s_m"])


def waypoint_name(hit) -> str:
    t = hit["tags"]
    return (hit.get("name") or t.get("name") or t.get("brand") or t.get("operator") or label_for(hit["kind"]).rstrip("s"))[:200]


def waypoint_note(hit) -> str | None:
    t = hit["tags"]
    if hit.get("origin") == "club":
        return hit.get("note")
    bits = [t.get("defibrillator:location"), t.get("description"), t.get("note")]
    return "; ".join(b for b in bits if b) or None


def apply_scan(route, hits) -> dict:
    """Merge scan results into the route's OSM candidates.

    - hit matches an existing OSM waypoint (same ref, within RESCAN_MATCH_M): refresh its position data,
      keep the leader's included/pinned/name/note edits
    - new hit: add as an unselected candidate
    - existing OSM waypoint with no hit: removed unless pinned
    GPX-origin and manual waypoints are never touched.
    """
    existing = [w for w in route.waypoints if w.origin in ("osm", "club")]
    unmatched = set(w.id for w in existing)
    added = updated = 0
    for h in hits:
        match = next((w for w in existing if w.osm_ref == h["ref"] and w.id in unmatched
                      and abs(w.s_m - h["s_m"]) < RESCAN_MATCH_M), None)
        point = WKTElement(f"POINT({h['lon']} {h['lat']})", srid=4326)
        if match:
            unmatched.discard(match.id)
            if (match.tags or {}).get("opening_hours") != h["tags"].get("opening_hours"):
                match.osm_hours_changed_at = datetime.now(timezone.utc)
            match.s_m, match.offset_m, match.side, match.geom, match.tags = (
                h["s_m"], h["offset_m"], h["side"], point, h["tags"])
            if h.get("origin") == "club":                      # the library is the authority for its own stops
                match.name, match.note, match.club_stop_id = waypoint_name(h), waypoint_note(h), h["club_stop_id"]
                match.club_hours, match.club_hours_at = h.get("hours"), h.get("verified_at") or match.club_hours_at
            updated += 1
        else:
            route.waypoints.append(Waypoint(
                kind=h["kind"], name=waypoint_name(h), note=waypoint_note(h), geom=point, s_m=h["s_m"],
                offset_m=h["offset_m"], side=h["side"], origin=h.get("origin", "osm"), osm_ref=h["ref"], tags=h["tags"],
                included=False, club_stop_id=h.get("club_stop_id"), club_hours=h.get("hours"),
                club_hours_at=(h.get("verified_at") or datetime.now(timezone.utc)) if h.get("hours") else None))
            added += 1
    removed = 0
    for w in existing:
        if w.id in unmatched and not w.pinned:
            db.session.delete(w)
            removed += 1
    kept_pinned = sum(1 for w in existing if w.id in unmatched and w.pinned)
    counts: dict[str, int] = {}
    for h in hits:
        counts[h["kind"]] = counts.get(h["kind"], 0) + 1
    return {"added": added, "updated": updated, "removed": removed, "kept_pinned": kept_pinned, "counts": counts}


def run_scan(route, client, ttl, corridors=None, enabled=None, progress=lambda f, msg: None) -> dict:
    from .routemodel import route_arrays
    corridors = {**DEFAULT_CORRIDORS, **(corridors or {})}
    enabled = set(enabled) & SCANNABLE if enabled else set(SCANNABLE)
    lats, lons = route_arrays(route)
    pad = max(corridors[k] for k in enabled) / 66_000 + 0.003  # metres -> degrees of longitude, with margin
    elements = fetch_elements(client, tiles_for_points(lats, lons, pad), ttl, progress)
    osm, club = normalise(elements), club_stops.club_pois(enabled, route.club_id)
    hits = find_hits(route, club_stops.drop_osm_duplicates(osm, club) + club, corridors, enabled)
    summary = apply_scan(route, hits)
    db.session.commit()
    return summary


def nearest_aeds(waypoints, s_m: float, limit: int = 1) -> list:
    """Nearest AEDs for a point on the route, ranked by distance along route plus off-route distance.
    Includes unselected candidates: this is safety information, not an export choice."""
    aeds = [w for w in waypoints if w.kind == "aed"]
    return sorted(aeds, key=lambda w: abs(w.s_m - s_m) + (w.offset_m or 0))[:limit]
