"""Privacy Contract v2: the key registry, event storage and heartbeat storage (docs/contract-v2-design.md).

Everything here runs on a connection the CALLER opened and owns (main.py's `engine.begin()`), so a conflict or a failure rolls
the whole request back, exactly like v1's single transaction.

Two rules shape this module:

  * Every INSERT/UPDATE names its columns explicitly (`*_COLUMNS` below, one tuple per table) and builds its parameters from
    each model field BY NAME. There is no `model_dump()` anywhere near SQL, so a field added to a model can never reach the
    database by accident -- a test compares each tuple with its model AND with the migration's columns.
  * Tenant scope (`customer_id`, `branch_id`) is an argument the CALLER takes from the authenticated token. Nothing in a
    payload can supply or override it: the v2 models have no such fields.

The SQL text is built only from the constants below and integer positions; request data is only ever a bound parameter.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import bindparam, text

from .ingest_v2_models import (
    AcsHoldEvent,
    AcsNonHoldEvent,
    CheckinEvent,
    RejectEvent,
    StatusV2Request,
)

ALGORITHM = "hmac-sha256-v1"
# collector_enrollment_service.INITIAL_CUTOVER_SET_BY, which imports this module. A real redemption in
# tests/test_legacy_credential_safeguards_postgres.py fails if they ever differ.
ENROLLMENT_CUTOVER_SET_BY = "collector-enrollment"

# Explicit insert columns, per table (tenant columns and `received_at` are added by the statement, not by the model).
CHECKIN_COLUMNS = ("event_key", "event_time", "item_key", "destination", "bin")
REJECT_COLUMNS = ("event_key", "event_time", "error_class", "item_key")
# ONE table holds every ACS item record: a hold carries all of these; a non-hold carries only event_key, event_time, state and
# item_key, and its other columns are stored as NULL (never a placeholder value).
ACS_ITEM_COLUMNS = ("event_key", "event_time", "state", "item_key", "destination", "is_ill", "is_branch_services",
                    "is_collection_services", "ruleset_id")

_INSERT_CHUNK = 200  # rows per INSERT statement: bounded statement size, whatever the request holds
_MAX_REPORTED_CONFLICTS = 50


@dataclass(frozen=True)
class _Kind:
    label: str  # the key used in the request and the response
    table: str
    columns: tuple[str, ...]
    compare_columns: tuple[str, ...]


CHECKIN_COMPARE_COLUMNS = (
    "event_time",
    "item_key",
    "destination",
    "bin",
)

REJECT_COMPARE_COLUMNS = (
    "event_time",
    "error_class",
    "item_key",
)

ACS_ITEM_COMPARE_COLUMNS = (
    "event_time",
    "state",
    "item_key",
    "destination",
    "is_ill",
    "is_branch_services",
    "is_collection_services",
)

KINDS = (
    _Kind(
        "checkins",
        "checkin_events",
        CHECKIN_COLUMNS,
        CHECKIN_COMPARE_COLUMNS,
    ),
    _Kind(
        "rejects",
        "reject_events",
        REJECT_COLUMNS,
        REJECT_COMPARE_COLUMNS,
    ),
    _Kind(
        "acs_items",
        "acs_item_events",
        ACS_ITEM_COLUMNS,
        ACS_ITEM_COMPARE_COLUMNS,
    ),
)


class EventConflict(Exception):
    """The same identity arrived with different content. Carries request positions only (never a key or a value), so the
    caller can answer with a safe 409. Raised inside the caller's transaction, which rolls back."""

    def __init__(self, conflicts: dict[str, list[int]]):
        super().__init__("event conflict")
        self.conflicts = conflicts


# --- explicit row mapping (no model_dump) ------------------------------------------------------------------------

def _iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat()


def checkin_row(event: CheckinEvent) -> dict[str, Any]:
    return {"event_key": event.event_key, "event_time": _iso(event.event_time), "item_key": event.item_key,
            "destination": event.destination, "bin": event.bin}


def reject_row(event: RejectEvent) -> dict[str, Any]:
    return {"event_key": event.event_key, "event_time": _iso(event.event_time), "error_class": event.error_class,
            "item_key": event.item_key}


