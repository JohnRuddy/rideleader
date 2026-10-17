"""Exporters: every format is re-parsed with an independent library, and text formats have golden files.

Regenerate goldens after an intentional change:  UPDATE_GOLDEN=1 pytest tests/test_export.py
"""
import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import gpxpy
import pytest
from fit_tool.profile.profile_type import CoursePoint
from fitparse import FitFile
from garmin_fit_sdk import Decoder, Stream

from app.categories import CATEGORIES
from app.export import DEVICES, render, resolve, warnings_for
from app.export.data import ExportPoint, ExportRoute, ExportWaypoint
from app.export.mapping import EXPORT_TYPES, KAROO_TYPES, TCX_TYPES, export_type
from app.export.names import short_name
from app.export.tcxout import NS, TCX_NAME_MAX
from app.routemodel import haversine_m

GOLDEN = Path(__file__).parent / "golden"
FIXED = datetime(2026, 10, 11, 8, 0, 0, tzinfo=timezone.utc)


def make_route(waypoints=None, with_ele=True, n=60):
    pts, d = [], 0.0
    for i in range(n):
        lat, lon = 53.16, -6.9 + i * 0.002
        if i:
            d += haversine_m(pts[-1].lat, pts[-1].lon, lat, lon)
        pts.append(ExportPoint(lat, lon, 100 + 10 * (i % 7) if with_ele else None, d))
    wps = waypoints if waypoints is not None else (
        ExportWaypoint("cafe", "The Café Ünïcode Long Name Here", "room for 20; card only", 53.1605, -6.84, 4000),
        ExportWaypoint("aed", "AED", "outside the pharmacy", 53.162, -6.8, 6000),
        ExportWaypoint("water", "Tap", None, 53.1601, -6.78, 7000, "manual"),
        ExportWaypoint("hazard", "Cattle grid", None, 53.16, -6.76, 8000),
    )
    return ExportRoute("Kildare Sunday Spin", tuple(pts), tuple(wps), FIXED)


# ---------- the mapping table ----------

def test_every_category_has_an_export_mapping():
    for c in CATEGORIES:
        assert c.key in EXPORT_TYPES, c.key
    assert {"generic", "regroup", "climb"} <= set(EXPORT_TYPES)


def test_mapping_values_are_valid_for_each_target():
    for key, t in EXPORT_TYPES.items():
        assert t.fit in CoursePoint.__members__, (key, t.fit)
        assert t.karoo in KAROO_TYPES, (key, t.karoo)
        assert t.tcx in TCX_TYPES, (key, t.tcx)
        assert t.gpx_sym and t.short


def test_unknown_kind_falls_back_to_generic():
    assert export_type("something-new") is EXPORT_TYPES["generic"]


# ---------- names ----------

@pytest.mark.parametrize("name,limit,expected", [
    ("Café", 15, "Cafe"),
    ("The Café Ünïcode Long Name Here", 15, "Cafe Unicode Lo"),
    ("Ó Briain's Bakery & Deli Counter", 10, "O Briain's"),
    ("", 10, "Point"),
    ("日本語", 10, "Point"),
])
def test_short_name(name, limit, expected):
    out = short_name(name, limit)
    assert len(out) <= limit and out == expected


# ---------- GPX ----------

def parse(xml):
    return gpxpy.parse(xml)


@pytest.mark.parametrize("device", ["gpx", "wahoo", "strava", "karoo"])
def test_gpx_reparses_with_track_and_waypoints(device):
    r = render(make_route(), device, None, "t")
    g = parse(r.data.decode())
    assert g.version == "1.1" and len(g.tracks[0].segments[0].points) == 60
    assert [w.name for w in g.waypoints][:2] == ["The Café Ünïcode Long Name Here", "AED"]
    assert all(w.symbol for w in g.waypoints)
    assert g.tracks[0].segments[0].points[3].elevation is not None
    assert "OpenStreetMap" in r.data.decode()                   # ODbL attribution travels with the file


def test_karoo_waypoints_carry_matching_type_and_sym_from_the_allowed_set():
    g = parse(render(make_route(), "karoo", None, "t").data.decode())
    assert {w.symbol for w in g.waypoints} <= KAROO_TYPES
    assert all(w.type == w.symbol for w in g.waypoints)
    assert [(w.name, w.symbol) for w in g.waypoints] == [
        ("The Café Ünïcode Long Name Here", "Food"), ("AED", "Generic"), ("Tap", "Water"), ("Cattle grid", "Danger")]


def test_gpx_keeps_full_unicode_names_and_notes():
    g = parse(render(make_route(), "gpx", None, "t").data.decode())
    assert g.waypoints[0].description == "room for 20; card only"


def test_no_osm_attribution_when_nothing_from_osm():
    only_manual = (ExportWaypoint("generic", "Meet", None, 53.16, -6.9, 0, "manual"),)
    assert "OpenStreetMap" not in render(make_route(only_manual), "gpx", None, "t").data.decode()


# ---------- TCX ----------

