"""Outgoing email: transport selection, retry, and what a visitor sees when mail cannot be sent."""
import smtplib

import pytest

from app.mailer import MailError, SmtpMailer
from app.models import LoginToken


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL and records what happened."""
    log: list = []
    fail_connects = 0
    login_error = None

    def __init__(self, host, port, timeout=None):
        if FakeSMTP.fail_connects > 0:
            FakeSMTP.fail_connects -= 1
            raise ConnectionRefusedError("down")
        self.kind = type(self).__name__
        FakeSMTP.log.append(("connect", self.kind, host, port))

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        FakeSMTP.log.append(("starttls",))

    def login(self, u, p):
        if FakeSMTP.login_error:
            raise FakeSMTP.login_error
        FakeSMTP.log.append(("login", u))

    def send_message(self, msg):
        FakeSMTP.log.append(("sent", msg["To"], msg["From"], msg["Subject"], bool(msg["Message-ID"])))


class FakeSMTP_SSL(FakeSMTP):
    pass


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.log, FakeSMTP.fail_connects, FakeSMTP.login_error = [], 0, None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP_SSL)


def mailer(port):
    return SmtpMailer("smtp.example.test", port, "user", "pw", "Ride Leader Kit <no-reply@cyclingtools.cc>")


def test_port_465_uses_implicit_tls_and_never_starttls():
    mailer(465).send("a@example.com", "Hi", "body")
    assert ("connect", "FakeSMTP_SSL", "smtp.example.test", 465) in FakeSMTP.log
    assert ("starttls",) not in FakeSMTP.log
    assert ("login", "user") in FakeSMTP.log
    assert ("sent", "a@example.com", "Ride Leader Kit <no-reply@cyclingtools.cc>", "Hi", True) in FakeSMTP.log


def test_other_ports_start_plain_and_upgrade_with_starttls():
    mailer(587).send("a@example.com", "Hi", "body")
    assert ("connect", "FakeSMTP", "smtp.example.test", 587) in FakeSMTP.log
    assert FakeSMTP.log.index(("starttls",)) < FakeSMTP.log.index(("login", "user"))


def test_one_transient_failure_is_retried_and_the_mail_still_goes():
    FakeSMTP.fail_connects = 1
    mailer(465).send("a@example.com", "Hi", "body")
    assert any(e[0] == "sent" for e in FakeSMTP.log)


def test_two_failures_raise_a_mail_error_that_names_the_failure_but_not_the_credentials():
    FakeSMTP.fail_connects = 2
    with pytest.raises(MailError) as e:
        mailer(465).send("a@example.com", "Hi", "body")
    assert "pw" not in str(e.value) and "ConnectionRefusedError" in str(e.value)


def test_bad_credentials_are_not_retried():
    FakeSMTP.login_error = smtplib.SMTPAuthenticationError(535, b"bad")
    with pytest.raises(MailError):
        mailer(465).send("a@example.com", "Hi", "body")
    assert [e for e in FakeSMTP.log if e[0] == "connect"] == [("connect", "FakeSMTP_SSL", "smtp.example.test", 465)]


def test_in_production_without_smtp_sign_in_says_so_instead_of_pretending_to_send(client):
    cfg = client.application.config
    cfg.update(RLK_ENV="production", SMTP_HOST="", MAILER=None)
    r = client.post("/login", data={"email": "a@example.com"})
    assert r.status_code == 503 and "set up on this server" in r.data.decode()
    assert LoginToken.query.count() == 0


def test_when_the_mail_server_fails_the_visitor_gets_a_retry_message_not_a_500(client):
    class Broken:
        def send(self, *a):
            raise MailError("ConnectionRefusedError")
    client.application.config["MAILER"] = Broken()
    r = client.post("/login", data={"email": "a@example.com"})
    assert r.status_code == 503 and "try again" in r.data.decode()


def test_mail_test_command_sends_through_the_configured_server(client):
    cfg = client.application.config
    cfg.update(MAILER=None, SMTP_HOST="smtp.example.test", SMTP_PORT=465, SMTP_USER="user", SMTP_PASSWORD="pw",
               MAIL_FROM="Ride Leader Kit <no-reply@cyclingtools.cc>")
    r = client.application.test_cli_runner().invoke(args=["mail-test", "me@example.com"])
    assert r.exit_code == 0 and "Sent to me@example.com" in r.output
    assert any(e[0] == "sent" and e[1] == "me@example.com" for e in FakeSMTP.log)


def test_mail_test_command_refuses_when_smtp_is_not_configured(client):
    client.application.config.update(MAILER=None, SMTP_HOST="")
    r = client.application.test_cli_runner().invoke(args=["mail-test", "me@example.com"])
    assert r.exit_code != 0 and "SMTP_HOST is not set" in r.output