def acs_item_row(event: AcsHoldEvent | AcsNonHoldEvent) -> dict[str, Any]:
    """A hold stores its derived fields. A non-hold stores NULL for each of them: the columns it does not have are never filled
    with an invented value. Fields are read by name from the event; nothing is dumped generically."""
    row: dict[str, Any] = {
        "event_key": event.event_key, "event_time": _iso(event.event_time), "state": event.state, "item_key": event.item_key,
        "destination": None, "is_ill": None, "is_branch_services": None, "is_collection_services": None, "ruleset_id": None,
    }
    if isinstance(event, AcsHoldEvent):
        row["destination"] = event.destination
        row["is_ill"] = event.is_ill
        row["is_branch_services"] = event.is_branch_services
        row["is_collection_services"] = event.is_collection_services
        row["ruleset_id"] = event.ruleset_id
    return row


_ROW_BUILDERS: dict[str, Callable[[Any], dict[str, Any]]] = {
    "checkins": checkin_row, "rejects": reject_row, "acs_items": acs_item_row,
}


# --- tenant guard shared by the provisioning writes ----------------------------------------------------------------

def _require_provisionable_tenant(conn, customer_id: int, branch_id: int) -> None:
    """Raises ValueError unless (customer_id, branch_id) is the fully mapped operational pair of an organization that is
    not cancelled. A cancelled organization is permanently offboarded: it can never be issued a key or given a cutover,
    whoever calls and from wherever. (A suspended one still can -- suspension is reversible and changes nothing here.)"""
    row = conn.execute(
        text("""
            SELECT o.status
            FROM organizations o
            JOIN branches b ON b.organization_id = o.id
            WHERE o.operational_customer_id = :customer_id
              AND b.operational_branch_id = :branch_id
        """),
        {"customer_id": customer_id, "branch_id": branch_id},
    ).first()
    if row is None:
        raise ValueError("customer_id / branch_id is not a mapped operational tenant")
    if row[0] == "cancelled":
        raise ValueError("the organization is cancelled (permanently offboarded)")


# --- key registry -------------------------------------------------------------------------------------------------

def ingest_key_problem(conn, customer_id: int, branch_id: int, key_id: str, *, token_installation_id: int | None) -> str | None:
    """Why `key_id` may not be used by the token's tenant, or None if it may. The reason is for server-side logs only:
    the caller answers unknown, retired, wrong-tenant and wrong-owner keys with the same generic 403.

    `token_installation_id` is the installation the token is bound to (None: a legacy unbound token). A key with no owner is
    not restricted by it; a key with an owner accepts only a token bound to that owner."""
    row = conn.execute(
        text("SELECT customer_id, branch_id, status, installation_id FROM ingest_key_ids WHERE key_id = :key_id"),
        {"key_id": key_id},
    ).mappings().first()
    if row is None:
        return "unknown key"
    if int(row["customer_id"]) != int(customer_id) or int(row["branch_id"]) != int(branch_id):
        return "key belongs to another tenant"
    if row["status"] != "active":
        return f"key status {row['status']!r}"
    owner = row["installation_id"]
    if owner is not None and (token_installation_id is None or int(owner) != int(token_installation_id)):
        return "key is owned by another installation"
    return None


def _lock_tenant_for_unowned_key(conn, customer_id: int, branch_id: int) -> None:
    """What issuing an unowned key serializes against, held to the end of the caller's transaction. PostgreSQL only: SQLite
    (unit tests) has no row locks and serializes writers itself.

    The organization, FOR SHARE: offboarding holds it FOR UPDATE, so it either waits for this key to commit and then retires
    it, or has already committed and the caller's status check sees 'cancelled'. Without this a key can commit just after
    offboarding's sweep and stay active on a cancelled tenant.

    The branch, FOR NO KEY UPDATE: conflicts with an enrollment's and a retirement's lock on it, and not with the FOR KEY SHARE
    an event insert takes, so an upload is not held up."""
    if getattr(getattr(conn, "dialect", None), "name", "") != "postgresql":
        return
    conn.execute(text("SELECT id FROM organizations WHERE operational_customer_id = :customer_id FOR SHARE"),
                 {"customer_id": customer_id})
    conn.execute(text("SELECT id FROM branches WHERE operational_branch_id = :branch_id FOR NO KEY UPDATE"),
                 {"branch_id": branch_id})


