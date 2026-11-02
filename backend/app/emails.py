"""Outgoing email: verification and password-reset links."""

import logging
import smtplib
from email.message import EmailMessage

from .config import Settings

log = logging.getLogger(__name__)

# The console backend keeps sent mail here too, so tests can read the links.
outbox: list[EmailMessage] = []


def send_email(settings: Settings, to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    if settings.email_backend == "console":
        outbox.append(msg)
        log.warning(
            "EMAIL (console backend, not sent)\nTo: %s\nSubject: %s\n\n%s", to, subject, body
        )
        return

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            if settings.smtp_starttls:
                smtp.starttls()
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException):
        # Runs as a background task after the response; log rather than crash.
        log.exception("Failed to send email to %s (%s)", to, subject)


def verification_email(settings: Settings, name: str, link: str) -> tuple[str, str]:
    return (
        "Confirm your email for Defog",
        f"Hi {name},\n\n"
        f"Confirm your email address to finish setting up Defog:\n\n{link}\n\n"
        "This link expires in 24 hours. If you didn't sign up for Defog, you can ignore this email.\n",
    )


def reset_email(settings: Settings, name: str, link: str) -> tuple[str, str]:
    return (
        "Reset your Defog password",
        f"Hi {name},\n\n"
        f"Use this link to choose a new password:\n\n{link}\n\n"
        "The link works once and expires in 1 hour. If you didn't ask to reset your password, "
        "you can ignore this email; your password hasn't changed.\n",
    )
