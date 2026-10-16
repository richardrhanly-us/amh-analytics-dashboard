from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database import get_engine

from . import auth_service, collector_rules, internal_routing
from .routing_destination import destination_key
from .routing_settings import (
    MEANS_HOME,
    RoutingSettingsError,
    validate_routing_settings,
)
from .settings_merge import merge_settings


class PlanNotAvailableError(RuntimeError):
    """The plan code is not an active plan. A RuntimeError, as it always was."""


class OperationalIdentityConflictError(RuntimeError):
    """An existing operational identity mapping is inconsistent: refused, never rebuilt. A RuntimeError, as it always
    was."""


def create_organization_with_primary_branch(
    org_name: str,
    org_slug: str,
    branch_name: str,
    branch_slug: str,
    plan_code: str,
    org_settings: dict[str, Any] | None = None,
    branch_settings: dict[str, Any] | None = None,
    *,
    conn: Any = None,
) -> dict[str, Any]:
    """Creates the organization, its one active primary branch, a trial subscription on an ACTIVE plan and the two
    settings rows. Its own transaction, or -- given `conn` -- inside the caller's (provision_library). Raises
    PlanNotAvailableError, writing nothing, for a plan code that is not an active plan."""
    org_settings = org_settings or {}
    branch_settings = branch_settings or {}

    sql_insert_org = text("""
        INSERT INTO organizations (name, slug, status)
        VALUES (:name, :slug, 'active')
        RETURNING id, name, slug, status, created_at
    """)

    sql_insert_branch = text("""
        INSERT INTO branches (organization_id, name, slug, is_primary, status)
        VALUES (:organization_id, :name, :slug, TRUE, 'active')
        RETURNING id, organization_id, name, slug, is_primary, status, created_at
    """)

    sql_find_plan = text("""
        SELECT id, code, name
        FROM plans
        WHERE code = :plan_code
          AND is_active = TRUE
        LIMIT 1
    """)

    sql_insert_subscription = text("""
        INSERT INTO subscriptions (organization_id, plan_id, status)
        VALUES (:organization_id, :plan_id, 'trial')
        RETURNING id, organization_id, plan_id, status, started_at
    """)

    sql_insert_org_settings = text("""
        INSERT INTO organization_settings (organization_id, settings_json)
        VALUES (:organization_id, CAST(:settings_json AS JSONB))
        RETURNING id
    """)

    sql_insert_branch_settings = text("""
        INSERT INTO branch_settings (branch_id, settings_json)
        VALUES (:branch_id, CAST(:settings_json AS JSONB))
        RETURNING id
    """)

    if conn is None:
        with get_engine().begin() as own:
            return create_organization_with_primary_branch(
                org_name, org_slug, branch_name, branch_slug, plan_code, org_settings, branch_settings, conn=own
            )

    plan = conn.execute(sql_find_plan, {"plan_code": plan_code}).mappings().first()
    if not plan:
        raise PlanNotAvailableError(f"Plan not found: {plan_code}")

    org = conn.execute(
        sql_insert_org,
        {"name": org_name, "slug": org_slug},
    ).mappings().first()

    branch = conn.execute(
        sql_insert_branch,
        {
            "organization_id": org["id"],
            "name": branch_name,
            "slug": branch_slug,
        },
    ).mappings().first()

    subscription = conn.execute(
        sql_insert_subscription,
        {
            "organization_id": org["id"],
            "plan_id": plan["id"],
        },
    ).mappings().first()

    conn.execute(
        sql_insert_org_settings,
        {
            "organization_id": org["id"],
            "settings_json": json.dumps(org_settings),
        },
    )

    conn.execute(
        sql_insert_branch_settings,
        {
            "branch_id": branch["id"],
            "settings_json": json.dumps(branch_settings),
        },
    )

    return {
        "organization": dict(org),
        "branch": dict(branch),
        "plan": dict(plan),
        "subscription": dict(subscription),
    }


def list_active_plans() -> list[dict[str, Any]]:
    """The plans a new organization can be put on: every active row of `plans`, as {code, name}, in id order. The same
    `is_active` rule create_organization_with_primary_branch looks a plan code up under."""
    sql = text("""
        SELECT code, name
        FROM plans
        WHERE is_active = TRUE
        ORDER BY id
    """)

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql).mappings().all()
        return [dict(row) for row in rows]


def get_organization_by_slug(org_slug: str) -> dict[str, Any] | None:
    sql = text("""
        SELECT id, name, slug, status, created_at, updated_at
        FROM organizations
        WHERE slug = :org_slug
        LIMIT 1
    """)

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(sql, {"org_slug": org_slug}).mappings().first()
        return dict(row) if row else None


def get_branch_by_slug(org_slug: str, branch_slug: str) -> dict[str, Any] | None:
    sql = text("""
        SELECT
            b.id,
            b.organization_id,
            b.name,
            b.slug,
            b.is_primary,
            b.status
        FROM branches b
        JOIN organizations o
          ON o.id = b.organization_id
        WHERE o.slug = :org_slug
          AND b.slug = :branch_slug
        LIMIT 1
    """)

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            sql,
            {"org_slug": org_slug, "branch_slug": branch_slug},
        ).mappings().first()
        return dict(row) if row else None


# --- operational identity ----------------------------------------------------
#
# Two ID domains exist and must never be mixed:
#
#   SaaS:         organizations.id, branches.id
#   operational:  customers.id (via organizations.operational_customer_id) and
#                 branches.id (via branches.operational_branch_id, which for a
#                 provisioned branch equals branches.id -- there is no separate
#                 operational-branches table)
#
# Collector/API scope, agent tokens and pipeline_status are all keyed by the
# operational pair. organizations.id is NEVER a valid customer_id.


