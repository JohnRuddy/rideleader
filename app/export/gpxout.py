"""GPX 1.1 exporters: generic, Wahoo, Karoo, Strava. Built with gpxpy so element order is schema-valid
(metadata, wpt*, rte*, trk*)."""
import gpxpy.gpx as g

from .data import ODBL_NOTE, ExportRoute
from .mapping import export_type

CREATOR = "Ride Leader Kit"
VARIANTS = {"generic", "wahoo", "karoo", "strava"}


def build_gpx(route: ExportRoute, variant: str = "generic") -> str:
    if variant not in VARIANTS:
        raise ValueError(f"unknown GPX variant {variant!r}")
    gpx = g.GPX()
    gpx.version, gpx.creator = "1.1", CREATOR
    gpx.name = route.title
    gpx.time = route.created
    if route.uses_osm_data:
        gpx.description = ODBL_NOTE
        gpx.author_name = "Ride Leader Kit"
        gpx.copyright_author = "OpenStreetMap contributors"
        gpx.copyright_license = "https://opendatacommons.org/licenses/odbl/1-0/"

    for w in route.waypoints:
        t = export_type(w.kind)
        sym = t.karoo if variant == "karoo" else t.gpx_sym
        wpt = g.GPXWaypoint(latitude=w.lat, longitude=w.lon, name=w.name, description=w.note or None, symbol=sym)
        # Karoo drops waypoints unless <type> and <sym> both carry one of its POI types.
        wpt.type = t.karoo if variant == "karoo" else t.fit.replace("_", " ").title()
        gpx.waypoints.append(wpt)

    trk = g.GPXTrack(name=route.title)
    seg = g.GPXTrackSegment()
    for p in route.points:
        seg.points.append(g.GPXTrackPoint(latitude=p.lat, longitude=p.lon, elevation=p.ele))
    trk.segments.append(seg)
    gpx.tracks.append(trk)
    return gpx.to_xml(version="1.1")
