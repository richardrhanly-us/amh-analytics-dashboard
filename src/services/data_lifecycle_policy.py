"""The tenant data-lifecycle policy, as data (docs/data-lifecycle-offboarding.md is the prose version).

ONE REGISTRY, THREE CONSUMERS. `DATABASE_SURFACES` classifies every application-owned table. It is read by
  * scripts/purge_tenant_data.py -- the ONLY place a tenant purge takes its DELETE predicates and their order from;
  * platform_admin_service.offboard_library -- which records its evidence through record_tenant_lifecycle_event below;
  * tests/test_data_lifecycle_policy.py -- which fails if a table in the migrated schema is not classified here, so a new
    table cannot be added without someone deciding what happens to it when a tenant leaves.

THREE CONCEPTS THAT MUST STAY SEPARATE.
  suspension   organizations.status = 'suspended'. Reversible. Touches nothing in this file.
  cutoff       organizations.status = 'cancelled' plus revocation of every access artifact (the `revoke` category).
               Permanent, but nothing is deleted.
  purge        a separately authorized DELETE of the tenant's rows, by the table owner, never by the runtime role.

NO RETENTION PERIOD IS INVENTED HERE. A purge is an operator action, not a timer. Where the product already has a period
(the Collector's local files, Neon's history window) the documentation cites it; this module does not restate or change it.

THIS MODULE HAS NO ENGINE AND NO STREAMLIT, so the purge tool (a plain script) and the Streamlit services can both import it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import JSON, bindparam, text

# --- categories -------------------------------------------------------------------------------------------------------

REVOKE = "revoke"  # an access artifact: disabled at cutoff, deleted by a later purge
PURGE = "purge"  # tenant data: untouched at cutoff, deleted by a purge
RETAIN = "retain"  # lifecycle evidence: never deleted by a tenant purge
GLOBAL_REFERENCE = "global_reference"  # not tenant data at all
SEPARATELY_GOVERNED = "separately_governed"  # user identity / security audit: awaits its own retention policy
PROVIDER_AGING = "provider_aging"  # held by a third party; ages out on the provider's schedule, not ours
LOCAL_MACHINE = "local_machine"  # on the customer's Collector machine; removed only by an explicit customer action

DATABASE_CATEGORIES = (REVOKE, PURGE, RETAIN, GLOBAL_REFERENCE, SEPARATELY_GOVERNED)

# The predicates below are the tenant scope of every DELETE a purge issues. :organization_id is organizations.id (SaaS);
# :customer_id is organizations.operational_customer_id (operational). The two id domains are never interchangeable.
_ORG_BRANCHES = "SELECT id FROM branches WHERE organization_id = :organization_id"
_ORG_INSTALLATIONS = "SELECT id FROM collector_installations WHERE organization_id = :organization_id"
_BY_CUSTOMER = "customer_id = :customer_id"
_BY_ORGANIZATION = "organization_id = :organization_id"


@dataclass(frozen=True)
class SurfacePolicy:
    table: str
    category: str
    data_class: str
    at_cutoff: str  # what offboard_library does to it
    purge_where: str | None = None  # tenant-scoped DELETE predicate; None = a tenant purge never deletes from it
    purge_order: int | None = None  # ascending; children before the rows they reference
    # Tenant-key columns the schema allows to be NULL. A row with a NULL here cannot be attributed to any tenant, so a
    # purge REFUSES while one exists (it would otherwise leave possibly-tenant-owned data behind and report success).
    nullable_tenant_keys: tuple[str, ...] = ()
    # The table carries BOTH customer_id and branch_id: a purge refuses if the two disagree about whose row it is.
    paired_keys: bool = False
    note: str = ""


DATABASE_SURFACES: tuple[SurfacePolicy, ...] = (
    # --- operational event data: the trigger-fed copies first, then v1, then v2 ---------------------------------------
    SurfacePolicy("checkins_clean", PURGE, "operational copy (barcode, title)", "kept",
                  _BY_CUSTOMER, 10, nullable_tenant_keys=("customer_id", "branch_id"), paired_keys=True,
                  note="Trigger-fed copy of checkins; no FK, no RLS, no runtime-role access."),
    SurfacePolicy("rejects_clean", PURGE, "operational copy (barcode)", "kept",
                  _BY_CUSTOMER, 11, nullable_tenant_keys=("customer_id", "branch_id"), paired_keys=True,
                  note="Trigger-fed copy of rejects; no FK, no RLS, no runtime-role access."),
    SurfacePolicy("checkins", PURGE, "operational v1 (barcode, title)", "kept", _BY_CUSTOMER, 20, paired_keys=True),
    SurfacePolicy("rejects", PURGE, "operational v1 (barcode)", "kept", _BY_CUSTOMER, 21, paired_keys=True),
    SurfacePolicy("acs_events", PURGE, "patron-adjacent v1 (patron_id, raw_message, title, barcode)", "kept",
                  _BY_CUSTOMER, 22, nullable_tenant_keys=("customer_id", "branch_id"), paired_keys=True,
                  note="The most sensitive table. customer_id/branch_id are nullable and have no FK."),
    SurfacePolicy("checkin_events", PURGE, "operational v2 (pseudonymised)", "kept", _BY_CUSTOMER, 30, paired_keys=True),
    SurfacePolicy("reject_events", PURGE, "operational v2 (pseudonymised)", "kept", _BY_CUSTOMER, 31, paired_keys=True),
    SurfacePolicy("acs_item_events", PURGE, "operational v2 (pseudonymised)", "kept", _BY_CUSTOMER, 32, paired_keys=True,
                  note="Named acs_hold_events before revision e5a2c7b93d14; a purge requires the schema at head."),
    # --- operational bookkeeping ------------------------------------------------------------------------------------
    SurfacePolicy("ingest_key_ids", REVOKE, "v2 key registry (no key material)", "active keys retired",
                  _BY_CUSTOMER, 40, paired_keys=True),
    SurfacePolicy("v2_cutovers", PURGE, "operator audit of the v1/v2 read boundary", "kept", _BY_CUSTOMER, 41,
                  paired_keys=True, note="References customers/branches with NO ACTION: must go before either."),
    SurfacePolicy("pipeline_status", PURGE, "ingestion health counts", "kept", _BY_CUSTOMER, 42, paired_keys=True),
    # --- access artifacts -------------------------------------------------------------------------------------------
    SurfacePolicy("agent_tokens", REVOKE, "credential hash", "is_active = FALSE (bound and legacy unbound alike)",
                  f"({_BY_CUSTOMER} OR installation_id IN ({_ORG_INSTALLATIONS}))", 50,
                  note="A legacy token has installation_id NULL and is reachable only through customer_id."),
    SurfacePolicy("collector_enrollment_codes", REVOKE, "credential hash", "unused codes revoked",
                  f"installation_id IN ({_ORG_INSTALLATIONS})", 51),
    SurfacePolicy("collector_installations", REVOKE, "deployment bookkeeping (hostname)", "status = 'retired'",
                  _BY_ORGANIZATION, 52),
    # --- account structure --------------------------------------------------------------------------------------------
    SurfacePolicy("branch_settings", PURGE, "configuration", "kept", f"branch_id IN ({_ORG_BRANCHES})", 60),
    SurfacePolicy("branches", PURGE, "account structure", "kept", _BY_ORGANIZATION, 61,
                  note="branches.organization_id has no FK to organizations: nothing cascades, so it is explicit."),
    SurfacePolicy("organization_settings", PURGE, "configuration (incl. the admin-lock hash)", "kept", _BY_ORGANIZATION, 70),
    SurfacePolicy("subscriptions", PURGE, "plan assignment", "kept, unchanged", _BY_ORGANIZATION, 71,
                  note="Cutoff deliberately does not touch billing state; a future billing integration must reconcile "
                       "cancellation, refunds and record retention before this row may be deleted."),
    SurfacePolicy("memberships", PURGE, "user-to-tenant access", "kept", _BY_ORGANIZATION, 72,
                  note="Deleting a membership removes the tenant link only; the app_users row is retained."),
    SurfacePolicy("organizations", REVOKE, "account", "status = 'cancelled' (terminal)", "id = :organization_id", 80),
    SurfacePolicy("customers", PURGE, "operational account identity", "kept", "id = :customer_id", 90),
    # --- retained evidence ----------------------------------------------------------------------------------------------
    SurfacePolicy("tenant_lifecycle_events", RETAIN, "lifecycle evidence (counts and ids only)", "one row appended",
                  note="Append-only for every role (trigger). Survives the purge of the tenant it describes."),
    # --- separately governed: global user identity and security audit -------------------------------------------------
    SurfacePolicy("app_users", SEPARATELY_GOVERNED, "global user identity (staff e-mail)", "kept",
                  note="A user is global and may belong to several tenants. Not deleted by a tenant purge, pending a "
                       "user-account lifecycle policy."),
    SurfacePolicy("auth_sessions", SEPARATELY_GOVERNED, "session token hash",
                  "revoked for users left with no usable organization",
                  note="User-scoped, not tenant-scoped. Never deleted here; goes only with its app_users row."),
    SurfacePolicy("password_reset_tokens", SEPARATELY_GOVERNED, "reset token hash", "kept (short-lived, single use)",
                  note="User-scoped. A reset restores a login, never access to a cancelled organization."),
    SurfacePolicy("auth_audit_log", SEPARATELY_GOVERNED, "security audit (staff e-mail)", "kept",
                  note="No tenant key. Retained as security evidence, pending an audit-retention policy."),
    # --- global / reference: not tenant data -------------------------------------------------------------------------
    SurfacePolicy("plans", GLOBAL_REFERENCE, "product catalogue", "n/a"),
    SurfacePolicy("feature_entitlements", GLOBAL_REFERENCE, "product catalogue", "n/a"),
    SurfacePolicy("bin_routing_map", GLOBAL_REFERENCE, "routing labels (no tenant key)", "n/a",
                  note="One global table keyed by bin; holds no patron or item data and cannot be purged per tenant."),
    SurfacePolicy("alembic_version", GLOBAL_REFERENCE, "schema revision", "n/a"),
)

# Views are derived, hold nothing themselves, and empty with their base table.
DERIVED_VIEWS: dict[str, str] = {"checkins_routed": "checkins_clean"}


@dataclass(frozen=True)
class ExternalSurface:
    name: str
    category: str
    controlled_by: str
    disposal: str


# Everything that is NOT a row in the live database. SortView deletes none of this from the purge tool.
EXTERNAL_SURFACES: tuple[ExternalSurface, ...] = (
    ExternalSurface("Neon point-in-time history", PROVIDER_AGING, "Neon",
                    "Deleted rows stay restorable until the project's history window passes."),
    ExternalSurface("Neon branches created before the purge", PROVIDER_AGING, "SortView operator, in the Neon console",
                    "Hold the data indefinitely; each must be deleted by hand."),
    ExternalSurface("Sentry events", PROVIDER_AGING, "Sentry",
                    "Payloads are scrubbed before sending; stored events age out on Sentry's retention setting."),
    ExternalSurface("Hosting platform logs (DigitalOcean, Streamlit Community Cloud)", PROVIDER_AGING, "the host",
                    "Carry internal ids and reasons only; age out on the host's schedule."),
    ExternalSurface("GitHub Actions logs", PROVIDER_AGING, "GitHub", "Age out on GitHub's log retention."),
    ExternalSurface("Transactional e-mail (password resets)", PROVIDER_AGING, "the e-mail provider",
                    "Staff addresses and delivery metadata; age out on the provider's schedule."),
    ExternalSurface("Collector DataRoot (config, state, logs, DPAPI secrets, v2 patron cache)", LOCAL_MACHINE,
                    "the customer", "uninstall-collector.ps1 -PurgeData; a plain uninstall preserves it."),
    ExternalSurface("Collector runtime backups (<InstallRoot>.backup-<timestamp>[-full])", LOCAL_MACHINE,
                    "the customer", "Outside DataRoot and InstallRoot; removed by hand."),
    ExternalSurface("Legacy Machine-scope SORTVIEW_API_TOKEN variable", LOCAL_MACHINE, "the customer",
                    "Reported by the uninstaller, removed by hand."),
    ExternalSurface("Legacy agent footprint (C:\\SortViewAgent)", LOCAL_MACHINE, "the customer",
                    "Separate from the Collector; removed by hand."),
)

OUT_OF_SCOPE = (
    "Tech Logic source logs on the AMH machine are vendor-owned. SortView's responsibility begins after reading them; "
    "no SortView tool may delete them."
)


def surface(table: str) -> SurfacePolicy:
    for entry in DATABASE_SURFACES:
        if entry.table == table:
            return entry
    raise KeyError(table)


def purge_plan() -> tuple[SurfacePolicy, ...]:
    """Every table a tenant purge deletes from, in the order it must happen."""
    planned = [entry for entry in DATABASE_SURFACES if entry.purge_where is not None]
    return tuple(sorted(planned, key=lambda entry: entry.purge_order or 0))


def retained_tables() -> tuple[str, ...]:
    """Every table a tenant purge leaves alone, whatever the reason."""
    return tuple(entry.table for entry in DATABASE_SURFACES if entry.purge_where is None)


# --- lifecycle evidence -----------------------------------------------------------------------------------------------

EVENT_ACCESS_CUTOFF = "access_cutoff"
EVENT_ACCESS_CUTOFF_REVERTED = "access_cutoff_reverted"
EVENT_PURGE_EXECUTED = "purge_executed"
LIFECYCLE_EVENT_TYPES = (EVENT_ACCESS_CUTOFF, EVENT_ACCESS_CUTOFF_REVERTED, EVENT_PURGE_EXECUTED)

# Evidence is counts, internal row ids and status names -- never a payload. Numbers, booleans and containers of them are
# always fine. A STRING is accepted only under one of the keys below and only if it is a short lower-case identifier
# (a status name, an Alembic revision): a raw token (43 characters, mixed case), a SHA-256 digest (64) and free text all
# fail. A key naming a payload is refused outright, whatever it holds.
_STRING_DETAIL_KEYS = frozenset({"status_before", "status_after", "schema_revision"})
_SAFE_DETAIL_STRING = re.compile(r"^[a-z0-9_]{1,32}$")
_FORBIDDEN_DETAIL_KEY_PARTS = (
    "hash", "secret", "password", "barcode", "patron", "title", "raw", "message", "email", "hostname", "item_key",
    "event_key",
)


def _check_detail(key: str, value: Any) -> None:
    lowered = key.lower()
    if any(part in lowered for part in _FORBIDDEN_DETAIL_KEY_PARTS):
        raise ValueError(f"lifecycle details may not carry {key!r}")
    if value is None or isinstance(value, bool | int):
        return
    if isinstance(value, str):
        if lowered not in _STRING_DETAIL_KEYS or not _SAFE_DETAIL_STRING.fullmatch(value):
            raise ValueError(f"lifecycle details value for {key!r} must be a number, not text")
        return
    if isinstance(value, dict):
        for inner_key, inner_value in value.items():
            _check_detail(str(inner_key), inner_value)
        return
    if isinstance(value, list | tuple):
        for item in value:
            _check_detail(key, item)
        return
    raise ValueError(f"lifecycle details value for {key!r} has unsupported type {type(value).__name__}")


def validate_lifecycle_details(details: dict[str, Any]) -> dict[str, Any]:
    """Raises ValueError unless `details` is counts / internal ids / short identifiers only."""
    if not isinstance(details, dict):
        raise ValueError("lifecycle details must be an object")
    for key, value in details.items():
        _check_detail(str(key), value)
    return details


# No RETURNING: the runtime role holds INSERT on this table and nothing else.
_INSERT_LIFECYCLE_EVENT_SQL = text("""
    INSERT INTO tenant_lifecycle_events
        (event_type, organization_id, organization_slug, operational_customer_id, actor_user_id, actor_label, details)
    VALUES
        (:event_type, :organization_id, :organization_slug, :operational_customer_id, :actor_user_id, :actor_label,
         :details)
""").bindparams(bindparam("details", type_=JSON))


def record_tenant_lifecycle_event(
    conn,
    *,
    event_type: str,
    organization_id: int,
    organization_slug: str,
    operational_customer_id: int | None,
    actor_user_id: int | None,
    actor_label: str,
    details: dict[str, Any],
) -> None:
    """Appends ONE tenant_lifecycle_events row inside the caller's transaction, so the evidence commits or rolls back with
    the action it describes. The identifiers are stored as plain historical values (no FK): the row outlives the tenant."""
    if event_type not in LIFECYCLE_EVENT_TYPES:
        raise ValueError(f"unknown lifecycle event type {event_type!r}")
    if not (actor_label or "").strip():
        raise ValueError("actor_label is required and cannot be blank")
    conn.execute(
        _INSERT_LIFECYCLE_EVENT_SQL,
        {
            "event_type": event_type,
            "organization_id": int(organization_id),
            "organization_slug": organization_slug,
            "operational_customer_id": operational_customer_id,
            "actor_user_id": actor_user_id,
            "actor_label": actor_label.strip(),
            "details": validate_lifecycle_details(details),
        },
    )
