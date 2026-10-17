"""Export dispatch: (device, format) -> bytes, plus device-limit warnings."""
from dataclasses import dataclass

from .data import ExportRoute, from_route  # noqa: F401  (re-exported)
from .devices import DEFAULT_DEVICE, DEVICES, Device, Format  # noqa: F401
from .fitout import build_fit
from .gpxout import build_gpx
from .tcxout import build_tcx


@dataclass(frozen=True)
class Rendered:
    data: bytes
    fmt: Format
    filename: str


def resolve(device_key: str, fmt_key: str | None) -> tuple[Device, Format]:
    dev = DEVICES.get(device_key)
    if dev is None:
        raise KeyError(f"unknown device {device_key!r}")
    if fmt_key is None:
        return dev, dev.formats[0]
    fmt = next((f for f in dev.formats if f.key == fmt_key), None)
    if fmt is None:
        raise KeyError(f"{dev.label} does not use {fmt_key!r}")
    return dev, fmt


def render(route: ExportRoute, device_key: str, fmt_key: str | None, slug: str) -> Rendered:
    dev, fmt = resolve(device_key, fmt_key)
    if fmt.key == "fit":
        data = build_fit(route)
    elif fmt.key == "tcx":
        data = build_tcx(route)
    else:
        variant = dev.key if dev.key in {"wahoo", "karoo", "strava"} else "generic"
        data = build_gpx(route, variant).encode("utf-8")
    return Rendered(data, fmt, f"{slug}-{dev.key}.{fmt.ext}")


def warnings_for(route: ExportRoute, device: Device) -> list[str]:
    out = []
    n = len(route.waypoints)
    if device.course_point_limit and n > device.course_point_limit:
        out.append(f"{n} waypoints selected, but {device.label} units are commonly limited to "
                   f"{device.course_point_limit} course points. Trim some (the Max and One per km actions help) "
                   f"or the extras may not show. {device.limit_note}")
    if not route.has_elevation:
        out.append("This route has no elevation data, so the exported file won't have an elevation profile.")
    return out
