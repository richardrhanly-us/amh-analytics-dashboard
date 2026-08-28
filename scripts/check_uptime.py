"""Checks that the SortView backend API is reachable and responding.

Meant to run from somewhere other than the backend itself (a GitHub Actions
schedule by default) -- a health check the backend runs on itself can't
tell you anything once the backend is actually down.

On failure, sends a single alert email using the same SMTP configuration as
the dashboard's password-reset emails (SORTVIEW_SMTP_*, SORTVIEW_EMAIL_FROM),
to the address(es) in SORTVIEW_ALERT_EMAIL_TO. If that env var is unset, the
check still runs and still exits non-zero on failure -- it just skips the
email.

Usage:

    SORTVIEW_API_BASE_URL=https://api.example.com python scripts/check_uptime.py
"""

from __future__ import annotations

import os
import smtplib
import sys
import urllib.error
import urllib.request
from email.message import EmailMessage

REQUEST_TIMEOUT_SECONDS = 15


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Required environment variable is not set: {name}")
    return value


def check_backend(base_url: str) -> tuple[bool, str]:
    url = base_url.rstrip("/") + "/"

    if not url.startswith(("http://", "https://")):
        return False, f"SORTVIEW_API_BASE_URL must be an http(s) URL, got: {base_url!r}"

    try:
        # base_url comes from trusted config (an env var), not user input, and
        # the scheme is validated above, so this isn't an open URL-scheme risk.
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310
            status = response.status
            if status == 200:
                return True, f"{url} responded 200 OK"
            return False, f"{url} responded with unexpected status {status}"
    except urllib.error.HTTPError as exc:
        return False, f"{url} responded with HTTP {exc.code}: {exc.reason}"
    except TimeoutError:
        return False, (
    f"{url} timed out after {REQUEST_TIMEOUT_SECONDS} seconds"
    )
    except urllib.error.URLError as exc:
        return False, f"{url} could not be reached: {exc.reason}"


def send_alert_email(subject: str, body: str) -> None:
    recipients_raw = os.getenv("SORTVIEW_ALERT_EMAIL_TO", "")
    recipients = [addr.strip() for addr in recipients_raw.split(",") if addr.strip()]

    if not recipients:
        print(
            "SORTVIEW_ALERT_EMAIL_TO is not set -- skipping alert email.",
            file=sys.stderr,
        )
        return

    smtp_host = _get_required_env("SORTVIEW_SMTP_HOST")
    smtp_port = int(os.getenv("SORTVIEW_SMTP_PORT", "587"))
    smtp_username = _get_required_env("SORTVIEW_SMTP_USERNAME")
    smtp_password = _get_required_env("SORTVIEW_SMTP_PASSWORD")
    email_from = _get_required_env("SORTVIEW_EMAIL_FROM")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = email_from
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)


def main() -> None:
    base_url = os.getenv("SORTVIEW_API_BASE_URL")
    if not base_url:
        raise SystemExit(
            "Usage: SORTVIEW_API_BASE_URL=https://api.example.com "
            "python scripts/check_uptime.py"
        )

    ok, detail = check_backend(base_url)
    print(detail)

    if ok:
        return

    try:
        send_alert_email(
            subject="SortView alert: backend API unreachable",
            body=f"The SortView backend API failed its uptime check.\n\n{detail}\n",
        )
    except Exception as exc:
        print(
            f"Backend check failed AND the alert email could not be sent: {exc}",
            file=sys.stderr,
        )

    raise SystemExit(1)


if __name__ == "__main__":
    main()
