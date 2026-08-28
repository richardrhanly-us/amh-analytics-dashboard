"""Checks whether every active branch's AMH pipeline is still reporting in.

Reads pipeline_status for every active branch and flags branches where the
most recent report is older than SORTVIEW_PIPELINE_STALE_MINUTES, has never
reported at all, or reported a failed run. Meant to run on a schedule (a
GitHub Actions cron job by default) so a dead agent or a stalled AMH machine
gets noticed without a human staring at the dashboard's pipeline-status
panel.

SORTVIEW_PIPELINE_STALE_MINUTES has no single correct value -- it depends on
how often each branch's AMH agent is actually scheduled to run, which lives
on the AMH machine, not in this repo. Tune it to comfortably exceed that
interval, or this will alert on every normal run gap.

On any problem, sends a single alert email covering all affected branches,
using the same SMTP configuration as the dashboard's password-reset emails.
If SORTVIEW_ALERT_EMAIL_TO is unset, the check still runs and still exits
non-zero on failure -- it just skips the email.

Usage:

    DATABASE_URL=postgresql://... SORTVIEW_PIPELINE_STALE_MINUTES=60 \\
        python scripts/check_pipeline_health.py
"""

from __future__ import annotations

import os
import smtplib
import sys
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

DEFAULT_STALE_MINUTES = 60


def _get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Required environment variable is not set: {name}")
    return value


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise SystemExit(
            "Usage: DATABASE_URL=postgresql://... python scripts/check_pipeline_health.py"
        )
    return database_url


def find_unhealthy_branches(
    conn: Connection, stale_after: datetime
) -> list[dict[str, Any]]:
    rows = conn.execute(
        text("""
            SELECT
                o.name AS organization_name,
                b.name AS branch_name,
                ps.status,
                ps.last_run,
                ps.last_attempt,
                ps.updated_at
            FROM branches b
            JOIN organizations o ON o.id = b.organization_id
            LEFT JOIN pipeline_status ps
                ON ps.customer_id = o.operational_customer_id
               AND ps.branch_id = b.operational_branch_id
            WHERE b.status = 'active'
            ORDER BY o.name, b.name
        """)
    ).mappings().all()

    unhealthy = []

    for row in rows:
        reasons = []

        updated_at = row["updated_at"]
        if updated_at is None:
            reasons.append("has never reported a pipeline run")
        else:
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            if updated_at < stale_after:
                reasons.append(f"last reported at {updated_at.isoformat()} (stale)")

        if row["status"] and str(row["status"]).startswith("failed"):
            reasons.append(f"latest run status is '{row['status']}'")

        if reasons:
            unhealthy.append({**row, "reasons": reasons})

    return unhealthy


def send_alert_email(unhealthy: list[dict[str, Any]]) -> None:
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

    lines = [
        f"{item['organization_name']} / {item['branch_name']}: "
        f"{'; '.join(item['reasons'])}"
        for item in unhealthy
    ]

    message = EmailMessage()
    message["Subject"] = f"SortView alert: {len(unhealthy)} branch(es) with pipeline issues"
    message["From"] = email_from
    message["To"] = ", ".join(recipients)
    message.set_content(
        "The following branches failed their pipeline health check:\n\n"
        + "\n".join(lines)
        + "\n"
    )

    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)


def main() -> None:
    stale_minutes_raw = os.getenv("SORTVIEW_PIPELINE_STALE_MINUTES")

    stale_minutes = (
        int(stale_minutes_raw)
        if stale_minutes_raw
        else DEFAULT_STALE_MINUTES
    )
    stale_after = datetime.now(UTC) - timedelta(minutes=stale_minutes)

    engine = create_engine(get_database_url(), connect_args={"sslmode": "require"})

    with engine.connect() as conn:
        unhealthy = find_unhealthy_branches(conn, stale_after)

    if not unhealthy:
        print(
            f"All active branches reported within the last {stale_minutes} "
            "minute(s) with no failed runs."
        )
        return

    print(f"{len(unhealthy)} branch(es) with pipeline issues:")
    for item in unhealthy:
        print(
            f"  {item['organization_name']} / {item['branch_name']}: "
            f"{'; '.join(item['reasons'])}"
        )

    try:
        send_alert_email(unhealthy)
    except Exception as exc:
        print(
            f"Pipeline issues found AND the alert email could not be sent: {exc}",
            file=sys.stderr,
        )

    raise SystemExit(1)


if __name__ == "__main__":
    main()
