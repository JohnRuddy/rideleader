"""Sunrise and sunset from the NOAA / "sunrise equation" formulas. Accurate to about a minute at Irish latitudes,
which is what a 'lights needed' warning wants. Deterministic, no network."""
import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

J2000 = 2451545.0
SUN_ALTITUDE = -0.833      # degrees: the sun's upper edge on the horizon, allowing for refraction


def _from_jd(jd: float) -> datetime:
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(days=jd - 2440587.5)


def sun_times(day: date, lat: float, lon: float, tz: str = "Europe/Dublin") -> tuple[datetime, datetime] | None:
    """(sunrise, sunset) as aware datetimes in `tz` for the local date, or None if the sun doesn't rise or set that day."""
    # whole days since 2000-01-01 12:00 UTC at the local date's noon-ish
    n = (datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc) - datetime(2000, 1, 1, 12, tzinfo=timezone.utc)).days
    j_star = n - lon / 360.0                                        # mean solar noon (days since J2000)
    m = math.radians((357.5291 + 0.98560028 * j_star) % 360)         # solar mean anomaly
    c = 1.9148 * math.sin(m) + 0.0200 * math.sin(2 * m) + 0.0003 * math.sin(3 * m)
    lam = math.radians((math.degrees(m) + c + 180 + 102.9372) % 360)  # ecliptic longitude
    j_transit = J2000 + j_star + 0.0053 * math.sin(m) - 0.0069 * math.sin(2 * lam)
    sin_dec = math.sin(lam) * math.sin(math.radians(23.4397))
    cos_dec = math.sqrt(1 - sin_dec ** 2)
    phi = math.radians(lat)
    cos_h = (math.sin(math.radians(SUN_ALTITUDE)) - math.sin(phi) * sin_dec) / (math.cos(phi) * cos_dec)
    if not -1 <= cos_h <= 1:
        return None
    h = math.degrees(math.acos(cos_h)) / 360.0
    zone = ZoneInfo(tz)
    return _from_jd(j_transit - h).astimezone(zone), _from_jd(j_transit + h).astimezone(zone)
