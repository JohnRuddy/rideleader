"""Scheduled Strava housekeeping: `python -m app.scheduler` (the `scheduler` service in docker-compose).

- every 10 minutes: poll Met Éireann's warnings (the brief asks for 10-15) and drop expired ones;
- every 10 minutes: delete Strava data that has reached its 7 days;
- every 6 hours: ask Strava whether each cached route still exists (webhooks don't cover routes) and remove local
  copies of ones deleted on Strava. Routes are re-checked when more than 20 hours old, so a deletion on Strava is
  reflected here within about a day: inside the 48 hours the brief requires.
"""
import logging
import time
from datetime import datetime, timedelta, timezone

from . import strava_service as svc
from . import warnings_feed
from .strava import get_client

log = logging.getLogger("rlk.scheduler")
PURGE_EVERY = timedelta(minutes=10)
REVALIDATE_EVERY = timedelta(hours=6)


def tick(app, now: datetime | None = None, last_revalidate: datetime | None = None) -> dict:
    """One scheduler pass. Returns what was done (and when revalidation last ran) so callers/tests can see it."""
    now = now or datetime.now(timezone.utc)
    with app.app_context():
        out = {"purged": svc.purge_expired(now), "revalidated": None, "last_revalidate": last_revalidate,
               "warnings": warnings_feed.poll(warnings_feed.get_provider(app.config), now)}
        client = get_client(app.config)
        if client is not None and (last_revalidate is None or now - last_revalidate >= REVALIDATE_EVERY):
            out["revalidated"] = svc.revalidate(client, now)
            out["last_revalidate"] = now
        return out


def main() -> None:
    from . import create_app
    logging.basicConfig(level=logging.INFO)
    app = create_app()
    last = None
    while True:
        try:
            res = tick(app, last_revalidate=last)
            last = res["last_revalidate"]
            log.info("scheduler pass: %s", res)
        except Exception:
            log.exception("scheduler pass failed; will retry")
        time.sleep(PURGE_EVERY.total_seconds())


if __name__ == "__main__":
    main()
