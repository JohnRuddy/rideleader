"""TCX course export (Garmin Courses schema). CoursePoint Name is limited to 10 characters by the schema."""
from xml.etree import ElementTree as ET

from .data import SPEED_MPS, ExportRoute
from .mapping import export_type
from .names import short_name

NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
TCX_NAME_MAX = 10
COURSE_NAME_MAX = 15


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pos(parent, lat, lon):
    pos = ET.SubElement(parent, f"{{{NS}}}Position")
    ET.SubElement(pos, f"{{{NS}}}LatitudeDegrees").text = f"{lat:.6f}"
    ET.SubElement(pos, f"{{{NS}}}LongitudeDegrees").text = f"{lon:.6f}"


def _el(parent, tag, text=None):
    e = ET.SubElement(parent, f"{{{NS}}}{tag}")
    if text is not None:
        e.text = text
    return e


def build_tcx(route: ExportRoute) -> bytes:
    ET.register_namespace("", NS)
    root = ET.Element(f"{{{NS}}}TrainingCenterDatabase")
    course = _el(_el(root, "Courses"), "Course")
    _el(course, "Name", short_name(route.title, COURSE_NAME_MAX))
    first, last = route.points[0], route.points[-1]
    lap = _el(course, "Lap")
    _el(lap, "TotalTimeSeconds", f"{route.length_m / SPEED_MPS:.0f}")
    _el(lap, "DistanceMeters", f"{route.length_m:.1f}")
    _pos(_el(lap, "BeginPosition"), first.lat, first.lon)
    _pos(_el(lap, "EndPosition"), last.lat, last.lon)
    _el(lap, "Intensity", "Active")
    track = _el(course, "Track")
    for p in route.points:
        tp = _el(track, "Trackpoint")
        _el(tp, "Time", _iso(route.time_at(p.dist_m)))
        _pos(tp, p.lat, p.lon)
        if p.ele is not None:
            _el(tp, "AltitudeMeters", f"{p.ele:.1f}")
        _el(tp, "DistanceMeters", f"{p.dist_m:.1f}")
    for w in route.waypoints:
        cp = _el(course, "CoursePoint")
        short = short_name(w.name, TCX_NAME_MAX)
        _el(cp, "Name", short)
        _el(cp, "Time", _iso(route.time_at(w.s_m)))
        _pos(cp, w.lat, w.lon)
        _el(cp, "PointType", export_type(w.kind).tcx)
        # keep the full name and any note, since the 10-character Name loses them
        notes = ": ".join(x for x in ((w.name if short != w.name else None), w.note) if x)
        if notes:
            _el(cp, "Notes", notes)
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
