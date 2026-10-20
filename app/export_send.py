"""Getting a finished route onto a phone, and from there onto a bike computer.

Two small pieces:

* a short-lived signed link (`/send/<token>`) for one route, one device and one file type, so a leader who finished a route on a
  laptop can scan a QR code with their phone and open the file straight in the Wahoo (or other) app. The link needs no sign-in,
  stops working after 20 minutes, and can't be changed to fetch a different route (the route is inside the signed token).
* the QR code itself, drawn inline as an SVG (no image request, no script, nothing sent to a third party).
"""
from __future__ import annotations

import re

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from markupsafe import Markup

TTL = 20 * 60
SALT = "rlk-send-route-v1"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=SALT)


def make_token(route_id: int, device_key: str, fmt_key: str | None) -> str:
    return _serializer().dumps([int(route_id), device_key, fmt_key])


def read_token(token: str):
    """(route_id, device_key, fmt_key) from a good token; 'expired' for an old one; None for anything else."""
    try:
        route_id, device_key, fmt_key = _serializer().loads(token, max_age=TTL)
        return int(route_id), str(device_key), (str(fmt_key) if fmt_key else None)
    except SignatureExpired:
        return "expired"
    except (BadSignature, ValueError, TypeError):
        return None


def qr_svg(url: str, label: str = "QR code") -> Markup:
    """The link as a QR code: an inline SVG with a text alternative, in the app's own ink colour."""
    import segno
    code = segno.make(url, error="m")
    svg = code.svg_inline(scale=5, border=2, dark="#101418", light="#ffffff", title=label, desc=url)
    return Markup(svg)


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:60] or "route"
