"""Road closures and roadworks along a route on the ride day, from several sources behind one interface.

Sources (see `coverage()` for what the UI tells the leader):
- Closures entered by leaders or the club: always available, always with an end date.
- OpenStreetMap: ways tagged highway=construction, or closed to bicycles/all vehicles. Volunteer-mapped: partial.
- Any ArcGIS feature service a club configures (e.g. a council's open closures layer), tried only if configured.
- NOT available yet, and said so: TII national road events (no public feed found), most county councils (notices only),
  TrafficWatchNI. Nothing found never means the route is clear.
"""
import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

import numpy as np
import requests
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform
from shapely.prepared import prep

from . import db
from .models import Closure, OsmClosureCache

log = logging.getLogger("rlk.closures")

EARTH_R = 6371008.8
ROUTE_BUFFER_M = 30.0
ALONG_MIN_M = 100.0     # a "closed to vehicles / bicycles" way must share at least this much of the route to be reported
# Ways a road cyclist can't be riding anyway. 'bicycle=no' on a footway is just what a footway is, and a footpath being
# built or fenced off doesn't close the road, so none of these count as a closure.
NOT_FOR_BIKES = {"footway", "path", "steps", "pedestrian", "bridleway", "corridor", "platform", "elevator", "cycleway"}
OSM_TTL = timedelta(hours=6)
CLEAR_NOT_PROVEN = "No closures found in the sources above. That is not a guarantee the road is clear."

NOT_AVAILABLE = [
    "TII national road events: no public feed found yet",
    "County council road-closure notices (Kildare and most others publish notices, not data)",
    "TrafficWatchNI (Northern Ireland): terms not yet checked",
]


@dataclass
class RawClosure:
    id: str
    source: str
    description: str
    geometry: object                      # shapely geometry in lon/lat
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    url: str | None = None
    radius_m: float = 0.0                 # extra buffer for point closures


@dataclass
class SourceResult:
    name: str
    label: str
    coverage: str
    items: list[RawClosure] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    fetched_at: datetime | None = None


class TrafficProvider(Protocol):
    name: str
    label: str
    coverage: str

    def fetch(self, bbox: tuple[float, float, float, float], start: datetime, end: datetime) -> SourceResult: ...


def _aware(s) -> datetime | None:
    if s in (None, "", 0):
        return None
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s / 1000 if s > 1e11 else s, tz=timezone.utc)
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class ClubClosures:
    name, label = "club", "Closures entered by leaders and the club"
    coverage = "Wherever a leader has entered one. Always checked."

    def __init__(self, club_id: int | None = None):
        self.club_id = club_id

    def fetch(self, bbox, start, end):
        from geoalchemy2.shape import to_shape
        items = []
        scope = Closure.club_id == self.club_id if self.club_id else Closure.club_id.is_(None)
        for c in Closure.query.filter(Closure.starts_at <= end, Closure.ends_at >= start, scope).all():
            items.append(RawClosure(f"club:{c.id}", "club", c.description, to_shape(c.geom), c.starts_at, c.ends_at,
                                    None, c.radius_m))
        return SourceResult(self.name, self.label, self.coverage, items, fetched_at=datetime.now(timezone.utc))