def assign_operational_identity(organization_id: int, branch_id: int, *, conn: Any = None) -> dict[str, Any]:
    """Assign (or confirm) the operational identity for one organization/branch.

    Atomic (one transaction) and idempotent:

    * If the organization has no operational customer yet, a customers row is
      created from the organization's name and its id is stored in
      organizations.operational_customer_id.
    * The branch's operational_branch_id is set to branches.id.
    * Re-running an already-correct assignment changes nothing and creates no
      second customer.

    Fails closed (RuntimeError, nothing written) if the organization or branch
    is missing, the branch belongs to another organization, or an existing
    mapping is inconsistent: a dangling customer, a customer already claimed by
    a different organization, or an operational_branch_id that is not the
    branch's own id. Never creates tokens.

    Returns {organization_id, branch_id, operational_customer_id,
    operational_branch_id, created_customer, changed}.

    Its own transaction, or -- given `conn` -- inside the caller's. An
    inconsistent existing mapping raises OperationalIdentityConflictError (a
    RuntimeError): it is refused, never rebuilt.
    """
    sql_lock_org = text("""
        SELECT id, name, operational_customer_id
        FROM organizations
        WHERE id = :organization_id
        FOR UPDATE
    """)

    sql_lock_branch = text("""
        SELECT id, organization_id, operational_branch_id
        FROM branches
        WHERE id = :branch_id
          AND organization_id = :organization_id
        FOR UPDATE
    """)

    sql_customer_exists = text("""
        SELECT id
        FROM customers
        WHERE id = :customer_id
    """)

    sql_customer_claimed_elsewhere = text("""
        SELECT id
        FROM organizations
        WHERE operational_customer_id = :customer_id
          AND id <> :organization_id
        LIMIT 1
    """)

    sql_insert_customer = text("""
        INSERT INTO customers (name)
        VALUES (:name)
        RETURNING id
    """)

    sql_set_org_customer = text("""
        UPDATE organizations
        SET operational_customer_id = :customer_id,
            updated_at = NOW()
        WHERE id = :organization_id
          AND operational_customer_id IS NULL
    """)

    sql_set_branch_identity = text("""
        UPDATE branches
        SET operational_branch_id = id,
            updated_at = NOW()
        WHERE id = :branch_id
          AND operational_branch_id IS NULL
    """)

    if conn is None:
        with get_engine().begin() as own:
            return assign_operational_identity(organization_id, branch_id, conn=own)

    # Row locks serialise concurrent assignments for the same tenant so two
    # racing runs cannot both allocate a customer.
    org = conn.execute(
        sql_lock_org, {"organization_id": organization_id}
    ).mappings().first()
    if not org:
        raise RuntimeError(f"Organization {organization_id} not found")

    branch = conn.execute(
        sql_lock_branch,
        {"branch_id": branch_id, "organization_id": organization_id},
    ).mappings().first()
    if not branch:
        raise RuntimeError(
            f"Branch {branch_id} not found for organization {organization_id}"
        )

    existing_branch_identity = branch["operational_branch_id"]
    if existing_branch_identity is not None and existing_branch_identity != branch["id"]:
        raise OperationalIdentityConflictError(
            f"Inconsistent operational identity: branch {branch_id} has "
            f"operational_branch_id {existing_branch_identity}, expected {branch_id}"
        )

    customer_id = org["operational_customer_id"]
    created_customer = False

    if customer_id is not None:
        customer = conn.execute(
            sql_customer_exists, {"customer_id": customer_id}
        ).mappings().first()
        if not customer:
            raise OperationalIdentityConflictError(
                f"Inconsistent operational identity: organization {organization_id} "
                f"maps to customer {customer_id}, which does not exist"
            )

        claimed = conn.execute(
            sql_customer_claimed_elsewhere,
            {"customer_id": customer_id, "organization_id": organization_id},
        ).mappings().first()
        if claimed:
            raise OperationalIdentityConflictError(
                f"Operational customer {customer_id} is already mapped to "
                f"organization {claimed['id']}"
            )
    else:
        new_customer = conn.execute(
            sql_insert_customer, {"name": org["name"]}
        ).mappings().first()
        if not new_customer:
            raise RuntimeError("Failed to create operational customer")
        customer_id = new_customer["id"]
        created_customer = True

        conn.execute(
            sql_set_org_customer,
            {"customer_id": customer_id, "organization_id": organization_id},
        )

    branch_changed = existing_branch_identity is None
    if branch_changed:
        conn.execute(sql_set_branch_identity, {"branch_id": branch_id})

    return {
        "organization_id": organization_id,
        "branch_id": branch_id,
        "operational_customer_id": customer_id,
        "operational_branch_id": branch_id,
        "created_customer": created_customer,
        "changed": created_customer or branch_changed,
    }


# Defaults match the Tech Logic source paths used by the legacy (since retired)
# Super Admin agent-config generators.
DEFAULT_RAW_CHECKINS_FILE = r"C:\TLCFinalDlls\Checkins.txt"
DEFAULT_RAW_REJECTS_FILE = r"C:\TLCFinalDlls\Rejects.txt"
DEFAULT_RAW_ACS_FILE = r"C:\TLCFinalDlls\ACS Log.txt"


def build_collector_agent_config(
    operational_customer_id: int | None,
    operational_branch_id: int | None,
    api_url: str,
    raw_checkins_file: str = DEFAULT_RAW_CHECKINS_FILE,
    raw_rejects_file: str = DEFAULT_RAW_REJECTS_FILE,
    raw_acs_file: str = DEFAULT_RAW_ACS_FILE,
) -> dict[str, Any]:
    """Build the agent_config.json content for a provisioned tenant.

    customer_id / branch_id are the OPERATIONAL pair. Raises ValueError if
    either is missing: there is deliberately no fallback to SaaS
    organizations.id / branches.id, so an unprovisioned tenant can never
    produce a config that would upload under the wrong scope.
    """
    if operational_customer_id is None or operational_branch_id is None:
        raise ValueError(
            "Operational identity is not assigned; cannot generate Collector config"
        )

    return {
        "database_url": "",
        "customer_id": int(operational_customer_id),
        "branch_id": int(operational_branch_id),
        "raw_checkins_file": raw_checkins_file.strip(),
        "raw_rejects_file": raw_rejects_file.strip(),
        "processed_checkins_file": r"data\processed\checkins_clean.csv",
        "processed_rejects_file": r"data\processed\rejects_clean.csv",
        "checkins_history_file": r"data\processed\checkins_history.csv",
        "rejects_history_file": r"data\processed\rejects_history.csv",
        "status_file": r"data\processed\pipeline_status.json",
        "api_url": api_url.strip().rstrip("/"),
        "raw_acs_file": raw_acs_file.strip(),
        "processed_acs_file": r"data\processed\acs_clean.csv",
        "acs_history_file": r"data\processed\acs_history.csv",
    }


def _require_installation_id(installation_id: int | None) -> int:
    if installation_id is None or isinstance(installation_id, bool) or int(installation_id) < 1:
        raise ValueError(
            "Installation ID is not available; create the Collector installation record first"
        )
    return int(installation_id)


