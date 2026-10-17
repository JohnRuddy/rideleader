"""FIT course file: file_id, course, lap, start event, records, course_points, stop event."""
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.course_message import CourseMessage
from fit_tool.profile.messages.course_point_message import CoursePointMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.profile_type import CoursePoint, Event, EventType, FileType, Manufacturer, Sport

from .data import SPEED_MPS, ExportRoute
from .mapping import export_type
from .names import short_name

FIT_NAME_MAX = 15


def _ms(dt) -> int:
    return round(dt.timestamp() * 1000)


def build_fit(route: ExportRoute) -> bytes:
    b = FitFileBuilder(auto_define=True, min_string_size=32)
    t0 = _ms(route.created)

    fid = FileIdMessage()
    fid.type = FileType.COURSE
    fid.manufacturer = Manufacturer.DEVELOPMENT.value
    fid.product = 0
    fid.time_created = t0
    fid.serial_number = 0x524C4B01
    b.add(fid)

    course = CourseMessage()
    course.sport = Sport.CYCLING
    course.course_name = short_name(route.title, FIT_NAME_MAX)
    b.add(course)

    first, last = route.points[0], route.points[-1]
    total_s = route.length_m / SPEED_MPS
    lap = LapMessage()
    lap.timestamp = _ms(route.time_at(route.length_m))
    lap.start_time = t0
    lap.total_elapsed_time = total_s
    lap.total_timer_time = total_s
    lap.total_distance = route.length_m
    lap.start_position_lat, lap.start_position_long = first.lat, first.lon
    lap.end_position_lat, lap.end_position_long = last.lat, last.lon
    b.add(lap)

    ev = EventMessage()
    ev.event, ev.event_type, ev.timestamp = Event.TIMER, EventType.START, t0
    b.add(ev)

    for p in route.points:
        r = RecordMessage()
        r.timestamp = _ms(route.time_at(p.dist_m))
        r.position_lat, r.position_long = p.lat, p.lon
        r.distance = p.dist_m
        if p.ele is not None:
            r.altitude = p.ele
        b.add(r)

    for w in route.waypoints:
        cp = CoursePointMessage()
        cp.timestamp = _ms(route.time_at(w.s_m))
        cp.position_lat, cp.position_long = w.lat, w.lon
        cp.distance = w.s_m
        cp.type = CoursePoint[export_type(w.kind).fit]
        cp.course_point_name = short_name(w.name, FIT_NAME_MAX)
        b.add(cp)

    stop = EventMessage()
    stop.event, stop.event_type, stop.timestamp = Event.TIMER, EventType.STOP_ALL, _ms(route.time_at(route.length_m))
    b.add(stop)
    return b.build().to_bytes()