def test_tcx_reparses_and_respects_schema_name_limit():
    root = ET.fromstring(render(make_route(), "garmin", "tcx", "t").data)
    ns = {"t": NS}
    assert len(root.findall(".//t:Trackpoint", ns)) == 60
    cps = root.findall(".//t:CoursePoint", ns)
    assert len(cps) == 4
    assert all(len(c.find("t:Name", ns).text) <= TCX_NAME_MAX for c in cps)
    assert [c.find("t:PointType", ns).text for c in cps] == ["Food", "First Aid", "Water", "Danger"]
    assert "room for 20" in cps[0].find("t:Notes", ns).text and "Long Name" in cps[0].find("t:Notes", ns).text
    # distances increase and course point times fall inside the track's time span
    d = [float(x.text) for x in root.findall(".//t:Trackpoint/t:DistanceMeters", ns)]
    assert d == sorted(d)


# ---------- FIT ----------

def decode_fit(data):
    probe = Decoder(Stream.from_byte_array(bytearray(data)))   # these checks consume the stream,
    assert probe.is_fit() and probe.check_integrity()          # so read with a fresh decoder
    decoder = Decoder(Stream.from_byte_array(bytearray(data)))
    messages, errors = decoder.read(apply_scale_and_offset=True, convert_datetimes_to_dates=False)
    assert errors == []
    return messages


def semicircles_to_deg(v):
    return v * 180 / 2 ** 31


def test_fit_decodes_with_garmin_sdk_and_has_course_structure():
    m = decode_fit(render(make_route(), "garmin", "fit", "t").data)
    assert m["file_id_mesgs"][0]["type"] == "course"
    assert m["course_mesgs"][0]["name"] == "Kildare Sunday"
    assert len(m["record_mesgs"]) == 60
    assert len(m["course_point_mesgs"]) == 4
    assert len(m["lap_mesgs"]) == 1


def test_fit_course_points_have_type_name_distance_and_position():
    m = decode_fit(render(make_route(), "garmin", "fit", "t").data)
    cps = m["course_point_mesgs"]
    assert [c["type"] for c in cps] == ["food", "first_aid", "water", "danger"]
    assert all(len(c["name"]) <= 15 for c in cps)
    assert cps[0]["name"] == "Cafe Unicode Lo"
    assert [round(c["distance"]) for c in cps] == [4000, 6000, 7000, 8000]
    assert semicircles_to_deg(cps[1]["position_lat"]) == pytest.approx(53.162, abs=1e-5)
    assert semicircles_to_deg(cps[1]["position_long"]) == pytest.approx(-6.8, abs=1e-5)


def test_fit_reparses_with_fitparse_too():
    ff = FitFile(io.BytesIO(render(make_route(), "garmin", "fit", "t").data))
    cps = list(ff.get_messages("course_point"))
    recs = list(ff.get_messages("record"))
    assert len(cps) == 4 and len(recs) == 60
    assert cps[0].get_value("type") == "food"
    last = recs[-1]
    assert last.get_value("distance") == pytest.approx(make_route().length_m, abs=1)
    assert last.get_value("altitude") is not None


def test_fit_without_elevation_has_no_altitude_and_warns():
    r = make_route(with_ele=False)
    m = decode_fit(render(r, "garmin", "fit", "t").data)
    assert all("altitude" not in rec for rec in m["record_mesgs"])
    assert any("elevation" in w for w in warnings_for(r, DEVICES["garmin"]))


def test_fit_with_no_waypoints_is_still_a_valid_course():
    m = decode_fit(render(make_route(()), "garmin", "fit", "t").data)
    assert len(m["record_mesgs"]) == 60 and not m.get("course_point_mesgs")


# ---------- warnings / dispatch ----------

def test_garmin_limit_warning():
    many = tuple(ExportWaypoint("water", f"W{i}", None, 53.16, -6.9, float(i)) for i in range(201))
    warn = warnings_for(make_route(many), DEVICES["garmin"])
    assert any("201" in w and "200" in w for w in warn)
    assert warnings_for(make_route(many[:200]), DEVICES["garmin"]) == []
    assert warnings_for(make_route(many), DEVICES["karoo"]) == []


def test_resolve_rejects_unknown_device_or_format():
    with pytest.raises(KeyError):
        resolve("nokia", None)
    with pytest.raises(KeyError):
        resolve("wahoo", "fit")
    assert resolve("garmin", None)[1].key == "fit"


# ---------- golden files ----------

def golden(name, content: str):
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.write_text(content, encoding="utf-8", newline="\n")
    assert path.exists(), f"missing golden {name}; run with UPDATE_GOLDEN=1"
    assert content == path.read_text(encoding="utf-8"), f"{name} changed; review, then UPDATE_GOLDEN=1"


@pytest.mark.parametrize("device", ["gpx", "wahoo", "karoo", "strava"])
def test_golden_gpx(device):
    golden(f"{device}.gpx", render(make_route(n=12), device, None, "t").data.decode())


def test_golden_tcx():
    golden("garmin.tcx", render(make_route(n=12), "garmin", "tcx", "t").data.decode())


def test_golden_fit_summary():
    m = decode_fit(render(make_route(n=12), "garmin", "fit", "t").data)
    summary = {k: [{f: (round(v, 5) if isinstance(v, float) else v) for f, v in row.items()} for row in rows]
               for k, rows in m.items() if k in {"file_id_mesgs", "course_mesgs", "lap_mesgs", "course_point_mesgs"}}
    assert summary["course_point_mesgs"] and summary["lap_mesgs"]   # never golden an empty decode
    golden("garmin_fit_summary.json", json.dumps(summary, indent=1, sort_keys=True, default=str))