def _refuse_an_unowned_key(conn, customer_id: int, branch_id: int) -> None:
    """Raises ValueError unless the branch may be given an unowned key: it has no active key, has never had an owned one, and
    no enrollment cutover says a Collector was enrolled there (a key from before C6a M2 left no owner, only that row). A forged
    row can only cause a refusal. Under row level security the keys are read in the caller's tenant context; with none this
    reads no key, and the INSERT that follows fails the same policy."""
    keys = conn.execute(
        text("SELECT status, installation_id FROM ingest_key_ids WHERE customer_id = :customer_id AND branch_id = :branch_id"),
        {"customer_id": customer_id, "branch_id": branch_id},
    ).fetchall()
    active = [owner for status, owner in keys if status == "active"]
    if any(owner is not None for owner in active):
        raise ValueError("the branch has an active key owned by an installation")
    if active:
        raise ValueError("the branch already has an active key")
    if any(owner is not None for _status, owner in keys):
        raise ValueError("the branch has a retired key owned by an installation")
    enrolled = conn.execute(
        text("SELECT 1 FROM v2_cutovers WHERE customer_id = :customer_id AND branch_id = :branch_id AND set_by = :set_by LIMIT 1"),
        {"customer_id": customer_id, "branch_id": branch_id, "set_by": ENROLLMENT_CUTOVER_SET_BY},
    ).first()
    if enrolled is not None:
        raise ValueError("the branch was enrolled through Contract v2")


def issue_ingest_key(conn, customer_id: int, branch_id: int, *, installation_id: int | None) -> str:
    """Registers a NEW server-issued key for one tenant and returns its `key_id` (a random UUIDv4; non-secret). The tenant
    must be the fully mapped operational pair -- the same bridge the token lookup uses -- and its organization must not be
    cancelled (permanently offboarded), or nothing is written.

    `installation_id` is the key's owner. An enrollment names its installation; it already holds the organization FOR SHARE,
    took it before its code row, and has locked the branch and refused one with any active key. None is an unowned key, the
    operator's route, and is refused as _refuse_an_unowned_key says: one active key per branch, and an unowned key would be open
    to a token an owned one refuses."""
    if installation_id is None:
        _lock_tenant_for_unowned_key(conn, customer_id, branch_id)
    _require_provisionable_tenant(conn, customer_id, branch_id)
    if installation_id is None:
        _refuse_an_unowned_key(conn, customer_id, branch_id)
    key_id = str(uuid.uuid4())
    conn.execute(
        text("""
            INSERT INTO ingest_key_ids (key_id, customer_id, branch_id, algorithm, status, installation_id)
            VALUES (:key_id, :customer_id, :branch_id, :algorithm, 'active', :installation_id)
        """),
        {"key_id": key_id, "customer_id": customer_id, "branch_id": branch_id, "algorithm": ALGORITHM,
         "installation_id": installation_id},
    )
    return key_id


def active_ingest_key_owners(conn, customer_id: int, branch_id: int) -> list[int | None]:
    """The owner of each of the tenant's ACTIVE keys (None: a key with no owner). Under row level security the runtime role
    sees only the tenant named by its transaction-local context, so the caller must have set it."""
    rows = conn.execute(
        text("""
            SELECT installation_id
            FROM ingest_key_ids
            WHERE customer_id = :customer_id AND branch_id = :branch_id AND status = 'active'
            ORDER BY id
        """),
        {"customer_id": customer_id, "branch_id": branch_id},
    ).fetchall()
    return [None if row[0] is None else int(row[0]) for row in rows]


def has_active_ingest_key(conn, customer_id: int, branch_id: int) -> bool:
    """True if the tenant already has an ACTIVE key, whoever owns it. A retired key does not count. This is a statement about
    the branch: it has one Contract v2 identity."""
    return conn.execute(
        text("""
            SELECT 1
            FROM ingest_key_ids
            WHERE customer_id = :customer_id AND branch_id = :branch_id AND status = 'active'
            LIMIT 1
        """),
        {"customer_id": customer_id, "branch_id": branch_id},
    ).first() is not None