def format_collector_install_parameters(
    operational_customer_id: int | None,
    operational_branch_id: int | None,
    installation_id: int | None,
) -> str:
    """The three identity parameters the on-site installer (install.ps1 in
    the Collector release bundle) requires, as a paste-ready command fragment:
    Operational Customer ID, Operational Branch ID and Installation ID (the id
    RETURNED by create_collector_installation -- never typed). No operational
    identity, or no installation record, means no onboarding values.

    This is deliberately separate from build_collector_agent_config, which
    generates the LEGACY agent_config.json and is not consumed by the
    scheduled Collector: the Collector's own collector_config.json is written
    on the site by install.ps1 from these values."""
    if operational_customer_id is None or operational_branch_id is None:
        raise ValueError(
            "Operational identity is not assigned; cannot generate Collector install parameters"
        )
    installation_id = _require_installation_id(installation_id)
    return (
        f"-CustomerId {int(operational_customer_id)} "
        f"-BranchId {int(operational_branch_id)} "
        f"-InstallationId {installation_id}"
    )


# --- collector installations -------------------------------------------------
#
# collector_installations is server-side bookkeeping only: one row per
# deployed Collector instance/machine. It holds no credentials, and it is not
# a health signal -- pipeline_status remains the source of branch/ingestion
# health. None of these functions populates last_seen_at: it is set only by
# the API's handling of a confirmed installation contact
# (main.record_installation_heartbeat -- a scheduled-run heartbeat or an
# install-time preflight probe carrying the installation_id), which also moves
# provisioning -> active, stamps installed_at at the first such contact, and
# records the running Collector version.

COLLECTOR_INSTALLATION_STATUSES = ("provisioning", "active", "inactive", "retired")

# The ingest contract an installation must be enrolled on (collector_installations.required_contract; see
# services.collector_enrollment_service): 1 is the legacy contract, 2 is Contract v2.
#
# THIS MODULE'S DEFAULT IS 1, DELIBERATELY: an installation is contract 2 only when a caller says so explicitly.
# The administration API's creation paths always do say (as the retired Super Admin forms did, whose selector started on
# Contract v2 from onboarding stage A3b) -- so this default is what every OTHER caller gets, and is unchanged.
COLLECTOR_CONTRACT_LEGACY = 1
COLLECTOR_CONTRACT_V2 = 2
COLLECTOR_CONTRACTS = (COLLECTOR_CONTRACT_LEGACY, COLLECTOR_CONTRACT_V2)


def _clean_optional(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def _validate_installation_fields(name: str, status: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Installation name is required")
    if status not in COLLECTOR_INSTALLATION_STATUSES:
        raise ValueError(
            f"Invalid installation status: {status!r} "
            f"(expected one of {', '.join(COLLECTOR_INSTALLATION_STATUSES)})"
        )
    return name


def create_collector_installation(
    organization_id: int,
    branch_id: int,
    name: str,
    hostname: str | None = None,
    collector_version: str | None = None,
    status: str = "provisioning",
    required_contract: int = COLLECTOR_CONTRACT_LEGACY,
) -> dict[str, Any]:
    engine = get_engine()
    with engine.begin() as conn:
        return insert_collector_installation(
            conn,
            organization_id=organization_id,
            branch_id=branch_id,
            name=name,
            hostname=hostname,
            collector_version=collector_version,
            status=status,
            required_contract=required_contract,
        )


def insert_collector_installation(
    conn: Any,
    *,
    organization_id: int,
    branch_id: int,
    name: str,
    hostname: str | None = None,
    collector_version: str | None = None,
    status: str = "provisioning",
    required_contract: int = COLLECTOR_CONTRACT_LEGACY,
) -> dict[str, Any]:
    """create_collector_installation inside the CALLER's transaction (A2 item 5's administration API records its audit
    row in that same transaction). Same checks, same row, same answer."""
    name = _validate_installation_fields(name, status)
    # Exactly an int: True, "2" and 2.0 all compare equal to a contract and are none of them one.
    if type(required_contract) is not int or required_contract not in COLLECTOR_CONTRACTS:
        raise ValueError(
            f"Invalid required contract: {required_contract!r} "
            f"(expected one of {', '.join(str(contract) for contract in COLLECTOR_CONTRACTS)})"
        )

    # The organization's status comes back with the branch, so the cancelled
    # check below costs no extra statement.
    sql_find_branch = text("""
        SELECT b.id, o.status AS organization_status
        FROM branches b
        JOIN organizations o
          ON o.id = b.organization_id
        WHERE b.id = :branch_id
          AND b.organization_id = :organization_id
        LIMIT 1
    """)

    sql_insert = text("""
        INSERT INTO collector_installations
            (organization_id, branch_id, name, hostname, collector_version, status, required_contract)
        VALUES
            (:organization_id, :branch_id, :name, :hostname, :collector_version, :status, :required_contract)
        RETURNING
            id, organization_id, branch_id, name, hostname, collector_version,
            status, required_contract, installed_at, last_seen_at, created_at, updated_at
    """)

    branch = conn.execute(
        sql_find_branch,
        {"branch_id": branch_id, "organization_id": organization_id},
    ).mappings().first()
    if not branch:
        raise RuntimeError(
            f"Branch {branch_id} not found for organization {organization_id}"
        )
    # A cancelled (permanently offboarded) organization can never gain a
    # new installation. Authoritative here, whatever the page shows.
    if branch.get("organization_status") == "cancelled":
        raise RuntimeError(
            f"Organization {organization_id} is cancelled; it cannot have a new Collector installation"
        )

    row = conn.execute(
        sql_insert,
        {
            "organization_id": organization_id,
            "branch_id": branch_id,
            "name": name,
            "hostname": _clean_optional(hostname),
            "collector_version": _clean_optional(collector_version),
            "status": status,
            "required_contract": required_contract,
        },
    ).mappings().first()

    return dict(row) if row else {}


def list_collector_installations_for_branch(branch_id: int) -> list[dict[str, Any]]:
    sql = text("""
        SELECT
            ci.id,
            ci.organization_id,
            ci.branch_id,
            b.name AS branch_name,
            b.slug AS branch_slug,
            ci.name,
            ci.hostname,
            ci.collector_version,
            ci.status,
            ci.required_contract,
            ci.installed_at,
            ci.last_seen_at,
            ci.created_at,
            ci.updated_at
        FROM collector_installations ci
        JOIN branches b
          ON b.id = ci.branch_id
        WHERE ci.branch_id = :branch_id
        ORDER BY ci.id
    """)

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql, {"branch_id": branch_id}).mappings().all()
        return [dict(row) for row in rows]


def list_collector_installations_for_organization(organization_id: int) -> list[dict[str, Any]]:
    sql = text("""
        SELECT
            ci.id,
            ci.organization_id,
            ci.branch_id,
            b.name AS branch_name,
            b.slug AS branch_slug,
            ci.name,
            ci.hostname,
            ci.collector_version,
            ci.status,
            ci.required_contract,
            ci.installed_at,
            ci.last_seen_at,
            ci.created_at,
            ci.updated_at
        FROM collector_installations ci
        JOIN branches b
          ON b.id = ci.branch_id
        WHERE ci.organization_id = :organization_id
        ORDER BY b.is_primary DESC, b.id, ci.id
    """)

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql, {"organization_id": organization_id}).mappings().all()
        return [dict(row) for row in rows]


