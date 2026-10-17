"""Background scan job. RQ runs run_scan_job in a worker; tests call execute_scan inline."""
from datetime import datetime, timedelta, timezone

from flask import current_app

from . import db
from .models import Route, ScanJob
from .overpass import get_client
from .poi import run_scan


def execute_scan(job_id: int, client=None) -> None:
    """Runs inside an app context."""
    job = db.session.get(ScanJob, job_id)
    route = db.session.get(Route, job.route_id)
    job.status, job.message = "running", "Starting"
    db.session.commit()

    def progress(frac, msg):
        job.progress, job.message = frac, msg
        db.session.commit()

    try:
        client = client or get_client(current_app.config)
        params = job.params or {}
        job.summary = run_scan(route, client, timedelta(days=current_app.config["POI_TTL_DAYS"]),
                               params.get("corridors"), params.get("categories"), progress)
        from . import hazard_service
        try:                                              # roads and hazards along the line: useful, never essential
            if hazard_service.scan_roads(route, client) is None:
                hazard_service.sync(route, route.road_summary)
            db.session.commit()
        except Exception:
            db.session.rollback()
            job = db.session.get(ScanJob, job_id)
        job.status, job.progress, job.message = "done", 1.0, "Done"
    except Exception as e:  # surfaced to the UI; the leader can retry
        db.session.rollback()
        job = db.session.get(ScanJob, job_id)
        job.status, job.message = "failed", f"Scan failed: {e}"[:300]
    job.finished_at = datetime.now(timezone.utc)
    db.session.commit()


def run_scan_job(job_id: int) -> None:
    """RQ entry point (dotted path app.jobs.run_scan_job)."""
    from . import create_app
    with create_app().app_context():
        execute_scan(job_id)


def enqueue_scan(job_id: int) -> None:
    if current_app.config.get("QUEUE_INLINE"):
        execute_scan(job_id, current_app.config.get("OVERPASS_CLIENT"))
        return
    from redis import Redis
    from rq import Queue
    Queue(connection=Redis.from_url(current_app.config["REDIS_URL"])).enqueue(
        "app.jobs.run_scan_job", job_id, job_timeout=900)


def run_strava_deauth(athlete_id: int) -> None:
    """RQ entry point: an athlete revoked access on Strava, so delete everything of theirs."""
    from . import create_app
    from . import strava_service as svc
    from .strava import get_client
    app = create_app()
    with app.app_context():
        svc.deauthorized_by_strava(athlete_id, get_client(app.config))


def enqueue_strava_deauth(athlete_id: int) -> None:
    if current_app.config.get("QUEUE_INLINE"):
        from . import strava_service as svc
        from .strava import get_client
        svc.deauthorized_by_strava(athlete_id, get_client(current_app.config))
        return
    from redis import Redis
    from rq import Queue
    Queue(connection=Redis.from_url(current_app.config["REDIS_URL"])).enqueue("app.jobs.run_strava_deauth", athlete_id)
