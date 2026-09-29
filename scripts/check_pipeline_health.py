"""Checks whether every monitored branch's AMH pipeline is still reporting in.

For branches without a v2 cutover, reads pipeline_status and applies the
legacy freshness/health rules. For branches with a v2_cutovers row, ignores
the frozen legacy pipeline_status timestamp and instead evaluates the newest
active ingest_key_ids heartbeat and health state. Meant to run on a schedule
(a GitHub Actions cron job by default)
so a dead agent or a stalled AMH machine gets noticed without a human
staring at the dashboard's pipeline-status panel.

SCOPE: a branch is evaluated only when ALL of these hold:
  1. its organization's status is 'active' or 'trial';
  2. the branch's own status is 'active'; and
  3. at least one collector_installations row for that SaaS organization and
     branch has status = 'active'.
A suspended or cancelled organization is intentionally non-operational -- the
API rejects its Collector traffic (see main.authenticate_agent), so it stops
heartbeating by design and must not raise stale/never-reported alerts. Likewise
a Collector installation has its own lifecycle (provisioning / active /
inactive / retired): a branch whose installations are all inactive, retired or
still provisioning -- or that has none -- is not expected to be reporting, so
its old (or absent) pipeline_status row must not alert, even though the branch
itself stays active for future use. Those branches are excluded entirely, not
merely downgraded.

The installation condition is an EXISTS, not a JOIN: a branch may have several
Collector installations, and more than one active one must not produce
duplicate results for the branch. collector_installations.organization_id and
branch_id are SaaS ids (organizations.id / branches.id), matched as such.
pipeline_status, v2_cutovers, and ingest_key_ids are bridged ONLY through the
operational ids (o.operational_customer_id / b.operational_branch_id) -- never
the SaaS ids. Monitoring tables are only ever read here, never modified.

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
                ps.updated_at,
                ps.health_status AS legacy_health_status,
                ps.quarantined_count AS legacy_quarantined_count,
                ps.last_error AS legacy_last_error,

                vc.cutover_at,

                ik.key_id,
                ik.status AS key_status,
                ik.last_heartbeat_at,
                ik.health_status AS v2_health_status,
                ik.last_error_class AS v2_last_error,
                ik.pending_outbox_count,
                ik.quarantined_count AS v2_quarantined_count,
                ik.last_success_at

            FROM branches b
            JOIN organizations o
                ON o.id = b.organization_id

            LEFT JOIN pipeline_status ps
                ON ps.customer_id = o.operational_customer_id
               AND ps.branch_id = b.operational_branch_id

            LEFT JOIN v2_cutovers vc
                ON vc.customer_id = o.operational_customer_id
               AND vc.branch_id = b.operational_branch_id

            LEFT JOIN ingest_key_ids ik
                ON ik.id = (
                    SELECT ik2.id
                    FROM ingest_key_ids ik2
                    WHERE ik2.customer_id = o.operational_customer_id
                      AND ik2.branch_id = b.operational_branch_id
                      AND ik2.status = 'active'
                    ORDER BY ik2.created_at DESC, ik2.id DESC
                    LIMIT 1
                )

            WHERE b.status = 'active'
              AND o.status IN ('active', 'trial')
              AND EXISTS (
                  SELECT 1
                  FROM collector_installations ci
                  WHERE ci.organization_id = o.id
                    AND ci.branch_id = b.id
                    AND ci.status = 'active'
              )

            ORDER BY o.name, b.name
        """)
    ).mappings().all()

    unhealthy = []

    for row in rows:
        reasons = []

        if row["cutover_at"] is not None:
            # Once a branch has cut over to v2, pipeline_status is historical.
            # Freshness and health come from the active ingest key instead.
            if row["key_id"] is None:
                reasons.append("v2 cutover is active but no active ingest key exists")
            else:
                heartbeat = row["last_heartbeat_at"]
                if heartbeat is None:
                    reasons.append("v2 collector has never reported a heartbeat")
                else:
                    if heartbeat.tzinfo is None:
                        heartbeat = heartbeat.replace(tzinfo=UTC)
                    if heartbeat < stale_after:
                        reasons.append(
                            f"last v2 heartbeat at {heartbeat.isoformat()} "
                            "(stale/no heartbeat)"
                        )

                health_status = row["v2_health_status"]
                if health_status == "auth_failure":
                    reasons.append("v2 collector heartbeat reports auth_failure")
                elif health_status == "degraded":
                    detail = (
                        f" ({row['v2_last_error']})"
                        if row["v2_last_error"]
                        else ""
                    )
                    reasons.append(
                        f"v2 collector heartbeat reports degraded{detail}"
                    )
        else:
            # Pre-cutover branches retain the existing pipeline_status behavior.
            updated_at = row["updated_at"]
            if updated_at is None:
                reasons.append("has never reported a pipeline run")
            else:
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=UTC)
                if updated_at < stale_after:
                    reasons.append(
                        f"last reported at {updated_at.isoformat()} "
                        "(stale/no heartbeat)"
                    )

            if row["legacy_health_status"] is not None:
                if row["legacy_health_status"] == "auth_failure":
                    reasons.append("agent heartbeat reports auth_failure")
                elif row["legacy_health_status"] == "degraded":
                    detail = (
                        f" ({row['legacy_last_error']})"
                        if row["legacy_last_error"]
                        else ""
                    )
                    reasons.append(f"agent heartbeat reports degraded{detail}")
            elif row["status"] and str(row["status"]).startswith("failed"):
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

    engine = create_engine(get_database_url(), connect_args={"sslmode": "require"}, hide_parameters=True)

    with engine.connect() as conn:
        unhealthy = find_unhealthy_branches(conn, stale_after)

    if not unhealthy:
        print(
            f"All monitored branches reported within the last {stale_minutes} "
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