def update_collector_installation(
    installation_id: int,
    organization_id: int,
    name: str,
    hostname: str | None,
    collector_version: str | None,
    status: str,
) -> dict[str, Any]:
    """Set an installation's name/hostname/collector_version/status.

    All four fields are written; an empty hostname or collector_version is
    stored as NULL. organization_id scopes the update so one tenant's
    installation can never be edited through another tenant's context.
    installed_at is stamped once, the first time an installation becomes
    'active'; last_seen_at is never touched here.

    An installation of a cancelled (permanently offboarded) organization is
    never updated: the guard is part of the UPDATE itself, so it cannot be
    raced, and such an installation is reported like one that was not found.
    """
    name = _validate_installation_fields(name, status)

    sql = text("""
        UPDATE collector_installations
        SET name = :name,
            hostname = :hostname,
            collector_version = :collector_version,
            status = :status,
            installed_at = CASE
                WHEN :status = 'active' AND installed_at IS NULL THEN NOW()
                ELSE installed_at
            END,
            updated_at = NOW()
        WHERE id = :installation_id
          AND organization_id = :organization_id
          AND NOT EXISTS (
              SELECT 1
              FROM organizations o
              WHERE o.id = :organization_id
                AND o.status = 'cancelled'
          )
        RETURNING
            id, organization_id, branch_id, name, hostname, collector_version,
            status, installed_at, last_seen_at, created_at, updated_at
    """)

    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(
            sql,
            {
                "installation_id": installation_id,
                "organization_id": organization_id,
                "name": name,
                "hostname": _clean_optional(hostname),
                "collector_version": _clean_optional(collector_version),
                "status": status,
            },
        ).mappings().first()

    if not row:
        raise RuntimeError(
            f"Collector installation {installation_id} not found for organization {organization_id}"
            " (or the organization is cancelled)"
        )
    return dict(row)


def get_effective_settings(org_slug: str, branch_slug: str | None = None) -> dict[str, Any]:
    sql_org = text("""
        SELECT
            o.id,
            o.name,
            o.slug,
            os.settings_json AS org_settings
        FROM organizations o
        LEFT JOIN organization_settings os
          ON os.organization_id = o.id
        WHERE o.slug = :org_slug
        LIMIT 1
    """)

    sql_branch = text("""
        SELECT
            b.id,
            b.name,
            b.slug,
            b.is_primary,
            bs.settings_json AS branch_settings
        FROM branches b
        LEFT JOIN branch_settings bs
          ON bs.branch_id = b.id
        WHERE b.organization_id = :organization_id
          AND (
                (:branch_slug IS NOT NULL AND b.slug = :branch_slug)
                OR (:branch_slug IS NULL AND b.is_primary = TRUE)
              )
        ORDER BY b.is_primary DESC, b.id ASC
        LIMIT 1
    """)

    sql_subscription = text("""
        SELECT
            s.id,
            s.status,
            p.code AS plan_code,
            p.name AS plan_name
        FROM subscriptions s
        JOIN plans p
          ON p.id = s.plan_id
        WHERE s.organization_id = :organization_id
        ORDER BY s.created_at DESC
        LIMIT 1
    """)

    sql_entitlements = text("""
        SELECT feature_key, enabled, limit_value
        FROM feature_entitlements
        WHERE plan_id = (
            SELECT s.plan_id
            FROM subscriptions s
            WHERE s.organization_id = :organization_id
            ORDER BY s.created_at DESC
            LIMIT 1
        )
    """)

    engine = get_engine()
    with engine.connect() as conn:
        org = conn.execute(sql_org, {"org_slug": org_slug}).mappings().first()
        if not org:
            raise RuntimeError(f"Organization not found: {org_slug}")

        branch = conn.execute(
            sql_branch,
            {
                "organization_id": org["id"],
                "branch_slug": branch_slug,
            },
        ).mappings().first()

        if not branch:
            raise RuntimeError(f"Branch not found for organization: {org_slug}")

        subscription = conn.execute(
            sql_subscription,
            {"organization_id": org["id"]},
        ).mappings().first()

        entitlement_rows = conn.execute(
            sql_entitlements,
            {"organization_id": org["id"]},
        ).mappings().all()

    org_settings = dict(org["org_settings"] or {})
    branch_settings = dict(branch["branch_settings"] or {})

    effective_settings = merge_settings(org_settings, branch_settings)

    entitlements = {
        row["feature_key"]: {
            "enabled": bool(row["enabled"]),
            "limit_value": row["limit_value"],
        }
        for row in entitlement_rows
    }

    return {
        "organization": {
            "id": org["id"],
            "name": org["name"],
            "slug": org["slug"],
        },
        "branch": {
            "id": branch["id"],
            "name": branch["name"],
            "slug": branch["slug"],
            "is_primary": branch["is_primary"],
        },
        "subscription": dict(subscription) if subscription else None,
        "settings": effective_settings,
        "entitlements": entitlements,
    }


# --- collector site settings (Contract v2) ------------------------------------
#
# What a Contract v2 enrollment reads from a site (services.collector_enrollment_service._prepare_v2_enrollment):
# the ORGANIZATION's own `collector_timezone` and `collector_rules` review record, and the classification rules built
# from its `transit.destinations` and `internal_routing` lists. These functions read and write the organization's
# document, never a branch's and never the merged ("effective") settings -- so "ready here" is "ready there".
#
# The `internal_routing` lists hold patron account names, some of them a staff member's own. They are never returned
# from here -- only how many entries each list has -- and are edited on the Admin Settings page alone.


def _transit_labels(labels: Iterable[object]) -> list[str]:
    """The given labels, in order: each trimmed, a blank or a repeat (whatever its case) left out."""
    kept: list[str] = []
    seen: set[str] = set()
    for label in labels:
        text_label = label.strip() if isinstance(label, str) else ""
        if not text_label or text_label.lower() in seen:
            continue
        seen.add(text_label.lower())
        kept.append(text_label)
    return kept


