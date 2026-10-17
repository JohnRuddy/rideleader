"""Pace groups and arrival times. Pure functions.

A "planned stop" is a waypoint the group actually stops at (usually the café). Every group dwells
`stop_min` minutes at each planned stop, so ETAs after a stop include the dwell.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Group:
    name: str
    speed_kmh: float
    stop_min: int


def seconds_to(s_m: float, group: Group, planned_stops_s: list[float]) -> float:
    """Elapsed seconds from the start to distance s_m, arriving (before any dwell at s_m itself)."""
    moving = s_m / (group.speed_kmh / 3.6)
    dwell = group.stop_min * 60 * sum(1 for s in planned_stops_s if s < s_m)
    return moving + dwell


def start_instant(day: date, at: time, tz: str) -> datetime:
    """Local ride start as a timezone-aware datetime (DST-correct)."""
    return datetime.combine(day, at, tzinfo=ZoneInfo(tz))


def eta(start: datetime, s_m: float, group: Group, planned_stops_s: list[float]) -> datetime:
    return start + timedelta(seconds=seconds_to(s_m, group, planned_stops_s))


def summary(start: datetime, length_m: float, group: Group, planned_stops_s: list[float]) -> dict:
    moving = length_m / (group.speed_kmh / 3.6)
    dwell = group.stop_min * 60 * len(planned_stops_s)
    finish = start + timedelta(seconds=moving + dwell)
    return {"name": group.name, "speed_kmh": group.speed_kmh, "stop_min": group.stop_min,
            "moving_s": round(moving), "stops_s": dwell, "total_s": round(moving + dwell),
            "finish": finish.isoformat(), "finish_local": finish.strftime("%H:%M")}


def fmt_duration(seconds: float) -> str:
    m = round(seconds / 60)
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"
