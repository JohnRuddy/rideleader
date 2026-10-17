"""A route's ride plan (date, start, pace groups, planned stops) turned into what the calculations need.

Shared by the plan page endpoints and the ride sheet, so both always answer for the same plan and the same clock."""
from datetime import date, datetime, time, timedelta, timezone

from flask import current_app

from . import along_route as ar
from . import club as clubmod
from . import counties, db, pace
from . import warnings_feed as wf
from . import weather
from .models import PaceGroup, Ride, Route
from .routemodel import route_arrays

DEFAULT_GROUPS = (("A", 28.0, 20), ("B", 24.0, 20), ("C", 20.0, 20))


def clock_now() -> datetime:
    clock = current_app.config.get("CLOCK")
    return clock() if clock else datetime.now(timezone.utc)


def next_sunday(today: date) -> date:
    return today + timedelta(days=(6 - today.weekday()) % 7 or 7)


def get_or_create_ride(route: Route) -> Ride:
    if route.ride is None:
        route.ride = Ride(date=next_sunday(date.today()), start_time=time(9, 0), tz="Europe/Dublin",
                          groups=[PaceGroup(position=i, name=n, speed_kmh=v, stop_min=m)
                                  for i, (n, v, m) in enumerate(DEFAULT_GROUPS)])
        db.session.commit()
    return route.ride


def planned_s(route: Route) -> list[float]:
    return sorted(w.s_m for w in route.waypoints if w.planned_stop)


class Plan:
    def __init__(self, route: Route):
        from zoneinfo import ZoneInfo  # noqa: F401  (kept local: only the tz name is stored on the ride)
        self.route = route
        self.ride = get_or_create_ride(route)
        self.tz = self.ride.tz
        self.start = pace.start_instant(self.ride.date, self.ride.start_time, self.tz)
        self.groups = [pace.Group(g.name, g.speed_kmh, g.stop_min) for g in self.ride.groups]
        self.planned = planned_s(route)
        self.lats, self.lons = route_arrays(route)
        self.line = ar.Line(self.lats, self.lons, route.dist_m)
        self.summaries = [pace.summary(self.start, route.length_m, g, self.planned) for g in self.groups]
        self.finish = max(datetime.fromisoformat(s["finish"]) for s in self.summaries)
        self.settings = clubmod.settings()

    def counties(self):
        return counties.route_counties(self.lats, self.lons, self.route.dist_m)

    def summits(self) -> list[float]:
        return [c.s_end_m for c in self.route.climbs]

    def forecast(self, lat, lon):
        return weather.get_forecast(lat, lon, current_app.config, clock_now())

    def daylight(self):
        lat, lon = self.line.point(0)
        return ar.daylight(self.ride.date, lat, lon, {s["name"]: datetime.fromisoformat(s["finish"]) for s in self.summaries},
                           self.start, self.settings["lights_min_before_sunset"], self.tz)

    def banner(self, start=None, end=None) -> dict:
        now = clock_now()
        wf.ensure_fresh(wf.get_provider(current_app.config), now)
        return wf.banner(self.counties(), start or self.start, end or self.finish, self.settings["warning_buffer_h"], now)

    def weather(self) -> dict:
        day = self.daylight()
        res = ar.weather_along(self.line, self.start, self.groups, self.planned, self.summits(), self.forecast,
                               self.settings, clock_now(), self.tz, day["lights_needed"])
        res["daylight"] = day
        return res