def transit_destination_entries(labels: Iterable[object]) -> list[dict[str, Any]]:
    """`transit.destinations` entries for the given labels, in order: each trimmed, a blank or a repeat (whatever its
    case) left out, each enabled.

    EACH IS STORED WITH ITS CANONICAL KEY: services.routing_destination.destination_key(label) -- the one key the
    reports look a destination up by, and a valid collector destination slug, so services.collector_rules hands the
    Collector that same key unchanged. A key that is not the label's own (a "dest_1") would file that destination's
    check-ins where the reports cannot find them."""
    return [{"key": destination_key(label), "label": label, "enabled": True} for label in _transit_labels(labels)]


def transit_destination_key_collisions(labels: Iterable[object]) -> list[list[str]]:
    """Each group of DIFFERENT labels among those given that share one canonical key (destination_key), in the order
    first seen; empty when every label is a destination of its own. The same label twice is one label, not a
    collision. Two labels that share a key would be one destination to the Collector and to the reports, so a set
    with a collision is refused where it is saved -- never silently collapsed, and no key is invented to tell them
    apart."""
    by_key: dict[str, list[str]] = {}
    for label in _transit_labels(labels):
        by_key.setdefault(destination_key(label), []).append(label)
    return [group for group in by_key.values() if len(group) > 1]


def transit_destination_collision_messages(labels: Iterable[object]) -> list[str]:
    """One sentence for each collision among the labels (transit_destination_key_collisions), for the form that is
    saving them. Labels are a library's branch names, not patron data."""
    return [
        "These transit destinations would be the same destination (they share one key): "
        + ", ".join(f'"{label}"' for label in group)
        + ". Change one so that they differ in their first 32 letters and digits."
        for group in transit_destination_key_collisions(labels)
    ]


def transit_destination_home_messages(home_branch_label: object, labels: Iterable[object]) -> list[str]:
    """One sentence for each label that MEANS HOME -- the collector contract's home ("Main", "Local", "1") or the
    library's own home label -- and so cannot be a transit destination: its check-ins are counted as staying put.

    The rule is not decided here. It is the customer API's own (services.routing_settings.validate_routing_settings,
    which refuses such a label as `means_home`); this only says, in words, which labels it refused."""
    kept = _transit_labels(labels)
    block = {
        "home_branch_label": home_branch_label.strip() if isinstance(home_branch_label, str) else "",
        "destinations": [{"label": label, "enabled": True} for label in kept],
    }
    try:
        validate_routing_settings(block)
    except RoutingSettingsError as refused:
        problems = refused.problems
    else:
        return []
    return [
        f'"{kept[int(problem.field.split(".")[1])]}" means this library\'s own (home) branch, so it cannot be a transit '
        "destination. Remove it, or change the Home branch label."
        for problem in problems
        if problem.code == MEANS_HOME
    ]


def transit_destination_problems(home_branch_label: object, labels: Iterable[object]) -> list[str]:
    """Everything that stops a set of ENABLED transit destination labels being saved, as sentences for the form saving
    them (empty = nothing): a label that means home, and different labels that share one canonical key."""
    labels = list(labels)
    return transit_destination_home_messages(home_branch_label, labels) + transit_destination_collision_messages(labels)


def _organization_settings_document(value: object) -> dict[str, Any]:
    """A stored organization settings_json as a dict (PostgreSQL hands JSONB back parsed; text is parsed here)."""
    if isinstance(value, (str, bytes)):
        value = json.loads(value)
    return dict(value) if isinstance(value, dict) else {}


_COLLECTOR_SITE_SQL = """
    SELECT o.status AS organization_status, os.settings_json AS settings_json
    FROM organizations o
    LEFT JOIN organization_settings os
      ON os.organization_id = o.id
    WHERE o.id = :organization_id
"""

# Sets the two enrollment keys and the `transit` block of the organization's document, creating the row if there is
# none. Every other key of the document is left exactly as it is, by the statement itself. A cancelled organization
# is not written to, whatever was read a moment earlier: the statement then touches no row.
_SET_COLLECTOR_SITE_SQL = """
    INSERT INTO organization_settings (organization_id, settings_json)
    SELECT o.id, CAST(:document AS JSONB)
    FROM organizations o
    WHERE o.id = :organization_id
      AND o.status <> 'cancelled'
    ON CONFLICT (organization_id) DO UPDATE
    SET settings_json = jsonb_set(
            organization_settings.settings_json || CAST(:site AS JSONB), '{transit}', CAST(:transit AS JSONB), true
        ),
        updated_at = NOW()
    RETURNING organization_id
"""


def get_collector_site_settings(organization_id: int) -> dict[str, Any]:
    """What an organization has stored of the settings a Contract v2 enrollment needs -- and nothing else of its
    document:

        collector_timezone          the stored value (None if there is none)
        collector_rules             the stored review record (None if there is none)
        home_branch_label           its stored home label ("" if there is none)
        transit_destination_labels  the labels of its enabled transit destinations, in stored order
        internal_routing_counts     {list name: how many entries} for the four classification lists -- never an entry

    The first two are under the keys services.collector_rules reads, so its own checks can be run on the result."""
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(text(_COLLECTOR_SITE_SQL), {"organization_id": organization_id}).mappings().first()
    if not row:
        raise RuntimeError(f"Organization {organization_id} not found")

    stored = _organization_settings_document(row["settings_json"])
    transit = stored.get("transit")
    destinations = transit.get("destinations") if isinstance(transit, dict) else None
    home_branch_label = transit.get("home_branch_label") if isinstance(transit, dict) else None
    internal_routing = stored.get("internal_routing")
    lists = internal_routing if isinstance(internal_routing, dict) else {}

    def entry_count(name: str) -> int:
        values = lists.get(name)
        return sum(1 for value in values if str(value).strip()) if isinstance(values, list) else 0

    return {
        collector_rules.TIMEZONE_KEY: stored.get(collector_rules.TIMEZONE_KEY),
        collector_rules.RULES_REVIEW_KEY: stored.get(collector_rules.RULES_REVIEW_KEY),
        "home_branch_label": home_branch_label.strip() if isinstance(home_branch_label, str) else "",
        "transit_destination_labels": [
            entry["label"] for entry in transit_destination_entries(
                entry.get("label") for entry in (destinations if isinstance(destinations, list) else [])
                if isinstance(entry, dict) and bool(entry.get("enabled", True))
            )
        ],
        "internal_routing_counts": {name: entry_count(name) for name in collector_rules.NAME_LISTS},
    }