class OsmRoadworks:
    name, label = "osm", "Roadworks and closed roads mapped in OpenStreetMap"
    coverage = "Volunteer-mapped: only works someone has added to the map, and only while they keep it up to date."

    def __init__(self, overpass):
        self.overpass = overpass

    @staticmethod
    def query(bbox) -> str:
        s, w, n, e = bbox
        box = f"({s:.5f},{w:.5f},{n:.5f},{e:.5f})"
        return ("[out:json][timeout:60];(" + f'way["highway"="construction"]{box};way["highway"]["access"="no"]{box};'
                f'way["highway"]["bicycle"="no"]{box};way["highway"]["vehicle"="no"]{box};' + ");out geom tags;")

    def fetch(self, bbox, start, end):
        key = hashlib.sha1(json.dumps([round(x, 2) for x in bbox]).encode()).hexdigest()
        now = datetime.now(timezone.utc)
        row = db.session.get(OsmClosureCache, key)
        try:
            if row is None or now - row.fetched_at > OSM_TTL:
                elements = self.overpass.run(self.query(bbox))
                if row is None:
                    row = OsmClosureCache(key=key, payload={}, fetched_at=now)
                    db.session.add(row)
                row.payload, row.fetched_at = {"elements": elements}, now
                db.session.commit()
        except Exception as e:  # network, Overpass busy, ...
            if row is None:
                return SourceResult(self.name, self.label, self.coverage, ok=False, error=str(e))
        items = [self._item(el) for el in row.payload["elements"]]
        return SourceResult(self.name, self.label, self.coverage, [i for i in items if i and _active(i, start, end)],
                            fetched_at=row.fetched_at)

    @staticmethod
    def _item(el) -> RawClosure | None:
        tags, geom = el.get("tags") or {}, el.get("geometry") or []
        if len(geom) < 2:
            return None
        if tags.get("highway") in NOT_FOR_BIKES or (tags.get("highway") == "construction" and tags.get("construction") in NOT_FOR_BIKES):
            return None
        if tags.get("highway") != "construction" and tags.get("bicycle") in ("yes", "designated", "permissive"):
            return None                                   # closed to cars but open to bikes
        if tags.get("highway") != "construction" and tags.get("access") == "no" and tags.get("bicycle") == "yes":
            return None
        name = tags.get("name") or tags.get("ref") or ""
        if tags.get("highway") == "construction":
            what = f"Road under construction{': ' + tags['construction'] if tags.get('construction') else ''}"
        else:
            what = "Closed to bicycles" if tags.get("bicycle") == "no" else "Closed to vehicles (access=no)"
        ends = None
        for k in ("end_date", "construction:end_date", "opening_date"):
            if tags.get(k):
                try:
                    ends = _aware(tags[k] if "T" in tags[k] else tags[k] + "T23:59:59")
                except ValueError:
                    pass
                break
        return RawClosure(f"osm:way/{el['id']}", "osm", f"{what}{' (' + name + ')' if name else ''}",
                          LineString([(p["lon"], p["lat"]) for p in geom]), None, ends,
                          f"https://www.openstreetmap.org/way/{el['id']}")


class ArcGisClosures:
    """Any ArcGIS FeatureServer layer of closures, described by a small mapping. Nothing is assumed about a layer's
    field names, so a council's layer is only used once someone has configured and checked its mapping."""

    def __init__(self, name, label, url, mapping, coverage="", session=None, timeout=20):
        self.name, self.label, self.url, self.mapping = name, label, url.rstrip("/"), mapping
        self.coverage = coverage or "Published by the council; may not include every closure."
        self.session, self.timeout = session or requests.Session(), timeout

    def fetch(self, bbox, start, end):
        s, w, n, e = bbox
        try:
            r = self.session.get(f"{self.url}/query", timeout=self.timeout, params={
                "where": "1=1", "outFields": "*", "outSR": 4326, "f": "geojson", "inSR": 4326,
                "geometry": f"{w},{s},{e},{n}", "geometryType": "esriGeometryEnvelope", "spatialRel": "esriSpatialRelIntersects"})
            r.raise_for_status()
            feats = r.json()["features"]
        except (requests.RequestException, KeyError, ValueError) as e_:
            return SourceResult(self.name, self.label, self.coverage, ok=False, error=str(e_))
        m, items = self.mapping, []
        for i, f in enumerate(feats):
            p = f.get("properties") or {}
            if not f.get("geometry"):
                continue                                   # layers often hold rows with no shape: nothing to place on a map
            try:
                geom = shape(f["geometry"])
                a, b = _aware(p.get(m.get("start"))), _aware(p.get(m.get("end")))
            except (KeyError, ValueError, TypeError):
                continue
            it = RawClosure(f"{self.name}:{p.get(m.get('id')) or i}", self.name, str(p.get(m.get("description")) or "Road closure"),
                            geom, a, b, p.get(m.get("url")), 0.0)
            if _active(it, start, end):
                items.append(it)
        return SourceResult(self.name, self.label, self.coverage, items, fetched_at=datetime.now(timezone.utc))


def _active(c: RawClosure, start: datetime, end: datetime) -> bool:
    return (c.starts_at is None or c.starts_at <= end) and (c.ends_at is None or c.ends_at >= start)


# ------------------------------------------------------------------ intersecting with the route

def _projector(lat0: float):
    kx = math.radians(1) * EARTH_R * math.cos(math.radians(lat0))
    ky = math.radians(1) * EARTH_R
    return (lambda x, y, z=None: (x * kx, y * ky)), kx, ky


