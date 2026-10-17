"""POI categories: the single place that defines OSM tag rules, default corridors and colours.

Export mappings (FIT / GPX sym / Karoo) will hang off `key` in step 3.
"""
import re
from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class Rule:
    k: str
    v: str | None = None          # None = any value
    and_k: str | None = None      # optional second tag that must match and_re
    and_re: str | None = None

    def matches(self, tags: dict) -> bool:
        if self.k not in tags or (self.v is not None and tags[self.k] != self.v):
            return False
        if self.and_k:
            return bool(re.search(self.and_re, tags.get(self.and_k, "")))
        return True

    def overpass(self) -> str:
        s = f'["{self.k}"="{self.v}"]' if self.v is not None else f'["{self.k}"]'
        if self.and_k:
            s += f'["{self.and_k}"~"{self.and_re}"]'
        return s


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    corridor_m: int
    color: str
    rules: tuple[Rule, ...]
    exclude: Callable[[dict], bool] = field(default=lambda tags: False)
    always_list: bool = False     # AEDs: listed whenever within corridor, never filtered out of the candidate list
    scan: bool = True             # False for categories we generate ourselves (regroups)
    listed: bool = True           # False: still scanned (regroup proposals use them) but never shown or exported as a stop

    def matches(self, tags: dict) -> bool:
        return any(r.matches(tags) for r in self.rules) and not self.exclude(tags)


_COFFEE_CUISINE = "coffee|cafe|tea|sandwich|bakery|breakfast|brunch"

CATEGORIES: tuple[Category, ...] = (
    Category("hazard", "Hazards", 30, "#ff3b30", (
        Rule("barrier", "cattle_grid"), Rule("ford", "yes"), Rule("railway", "level_crossing"))),
    Category("aed", "Defibrillators (AED)", 1000, "#d70015", (Rule("emergency", "defibrillator"),), always_list=True),
    Category("cafe", "Cafés", 500, "#a2845e", (
        Rule("amenity", "cafe"), Rule("shop", "bakery"), Rule("cuisine", "coffee_shop"),
        Rule("amenity", "restaurant", "cuisine", _COFFEE_CUISINE))),
    Category("water", "Drinking water", 250, "#32ade6", (
        Rule("amenity", "drinking_water"), Rule("drinking_water", "yes"))),
    Category("toilets", "Toilets", 300, "#5e5ce6", (Rule("amenity", "toilets"),)),
    Category("bike", "Bike shops & repair", 1000, "#30b0c7", (
        Rule("shop", "bicycle"), Rule("amenity", "bicycle_repair_station"), Rule("service:bicycle:repair", "yes"))),
    Category("fuel", "Service stations", 300, "#ff9f0a", (Rule("amenity", "fuel"),)),
    Category("shop", "Shops", 300, "#ff9f0a", (Rule("shop", "convenience"), Rule("shop", "supermarket")), listed=False),
    Category("shelter", "Shelter", 200, "#8e8e93", (Rule("amenity", "shelter"), Rule("shelter", "yes"))),
    Category("historic", "Historic sites", 300, "#ff2d55", (Rule("historic"),),
             exclude=lambda t: t.get("historic") in {"boundary_stone", "milestone", "yes"} and "tourism" not in t),
    # Shops, churches and car parks are scanned only so regroup proposals have somewhere sensible to stop; leaders never see them.
    Category("worship", "Churches (forecourts)", 150, "#a2845e", (Rule("amenity", "place_of_worship"),), listed=False),
    Category("parking", "Meeting / car parks", 300, "#636366", (Rule("amenity", "parking"),), listed=False,
             exclude=lambda t: t.get("access") in {"private", "customers", "permit"}
             or t.get("parking") in {"street_side", "lane"}),
)

# Cue types a leader places by hand (right-click the map). Danger, water, food and first aid reuse the scanned
# categories above so they export and group the same way; these are the extra ones Strava and head units understand.
CUES = (
    Category("cue_left", "Turn left", 0, "#1d6fb8", (), scan=False),
    Category("cue_right", "Turn right", 0, "#1d6fb8", (), scan=False),
    Category("cue_straight", "Straight on", 0, "#1d6fb8", (), scan=False),
    Category("summit", "Summit", 0, "#7a4b2a", (), scan=False),
    Category("valley", "Valley", 0, "#3c7a6e", (), scan=False),
    Category("sprint", "Sprint", 0, "#b0371f", (), scan=False),
)
CATEGORIES = CATEGORIES + CUES + (
    Category("regroup", "Regroups", 0, "#0a84ff", (), scan=False),
)
BY_KEY = {c.key: c for c in CATEGORIES}
SCANNABLE = {c.key for c in CATEGORIES if c.scan}
GENERIC = Category("generic", "Other", 0, "#8e8e93", ())
DEFAULT_CORRIDORS = {c.key: c.corridor_m for c in CATEGORIES if c.scan}


def classify(tags: dict) -> str | None:
    """First matching category in priority order (a tag set maps to one category)."""
    for c in CATEGORIES:
        if c.matches(tags):
            return c.key
    return None


def label_for(key: str) -> str:
    return BY_KEY.get(key, GENERIC).label


def color_for(key: str) -> str:
    return BY_KEY.get(key, GENERIC).color


# Kinds a leader never sees: the unlisted helpers above, plus kinds from earlier versions that are no longer offered.
HIDDEN = {c.key for c in CATEGORIES if not c.listed} | {"pharmacy", "bailout", "viewpoint"}


def shown(waypoints):
    return [w for w in waypoints if w.kind not in HIDDEN]


def public_json() -> list[dict]:
    return [{"key": c.key, "label": c.label, "corridor_m": c.corridor_m, "color": c.color, "scan": c.scan}
            for c in CATEGORIES if c.listed]