def set_collector_site_settings(
    organization_id: int,
    *,
    timezone: str,
    transit_destination_labels: Iterable[object],
    reviewed_by_user_id: int,
    reviewed_at: datetime | None = None,
) -> dict[str, Any]:
    """Records, on the ORGANIZATION's own settings, what a Contract v2 enrollment needs from a site: its Collector time
    zone, its enabled transit destinations, and the record that an administrator reviewed its collector routing
    configuration -- `reviewed_by_user_id` (the signed-in platform administrator; never a made-up id) at `reviewed_at` (now, if
    not given). Returns the two enrollment keys as stored.

    Validated BEFORE anything is read or written: a time zone that is not a known IANA name raises
    CollectorSettingsError; a reviewer id that is not a positive integer, or two different labels that share one
    canonical key (transit_destination_key_collisions), raises ValueError. A label that means home -- by the
    contract, or by the organization's STORED home label -- raises ValueError too, once that label has been read
    and before anything is written (transit_destination_home_messages).

    The enabled destinations become exactly the given labels (transit_destination_entries). A stored destination
    that is DISABLED is kept as it is, and so is the rest of the `transit` block (the home label). Every other key
    of the document -- the admin lock, the `internal_routing` lists, account settings -- is untouched.

    A cancelled (permanently offboarded) organization is refused, and so is one that does not exist: RuntimeError,
    with nothing written."""
    zone = collector_rules.collector_timezone({collector_rules.TIMEZONE_KEY: timezone})
    marker = collector_rules.rules_review_marker(
        reviewed_at=reviewed_at or datetime.now(UTC), reviewed_by_user_id=reviewed_by_user_id
    )
    site = {collector_rules.TIMEZONE_KEY: zone, collector_rules.RULES_REVIEW_KEY: marker}
    transit_destination_labels = list(transit_destination_labels)
    if transit_destination_key_collisions(transit_destination_labels):
        raise ValueError("transit destination labels share a canonical key")
    enabled = transit_destination_entries(transit_destination_labels)
    enabled_labels = {entry["label"].lower() for entry in enabled}

    engine = get_engine()
    with engine.begin() as conn:
        row = conn.execute(text(_COLLECTOR_SITE_SQL), {"organization_id": organization_id}).mappings().first()
        if not row:
            raise RuntimeError(f"Organization {organization_id} not found")
        if row["organization_status"] == "cancelled":
            raise RuntimeError(f"Organization {organization_id} is cancelled; its collector site settings cannot be changed")

        stored_transit = _organization_settings_document(row["settings_json"]).get("transit")
        transit = dict(stored_transit) if isinstance(stored_transit, dict) else {}
        # Against the home label that is STORED, which only this read knows: nothing is written for a label that
        # means home.
        if transit_destination_home_messages(transit.get("home_branch_label"), transit_destination_labels):
            raise ValueError("a transit destination label means home")
        stored_destinations = transit.get("destinations")
        disabled = [
            entry for entry in (stored_destinations if isinstance(stored_destinations, list) else [])
            if isinstance(entry, dict) and not bool(entry.get("enabled", True))
            and str(entry.get("label", "")).strip().lower() not in enabled_labels
        ]
        transit["destinations"] = enabled + disabled

        written = conn.execute(
            text(_SET_COLLECTOR_SITE_SQL),
            {
                "organization_id": organization_id,
                "document": json.dumps({**site, "transit": transit}),
                "site": json.dumps(site),
                "transit": json.dumps(transit),
            },
        ).mappings().first()
        if not written:
            raise RuntimeError(f"Organization {organization_id} is cancelled; its collector site settings cannot be changed")

    return site


# --- the administration API's collector configuration (A2 item 4) ----------------------------------------------------
#
# A platform administrator's two writes to an ORGANIZATION's own settings document, each touching only its own keys and
# each recorded in the auth audit log in the same transaction as the change:
#
#   set_collector_enrollment_settings   `collector_timezone` and the `collector_rules` review record
#   replace_internal_routing            the whole `internal_routing` block
#
# Neither reads, merges or writes a branch's document, and neither touches `transit`: the library edits that itself.
# A Collector already installed keeps the rules and time zone it was given at enrollment: nothing here reaches it.
#
# THE AUDIT RECORD names the library as `target_org_slug` -- never `org_slug`, which is what marks an event as one
# that happened IN an organization, for that organization's own members to see (user_admin_service). For the
# Internal Routing lists it holds how many entries each had and has: never an entry.

ADMIN_COLLECTOR_SETTINGS_UPDATED = "admin_collector_settings_updated"
ADMIN_INTERNAL_ROUTING_REPLACED = "admin_internal_routing_replaced"


class OrganizationCancelledError(RuntimeError):
    """The organization is cancelled (permanently offboarded): its settings are not written."""


_ORGANIZATION_SETTINGS_FOR_UPDATE_SQL = """
    SELECT o.status AS organization_status, os.settings_json AS settings_json
    FROM organizations o
    LEFT JOIN organization_settings os
      ON os.organization_id = o.id
    WHERE o.id = :organization_id
"""

# Merges the two enrollment keys into the organization's document, creating the row if there is none. Every other key
# is left as it is. A cancelled organization is not written to: the statement then touches no row.
_MERGE_ORGANIZATION_SETTINGS_SQL = """
    INSERT INTO organization_settings (organization_id, settings_json)
    SELECT o.id, CAST(:keys AS JSONB)
    FROM organizations o
    WHERE o.id = :organization_id
      AND o.status <> 'cancelled'
    ON CONFLICT (organization_id) DO UPDATE
    SET settings_json = COALESCE(organization_settings.settings_json, '{}'::jsonb) || CAST(:keys AS JSONB),
        updated_at = NOW()
    RETURNING organization_id
"""

# Replaces the `internal_routing` block of the organization's document, creating the row if there is none. Every
# other key is left as it is. A cancelled organization is not written to.
_SET_INTERNAL_ROUTING_SQL = """
    INSERT INTO organization_settings (organization_id, settings_json)
    SELECT o.id, jsonb_build_object('internal_routing', CAST(:block AS JSONB))
    FROM organizations o
    WHERE o.id = :organization_id
      AND o.status <> 'cancelled'
    ON CONFLICT (organization_id) DO UPDATE
    SET settings_json = jsonb_set(
            COALESCE(organization_settings.settings_json, '{}'::jsonb), '{internal_routing}', CAST(:block AS JSONB), true
        ),
        updated_at = NOW()
    RETURNING organization_id
"""


