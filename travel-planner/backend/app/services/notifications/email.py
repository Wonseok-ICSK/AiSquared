"""E-mail channel: SMTP with STARTTLS (port 465 = implicit TLS) via ``smtplib`` (DESIGN §13.3).

Messages are ``multipart/alternative`` (plain text + simple HTML); ``EmailMessage`` encodes
Korean headers (RFC 2047) automatically. Both presets need an *app password*:

* Gmail - 2-Step Verification, then https://myaccount.google.com/apppasswords
  (not available for security-key-only 2SV, Advanced Protection or work/school accounts;
  all app passwords are revoked when the Google password changes).
* Naver - 2단계 인증 + 애플리케이션 비밀번호 (required for SMTP since 2025), and
  메일 > 환경설정 > POP3/IMAP 설정 > IMAP/SMTP 사용 = 사용함. The From address must be the
  account's own address.

The SMTP password is never logged or included in error messages.
"""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from app.services.events import NotificationMessage
from app.services.notifications.base import (
    ChannelError,
    NotificationChannel,
    full_text,
    is_http_url,
    register_channel,
    secret_value,
    subject_line,
)
from app.settings_store import EmailNotify, RuntimeSettings

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_S = 20.0
IMPLICIT_TLS_PORT = 465
SENDER_NAME = "AiSquared Travel"


@dataclass(frozen=True)
class SmtpPreset:
    """Known SMTP server settings plus setup notes for the Settings page."""

    host: str
    port: int
    use_starttls: bool
    note: str


PRESETS: dict[str, SmtpPreset] = {
    "gmail": SmtpPreset(
        host="smtp.gmail.com",
        port=587,
        use_starttls=True,
        note=(
            "Turn on 2-Step Verification, then create an App Password at "
            "https://myaccount.google.com/apppasswords and use it as the SMTP password "
            "(your normal Google password does not work). Username = your full Gmail "
            "address. Changing your Google password revokes all app passwords."
        ),
    ),
    "naver": SmtpPreset(
        host="smtp.naver.com",
        port=587,
        use_starttls=True,
        note=(
            "In Naver Mail > 환경설정 > POP3/IMAP 설정 set 'IMAP/SMTP 사용' to 사용함. "
            "Naver requires 2단계 인증 and an 애플리케이션 비밀번호 (application password) "
            "for SMTP - use that as the password. Username = your Naver ID; the From "
            "address must be your own @naver.com address."
        ),
    ),
}


# --------------------------------------------------------------------------- message building


def _html_body(message: NotificationMessage) -> str:
    lines = []
    for line in message.body.splitlines():
        escaped = html.escape(line)
        lines.append(f"<strong>{escaped}</strong>" if line.endswith(":") else escaped)
    button = ""
    if is_http_url(message.url):
        button = (
            f'<p style="margin:24px 0"><a href="{html.escape(message.url or "", quote=True)}" '
            'style="background:#2563eb;color:#ffffff;padding:10px 18px;border-radius:6px;'
            f'text-decoration:none;display:inline-block">{html.escape(message.url_label)}</a></p>'
        )
    return (
        '<!doctype html><html><body style="font-family:Segoe UI,Malgun Gothic,Arial,'
        'sans-serif;font-size:15px;line-height:1.5;color:#111827">'
        f'<h2 style="margin:0 0 16px">{html.escape(message.title)}</h2>'
        f"<p>{'<br>'.join(lines)}</p>{button}"
        f'<p style="color:#6b7280;font-size:12px">{html.escape(SENDER_NAME)}</p>'
        "</body></html>"
    )


def _plain_body(message: NotificationMessage) -> str:
    text = full_text(message)
    if message.url:
        text += f"\n\n{message.url_label}: {message.url}"
    return f"{text}\n\n-- \n{SENDER_NAME}\n"


def build_email(message: NotificationMessage, sender: str, recipients: list[str]) -> EmailMessage:
    """``multipart/alternative`` e-mail (plain + HTML) for ``message``."""
    mail = EmailMessage()
    mail["Subject"] = subject_line(message)
    mail["From"] = formataddr((SENDER_NAME, sender))
    mail["To"] = ", ".join(recipients)
    mail["Date"] = formatdate(localtime=True)
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else None
    mail["Message-ID"] = make_msgid(domain=domain)
    mail.set_content(_plain_body(message))
    mail.add_alternative(_html_body(message), subtype="html")
    return mail


# --------------------------------------------------------------------------- sending


def resolve_sender(cfg: EmailNotify) -> str | None:
    """From address: ``from_addr``, else the username (Naver IDs get ``@naver.com``)."""
    if cfg.from_addr and cfg.from_addr.strip():
        return cfg.from_addr.strip()
    username = (cfg.username or "").strip()
    if not username:
        return None
    if "@" not in username and (cfg.smtp_host or "").lower().endswith("naver.com"):
        return f"{username}@naver.com"
    return username if "@" in username else None