def hits_for_route(lats, lons, dist_m, items: list[RawClosure], buffer_m: float = ROUTE_BUFFER_M) -> list[dict]:
    """Closures whose area touches the route, with the stretch of route (metres along it) they affect."""
    if not items:
        return []
    lats, lons, d = np.asarray(lats, float), np.asarray(lons, float), np.asarray(dist_m, float)
    project, kx, ky = _projector(float(lats.mean()))
    grid = np.append(np.arange(0.0, d[-1], 20.0), d[-1])
    pts = np.column_stack([np.interp(grid, d, lons) * kx, np.interp(grid, d, lats) * ky])
    out = []
    import shapely
    route_line = prep(LineString(pts))
    xs, ys = pts[:, 0], pts[:, 1]
    for c in items:
        area = transform(project, c.geometry).buffer(max(c.radius_m, 0) + buffer_m)
        if not route_line.intersects(area):          # most mapped ways are nowhere near the route: skip them cheaply
            continue
        inside = shapely.contains_xy(area, xs, ys)
        if not inside.any():
            continue
        # One report per separate pass: a loop that starts and finishes beside the same road meets it twice, 44 km apart,
        # and that is two short stretches, not one 44 km closure.
        idx = np.flatnonzero(inside)
        runs, a_, prev = [], int(idx[0]), int(idx[0])
        for k in idx[1:]:
            if int(k) - prev > 5:                       # more than 100 m of route away from it: a new pass
                runs.append((a_, prev))
                a_ = int(k)
            prev = int(k)
        runs.append((a_, prev))
        for n, (i_a, i_b) in enumerate(runs, 1):
            s0, s1 = float(grid[i_a]), float(grid[i_b])
            # A way tagged access=no or bicycle=no that merely crosses the route is a driveway or a field gate, not a
            # closed road. It only counts when the route actually runs along it.
            if c.source == "osm" and c.description.startswith("Closed to") and (s1 - s0) < ALONG_MIN_M:
                continue
            out.append({"id": c.id if len(runs) == 1 else f"{c.id}#{n}", "source": c.source, "description": c.description, "url": c.url,
                        "starts_at": c.starts_at.isoformat() if c.starts_at else None, "ends_at": c.ends_at.isoformat() if c.ends_at else None,
                        "s_start": s0, "s_end": s1, "radius_m": float(c.radius_m),
                        "geometry": c.geometry.__geo_interface__, "_geom": c.geometry})
    return sorted(out, key=lambda h: h["s_start"])


def avoid_circles(hit: dict, max_circles: int = 8) -> list[tuple[float, float, float]]:
    """(lat, lon, radius) circles that cover a closure, for a router's 'avoid' option."""
    g = hit["_geom"]
    base = max(hit["radius_m"], 40.0)
    if g.geom_type == "Point":
        return [(g.y, g.x, base)]
    line = g if g.geom_type == "LineString" else g.convex_hull
    n = max(1, min(max_circles, int(line.length * 111195 / 120) + 1))
    return [(p.y, p.x, base) for p in (line.interpolate(i / max(n - 1, 1), normalized=True) for i in range(n))]


# ------------------------------------------------------------------ the whole check

def providers(config, club_id: int | None = None) -> list[TrafficProvider]:
    if config.get("CLOSURE_PROVIDERS") is not None:
        return config["CLOSURE_PROVIDERS"]
    from .overpass import get_client
    out: list[TrafficProvider] = [ClubClosures(club_id), OsmRoadworks(config.get("OVERPASS_CLIENT") or get_client(config, interactive=True))]
    for src in json.loads(config.get("ARCGIS_CLOSURE_SOURCES") or "[]"):
        out.append(ArcGisClosures(src["name"], src["label"], src["url"], src["mapping"], src.get("coverage", "")))
    return out


def check(lats, lons, dist_m, start: datetime, end: datetime, provs: list[TrafficProvider]) -> dict:
    """Everything the closures card shows: hits on this route for this window, and honest coverage."""
    la, lo = np.asarray(lats, float), np.asarray(lons, float)
    pad = 0.005
    bbox = (float(la.min()) - pad, float(lo.min()) - pad, float(la.max()) + pad, float(lo.max()) + pad)
    results, items = [], []
    for p in provs:
        try:
            r = p.fetch(bbox, start, end)
        except Exception as e:
            log.warning("closure source %s failed: %s", getattr(p, "name", p), e)
            r = SourceResult(getattr(p, "name", "?"), getattr(p, "label", "?"), getattr(p, "coverage", ""), ok=False, error=str(e))
        results.append(r)
        items += r.items
    hits = hits_for_route(lats, lons, dist_m, items)
    return {"hits": [{k: v for k, v in h.items() if k != "_geom"} for h in hits], "_hits": hits,
            "coverage": [{"name": r.name, "label": r.label, "coverage": r.coverage, "ok": r.ok, "error": r.error,
                          "checked_at": r.fetched_at.isoformat() if r.fetched_at else None, "found": len(r.items)} for r in results],
            "not_checked": NOT_AVAILABLE, "disclaimer": CLEAR_NOT_PROVEN,
            "all_sources_ok": all(r.ok for r in results)}