def retire_ingest_key(conn, key_id: str) -> bool:
    """Retires an active unowned key (it is rejected from then on). True if a key was retired, False if there is no such active
    key. Raises ValueError for an active key an installation owns: that would strand its owner, and deactivating the owner
    already stops the only token an owned key accepts. Offboarding and a replacement cutover retire owned keys themselves.

    One statement, so no other lock: an ownership change or an offboarding it waits behind is seen when PostgreSQL rereads the
    row and applies this WHERE again."""
    result = conn.execute(
        text("""
            UPDATE ingest_key_ids
            SET status = 'retired', retired_at = CURRENT_TIMESTAMP
            WHERE key_id = :key_id AND status = 'active' AND installation_id IS NULL
        """),
        {"key_id": key_id},
    )
    if result.rowcount == 1:
        return True
    owned = conn.execute(
        text("SELECT 1 FROM ingest_key_ids WHERE key_id = :key_id AND status = 'active' AND installation_id IS NOT NULL"),
        {"key_id": key_id},
    ).first()
    if owned is not None:
        raise ValueError("the key is owned by an installation")
    return False


def latest_ingest_status(conn, customer_id: int, branch_id: int) -> dict[str, Any] | None:
    """The most recent v2 heartbeat snapshot for a tenant's ACTIVE key, or None if it has none. Used only by the
    dashboard's coexistence-aware health surface -- never by ingestion itself."""
    row = conn.execute(
        text("""
            SELECT key_id, status AS key_status, health_status, last_error_class, pending_outbox_count,
                   quarantined_count, oldest_pending_event_at, last_success_at, watcher_last_active_at,
                   last_heartbeat_at, collector_last_run_at, collector_next_run_at, collector_run_duration_ms,
                   collector_schedule_status
            FROM ingest_key_ids
            WHERE customer_id = :customer_id AND branch_id = :branch_id AND status = 'active'
            ORDER BY last_heartbeat_at DESC NULLS LAST
            LIMIT 1
        """),
        {"customer_id": customer_id, "branch_id": branch_id},
    ).mappings().first()
    return dict(row) if row is not None else None


# --- v2 cutovers (mixed-era read-model boundary) -------------------------------------------------------------------

def record_v2_cutover(
    conn,
    customer_id: int,
    branch_id: int,
    cutover_at: datetime | None,
    set_by: str,
    note: str | None = None,
) -> int:
    """Appends ONE new v2_cutovers row for a tenant -- the only way this table is ever written (see the migration's
    docstring: never UPDATE, never DELETE). `cutover_at=None` records an explicit rollback to v1-only, distinguishable
    from "never cut over" (no rows at all) by the mere presence of this row. Returns the new row's id.

    Raises ValueError if the tenant is not the fully mapped operational pair, if its organization is cancelled, or if
    `set_by` is blank -- the same guardrails issue_ingest_key applies, so a cutover can never be recorded for an unmapped
    or offboarded tenant, or by an anonymous operator."""
    _require_provisionable_tenant(conn, customer_id, branch_id)
    if not (set_by or "").strip():
        raise ValueError("set_by is required and cannot be blank")
    row = conn.execute(
        text("""
            INSERT INTO v2_cutovers (customer_id, branch_id, cutover_at, set_by, note)
            VALUES (:customer_id, :branch_id, :cutover_at, :set_by, :note)
            RETURNING id
        """),
        {
            "customer_id": customer_id,
            "branch_id": branch_id,
            "cutover_at": cutover_at,
            "set_by": set_by.strip(),
            "note": note,
        },
    ).first()
    return int(row[0])


def has_any_v2_cutover(conn, customer_id: int, branch_id: int) -> bool:
    """True if the tenant has ANY v2_cutovers row -- a cutover or a recorded rollback alike. Unlike
    get_effective_v2_cutover, a rollback row counts: it is an operator's decision about this branch, and nothing
    automatic may be layered on top of it."""
    return conn.execute(
        text("SELECT 1 FROM v2_cutovers WHERE customer_id = :customer_id AND branch_id = :branch_id LIMIT 1"),
        {"customer_id": customer_id, "branch_id": branch_id},
    ).first() is not None