def _stored_document_for_write(conn: Any, organization_id: int) -> dict[str, Any]:
    """The organization's stored document, read in the writing transaction. Raises RuntimeError for an organization
    that does not exist and OrganizationCancelledError for a cancelled one."""
    row = conn.execute(text(_ORGANIZATION_SETTINGS_FOR_UPDATE_SQL), {"organization_id": organization_id}).mappings().first()
    if not row:
        raise RuntimeError(f"Organization {organization_id} not found")
    if row["organization_status"] == "cancelled":
        raise OrganizationCancelledError(f"Organization {organization_id} is cancelled; its settings cannot be changed")
    return _organization_settings_document(row["settings_json"])


def _write_or_refuse(conn: Any, sql: str, parameters: dict[str, Any], organization_id: int) -> None:
    if not conn.execute(text(sql), parameters).mappings().first():
        # Cancelled between the read and the write: the statement itself refused it.
        raise OrganizationCancelledError(f"Organization {organization_id} is cancelled; its settings cannot be changed")


def get_internal_routing(organization_id: int) -> dict[str, list[str]]:
    """The organization's four Internal Routing lists as stored (internal_routing.stored_internal_routing: lenient,
    so what is there can be seen and corrected). For a platform administrator only: the entries are never logged."""
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(text(_COLLECTOR_SITE_SQL), {"organization_id": organization_id}).mappings().first()
    if not row:
        raise RuntimeError(f"Organization {organization_id} not found")
    stored = _organization_settings_document(row["settings_json"])
    return internal_routing.stored_internal_routing(stored.get(internal_routing.INTERNAL_ROUTING_KEY))


def replace_internal_routing(
    organization_id: int,
    lists: dict[str, list[str]],
    *,
    target_org_slug: str,
    actor: dict[str, Any],
) -> dict[str, list[str]]:
    """Replaces the organization's whole `internal_routing` block with `lists` -- already normalized by
    internal_routing.validate_internal_routing -- and records it, in the same transaction, as
    ADMIN_INTERNAL_ROUTING_REPLACED: the library's slug and each list's entry count before and after. Every other key
    of the document is left exactly as it is. Returns what was stored.

    `actor` is the platform administrator ({"id", "email"}). An organization that does not exist raises RuntimeError;
    a cancelled one OrganizationCancelledError. Nothing is written for either."""
    block = {name: list(lists[name]) for name in internal_routing.LISTS}

    engine = get_engine()
    with engine.begin() as conn:
        stored = _stored_document_for_write(conn, organization_id)
        previous = internal_routing.stored_internal_routing(stored.get(internal_routing.INTERNAL_ROUTING_KEY))
        _write_or_refuse(
            conn, _SET_INTERNAL_ROUTING_SQL,
            {"organization_id": organization_id, "block": json.dumps(block)}, organization_id,
        )
        auth_service.log_auth_event_with_connection(
            conn,
            event_type=ADMIN_INTERNAL_ROUTING_REPLACED,
            is_success=True,
            user_id=int(actor["id"]),
            email=actor.get("email"),
            message="Internal Routing replaced.",
            metadata={
                "target_org_slug": target_org_slug,
                "previous_counts": internal_routing.entry_counts(previous),
                "counts": internal_routing.entry_counts(block),
                "actor_user_id": int(actor["id"]),
            },
        )
    return block


def set_collector_enrollment_settings(
    organization_id: int,
    *,
    timezone: str,
    target_org_slug: str,
    actor: dict[str, Any],
    reviewed_at: datetime | None = None,
) -> dict[str, Any]:
    """Records, on the organization's own settings, the two things a Contract v2 enrollment reads there: its Collector
    time zone and the record that `actor` (the platform administrator, {"id", "email"}) reviewed its routing
    configuration at `reviewed_at` (now, if not given). Nothing else of the document changes -- not `transit`, not
    `internal_routing`. Recorded in the same transaction as ADMIN_COLLECTOR_SETTINGS_UPDATED. Returns the two keys as
    stored.

    A time zone that is not a known IANA name raises collector_rules.CollectorSettingsError before anything is read.
    An organization that does not exist raises RuntimeError; a cancelled one OrganizationCancelledError."""
    zone = collector_rules.collector_timezone({collector_rules.TIMEZONE_KEY: timezone})
    marker = collector_rules.rules_review_marker(
        reviewed_at=reviewed_at or datetime.now(UTC), reviewed_by_user_id=int(actor["id"])
    )
    keys = {collector_rules.TIMEZONE_KEY: zone, collector_rules.RULES_REVIEW_KEY: marker}

    engine = get_engine()
    with engine.begin() as conn:
        stored = _stored_document_for_write(conn, organization_id)
        previous_zone = stored.get(collector_rules.TIMEZONE_KEY)
        _write_or_refuse(
            conn, _MERGE_ORGANIZATION_SETTINGS_SQL,
            {"organization_id": organization_id, "keys": json.dumps(keys)}, organization_id,
        )
        auth_service.log_auth_event_with_connection(
            conn,
            event_type=ADMIN_COLLECTOR_SETTINGS_UPDATED,
            is_success=True,
            user_id=int(actor["id"]),
            email=actor.get("email"),
            message="Collector settings updated.",
            metadata={
                "target_org_slug": target_org_slug,
                "previous_collector_timezone": previous_zone if isinstance(previous_zone, str) else None,
                "collector_timezone": zone,
                "rules_reviewed": True,
                "actor_user_id": int(actor["id"]),
            },
        )
    return keys


# --- provisioning a library from SortView administration (A2 item 7) ---------------------------------------------------
#
# ONE transaction creates exactly: the organization (active), its one active primary branch, a trial subscription on
# the chosen ACTIVE plan, the two empty settings rows, the operational identity (one customers row and both bridge
# columns) and one auth_audit_log row -- through create_organization_with_primary_branch and assign_operational_identity
# themselves, on the caller's connection. Nothing else: no user or membership (C3), no installation, code, token, key
# or cutover (A2 items 4 and 5, enrollment), no Collector settings, contacts or notes. A failure anywhere leaves nothing.
#
# Values are validated, never rewritten: a name is trimmed, a slug must already be exactly in the slug form. Nothing
# returned, audited or raised carries an internal or operational id, a name, or a value that was refused.

MAX_LIBRARY_NAME_LENGTH = 120
MAX_SLUG_LENGTH = 63
SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

ADMIN_LIBRARY_PROVISIONED = "admin_library_provisioned"
ADMIN_OPERATIONAL_IDENTITY_ASSIGNED = "admin_operational_identity_assigned"

# The unique constraint on organizations.slug (baseline schema: an inline UNIQUE, so PostgreSQL's default name).
_ORGANIZATION_SLUG_CONSTRAINT = "organizations_slug_key"
_UNIQUE_VIOLATION = "23505"


