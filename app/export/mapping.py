"""The one mapping table: internal category -> FIT course point type -> GPX <sym> -> Karoo type -> TCX PointType.

Everything exporters emit for a waypoint's type comes from here (tests cover every row).

Sources / confidence:
- FIT names are members of the FIT SDK `course_point` enum (checked in tests against fit-tool). Newer values
  (SHELTER, MEETING_SPOT, OVERLOOK, TOILET, TRANSPORT, STORE, SERVICE) need recent device firmware; older units
  show them as generic points.
- TCX PointType is a closed schema enum (Generic, Summit, Valley, Water, Food, Danger, Left, Right, Straight,
  First Aid, 4th..1st/Hors Category, Sprint), so many categories collapse to Generic there.
- GPX <sym> uses Garmin-style symbol names; devices that do not know a name ignore it.
- Karoo values are the nine POI types reported by users to survive GPX import (Food, Water, Parking, Camping,
  Lodging, Geocache, Summit, Generic, Danger). Hammerhead's own docs do not confirm GPX POI import, so this
  needs a test on a real Karoo before we promise it.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class ExportType:
    fit: str       # fit_tool CoursePoint member name
    gpx_sym: str
    karoo: str
    tcx: str
    short: str     # short label used when a name has to be squeezed


KAROO_TYPES = {"Food", "Water", "Parking", "Camping", "Lodging", "Geocache", "Summit", "Generic", "Danger"}
TCX_TYPES = {"Generic", "Summit", "Valley", "Water", "Food", "Danger", "Left", "Right", "Straight", "First Aid",
             "4th Category", "3rd Category", "2nd Category", "1st Category", "Hors Category", "Sprint"}

EXPORT_TYPES: dict[str, ExportType] = {
    "hazard":    ExportType("DANGER",       "Danger Area",       "Danger",  "Danger",    "Hazard"),
    "aed":       ExportType("FIRST_AID",    "First Aid",         "Generic", "First Aid", "AED"),
    "cafe":      ExportType("FOOD",         "Restaurant",        "Food",    "Food",      "Cafe"),
    "water":     ExportType("WATER",        "Drinking Water",    "Water",   "Water",     "Water"),
    "toilets":   ExportType("TOILET",       "Restroom",          "Generic", "Generic",   "WC"),
    "bike":      ExportType("SERVICE",      "Bike Trail",        "Generic", "Generic",   "Bike shop"),
    "pharmacy":  ExportType("STORE",        "Medical Facility",  "Generic", "Generic",   "Pharmacy"),
    "fuel":      ExportType("STORE",        "Gas Station",       "Food",    "Food",      "Fuel"),
    "historic":  ExportType("OVERLOOK",     "Scenic Area",       "Generic", "Generic",   "History"),
    "shop":      ExportType("STORE",        "Convenience Store", "Food",    "Food",      "Shop"),
    "cue_left":  ExportType("LEFT",         "Left",              "Generic", "Generic",   "Left"),
    "cue_right": ExportType("RIGHT",        "Right",             "Generic", "Generic",   "Right"),
    "cue_straight": ExportType("STRAIGHT",  "Straight",          "Generic", "Generic",   "Straight"),
    "summit":    ExportType("SUMMIT",       "Summit",            "Summit",  "Summit",    "Summit"),
    "valley":    ExportType("VALLEY",       "Valley",            "Generic", "Valley",    "Valley"),
    "sprint":    ExportType("SPRINT",       "Sprint",            "Generic", "Sprint",    "Sprint"),
    "shelter":   ExportType("SHELTER",      "Picnic Area",       "Generic", "Generic",   "Shelter"),
    "bailout":   ExportType("TRANSPORT",    "Ground Transportation", "Generic", "Generic", "Station"),
    "viewpoint": ExportType("OVERLOOK",     "Scenic Area",       "Generic", "Generic",   "View"),
    "parking":   ExportType("MEETING_SPOT", "Parking Area",      "Parking", "Generic",   "Parking"),
    "worship":   ExportType("MEETING_SPOT", "Church",            "Generic", "Generic",   "Church"),
    # added now so steps 5+ (climbs, regroups) need no mapping changes
    "regroup":   ExportType("MEETING_SPOT", "Flag, Blue",        "Generic", "Generic",   "Regroup"),
    "climb":     ExportType("SUMMIT",       "Summit",            "Summit",  "Summit",    "Summit"),
    "generic":   ExportType("GENERIC",      "Flag, Blue",        "Generic", "Generic",   "Point"),
}


def export_type(kind: str) -> ExportType:
    return EXPORT_TYPES.get(kind, EXPORT_TYPES["generic"])
