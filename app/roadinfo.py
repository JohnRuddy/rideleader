"""Road types and surfaces along a route (OpenStreetMap), and the hazards that follow: busy roads, unpaved stretches,
and steep sections. Deterministic matching of the route to nearby mapped roads; no judgement beyond published tags.

What this can and can't say: it knows what OSM says about the road the route runs along. Where OSM has nothing near the
line the stretch is counted as 'unknown', never guessed. It does not know junctions or traffic volumes.
"""
import hashlib
import math
from datetime import datetime, timedelta, timezone

import numpy as np
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from . import db
from .models import OsmClosureCache

EARTH_R = 6371008.8
CACHE_TTL = timedelta(days=3)
MAIN = {"trunk", "trunk_link", "primary", "primary_link"}
UNPAVED_SURFACES = {"unpaved", "gravel", "fine_gravel", "dirt", "ground", "earth", "grass", "sand", "mud", "compacted",
                    "pebblestone", "grass_paver", "woodchips"}
PAVED_SURFACES = {"asphalt", "paved", "concrete", "paving_stones", "sett", "concrete:plates", "concrete:lanes", "metal", "bricks"}
MATCH_M = 30.0
MIN_BUSY_M = 300.0
MIN_UNPAVED_M = 100.0
STEEP_GRADE = 10.0
STEEP_MIN_M = 100.0
SAMPLE_M = 25.0


def classify(tags: dict) -> str:
    """'main' (trunk/primary), 'unpaved', or 'local' for a mapped way."""
    hw, surface = tags.get("highway", ""), tags.get("surface", "")
    if surface in UNPAVED_SURFACES or (hw == "track" and surface not in PAVED_SURFACES) or \
            (hw == "path" and surface not in PAVED_SURFACES):
        return "unpaved"
    return "main" if hw in MAIN else "local"


def _proj(lat0):
    return math.radians(1) * EARTH_R * math.cos(math.radians(lat0)), math.radians(1) * EARTH_R


def around_queries(lats, lons, radius=25, max_points=120) -> list[str]:
    """Overpass queries (one per chunk of the route) for highways within `radius` m of the line."""
    line = LineString(list(zip(lons, lats))).simplify(0.0004)
    pts = list(line.coords)
    queries = []
    for i in range(0, len(pts), max_points - 1):
        chunk = pts[i:i + max_points]
        if len(chunk) < 2:
            continue
        coords = ",".join(f"{lat:.5f},{lon:.5f}" for lon, lat in chunk)
        queries.append(f'[out:json][timeout:90];way(around:{radius},{coords})["highway"];out geom tags;')
    return queries


TILE_DEG = 0.1
NEAR_DEG = 0.0005          # about 55 m of latitude: wider than MATCH_M so nothing the matcher could use is dropped


def tile_queries(lats, lons) -> list[str]:
    """One query per 0.1-degree tile the route touches. Asking for every highway in a box is far cheaper for Overpass than
    'highways around this polyline' (on our own server: about 0.2 s a tile against about a minute for the whole line)."""
    tiles = {(math.floor(la / TILE_DEG), math.floor(lo / TILE_DEG)) for la, lo in zip(lats, lons)}
    out = []
    for iy, ix in sorted(tiles):
        s_, w_, n_, e_ = iy * TILE_DEG, ix * TILE_DEG, (iy + 1) * TILE_DEG, (ix + 1) * TILE_DEG
        out.append(f'[out:json][timeout:90];way["highway"]({s_:.4f},{w_:.4f},{n_:.4f},{e_:.4f});out geom tags;')
    return out


def _near_route(ways, lats, lons) -> list[dict]:
    """Keep the ways that come within NEAR_DEG of the route; a tile holds thousands we will never look at."""
    from shapely.prepared import prep
    k = math.cos(math.radians(float(np.mean(lats))))
    zone = prep(LineString([(lo * k, la) for la, lo in zip(lats, lons)]).simplify(0.00002).buffer(NEAR_DEG))
    kept = []
    for el in ways:
        g = el.get("geometry") or []
        if len(g) >= 2 and zone.intersects(LineString([(p["lon"] * k, p["lat"]) for p in g])):
            kept.append(el)
    return kept


