"""Response models for a sorter site's diagnostics in the administration API (A2 item 3).

Closed (extra="forbid"): only these fields can reach the admin app. The facts a site's collector last reported --
for a site cut over to Contract v2 its heartbeat, otherwise its legacy report -- and nothing that identifies a key, an
installation, a machine or a tenant: no key id, installation id, hostname, Collector version, operational id, free-text
error or naive timestamp. Every time is an aware UTC instant; codes are the API's own closed vocabularies, unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from admin_api.library_schemas import HostBranch, PipelineSummary
from services.branch_readiness import OnboardingStatus


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LibraryRef(_ResponseModel):
    slug: str
    name: str


class DiagnosticsSorter(_ResponseModel):
    slug: str
    name: str
    host_branch: HostBranch
    status: str
    collector_count: int
    onboarding_status: OnboardingStatus


class V2Heartbeat(_ResponseModel):
    """The site's latest Contract v2 heartbeat (services.operational_read_service.get_latest_ingest_status)."""

    last_heartbeat_at: datetime | None
    health_status: str | None
    last_error_class: str | None
    collector_schedule_status: str | None
    collector_last_run_at: datetime | None
    collector_next_run_at: datetime | None
    collector_run_duration_ms: int | None
    pending_outbox_count: int | None
    quarantined_count: int | None
    oldest_pending_event_at: datetime | None
    last_success_at: datetime | None
    watcher_last_active_at: datetime | None


class V2Section(_ResponseModel):
    """`status` is null when the site has no active ingest key."""

    status: V2Heartbeat | None


class LegacyReport(_ResponseModel):
    """The site's legacy pipeline_status row (services.admin_diagnostics_service.get_legacy_diagnostics)."""

    run_status: str | None
    run_reported_at: datetime | None
    health_status: str | None
    health_reported_at: datetime | None
    pending_outbox_count: int | None
    quarantined_count: int | None
    checkins_rows: int | None
    rejects_rows: int | None
    uploaded_checkins_rows: int | None
    uploaded_rejects_rows: int | None
    checkins_bad_datetime_rows: int | None
    rejects_bad_datetime_rows: int | None


class LegacySection(_ResponseModel):
    """`status` is null when the site has no legacy row."""

    status: LegacyReport | None


class SorterDiagnosticsResponse(_ResponseModel):
    """`contract` is the era the site's report is in now: `v2` from its effective cutover on, `legacy` before it,
    `unknown` when the site has no operational identity to read anything under. Only the current era's section is
    filled; the other is null -- a cut-over site's frozen legacy row is never shown. `cutover_at` is the site's
    effective cutover instant, if it has one (it may still be in the future, with `contract` still `legacy`)."""

    timezone: str
    library: LibraryRef
    sorter: DiagnosticsSorter
    pipeline: PipelineSummary
    contract: Literal["v2", "legacy", "unknown"]
    cutover_at: datetime | None
    v2: V2Section | None
    legacy: LegacySection | None