def record_initial_v2_cutover(
    conn,
    customer_id: int,
    branch_id: int,
    cutover_at: datetime,
    set_by: str,
    note: str | None = None,
) -> int:
    """Appends a branch's FIRST v2_cutovers row, for a branch that begins life on Contract v2 (collector enrollment).
    Returns the new row's id.

    It never moves a boundary: if the tenant has any row at all -- a cutover or a rollback -- nothing is written and
    ValueError is raised. `cutover_at` must be an aware instant; a boundary with no offset would have to be guessed at.
    The row itself is written by record_v2_cutover, with the same tenant guardrails."""
    if not isinstance(cutover_at, datetime) or cutover_at.tzinfo is None:
        raise ValueError("cutover_at must be an offset-aware datetime")
    if has_any_v2_cutover(conn, customer_id, branch_id):
        raise ValueError("the tenant already has a v2 cutover record")
    return record_v2_cutover(conn, customer_id, branch_id, cutover_at, set_by, note)


def get_effective_v2_cutover(conn, customer_id: int, branch_id: int) -> datetime | None:
    """The tenant's current mixed-era boundary: the `cutover_at` of its most recent v2_cutovers row by `set_at`, or
    None if the branch has no row at all (never piloted) or its latest row is a rollback (`cutover_at IS NULL`).
    Either None case means the same thing to a caller: read v1 only, exactly as before this table existed."""
    row = conn.execute(
        text("""
            SELECT cutover_at
            FROM v2_cutovers
            WHERE customer_id = :customer_id AND branch_id = :branch_id
            ORDER BY set_at DESC
            LIMIT 1
        """),
        {"customer_id": customer_id, "branch_id": branch_id},
    ).first()
    return row[0] if row is not None else None


# --- events -------------------------------------------------------------------------------------------------------

def _comparable(column: str, value: Any) -> Any:
    """One canonical spelling of a stored or incoming value, so a comparison never depends on the driver's types."""
    if value is None:
        return None
    if column == "event_time":
        instant = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return (instant if instant.tzinfo else instant.replace(tzinfo=UTC)).astimezone(UTC)
    if column.startswith("is_"):
        return bool(value)
    return str(value)


def _content(kind: _Kind, row: dict[str, Any]) -> tuple:
    return tuple(
        _comparable(column, row[column])
        for column in kind.compare_columns
    )


def _insert_sql(kind: _Kind, count: int) -> str:
    columns = ("customer_id", "branch_id", "key_id", *kind.columns)
    values = ", ".join("(" + ", ".join(f":{column}_{i}" for column in columns) + ")" for i in range(count))
    # Table and column names are the constants above; the only other text is integer positions. Every value is bound.
    return (
        f"INSERT INTO {kind.table} ({', '.join(columns)}) VALUES {values} "  # nosec B608
        "ON CONFLICT (customer_id, branch_id, key_id, event_key) DO NOTHING RETURNING event_key"
    )


def _store_kind(conn, kind: _Kind, rows: list[dict[str, Any]], customer_id: int, branch_id: int, key_id: str):
    """Stores one list. Returns (inserted, conflict_positions)."""
    conflicts: list[int] = []
    unique: dict[str, tuple[int, dict[str, Any]]] = {}
    for position, row in enumerate(rows):
        first = unique.get(row["event_key"])
        if first is None:
            unique[row["event_key"]] = (position, row)
        elif _content(kind, first[1]) != _content(kind, row):
            conflicts.append(position)  # the same identity twice in one request, with different content
    if conflicts:
        return 0, conflicts

    ordered = list(unique.values())
    inserted_keys: set[str] = set()
    for start in range(0, len(ordered), _INSERT_CHUNK):
        chunk = ordered[start:start + _INSERT_CHUNK]
        params: dict[str, Any] = {}
        for i, (_, row) in enumerate(chunk):
            params[f"customer_id_{i}"] = customer_id
            params[f"branch_id_{i}"] = branch_id
            params[f"key_id_{i}"] = key_id
            for column in kind.columns:
                params[f"{column}_{i}"] = row[column]
        inserted_keys.update(r[0] for r in conn.execute(text(_insert_sql(kind, len(chunk))), params))

    skipped = [(position, row) for position, row in ordered if row["event_key"] not in inserted_keys]
    for start in range(0, len(skipped), _INSERT_CHUNK):
        chunk = skipped[start:start + _INSERT_CHUNK]
        select = text(
            f"SELECT {', '.join(kind.columns)} FROM {kind.table} "  # nosec B608 - constants only
            "WHERE customer_id = :customer_id AND branch_id = :branch_id AND key_id = :key_id "
            "AND event_key IN :event_keys"
        ).bindparams(bindparam("event_keys", expanding=True))
        stored = {
            r["event_key"]: r for r in conn.execute(
                select,
                {"customer_id": customer_id, "branch_id": branch_id, "key_id": key_id,
                 "event_keys": [row["event_key"] for _, row in chunk]},
            ).mappings()
        }
        for position, row in chunk:
            existing = stored.get(row["event_key"])
            if existing is None:
                # ON CONFLICT skipped a row that is not there to compare: not a content conflict. Fail the request (a 500,
                # which the collector retries) rather than call an ordinary race a conflict.
                raise RuntimeError("v2 event was skipped as a duplicate but could not be read back")
            if _content(kind, dict(existing)) != _content(kind, row):
                conflicts.append(position)
    return len(inserted_keys), conflicts


