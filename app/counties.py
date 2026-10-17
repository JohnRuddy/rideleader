"""Irish counties: the boundary layer (Tailte Éireann, CC BY 4.0) in PostGIS, Met Éireann's region codes, and
working out which counties a route passes through, and where.

Met Éireann warnings name areas with FIPS 10-4 region codes (confirmed from its CAP feed: EI06 Donegal, EI10 Galway,
EI14 Leitrim, EI20 Mayo, EI25 Sligo). Only those five pairs are confirmed from live data; the rest of the table
comes from the FIPS standard as I know it. So warning relevance matches on the county NAMES in the warning's
headline as well as the codes: a wrong code can then only ever cause an extra warning, never a missed one.
"""
import json
import re
from functools import lru_cache
from pathlib import Path

import numpy as np
from geoalchemy2.elements import WKTElement
from geoalchemy2.shape import to_shape
from shapely.geometry import MultiPolygon, Point, shape
from shapely.prepared import prep

from . import db
from .models import County

DATA = Path(__file__).resolve().parent.parent / "data" / "counties.geojson"
ATTRIBUTION = "Contains Tailte Éireann data (county boundaries), CC BY 4.0"

# FIPS 10-4 region code -> county. CONFIRMED_PAIRS are verified against live Met Éireann warnings.
FIPS = {"EI01": "Carlow", "EI02": "Cavan", "EI03": "Clare", "EI04": "Cork", "EI06": "Donegal", "EI07": "Dublin",
        "EI10": "Galway", "EI11": "Kerry", "EI12": "Kildare", "EI13": "Kilkenny", "EI14": "Leitrim", "EI15": "Laois",
        "EI16": "Limerick", "EI18": "Longford", "EI19": "Louth", "EI20": "Mayo", "EI21": "Meath", "EI22": "Monaghan",
        "EI23": "Offaly", "EI24": "Roscommon", "EI25": "Sligo", "EI26": "Tipperary", "EI27": "Waterford",
        "EI29": "Westmeath", "EI30": "Wexford", "EI31": "Wicklow"}
CONFIRMED_PAIRS = {"EI06": "Donegal", "EI10": "Galway", "EI14": "Leitrim", "EI20": "Mayo", "EI25": "Sligo"}
CODE_BY_NAME = {v.lower(): k for k, v in FIPS.items()}


def name_key(name: str) -> str:
    return re.sub(r"[^a-z]", "", (name or "").lower())


@lru_cache(maxsize=1)
def _prepared_features() -> list[tuple[str, str]]:
    """(name, WKT) for each county, parsed and repaired once per process: the file is 1.3 MB and rebuilding it for
    every test database made the suite crawl."""
    out = []
    for f in json.loads(DATA.read_text(encoding="utf-8"))["features"]:
        g = shape(f["geometry"]).buffer(0)           # simplification can leave slivers/self-touching rings: repair
        g = g if isinstance(g, MultiPolygon) else MultiPolygon([g])
        out.append((f["properties"]["name"], g.wkt))
    return out


def ensure_loaded() -> int:
    """Load the county layer into PostGIS if the table is empty. Returns the number of counties."""
    if County.query.count():
        return County.query.count()
    if not DATA.exists():
        return 0
    for name, wkt in _prepared_features():
        db.session.add(County(code=CODE_BY_NAME.get(name.lower()), name=name, name_key=name_key(name),
                              geom=WKTElement(wkt, srid=4326)))
    db.session.commit()
    return County.query.count()


@lru_cache(maxsize=1)
def _cached_shapes(version: int):
    """({code, name, name_key}, polygon) pairs, rebuilt if the table's row count changes. Plain data, so nothing
    here is tied to a database session."""
    return [({"code": c.code, "name": c.name, "name_key": c.name_key}, to_shape(c.geom).buffer(0))
            for c in County.query.order_by(County.name).all()]


def _shapes():
    return _cached_shapes(County.query.count())


def clear_cache() -> None:
    _cached_shapes.cache_clear()


def county_at(lat: float, lon: float) -> dict | None:
    p = Point(lon, lat)
    return next((c for c, g in _shapes() if g.contains(p)), None)


def in_ireland(lat: float, lon: float) -> bool:
    return county_at(lat, lon) is not None


def route_counties(lats, lons, dist_m, step_m: float = 250.0) -> list[dict]:
    """Counties the route passes through, each with the stretches of route (metres along it) inside it.

    Checks a point every ~step_m along the line, so a county clipped by a short corner may be missed only if the
    clip is shorter than that, which cannot matter for a warning."""
    d = np.asarray(dist_m, float)
    grid = np.append(np.arange(0.0, d[-1], step_m), d[-1])
    pl, pn = np.interp(grid, d, np.asarray(lats, float)), np.interp(grid, d, np.asarray(lons, float))
    prepared = [(c, prep(g)) for c, g in _shapes()]
    out: dict[str, dict] = {}
    for s, la, lo in zip(grid, pl, pn):
        pt = Point(lo, la)
        for c, pg in prepared:
            if pg.contains(pt):
                e = out.setdefault(c["name_key"], {"code": c["code"], "name": c["name"], "name_key": c["name_key"], "sections": []})
                if e["sections"] and s - e["sections"][-1][1] <= step_m * 1.01:
                    e["sections"][-1][1] = float(s)
                else:
                    e["sections"].append([float(s), float(s)])
                break
    return sorted(out.values(), key=lambda e: e["sections"][0][0])
