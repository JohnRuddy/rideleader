import os

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text

db = SQLAlchemy()

# Until Alembic arrives: additive, idempotent column upgrades for existing dev databases.
_ADDED_COLUMNS = [
    "ALTER TABLE strava_route_cache ADD COLUMN IF NOT EXISTS tidy_name varchar(200)",
    "ALTER TABLE waypoint ADD COLUMN IF NOT EXISTS side varchar(1)",
    "ALTER TABLE waypoint ADD COLUMN IF NOT EXISTS osm_ref varchar(40)",
    "ALTER TABLE waypoint ADD COLUMN IF NOT EXISTS planned_stop boolean NOT NULL DEFAULT false",
    "ALTER TABLE waypoint ADD COLUMN IF NOT EXISTS club_hours varchar(300)",
    "ALTER TABLE waypoint ADD COLUMN IF NOT EXISTS club_hours_at timestamptz",
    "ALTER TABLE waypoint ADD COLUMN IF NOT EXISTS osm_hours_changed_at timestamptz",
    "ALTER TABLE route ADD COLUMN IF NOT EXISTS climb_settings jsonb",
    "ALTER TABLE route ADD COLUMN IF NOT EXISTS climbs_version integer",
    "ALTER TABLE route ADD COLUMN IF NOT EXISTS editor jsonb",
    "ALTER TABLE route ADD COLUMN IF NOT EXISTS state_seq integer",
    "ALTER TABLE route ADD COLUMN IF NOT EXISTS owner_id integer REFERENCES app_user(id) ON DELETE CASCADE",
    "ALTER TABLE route ADD COLUMN IF NOT EXISTS visibility varchar(10) NOT NULL DEFAULT 'club'",
    "ALTER TABLE route ADD COLUMN IF NOT EXISTS strava_route_id bigint",
    "ALTER TABLE route ADD COLUMN IF NOT EXISTS strava_expires_at timestamptz",
    "ALTER TABLE app_user ADD COLUMN IF NOT EXISTS role varchar(10) NOT NULL DEFAULT 'leader'",
    "ALTER TABLE ride ADD COLUMN IF NOT EXISTS meeting_name varchar(120)",
    "ALTER TABLE ride ADD COLUMN IF NOT EXISTS meeting_lat double precision",
    "ALTER TABLE ride ADD COLUMN IF NOT EXISTS meeting_lon double precision",
    "ALTER TABLE ride ADD COLUMN IF NOT EXISTS leader_phone varchar(30)",
    "ALTER TABLE ride ADD COLUMN IF NOT EXISTS notes text",
    "ALTER TABLE ride ADD COLUMN IF NOT EXISTS share_token varchar(40)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_ride_share_token ON ride (share_token)",
    "ALTER TABLE route ADD COLUMN IF NOT EXISTS road_summary jsonb",
    "ALTER TABLE waypoint ADD COLUMN IF NOT EXISTS club_stop_id integer REFERENCES club_stop(id) ON DELETE SET NULL",
]


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SQLALCHEMY_DATABASE_URI=os.environ.get("DATABASE_URL", "postgresql+psycopg://rlk:rlk_dev_only@db:5432/rlk"),
        MAX_CONTENT_LENGTH=20 * 1024 * 1024,
        TILE_URL=os.environ.get("TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png"),
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        REDIS_URL=os.environ.get("REDIS_URL", "redis://redis:6379/0"),
        OVERPASS_URL=os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
        OVERPASS_MIN_INTERVAL=float(os.environ.get("OVERPASS_MIN_INTERVAL", "1.0")),
        OVERPASS_WORKERS=int(os.environ.get("OVERPASS_WORKERS", "1")),      # parallel tile fetches; >1 only for your own server
        HOURS_URL=os.environ.get("HOURS_URL", "http://hours:3000"),
        BROUTER_URL=os.environ.get("BROUTER_URL", "https://brouter.de/brouter"),
        BROUTER_MIN_INTERVAL=float(os.environ.get("BROUTER_MIN_INTERVAL", "1.0")),
        ELEVATION_URL=os.environ.get("ELEVATION_URL", "https://api.open-meteo.com/v1/elevation"),
        POI_TTL_DAYS=int(os.environ.get("POI_TTL_DAYS", "7")),
        QUEUE_INLINE=False,
        RLK_ENV=os.environ.get("RLK_ENV", "dev"),                        # set to 'production' to enforce real secrets
        APP_BASE_URL=os.environ.get("APP_BASE_URL", ""),
        TOKEN_ENCRYPTION_KEY=os.environ.get("TOKEN_ENCRYPTION_KEY", ""),
        CSRF_ENABLED=True, RATELIMIT_ENABLED=True,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
        SMTP_HOST=os.environ.get("SMTP_HOST", ""), SMTP_PORT=int(os.environ.get("SMTP_PORT", "587")),
        SMTP_USER=os.environ.get("SMTP_USER", ""), SMTP_PASSWORD=os.environ.get("SMTP_PASSWORD", ""),
        MAIL_FROM=os.environ.get("MAIL_FROM", "Ride Leader Kit <no-reply@localhost>"),
        DEV_SHOW_LOGIN_LINK=os.environ.get("DEV_SHOW_LOGIN_LINK", "1") == "1",
        CONTROLLER_NAME=os.environ.get("CONTROLLER_NAME", ""), CONTROLLER_CONTACT=os.environ.get("CONTROLLER_CONTACT", ""),
        STRAVA_MODE=os.environ.get("STRAVA_MODE", "off"),                # off | mock | live
        STRAVA_CLIENT_ID=os.environ.get("STRAVA_CLIENT_ID", ""), STRAVA_CLIENT_SECRET=os.environ.get("STRAVA_CLIENT_SECRET", ""),
        STRAVA_VERIFY_TOKEN=os.environ.get("STRAVA_VERIFY_TOKEN", ""),
        STRAVA_SUBSCRIPTION_ID=os.environ.get("STRAVA_SUBSCRIPTION_ID", ""),
    )
    if config:
        app.config.update(config)
    if app.config["RLK_ENV"] == "production" and not app.config.get("TESTING"):
        missing = [k for k in ("SECRET_KEY", "TOKEN_ENCRYPTION_KEY") if not os.environ.get(k)]
        if missing or app.config["SESSION_COOKIE_SECURE"] is not True:
            raise RuntimeError("Production needs SECRET_KEY, TOKEN_ENCRYPTION_KEY and SESSION_COOKIE_SECURE=1 "
                               f"(missing: {', '.join(missing) or 'secure cookies'}).")
    if os.environ.get("TRUST_PROXY") == "1":
        # Behind Caddy: believe one hop of X-Forwarded-For/Proto/Host so https URLs and client IPs are right.
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    db.init_app(app)
    from . import ops
    app.register_blueprint(ops.bp)
    from . import views

    app.register_blueprint(views.bp)
    from . import edit_views

    app.register_blueprint(edit_views.bp)
    from . import account, auth, club_views, conditions_views, security, share_views, strava_views

    security.init_app(app)
    for module in (auth, account, strava_views, conditions_views, club_views, share_views):
        app.register_blueprint(module.bp)

    import click

    @app.cli.command("login-link")
    @click.argument("email")
    def login_link(email):
        """Print a one-time sign-in link for EMAIL. For bootstrapping the first admin before email is configured:
        it needs a shell on the server, which is the same trust as the database."""
        from .auth import issue_login_link
        with app.test_request_context("/"):
            click.echo(issue_login_link(email))

    @app.cli.command("strava-subscribe")
    @click.argument("callback_url")
    def strava_subscribe(callback_url):
        """Create the app's one Strava webhook subscription (run once, after the site is reachable at CALLBACK_URL)."""
        from .strava import get_client
        client = get_client(app.config)
        if client is None or not app.config["STRAVA_VERIFY_TOKEN"]:
            raise click.ClickException("Set STRAVA_MODE=live, STRAVA_CLIENT_ID/SECRET and STRAVA_VERIFY_TOKEN first.")
        sub_id = client.create_subscription(callback_url, app.config["STRAVA_VERIFY_TOKEN"])
        click.echo(f"Subscription created: id {sub_id}. Now set STRAVA_SUBSCRIPTION_ID={sub_id} and restart.")
    with app.app_context():
        # web, worker and scheduler start together and all run this: take a database-wide lock (held on its own
        # connection) so they set up the schema one at a time instead of deadlocking on each other's ALTERs.
        with db.engine.connect() as lock:
            lock.execute(text("SELECT pg_advisory_lock(7272740001)"))
            try:
                db.session.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
                db.session.commit()
                db.create_all()
                for stmt in _ADDED_COLUMNS:
                    db.session.execute(text(stmt))
                db.session.commit()
                from . import counties
                counties.ensure_loaded()
            finally:
                lock.execute(text("SELECT pg_advisory_unlock(7272740001)"))
    return app
