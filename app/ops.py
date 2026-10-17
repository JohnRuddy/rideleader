"""Operational endpoints: health for the load balancer, the deploy smoke test and the container healthcheck."""
import os

from flask import Blueprint, jsonify
from sqlalchemy import text

from . import db

bp = Blueprint("ops", __name__)


@bp.get("/healthz")
def healthz():
    """200 with the build id when the app can reach its database; 503 otherwise. Never exposes anything else."""
    build = (os.environ.get("APP_COMMIT") or "dev")[:7]
    try:
        db.session.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 (any failure means unhealthy)
        return jsonify(status="unavailable", build=build), 503
    return jsonify(status="ok", build=build)
