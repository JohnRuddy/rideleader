"""Outgoing email: sign-in links and deletion confirmations.

Without SMTP settings the ConsoleMailer just logs the message (development). Tests inject their own mailer."""
import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

from flask import current_app

log = logging.getLogger("rlk.mail")


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleMailer:
    def __init__(self):
        self.outbox: list[dict] = []

    def send(self, to, subject, body):
        self.outbox.append({"to": to, "subject": subject, "body": body})
        log.warning("MAIL to=%s subject=%r\n%s", to, subject, body)


class SmtpMailer:
    def __init__(self, host, port, user, password, from_addr, starttls=True):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.from_addr, self.starttls = from_addr, starttls

    def send(self, to, subject, body):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.from_addr, to, subject
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=20) as s:
            if self.starttls:
                s.starttls()
            if self.user:
                s.login(self.user, self.password)
            s.send_message(msg)


_console = ConsoleMailer()


def get_mailer() -> Mailer:
    cfg = current_app.config
    if cfg.get("MAILER"):
        return cfg["MAILER"]
    if cfg.get("SMTP_HOST"):
        return SmtpMailer(cfg["SMTP_HOST"], cfg["SMTP_PORT"], cfg.get("SMTP_USER"), cfg.get("SMTP_PASSWORD"),
                          cfg["MAIL_FROM"], cfg.get("SMTP_STARTTLS", True))
    return _console
