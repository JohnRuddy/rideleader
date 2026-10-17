"""ElevationProvider interface, an Open-Meteo implementation, and a helper that fills a whole track.

Used when a GPX has no elevation, or when an edit mixes legs with and without it. Elevation comes from
the DEM (90 m resolution), so sampling every ~200 m and interpolating loses nothing real.
"""
from typing import Protocol, Sequence

import numpy as np
import requests

SAMPLE_SPACING_M = 200.0


class ElevationError(RuntimeError):
    pass


class ElevationProvider(Protocol):
    def lookup(self, points: Sequence[tuple[float, float]]) -> list[float]:
        """Elevation in metres for each (lat, lon)."""


class OpenMeteoElevation:
    """https://open-meteo.com/en/docs/elevation-api (Copernicus 90 m DEM). Free for non-commercial use;
    point ELEVATION_URL at a self-hosted service for anything heavier."""

    BATCH = 100

    def __init__(self, url: str = "https://api.open-meteo.com/v1/elevation", timeout: float = 15, session=None):
        self.url, self.timeout, self.session = url, timeout, session or requests.Session()

    def lookup(self, points):
        out: list[float] = []
        for i in range(0, len(points), self.BATCH):
            chunk = points[i:i + self.BATCH]
            try:
                r = self.session.get(self.url, timeout=self.timeout, params={
                    "latitude": ",".join(f"{p[0]:.5f}" for p in chunk),
                    "longitude": ",".join(f"{p[1]:.5f}" for p in chunk)})
                r.raise_for_status()
                vals = r.json()["elevation"]
            except (requests.RequestException, KeyError, ValueError) as e:
                raise ElevationError(str(e)) from e
            if len(vals) != len(chunk) or any(v is None for v in vals):
                raise ElevationError("elevation service returned incomplete data")
            out += [float(v) for v in vals]
        return out


def fill_elevations(coords, dist_m, provider: ElevationProvider, spacing_m: float = SAMPLE_SPACING_M) -> list[float]:
    """Elevation for every coordinate: look up roughly every `spacing_m` metres of route and interpolate.

    coords: (lon, lat, *_) tuples; dist_m: cumulative distance for each. Raises ElevationError on failure.
    """
    d = np.asarray(dist_m, float)
    n_samples = max(2, int(d[-1] / spacing_m) + 1)
    targets = np.linspace(0, d[-1], n_samples)
    idx = sorted({int(np.searchsorted(d, t)) for t in targets} | {0, len(d) - 1})
    idx = [min(i, len(d) - 1) for i in idx]
    eles = provider.lookup([(coords[i][1], coords[i][0]) for i in idx])
    return [float(v) for v in np.interp(d, d[idx], eles)]
