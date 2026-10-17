"""Route names a leader can sort by: '063 km · Naas'.

The number comes first, zero padded, so an alphabetical list is a list by distance. The place is where the route turns
back (its furthest point from the start) named after the nearest town or village OpenStreetMap knows within a few
kilometres. We do not guess: when nothing sensible is near, the route keeps its own name and just gets the distance.
"""
import math
from concurrent.futures import ThreadPoolExecutor

from . import polyline

PLACE_RANK = {"city": 4, "town": 3, "village": 2, "hamlet": 1}
SEARCH_M = 3500
RANK_BONUS_M = 600           # a village 600 m further away still beats a hamlet; a town beats a village by 600 m


def _hav(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def farthest_point(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    """The point of the route furthest (as the crow flies) from where it starts: where a loop turns for home."""
    if len(points) < 2:
        return None
    start = points[0]
    return max(points, key=lambda p: _hav(start, p))


def place_query(lat: float, lon: float) -> str:
    return (f'[out:json][timeout:25];node(around:{SEARCH_M},{lat:.5f},{lon:.5f})["place"~"^(city|town|village|hamlet)$"]["name"];out;')


def pick_place(elements: list[dict], lat: float, lon: float) -> str | None:
    best, best_score = None, None
    for el in elements:
        tags = el.get("tags") or {}
        rank = PLACE_RANK.get(tags.get("place"))
        name = (tags.get("name:en") or tags.get("name") or "").strip()
        if not rank or not name or "lat" not in el:
            continue
        score = _hav((lat, lon), (el["lat"], el["lon"])) - rank * RANK_BONUS_M
        if best_score is None or score < best_score:
            best, best_score = name, score
    return best


def title_for(distance_m: float, place: str | None, fallback: str) -> str:
    km = int(round(distance_m / 1000.0))
    return f"{km:03d} km · {place or fallback}"


def suggest(distance_m: float, encoded_polyline: str, fallback: str, overpass) -> str:
    """The sortable name for one route. Never raises: any trouble leaves the route's own name after the distance."""
    try:
        pts = polyline.decode(encoded_polyline) if encoded_polyline else []
        far = farthest_point(pts)
        place = pick_place(overpass.run(place_query(*far)), *far) if far else None
    except Exception:           # Overpass busy or unreachable: a distance prefix is still worth having
        place = None
    return title_for(distance_m, place, fallback)


def suggest_many(items: list[tuple[float, str, str]], overpass, workers: int = 1) -> list[str]:
    """items: (distance_m, polyline, fallback name). Order is kept. Duplicates get ' (2)', ' (3)'... so names stay unique."""
    if workers > 1 and len(items) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            names = list(pool.map(lambda it: suggest(it[0], it[1], it[2], overpass), items))
    else:
        names = [suggest(d, p, f, overpass) for d, p, f in items]
    seen: dict[str, int] = {}
    out = []
    for n in names:
        seen[n] = seen.get(n, 0) + 1
        out.append(n if seen[n] == 1 else f"{n} ({seen[n]})")
    return out
