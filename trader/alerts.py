"""Alert delivery (email / Telegram). Credentials come from .env only and are never logged."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from trader.config import AppConfig, Secrets
from trader.timeutil import now_iso

log = logging.getLogger(__name__)


class AlertError(RuntimeError):
    pass


def send_email(secrets: Secrets, subject: str, body: str) -> None:
    if not secrets.email_configured():
        raise AlertError("email alerts not configured (SMTP_* / ALERT_EMAIL_* in .env)")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = secrets.alert_email_from or secrets.smtp_user
    msg["To"] = secrets.alert_email_to
    msg.set_content(body)
    with smtp_connect(secrets, timeout=20) as smtp:
        smtp.send_message(msg)


def smtp_connect(secrets: Secrets, timeout: float = 20) -> smtplib.SMTP:
    """Connected + logged-in SMTP client. Port 465 uses implicit TLS (SMTP_SSL);
    other ports use STARTTLS when SMTP_STARTTLS is true (default)."""
    if secrets.smtp_port == 465:
        smtp: smtplib.SMTP = smtplib.SMTP_SSL(secrets.smtp_host, secrets.smtp_port, timeout=timeout)
    else:
        smtp = smtplib.SMTP(secrets.smtp_host, secrets.smtp_port, timeout=timeout)
    try:
        smtp.ehlo()
        if secrets.smtp_port != 465 and secrets.smtp_starttls:
            smtp.starttls()
            smtp.ehlo()
        if secrets.smtp_user and secrets.smtp_password:
            smtp.login(secrets.smtp_user, secrets.smtp_password.get_secret_value())
    except Exception:
        smtp.close()
        raise
    return smtp


def send_telegram(secrets: Secrets, text: str) -> None:
    import httpx

    if not secrets.telegram_configured():
        raise AlertError("telegram alerts not configured (TELEGRAM_* in .env)")
    tok = secrets.telegram_bot_token.get_secret_value()  # type: ignore[union-attr]
    r = httpx.post(
        f"https://api.telegram.org/bot{tok}/sendMessage",
        json={"chat_id": secrets.telegram_chat_id, "text": text},
        timeout=15,
    )
    if r.status_code != 200:
        raise AlertError(f"telegram HTTP {r.status_code}")


def send_alert(cfg: AppConfig, secrets: Secrets, subject: str, body: str) -> bool:
    """Send via the configured channel. Returns False if alerts are disabled/unconfigured."""
    ch = cfg.alerts.channel
    if ch == "email" and secrets.email_configured():
        send_email(secrets, subject, body)
        return True
    if ch == "telegram" and secrets.telegram_configured():
        send_telegram(secrets, f"{subject}\n{body}")
        return True
    log.info("alert not sent (channel=%s not configured): %s", ch, subject)
    return False


def send_test_alert(cfg: AppConfig, secrets: Secrets) -> bool:
    return send_alert(
        cfg,
        secrets,
        "[trader] test alert",
        f"This is a test alert from your paper-trading agent at {now_iso()} UTC. "
        "No action needed. (Simulation only - no real trades.)",
    )
