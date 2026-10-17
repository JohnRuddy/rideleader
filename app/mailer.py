"""Outgoing email: sign-in links and deletion confirmations.

Without SMTP settings the ConsoleMailer just logs the message (development). Tests inject their own mailer."""
import logging
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
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


class MailError(Exception):
    """The message could not be handed to the mail server. Carries no credentials."""


class SmtpMailer:
    """Port 465 is implicit TLS (SMTPS); any other port starts plain and upgrades with STARTTLS."""

    def __init__(self, host, port, user, password, from_addr, starttls=True):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.from_addr, self.starttls = from_addr, starttls

    def _connect(self):
        if self.port == 465:
            return smtplib.SMTP_SSL(self.host, self.port, timeout=20)
        return smtplib.SMTP(self.host, self.port, timeout=20)

    def send(self, to, subject, body):
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.from_addr, to, subject
        msg["Message-ID"] = make_msgid(domain=parseaddr(self.from_addr)[1].rpartition("@")[2] or None)
        msg.set_content(body)
        last = None
        for attempt in (1, 2):      # one retry rides out a transient network or provider blip
            try:
                with self._connect() as s:
                    if self.starttls and self.port != 465:
                        s.starttls()
                    if self.user:
                        s.login(self.user, self.password)
                    s.send_message(msg)
                return
            except (smtplib.SMTPException, OSError) as e:
                last = e
                log.warning("SMTP send attempt %d to %s via %s:%s failed: %s", attempt, to, self.host, self.port,
                            type(e).__name__)
                if isinstance(e, (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused)):
                    break           # retrying cannot help
        raise MailError(f"{type(last).__name__}") from last


_console = ConsoleMailer()


def get_mailer() -> Mailer:
    cfg = current_app.config
    if cfg.get("MAILER"):
        return cfg["MAILER"]
    if cfg.get("SMTP_HOST"):
        return SmtpMailer(cfg["SMTP_HOST"], cfg["SMTP_PORT"], cfg.get("SMTP_USER"), cfg.get("SMTP_PASSWORD"),
                          cfg["MAIL_FROM"], cfg.get("SMTP_STARTTLS", True))
    return _console


def mail_is_real() -> bool:
    """True when messages really leave the building (an SMTP host is set, or a test injected a mailer)."""
    cfg = current_app.config
    return bool(cfg.get("MAILER") or cfg.get("SMTP_HOST"))