def _starttls(smtp: smtplib.SMTP, context: ssl.SSLContext, host: str, port: int) -> None:
    """Upgrade the connection; a server without STARTTLS gets a specific explanation."""
    try:
        smtp.starttls(context=context)
    except smtplib.SMTPNotSupportedError:
        raise ChannelError(
            f"{host}:{port} does not support STARTTLS - use port 465 (SSL) or check the server"
        ) from None
    smtp.ehlo()


def _login(smtp: smtplib.SMTP, username: str, password: str, *, encrypted: bool) -> None:
    """Log in; explains a server that offers no AUTH on this (possibly plain) connection."""
    try:
        smtp.login(username, password)
    except smtplib.SMTPNotSupportedError:
        hint = (
            "check the SMTP server/port and that SMTP access is enabled for your account"
            if encrypted
            else "turn on STARTTLS (port 587) or use port 465 (SSL)"
        )
        raise ChannelError(
            f"the SMTP server does not offer login (AUTH) on this connection - {hint}"
        ) from None


def send_email(
    *,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    sender: str,
    recipients: list[str],
    message: NotificationMessage,
    use_starttls: bool = True,
) -> None:
    """Deliver ``message`` over SMTP; raises :class:`ChannelError` with a helpful reason."""
    mail = build_email(message, sender, recipients)
    context = ssl.create_default_context()
    implicit_tls = port == IMPLICIT_TLS_PORT
    try:
        if implicit_tls:
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(
                host, port, timeout=SMTP_TIMEOUT_S, context=context
            )
        else:
            smtp = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_S)
        with smtp:
            smtp.ehlo()
            if use_starttls and not implicit_tls:
                _starttls(smtp, context, host, port)
            if username and password:
                _login(smtp, username, password, encrypted=implicit_tls or use_starttls)
            smtp.send_message(mail)
    except smtplib.SMTPAuthenticationError as exc:
        raise ChannelError(
            f"SMTP authentication failed ({exc.smtp_code}) - use an app password "
            "(Gmail: 2-Step Verification + App Password; Naver: 2단계 인증 + "
            "애플리케이션 비밀번호 and IMAP/SMTP enabled)"
        ) from None
    except smtplib.SMTPSenderRefused:
        raise ChannelError(
            "sender address rejected - the From address must be your account's own address"
        ) from None
    except smtplib.SMTPRecipientsRefused:
        raise ChannelError("recipient address rejected - check the 'To' addresses") from None
    except smtplib.SMTPException as exc:  # incl. SMTPNotSupportedError (e.g. SMTPUTF8)
        raise ChannelError(f"SMTP error: {type(exc).__name__}: {exc}") from None
    except ssl.SSLError as exc:
        raise ChannelError(f"TLS error talking to {host}:{port}: {exc.reason or exc}") from None
    except TimeoutError:
        raise ChannelError(f"timed out talking to {host}:{port}") from None
    except OSError as exc:
        raise ChannelError(f"cannot connect to {host}:{port}: {exc.strerror or exc}") from None


class EmailChannel(NotificationChannel):
    """E-mail through the user's own mailbox (Gmail / Naver / any STARTTLS server)."""

    key = "email"
    display_name = "E-mail"
    description = "E-mail via your mailbox's SMTP server (Gmail / Naver need an app password)."

    def is_configured(self, rs: RuntimeSettings) -> tuple[bool, str]:
        """Needs host, sender, recipients and (when a username is set) the app password."""
        cfg = rs.notifications.email
        if not (cfg.smtp_host or "").strip():
            return False, "SMTP server is not set (choose the Gmail or Naver preset)"
        if not [addr for addr in cfg.to_addrs if addr.strip()]:
            return False, "no recipient address"
        if not resolve_sender(cfg):
            return False, "sender address is not set"
        if (cfg.username or "").strip() and not secret_value(cfg.password):
            return False, "SMTP password (app password) is not set"
        return True, ""

    def send(self, message: NotificationMessage, rs: RuntimeSettings) -> None:
        """Send ``message`` to every configured recipient."""
        cfg = rs.notifications.email
        configured, why = self.is_configured(rs)
        if not configured:
            raise ChannelError(why)
        send_email(
            host=(cfg.smtp_host or "").strip(),
            port=cfg.smtp_port,
            username=(cfg.username or "").strip() or None,
            password=secret_value(cfg.password),
            sender=resolve_sender(cfg) or "",
            recipients=[addr.strip() for addr in cfg.to_addrs if addr.strip()],
            message=message,
            use_starttls=cfg.use_starttls,
        )


register_channel(EmailChannel())
