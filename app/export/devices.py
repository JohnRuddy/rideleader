"""Device picker definitions. Limits and advice are deliberately conservative and sourced; see notes."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Format:
    key: str
    label: str
    ext: str
    mime: str


FIT = Format("fit", "FIT course", "fit", "application/vnd.ant.fit")
TCX = Format("tcx", "TCX course", "tcx", "application/vnd.garmin.tcx+xml")
GPX = Format("gpx", "GPX", "gpx", "application/gpx+xml")


@dataclass(frozen=True)
class Device:
    key: str
    label: str
    formats: tuple[Format, ...]       # first is the default
    course_point_limit: int | None    # soft limit we warn about; None = none known
    limit_note: str
    advice: tuple[str, ...]


DEVICES: dict[str, Device] = {d.key: d for d in (
    Device("garmin", "Garmin Edge / watch", (FIT, TCX), 200,
           "Edge units are commonly reported to show at most 200 course points (varies by model and firmware; "
           "Garmin has removed the cap on some newer devices).",
           ("Copy the .fit file to the device's NewFiles folder over USB, or import it in Garmin Connect (Courses → Import).",
            "Course point names are shortened to 15 characters (10 in TCX) and accents are removed for display.",
            "Newer point types (shelter, toilet, overlook…) may appear as plain points on older firmware.")),
    Device("wahoo", "Wahoo ELEMNT", (GPX,), None,
           "No published limit.",
           ("Custom waypoints on the ELEMNT ACE, BOLT 3 and ROAM 3 (firmware 301724 or later) arrive through Strava, "
            "RideWithGPS or Komoot sync, so the most reliable route is to upload this GPX there and sync.",
            "Importing a plain GPX straight into the Wahoo app keeps the route, but waypoints may show as basic points "
            "or not at all, and the ELEMNT can treat waypoints far from the track as separate points.")),
    Device("karoo", "Hammerhead Karoo", (GPX,), None,
           "No published limit.",
           ("Waypoints use the nine POI types users report the Karoo accepts (Food, Water, Parking, Camping, Lodging, "
            "Geocache, Summit, Generic, Danger); anything else would be dropped silently.",
            "Hammerhead's own documentation does not confirm GPX POI import. Try a short route on your Karoo first.")),
    Device("gpx", "Other GPS (generic GPX)", (GPX,), None,
           "No published limit.",
           ("A standard GPX 1.1 file with the track and named waypoints with symbols.",)),
    Device("strava", "Strava route", (GPX,), None,
           "No published limit.",
           ("Strava has no way for apps to create routes, so this is a hand-off: download the file, then upload it on Strava.",)),
)}

DEFAULT_DEVICE = "gpx"
STRAVA_NEW_ROUTE_URL = "https://www.strava.com/routes/new"