def fetch_ways(overpass, lats, lons) -> list[dict]:
    now = datetime.now(timezone.utc)
    qs = tile_queries(lats, lons)
    shape = LineString(list(zip(lons, lats))).simplify(0.0002).coords
    key = "road2:" + hashlib.sha1((" ".join(qs) + "|" + ",".join(f"{x:.4f}/{y:.4f}" for x, y in shape)).encode()).hexdigest()
    row = db.session.get(OsmClosureCache, key)
    if row is None or now - row.fetched_at > CACHE_TTL:
        ways: dict[int, dict] = {}
        for q in qs:
            for el in overpass.run(q):
                if el.get("type") == "way" and el.get("geometry"):
                    ways[el["id"]] = el
        kept = _near_route(list(ways.values()), lats, lons)
        if row is None:
            row = OsmClosureCache(key=key, payload={}, fetched_at=now)
            db.session.add(row)
        row.payload, row.fetched_at = {"elements": kept}, now
        db.session.commit()
    return [el for el in row.payload["elements"] if el.get("type") == "way" and el.get("geometry")]


def summarise(lats, lons, dist_m, ways: list[dict]) -> dict:
    """Percent of the route on main / local / unpaved roads (of what OSM knows), plus the busy and unpaved stretches."""
    d = np.asarray(dist_m, float)
    kx, ky = _proj(float(np.mean(lats)))
    grid = np.append(np.arange(0.0, d[-1], SAMPLE_M), d[-1])
    pts = [(float(np.interp(s, d, lons)) * kx, float(np.interp(s, d, lats)) * ky) for s in grid]
    shapes = []
    for w in ways:
        g = w["geometry"]
        if len(g) >= 2:
            shapes.append((LineString([(p["lon"] * kx, p["lat"] * ky) for p in g]), w))
    labels: list[tuple[str, dict] | None] = []
    if shapes:
        tree = STRtree([s for s, _ in shapes])
        for x, y in pts:
            p = Point(x, y)
            idx = tree.nearest(p)
            near = shapes[int(idx)]
            labels.append((classify(near[1].get("tags", {})), near[1]) if near[0].distance(p) <= MATCH_M else None)
    else:
        labels = [None] * len(pts)
    known = [l for l in labels if l]
    n = max(len(labels), 1)
    count = lambda c: sum(1 for l in known if l[0] == c)
    out = {"main_pct": round(100 * count("main") / n, 1), "local_pct": round(100 * count("local") / n, 1),
           "unpaved_pct": round(100 * count("unpaved") / n, 1), "unknown_pct": round(100 * (n - len(known)) / n, 1),
           "busy": _runs(grid, labels, "main", MIN_BUSY_M), "unpaved": _runs(grid, labels, "unpaved", MIN_UNPAVED_M),
           "ways_seen": len(shapes)}
    return out


def _runs(grid, labels, cls, min_len) -> list[dict]:
    out, start, last, tags = [], None, None, {}
    for s, l in list(zip(grid, labels)) + [(grid[-1] + 1, None)]:
        if l and l[0] == cls:
            if start is None:
                start, tags = float(s), l[1].get("tags", {})
            last = float(s)
        elif start is not None:
            if last - start - 2 * MATCH_M >= min_len:     # matching reaches 30 m past each end of a way: don't count that overhang
                out.append({"s0": start, "s1": last, "ref": tags.get("ref") or tags.get("name") or "", "highway": tags.get("highway"),
                            "surface": tags.get("surface")})
            start = None
    return out


def steep_sections(profile, min_grade: float = STEEP_GRADE, min_len: float = STEEP_MIN_M) -> list[dict]:
    """Stretches steeper than min_grade (percent) for at least min_len metres, on the smoothed profile."""
    out, start, worst = [], None, 0.0
    grid = profile.grid
    for i in range(len(grid) - 1):
        s = float(grid[i])
        g = profile.grade_at(s, 60.0)
        if abs(g) >= min_grade:
            if start is None:
                start, worst, sign = s, g, 1 if g > 0 else -1
            elif (g > 0) == (sign > 0):
                worst = max(worst, g, key=abs)
            else:
                out += _close(start, s, worst, min_len)
                start, worst, sign = s, g, 1 if g > 0 else -1
        elif start is not None:
            out += _close(start, s, worst, min_len)
            start = None
    if start is not None:
        out += _close(start, float(grid[-1]), worst, min_len)
    return out


def _close(a, b, worst, min_len):
    return [{"s0": a, "s1": b, "grade": round(worst, 1), "kind": "climb" if worst > 0 else "descent"}] if b - a >= min_len else []