class AdminProvisioningError(Exception):
    """A refusal the administration API may answer with: `code` is fixed and public, and the only text."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def validate_provisioning(
    *, name: object, slug: object, branch_name: object, branch_slug: object, plan_code: object
) -> tuple[dict[str, str], list[dict[str, str]]]:
    """(the values to store, the problems). Each problem is {field, code}: required, too_long or invalid_slug -- never
    the value. Names are trimmed; slugs and the plan code are taken exactly as given."""
    values: dict[str, str] = {}
    problems: list[dict[str, str]] = []

    for field, value in (("name", name), ("branch_name", branch_name)):
        trimmed = value.strip() if isinstance(value, str) else ""
        if not trimmed:
            problems.append({"field": field, "code": "required"})
        elif len(trimmed) > MAX_LIBRARY_NAME_LENGTH:
            problems.append({"field": field, "code": "too_long"})
        values[field] = trimmed

    for field, value in (("slug", slug), ("branch_slug", branch_slug)):
        exact = value if isinstance(value, str) else ""
        if not exact:
            problems.append({"field": field, "code": "required"})
        elif len(exact) > MAX_SLUG_LENGTH:
            problems.append({"field": field, "code": "too_long"})
        elif not SLUG_PATTERN.fullmatch(exact):
            problems.append({"field": field, "code": "invalid_slug"})
        values[field] = exact

    code = plan_code if isinstance(plan_code, str) else ""
    if not code:
        problems.append({"field": "plan_code", "code": "required"})
    values["plan_code"] = code
    return values, problems


def _is_organization_slug_violation(error: IntegrityError) -> bool:
    original = getattr(error, "orig", None)
    diag = getattr(original, "diag", None)
    return (
        getattr(original, "pgcode", None) == _UNIQUE_VIOLATION
        and getattr(diag, "constraint_name", None) == _ORGANIZATION_SLUG_CONSTRAINT
    )


def _audit_admin(conn: Any, event_type: str, message: str, actor: dict[str, Any], **metadata: Any) -> None:
    auth_service.log_auth_event_with_connection(
        conn,
        event_type=event_type,
        is_success=True,
        user_id=int(actor["id"]),
        email=actor.get("email"),
        message=message,
        metadata={**metadata, "actor_user_id": int(actor["id"])},
    )


_SLUG_TAKEN_SQL = "SELECT 1 FROM organizations WHERE slug = :slug"


def provision_library(
    *, name: str, slug: str, branch_name: str, branch_slug: str, plan_code: str, actor: dict[str, Any]
) -> dict[str, Any]:
    """Provisions a library in one transaction (see above). The values must already have passed
    validate_provisioning. Raises AdminProvisioningError library_slug_taken (also when a concurrent request takes the
    slug first) or plan_not_available, writing nothing.

    Returns {library: {slug, name, status}, primary_branch: {slug, name, status}, subscription: {plan_code, plan_name,
    status}, operational_identity}: no id of any kind."""
    try:
        with get_engine().begin() as conn:
            if conn.execute(text(_SLUG_TAKEN_SQL), {"slug": slug}).first() is not None:
                raise AdminProvisioningError("library_slug_taken")
            try:
                created = create_organization_with_primary_branch(
                    name, slug, branch_name, branch_slug, plan_code, conn=conn
                )
            except PlanNotAvailableError:
                raise AdminProvisioningError("plan_not_available") from None
            organization, branch = created["organization"], created["branch"]
            assign_operational_identity(int(organization["id"]), int(branch["id"]), conn=conn)
            _audit_admin(
                conn, ADMIN_LIBRARY_PROVISIONED, "Library provisioned.", actor,
                target_org_slug=organization["slug"], branch_slug=branch["slug"], status=organization["status"],
                plan_code=created["plan"]["code"], operational_identity="assigned",
            )
    except IntegrityError as error:
        if _is_organization_slug_violation(error):
            raise AdminProvisioningError("library_slug_taken") from None
        raise
    return {
        "library": {"slug": organization["slug"], "name": organization["name"], "status": organization["status"]},
        "primary_branch": {"slug": branch["slug"], "name": branch["name"], "status": branch["status"]},
        "subscription": {
            "plan_code": created["plan"]["code"],
            "plan_name": created["plan"]["name"],
            "status": created["subscription"]["status"],
        },
        "operational_identity": "assigned",
    }


_LOCK_LIBRARY_BY_SLUG_SQL = "SELECT id, slug, status FROM organizations WHERE slug = :slug FOR UPDATE"
# LIMIT 2: a library with two primary branches has no one primary branch to assign to.
_PRIMARY_BRANCHES_SQL = (
    "SELECT id FROM branches WHERE organization_id = :organization_id AND is_primary = TRUE ORDER BY id LIMIT 2"
)


def assign_library_operational_identity(org_slug: str, *, actor: dict[str, Any]) -> dict[str, bool]:
    """Assigns (or confirms) the operational identity of an existing library's one primary branch, by
    assign_operational_identity itself, in one transaction. A change is audited in that transaction; confirming an
    identity that is already complete changes and records nothing (the item-6 convention).

    Raises AdminProvisioningError: library_not_found; library_cancelled; no_primary_branch (none, or more than one);
    operational_identity_inconsistent (an existing mapping that contradicts itself -- refused, never rebuilt).

    Returns {changed, created_customer}: no id of any kind."""
    with get_engine().begin() as conn:
        library = conn.execute(text(_LOCK_LIBRARY_BY_SLUG_SQL), {"slug": org_slug}).mappings().first()
        if library is None:
            raise AdminProvisioningError("library_not_found")
        if library["status"] == "cancelled":
            raise AdminProvisioningError("library_cancelled")
        primaries = conn.execute(text(_PRIMARY_BRANCHES_SQL), {"organization_id": library["id"]}).scalars().all()
        if len(primaries) != 1:
            raise AdminProvisioningError("no_primary_branch")
        try:
            assigned = assign_operational_identity(int(library["id"]), int(primaries[0]), conn=conn)
        except OperationalIdentityConflictError:
            raise AdminProvisioningError("operational_identity_inconsistent") from None
        if assigned["changed"]:
            _audit_admin(
                conn, ADMIN_OPERATIONAL_IDENTITY_ASSIGNED, "Operational identity assigned.", actor,
                target_org_slug=library["slug"], changed=True, created_customer=bool(assigned["created_customer"]),
            )
    return {"changed": bool(assigned["changed"]), "created_customer": bool(assigned["created_customer"])}
