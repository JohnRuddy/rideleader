import math
import os

import pytest

from app import create_app, db
from app.elevation import ElevationError
from app.routing import RoutingError


class OfflineTiles:
    """Default in tests: map tiles are never fetched from the network. A test that needs tiles passes its own fake."""
    def fetch(self, z, x, y):
        return None


class OfflineRouter:
    """Default in tests: any test that routes must pass its own fake, so nothing reaches the network."""
    def route(self, points, options, avoid=None):
        raise RoutingError("routing is offline in tests")


class CaptureMailer:
    def __init__(self):
        self.outbox = []

    def send(self, to, subject, body):
        self.outbox.append({"to": to, "subject": subject, "body": body})


class OfflineElevation:
    def lookup(self, points):
        raise ElevationError("elevation is offline in tests")


def make_gpx(points, waypoints=(), name="Test spin"):
    """points: (lat, lon, ele|None). waypoints: (name, lat, lon)."""
    trkpts = "".join(
        f'<trkpt lat="{la}" lon="{lo}">' + (f"<ele>{e}</ele>" if e is not None else "") + "</trkpt>"
        for la, lo, e in points
    )
    wpts = "".join(f'<wpt lat="{la}" lon="{lo}"><name>{n}</name></wpt>' for n, la, lo in waypoints)
    return (f'<?xml version="1.0"?><gpx version="1.1" creator="t" xmlns="http://www.topografix.com/GPX/1/1">'
            f"{wpts}<trk><name>{name}</name><trkseg>{trkpts}</trkseg></trk></gpx>").encode()


def hilly_points(n=200, with_ele=True):
    """~20 km west->east line near Kildare with a 100 m bump in the middle."""
    pts = []
    for i in range(n):
        lon = -6.9 + i * 0.0015
        ele = 100 + 100 * math.sin(math.pi * i / (n - 1)) if with_ele else None
        pts.append((53.16, lon, ele))
    return pts


@pytest.fixture
def client():
    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": os.environ.get(
        "TEST_DATABASE_URL", os.environ.get("DATABASE_URL")),
        "ROUTING_CLIENT": OfflineRouter(), "ELEVATION_CLIENT": OfflineElevation(), "TILE_CLIENT": OfflineTiles(),
        "CSRF_ENABLED": False, "RATELIMIT_ENABLED": False, "STRAVA_MODE": "off", "STRAVA_CLIENT": None, "MAILER": CaptureMailer(), "SECRET_KEY": "test-secret-key", "INVITE_REQUIRED": False})
    with app.app_context():
        yield app.test_client()
        db.session.rollback()
        db.drop_all()
