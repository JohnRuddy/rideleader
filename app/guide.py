"""The plain-language guide to how the site works. Public, so people can read it before they have an account."""
from flask import Blueprint, current_app, render_template

from . import club

bp = Blueprint("guide", __name__)


@bp.get("/how-it-works")
def page():
    return render_template("guide.html", defaults=club.DEFAULT_SETTINGS, contact=current_app.config.get("CONTROLLER_CONTACT", ""))
