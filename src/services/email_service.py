import os
import smtplib
from email.message import EmailMessage
from urllib.parse import urlencode


def _get_required_env(name: str) -> str:
    value = os.getenv(name)

    if not value:
        raise RuntimeError(f"Required environment variable is not set: {name}")

    return value


def build_password_reset_url(reset_token: str) -> str:
    """Build the SortView password reset URL."""

    app_url = _get_required_env("SORTVIEW_APP_URL").rstrip("/")

    query = urlencode({"reset_token": reset_token})

    return f"{app_url}/?{query}"


def send_password_reset_email(
    recipient_email: str,
    reset_token: str,
) -> None:
    """Send a one-time SortView password reset email."""

    smtp_host = _get_required_env("SORTVIEW_SMTP_HOST")
    smtp_username = _get_required_env("SORTVIEW_SMTP_USERNAME")
    smtp_password = _get_required_env("SORTVIEW_SMTP_PASSWORD")
    email_from = _get_required_env("SORTVIEW_EMAIL_FROM")

    smtp_port = int(os.getenv("SORTVIEW_SMTP_PORT", "587"))

    reset_url = build_password_reset_url(reset_token)

    message = EmailMessage()
    message["Subject"] = "Reset your SortView password"
    message["From"] = email_from
    message["To"] = recipient_email

    message.set_content(
        f"""A password reset was requested for your SortView account.

Use the link below to choose a new password:

    {reset_url}

This link expires in 30 minutes and can only be used once.

If you did not request a password reset, you can ignore this email.
"""
    )

    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)