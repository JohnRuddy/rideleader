"""Production plumbing: health endpoint, no dev sign-in link in production, bootstrap sign-in command, proxy headers."""
import re

from tests.test_auth_security import outbox


def test_healthz_reports_ok_with_the_build_and_nothing_else(client, monkeypatch):
    monkeypatch.setenv("APP_COMMIT", "abcdef1234567890")
    r = client.get("/healthz")
    assert r.status_code == 200 and r.get_json() == {"status": "ok", "build": "abcdef1"}


def test_the_sign_in_link_is_never_shown_on_the_page_in_production(client):
    cfg = client.application.config
    cfg.update(DEV_SHOW_LOGIN_LINK=True, SMTP_HOST="")
    cfg["RLK_ENV"] = "dev"
    page = client.post("/login", data={"email": "a@example.com"}).data.decode()
    assert "/login/" in page                     # the development convenience still works in development
    cfg["RLK_ENV"] = "production"
    page = client.post("/login", data={"email": "b@example.com"}).data.decode()
    assert "/login/" not in page and "b@example.com" in page


def test_login_link_command_prints_a_working_one_time_link_and_the_first_person_in_is_admin(client):
    from app.models import User
    cfg = client.application.config
    cfg["APP_BASE_URL"] = "https://rideleader.example.test"
    runner = client.application.test_cli_runner()
    result = runner.invoke(args=["login-link", "Boss@Example.com"])
    assert result.exit_code == 0, result.output
    link = result.output.strip()
    assert link.startswith("https://rideleader.example.test/login/")
    path = link.split(".test", 1)[1]
    assert client.get(path).status_code == 200
    r = client.post(path)
    assert r.status_code == 302
    assert User.query.filter_by(email="boss@example.com").one().role == "admin"
    assert client.post(path).status_code == 400   # one time only
    assert runner.invoke(args=["login-link", "not-an-email"]).exit_code != 0