def store_events(conn, *, customer_id: int, branch_id: int, key_id: str, checkins: list[CheckinEvent],
                 rejects: list[RejectEvent], acs_items: list[AcsHoldEvent | AcsNonHoldEvent]) -> dict[str, int]:
    """Stores every event of one request for the token's tenant. Identical resends are idempotent; the same identity with
    different content raises EventConflict (the caller's transaction then stores nothing). Returns the response counts."""
    lists: dict[str, list[Any]] = {"checkins": checkins, "rejects": rejects, "acs_items": acs_items}
    counts: dict[str, int] = {}
    conflicts: dict[str, list[int]] = {}
    for kind in KINDS:
        events = lists[kind.label]
        rows = [_ROW_BUILDERS[kind.label](event) for event in events]
        inserted, kind_conflicts = _store_kind(conn, kind, rows, customer_id, branch_id, key_id)
        if kind_conflicts:
            conflicts[kind.label] = sorted(kind_conflicts)[:_MAX_REPORTED_CONFLICTS]
        counts[f"{kind.label}_received"] = len(rows)
        counts[f"{kind.label}_inserted"] = inserted
        counts[f"{kind.label}_duplicates"] = len(rows) - inserted
    if conflicts:
        raise EventConflict(conflicts)
    return counts


# --- heartbeat ----------------------------------------------------------------------------------------------------

_HEARTBEAT_SQL = """
    UPDATE ingest_key_ids
    SET last_heartbeat_at = CURRENT_TIMESTAMP,
        health_status = :status,
        last_error_class = :last_error_class,
        pending_outbox_count = :pending_outbox_count,
        quarantined_count = :quarantined_count,
        oldest_pending_event_at = :oldest_pending_event_at,
        last_success_at = :last_success_at,
        watcher_last_active_at = :watcher_last_active_at,
        collector_last_run_at = :collector_last_run_at,
        collector_next_run_at = :collector_next_run_at,
        collector_run_duration_ms = :collector_run_duration_ms,
        collector_schedule_status = :collector_schedule_status
    WHERE key_id = :key_id
      AND customer_id = :customer_id
      AND branch_id = :branch_id
      AND status = 'active'
"""


def record_heartbeat(conn, *, customer_id: int, branch_id: int, data: StatusV2Request) -> bool:
    """Stores the heartbeat snapshot on the matching ACTIVE key. False if no such key (the caller answers with the generic
    403). Writes only `ingest_key_ids`; never a v1 table."""

    def stamp(value: datetime | None) -> str | None:
        return _iso(value) if value is not None else None

    result = conn.execute(text(_HEARTBEAT_SQL), {
        "key_id": data.key_id, "customer_id": customer_id, "branch_id": branch_id, "status": data.status,
        "last_error_class": data.last_error_class, "pending_outbox_count": data.pending_outbox_count,
        "quarantined_count": data.quarantined_count, "oldest_pending_event_at": stamp(data.oldest_pending_event_at),
        "last_success_at": stamp(data.last_success_at), "watcher_last_active_at": stamp(data.watcher_last_active_at),
        # Full-snapshot semantics apply to these too: a heartbeat without them (an older collector) stores NULL.
        "collector_last_run_at": stamp(data.collector_last_run_at),
        "collector_next_run_at": stamp(data.collector_next_run_at),
        "collector_run_duration_ms": data.collector_run_duration_ms,
        "collector_schedule_status": data.collector_schedule_status,
    })
    return result.rowcount == 1
