import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from typing import Literal, NoReturn

import sentry_sdk
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, SecretStr
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import create_engine, text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

from src.services.collector_enrollment_service import (
    PUBLIC_ENROLLMENT_ERROR,
    EnrollmentError,
    redeem_enrollment_code,
)
from src.services.ingest_v2_models import StatusV2Request, UploadV2Request
from src.services.ingest_v2_service import (
    EventConflict,
    ingest_key_problem,
    record_heartbeat,
    store_events,
)
from src.services.privacy_hardening import (
    api_docs_kwargs,
    build_sentry_options,
    log_safe_exception,
    safe_validation_body,
    safe_validation_errors,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("sortview.api")

SENTRY_DSN = os.getenv("SENTRY_DSN")
SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT", "development")

if SENTRY_DSN:
    # Every privacy-relevant option (no local variables, no request bodies, event scrubbing) is set in
    # build_sentry_options -- a v1 upload body and its frame locals carry patron_id / raw_message.
    sentry_sdk.init(**build_sentry_options(SENTRY_DSN, SENTRY_ENVIRONMENT))
    logger.info(
        "Sentry error tracking enabled | environment=%s",
        SENTRY_ENVIRONMENT,
    )
else:
    logger.info("Sentry error tracking disabled | SENTRY_DSN not configured")


# Agent uploads run from one machine per branch, so these limits exist to
# blunt brute-forcing/abuse of the bearer token, not to constrain
# legitimate traffic. Configurable per deployment via env var.
UPLOAD_RATE_LIMIT = os.getenv("SORTVIEW_UPLOAD_RATE_LIMIT", "30/minute")
# POST /collector/enroll takes no token, so it is limited per client address and
# much more tightly than uploads. The 79-bit code space already makes guessing
# infeasible; this only blunts abuse.
ENROLL_RATE_LIMIT = os.getenv("SORTVIEW_ENROLL_RATE_LIMIT", "10/minute")
MAX_REQUEST_BODY_BYTES = int(os.getenv("SORTVIEW_MAX_REQUEST_BODY_BYTES", str(5 * 1024 * 1024)))


def get_agent_rate_limit_key(request: Request) -> str:
    """Rate-limit key for /upload, derived from the bearer token instead
    of the client IP -- an IP is a proxy for "which branch," not the
    actual identity, and multiple branches can share an egress IP (or a
    NAT'd network can make one branch's traffic look like many IPs).

    Never returns or logs the raw token -- only a SHA-256 hash of it,
    the same hashing scheme already used to look up agent_tokens
    (see authenticate_agent below). Deliberately never raises: this runs
    as part of the rate-limit decorator, before the endpoint body's own
    get_bearer_token() gets a chance to return a clean 401, so a missing
    or malformed Authorization header falls back to IP-based limiting
    rather than a raw exception.
    """
    authorization = request.headers.get("authorization")

    if authorization:
        match = re.match(r"^Bearer\s+(.+)$", authorization.strip(), re.IGNORECASE)
        if match:
            token = match.group(1).strip()
            if token:
                token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                return f"agent:{token_hash}"

    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=get_remote_address)

# The interactive docs and the OpenAPI schema publish every field the API accepts, so they are off unless a
# developer opts in (SORTVIEW_API_DOCS_ENABLED=true). Production never sets it.
API_DOCS_ENABLED = os.getenv("SORTVIEW_API_DOCS_ENABLED", "false").strip().lower() == "true"

app = FastAPI(**api_docs_kwargs(API_DOCS_ENABLED))
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]


@app.exception_handler(RequestValidationError)
async def safe_request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's default 422 body echoes the submitted value of every failing field (`input`) -- and for a
    missing field, the whole row it was missing from. Under the v1 contract that row can hold `patron_id` /
    `raw_message`, and the Collector logs the first 500 characters of an unexpected response body and reports
    it back as `last_error`. This handler answers with WHERE and WHAT KIND only: same 422 status and the same
    `detail` list shape, plus a stable `code`, and never the submitted value."""
    errors = exc.errors()
    logger.warning(
        "Request validation failed | path=%s errors=%s",
        request.url.path,
        [f"{'.'.join(str(p) for p in e['loc'])}:{e['type']}" for e in safe_validation_errors(errors)],
    )
    return JSONResponse(status_code=422, content=safe_validation_body(errors))

ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "SORTVIEW_ALLOWED_ORIGINS",
        "http://localhost:8501,http://127.0.0.1:8501",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None and int(content_length) > MAX_REQUEST_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"detail": "Request body too large"},
            )
        return await call_next(request)

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )

        return response
    

app.add_middleware(MaxBodySizeMiddleware)
app.add_middleware(SecurityHeadersMiddleware)

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL not set")

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args={"sslmode": "require"},
    future=True,
    # A database error's text otherwise ends with `[parameters: {...}]` -- every bound value of the failing
    # statement, i.e. a whole uploaded row (patron_id, raw_message, barcode) or the bearer token.
    hide_parameters=True,
)


#***************************************************************
# Request Models
#
# Pydantic models for the agent upload payloads. These give the
# API automatic type validation, clear 422 error responses, and
# generated OpenAPI docs instead of accepting raw dicts.
#***************************************************************

# Continuous Ingestion Phase E: an optional deterministic transport/
# source-idempotency identifier -- see agent/event_identity.py (the
# canonical source of this format; duplicated here rather than imported
# because main.py/the backend and agent/ are independently deployed, the
# same reason the DB CHECK constraint in alembic revision 45ba2e7befbc
# duplicates it again at the schema layer). None (the field's default)
# is exactly what the currently deployed legacy scheduled agent sends
# today (it doesn't know this field exists at all, and Pydantic simply
# fills in the default) -- see main.py's upload() and the Phase E report
# for how a None here differs from a present-but-duplicate value.
SOURCE_EVENT_ID_PATTERN = r"^[0-9a-f]{64}$"


class CheckinRow(BaseModel):
    customer_id: int
    branch_id: int
    event_time: str | None = None
    title: str | None = None
    barcode: str | None = None
    collection_code: str | None = None
    call_number: str | None = None
    shelf_code: str | None = None
    destination: str | None = None
    bin: str | None = None
    is_problem: bool | None = None
    message: str | None = None
    flag_1: str | None = None
    flag_2: str | None = None
    flag_3: str | None = None
    source_file: str | None = None
    source_event_id: str | None = Field(default=None, pattern=SOURCE_EVENT_ID_PATTERN)


class RejectRow(BaseModel):
    customer_id: int
    branch_id: int
    event_time: str | None = None
    barcode: str | None = None
    message: str | None = None
    source_file: str | None = None
    source_event_id: str | None = Field(default=None, pattern=SOURCE_EVENT_ID_PATTERN)


class AcsRow(BaseModel):
    customer_id: int
    branch_id: int
    event_time: str | None = None
    message_code: str | None = None
    barcode: str | None = None
    title: str | None = None
    patron_id: str | None = None
    destination: str | None = None
    raw_message: str | None = None
    source_file: str | None = None
    source_event_id: str | None = Field(default=None, pattern=SOURCE_EVENT_ID_PATTERN)


class UploadRequest(BaseModel):
    checkins: list[CheckinRow] = Field(default_factory=list)
    rejects: list[RejectRow] = Field(default_factory=list)
    acs: list[AcsRow] = Field(default_factory=list)


# collector_installations.id is a BIGINT; larger values could only ever be
# rejected by the database as a 500, so they are rejected as a 422 up front.
_BIGINT_MAX = 2**63 - 1


class CollectorEnrollRequest(BaseModel):
    """Body of POST /collector/enroll. The code is a SecretStr so it is masked
    in reprs (and therefore in any error-tracker frame locals); hostname and
    collector_version are untrusted, informational metadata only -- they never
    identify or select an installation."""

    enrollment_code: SecretStr = Field(max_length=64)
    hostname: str | None = Field(default=None, max_length=255)
    collector_version: str | None = Field(default=None, max_length=64)


class PipelineStatusRequest(BaseModel):
    """Shared by two independent writers -- the legacy scheduled
    run_pipeline uploader (last_attempt..destination_breakdown, the
    original per-run fields) and the new continuous-agent heartbeat
    component (health_status..watcher_last_active_at). Every field below
    is optional specifically so each writer can send only the fields it
    owns; see upload_pipeline_status's partial-update logic, which uses
    model_fields_set to update only the fields actually present in a given
    request, leaving the other writer's columns untouched -- including
    letting a field explicitly sent as null clear a previously-stored
    value, which is different from a field being omitted entirely.
    """

    customer_id: int
    branch_id: int

    # --- legacy per-run fields (existing scheduled run_pipeline uploader) ---
    last_attempt: str | None = None
    last_run: str | None = None
    status: str | None = None
    checkins_rows: int | None = None
    rejects_rows: int | None = None
    acs_rows: int | None = None
    uploaded_checkins_rows: int | None = None
    uploaded_rejects_rows: int | None = None
    uploaded_acs_rows: int | None = None
    checkins_bad_datetime_rows: int | None = None
    rejects_bad_datetime_rows: int | None = None
    acs_bad_datetime_rows: int | None = None
    transit_items: int | None = None
    problem_items: int | None = None
    # None (omitted or explicit null) is distinct from {} (explicitly an
    # empty breakdown) -- Optional with no default factory so an omitted
    # field is never silently coerced into an empty dict that would then
    # look like an explicit value to model_fields_set.
    destination_breakdown: dict | None = None

    # --- continuous-agent heartbeat fields (Phase 3) -------------------------
    health_status: Literal["healthy", "degraded", "auth_failure"] | None = None
    pending_outbox_count: int | None = None
    quarantined_count: int | None = None
    oldest_pending_event_at: str | None = None
    last_success_at: str | None = None
    last_failure_category: Literal["retryable_infra", "auth_failure"] | None = None
    last_error: str | None = None
    watcher_last_active_at: str | None = None

    # --- installation lifecycle linkage (metadata only) ----------------------
    # NOT pipeline_status columns and deliberately absent from the
    # _PIPELINE_STATUS_*_FIELDS allowlists below: they identify which
    # collector_installations row this heartbeat belongs to (see
    # record_installation_heartbeat). Both are omitted by the legacy 1.0.2
    # Collector, and then collector_installations is never touched.
    # installation_id is an EXPLICIT identity, never inferred from branch or
    # hostname; collector_version is informational and never an
    # authorization input.
    installation_id: int | None = Field(default=None, ge=1, le=_BIGINT_MAX)
    collector_version: str | None = Field(default=None, max_length=64)


# Fixed allowlists of pipeline_status columns each writer type may update.
# _build_pipeline_status_upsert only ever builds SQL column references from
# these two Python lists -- never from caller-controlled field names -- so
# a request can only ever touch a column that's both a real Pydantic field
# and named here explicitly.
_PIPELINE_STATUS_LEGACY_FIELDS = [
    "last_attempt",
    "last_run",
    "status",
    "checkins_rows",
    "rejects_rows",
    "acs_rows",
    "uploaded_checkins_rows",
    "uploaded_rejects_rows",
    "uploaded_acs_rows",
    "checkins_bad_datetime_rows",
    "rejects_bad_datetime_rows",
    "acs_bad_datetime_rows",
    "transit_items",
    "problem_items",
    "destination_breakdown",
]
_PIPELINE_STATUS_HEARTBEAT_FIELDS = [
    "health_status",
    "pending_outbox_count",
    "quarantined_count",
    "oldest_pending_event_at",
    "last_success_at",
    "last_failure_category",
    "last_error",
    "watcher_last_active_at",
]
_PIPELINE_STATUS_UPDATABLE_FIELDS = _PIPELINE_STATUS_LEGACY_FIELDS + _PIPELINE_STATUS_HEARTBEAT_FIELDS


def _pipeline_status_column_sql(field: str) -> str:
    if field == "destination_breakdown":
        return f"CAST(:{field} AS JSONB)"
    return f":{field}"


def _pipeline_status_bind_value(field: str, value):
    if field == "destination_breakdown":
        return json.dumps(value) if value is not None else None
    return value


def _build_pipeline_status_upsert(data: PipelineStatusRequest) -> tuple[str, dict]:
    """Builds an INSERT ... ON CONFLICT DO UPDATE for pipeline_status that
    only overwrites the columns actually present in this request.

    The legacy scheduled uploader and the new heartbeat component both
    write to the same (customer_id, branch_id) row during Phase 0's
    parallel-validation coexistence window, each owning a disjoint set of
    columns. A field omitted from the request must leave the other
    writer's existing value untouched; a field explicitly sent as null
    must be allowed to clear a previously-stored value (e.g. heartbeat
    clearing last_error after recovery, or oldest_pending_event_at once
    the backlog drains) -- so this can't use a blanket
    COALESCE(EXCLUDED.col, pipeline_status.col), which would treat
    "omitted" and "explicit null" as the same thing.

    data.model_fields_set (Pydantic v2) is exactly the set of field names
    present in the incoming request body, regardless of whether their
    value is null -- an omitted field is never in it. Only fields in that
    set, intersected with the fixed allowlist above, appear in the UPDATE
    SET clause; every other existing column is left alone. The INSERT
    branch (first-ever row for a branch) always writes every updatable
    field from the request, defaulting absent ones to NULL, since there's
    no prior value to preserve there.
    """
    provided = data.model_fields_set
    update_fields = [f for f in _PIPELINE_STATUS_UPDATABLE_FIELDS if f in provided]

    insert_columns = ["customer_id", "branch_id", *_PIPELINE_STATUS_UPDATABLE_FIELDS, "updated_at"]
    insert_values_sql = ", ".join(
        [":customer_id", ":branch_id"]
        + [_pipeline_status_column_sql(f) for f in _PIPELINE_STATUS_UPDATABLE_FIELDS]
        + ["CURRENT_TIMESTAMP"]
    )

    update_set_parts = ["updated_at = CURRENT_TIMESTAMP"] + [
        f"{f} = {_pipeline_status_column_sql(f)}" for f in update_fields
    ]

    # nosec B608 -- every column name interpolated above comes from
    # _PIPELINE_STATUS_UPDATABLE_FIELDS / update_fields, both filtered from
    # the fixed _PIPELINE_STATUS_LEGACY_FIELDS + _PIPELINE_STATUS_HEARTBEAT_FIELDS
    # allowlists defined next to PipelineStatusRequest, never from caller-
    # controlled field names; every actual value is a bound :name parameter
    # in `params` below, nothing here is string-interpolated from request
    # data.
    sql = f"""
        INSERT INTO pipeline_status ({", ".join(insert_columns)})
        VALUES ({insert_values_sql})
        ON CONFLICT (customer_id, branch_id)
        DO UPDATE SET {", ".join(update_set_parts)}
    """  # nosec B608

    params = {"customer_id": data.customer_id, "branch_id": data.branch_id}
    for field in _PIPELINE_STATUS_UPDATABLE_FIELDS:
        params[field] = _pipeline_status_bind_value(field, getattr(data, field))

    return sql, params


def get_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    match = re.match(r"^Bearer\s+(.+)$", authorization.strip(), re.IGNORECASE)
    if not match:
        raise HTTPException(status_code=401, detail="Invalid Authorization header")

    token = match.group(1).strip()

    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    return token


# A request is authorized to ingest only when ALL of these hold: the token is
# valid, the token is active, the request's (customer_id, branch_id) match the
# token's scope, and the tenant that scope maps to is currently usable. Token
# state and tenant state are independent gates -- reactivating a library never
# reactivates a token, and an active token never outlives a suspended library.
#
# The tenant is resolved through the OPERATIONAL bridge (never the SaaS ids):
#   organizations.operational_customer_id = agent_tokens.customer_id
#   branches.operational_branch_id        = agent_tokens.branch_id
#   and the branch must belong to that organization.
# LEFT JOINs, so an unmapped scope or a branch that belongs to another
# organization yields NULL statuses and is rejected (fail closed), not dropped
# into a "token not found" 401.
ALLOWED_ORGANIZATION_STATUSES = ("active", "trial")
ALLOWED_BRANCH_STATUS = "active"

# A token issued by Collector enrollment is bound to ONE installation
# (agent_tokens.installation_id); the lookup below also resolves that
# installation, and bound_installation_unusable_reason enforces that it is still
# provisioning/active and belongs to the very tenant the token's scope resolves
# to. Legacy tokens (installation_id IS NULL) are exempt: nothing about how they
# authenticate changed. Reads agent_tokens.installation_id, so the migration that
# adds it (b4e91d7a3c58) must be applied BEFORE this code is deployed.


_AGENT_TOKEN_LOOKUP_SQL = (  # nosec B105
    """
    SELECT
        t.id,
        t.customer_id,
        t.branch_id,
        t.is_active,
        t.description,
        t.installation_id,
        o.id AS resolved_organization_id,
        o.status AS organization_status,
        b.id AS resolved_branch_id,
        b.status AS branch_status,
        ci.status AS installation_status,
        ci.organization_id AS installation_organization_id,
        ci.branch_id AS installation_branch_id
    FROM agent_tokens t
    LEFT JOIN organizations o
      ON o.operational_customer_id = t.customer_id
    LEFT JOIN branches b
      ON b.operational_branch_id = t.branch_id
     AND b.organization_id = o.id
    LEFT JOIN collector_installations ci
      ON ci.id = t.installation_id
    WHERE t.token_hash = encode(digest(:token, 'sha256'), 'hex')
    LIMIT 1
    """
)


def tenant_unusable_reason(token_row) -> str | None:
    """Why the tenant behind an otherwise-valid token may not ingest, or None
    if it may. The reason is for server-side logs only -- never sent to the
    client."""
    organization_status = token_row.get("organization_status")
    branch_status = token_row.get("branch_status")

    if organization_status is None:
        return "no mapped organization"
    if organization_status not in ALLOWED_ORGANIZATION_STATUSES:
        return f"organization status {organization_status!r}"
    if branch_status is None:
        return "no mapped branch for the organization"
    if branch_status != ALLOWED_BRANCH_STATUS:
        return f"branch status {branch_status!r}"
    return None


def bound_installation_unusable_reason(token_row) -> str | None:
    """Why the installation an enrollment-issued token is bound to may not be
    used, or None if it may (or the token is a legacy, unbound one). Fail closed:
    a bound token whose installation is inactive/retired -- a deliberate admin
    decision, e.g. a decommissioned machine -- stops authenticating everywhere
    (uploads and heartbeats alike), and so does one whose installation does not
    belong to the organization/branch the token's scope resolves to. For server-
    side logs only -- never sent to the client."""
    installation_id = token_row.get("installation_id")
    if installation_id is None:
        return None

    installation_status = token_row.get("installation_status")
    if installation_status is None:
        return f"bound installation {installation_id} not found"
    if installation_status not in INSTALLATION_HEARTBEAT_STATUSES:
        return f"bound installation {installation_id} status {installation_status!r}"
    if token_row.get("installation_organization_id") != token_row.get("resolved_organization_id"):
        return f"bound installation {installation_id} belongs to another organization"
    if token_row.get("installation_branch_id") != token_row.get("resolved_branch_id"):
        return f"bound installation {installation_id} belongs to another branch"
    return None


def authenticate_agent(conn, authorization: str | None, customer_id: int, branch_id: int):
    """v1: the request names its own (customer_id, branch_id) and the token must match it."""
    return _authenticate_agent(conn, authorization, (customer_id, branch_id))


def authenticate_agent_token(conn, authorization: str | None):
    """Contract v2: the request names NO tenant. Every gate of `authenticate_agent` applies, and the tenant is whatever the
    token itself is scoped to (`token_row["customer_id"]`, `token_row["branch_id"]`) -- nothing a payload says."""
    return _authenticate_agent(conn, authorization, None)


def _authenticate_agent(conn, authorization: str | None, expected_scope: tuple[int, int] | None):
    bearer_token = get_bearer_token(authorization)

    token_row = conn.execute(
        text(_AGENT_TOKEN_LOOKUP_SQL),
        {"token": bearer_token},
    ).mappings().first()

    if token_row is None:
        raise HTTPException(status_code=401, detail="Invalid agent token")

    if not token_row["is_active"]:
        raise HTTPException(status_code=403, detail="Agent token is inactive")

    if expected_scope is not None and (
        int(token_row["customer_id"]) != int(expected_scope[0]) or int(token_row["branch_id"]) != int(expected_scope[1])
    ):
        raise HTTPException(status_code=403, detail="Token scope does not match customer_id / branch_id")

    # Authenticated, but not authorized to ingest while the tenant is
    # suspended/cancelled or the branch is inactive. The specific reason is
    # logged for operators; the response stays generic and reveals nothing
    # about the tenant's state.
    unusable_reason = tenant_unusable_reason(token_row)
    if unusable_reason is not None:
        logger.warning(
            "Agent request rejected, tenant not usable | token_id=%s customer_id=%s "
            "branch_id=%s reason=%s",
            token_row["id"], token_row["customer_id"], token_row["branch_id"], unusable_reason,
        )
        raise HTTPException(status_code=403, detail="Agent is not currently authorized to upload data")

    # An enrollment-issued token also dies with its installation. Same generic
    # 403 as the tenant gate; the reason is only logged.
    installation_reason = bound_installation_unusable_reason(token_row)
    if installation_reason is not None:
        logger.warning(
            "Agent request rejected, bound installation not usable | token_id=%s customer_id=%s "
            "branch_id=%s reason=%s",
            token_row["id"], token_row["customer_id"], token_row["branch_id"], installation_reason,
        )
        raise HTTPException(status_code=403, detail="Agent is not currently authorized to upload data")

    conn.execute(
        text("""
            UPDATE agent_tokens
            SET last_used_at = CURRENT_TIMESTAMP
            WHERE id = :id
        """),
        {"id": token_row["id"]},
    )

    return token_row


# --- collector installation lifecycle linkage ------------------------------
#
# Runs AFTER authenticate_agent, inside the same transaction as the
# pipeline_status upsert, and only when the heartbeat carries an explicit
# installation_id. The installation must be exactly that row AND belong to the
# tenant the token resolves to through the same OPERATIONAL bridge
# authenticate_agent uses (never SaaS ids, never hostname, never "some
# installation of this branch"):
#   ci.id = :installation_id
#   organizations.operational_customer_id = token's customer_id, ci.organization_id = o.id
#   branches.operational_branch_id        = token's branch_id,   ci.branch_id       = b.id
#   and the branch must belong to that organization.
#
# Fail closed: an unknown/foreign/inactive/retired installation rejects the
# WHOLE heartbeat with one generic 403 (the specific reason is only logged), so
# the transaction rolls back and neither pipeline_status nor any installation
# state changes -- consistent with /upload's "authenticated but not authorized"
# 403 for an unusable tenant. The 403 is identical whether the installation does
# not exist, belongs to someone else, or is inactive/retired, so it discloses
# nothing about which installations or tenants exist.
#
# Lifecycle semantics. A "confirmed installation contact" is any successful,
# authenticated /upload-pipeline-status request carrying the installation_id of
# a provisioning/active installation of the authenticated customer/branch. The
# server cannot (and does not try to) tell WHICH Collector operation sent it: a
# scheduled run's status heartbeat and the install-time preflight probe are both
# confirmed contact, so activation does NOT wait for the Scheduled Task's first
# normal run.
#   installed_at = the FIRST such contact (stamped once, never re-stamped; if an
#                  admin sets the installation active first, that admin change
#                  stamped it instead -- see tenant_service.update_collector_installation)
#   last_seen_at = the MOST RECENT such contact
#   status       = provisioning -> active on the first such contact
INSTALLATION_NOT_AUTHORIZED_DETAIL = "Collector installation is not authorized to report status"
# Heartbeats may only ever keep a live installation alive. inactive/retired are
# deliberate admin decisions a heartbeat must never undo.
INSTALLATION_HEARTBEAT_STATUSES = ("provisioning", "active")

_INSTALLATION_LOOKUP_SQL = """
    SELECT ci.id, ci.status
    FROM collector_installations ci
    JOIN organizations o
      ON o.id = ci.organization_id
    JOIN branches b
      ON b.id = ci.branch_id
     AND b.organization_id = o.id
    WHERE ci.id = :installation_id
      AND o.operational_customer_id = :customer_id
      AND b.operational_branch_id = :branch_id
"""

# The status guard in the WHERE clause (not just the Python check above) keeps
# an admin's concurrent inactive/retired change from being overwritten between
# the lookup and this write. status is always 'active' afterwards: this only
# ever matches provisioning (-> active) or active (unchanged).
_INSTALLATION_HEARTBEAT_SQL = """
    UPDATE collector_installations
    SET status = 'active',
        installed_at = COALESCE(installed_at, CURRENT_TIMESTAMP),
        last_seen_at = CURRENT_TIMESTAMP,
        collector_version = COALESCE(:collector_version, collector_version),
        updated_at = CURRENT_TIMESTAMP
    WHERE id = :installation_id
      AND status IN ('provisioning', 'active')
"""


def _reject_installation(token_row, installation_id: int, reason: str) -> NoReturn:
    logger.warning(
        "Agent heartbeat rejected, installation not authorized | token_id=%s customer_id=%s "
        "branch_id=%s installation_id=%s reason=%s",
        token_row["id"], token_row["customer_id"], token_row["branch_id"], installation_id, reason,
    )
    raise HTTPException(status_code=403, detail=INSTALLATION_NOT_AUTHORIZED_DETAIL)


def record_installation_heartbeat(conn, token_row, installation_id: int, collector_version: str | None) -> None:
    """Validates that `installation_id` is exactly an installation of the
    authenticated tenant/branch and still provisioning/active, then records
    the confirmed contact (a scheduled-run heartbeat or a preflight probe --
    see "Lifecycle semantics" above): provisioning -> active, installed_at
    stamped once at the first such contact, last_seen_at and the
    runtime-reported collector_version updated.
    Raises the generic 403 otherwise, leaving the installation untouched."""
    version = (collector_version or "").strip() or None

    # A token issued by enrollment is bound to one installation and may not claim
    # another. A legacy token (installation_id NULL) is unaffected -- it may name
    # any installation of its own tenant/branch, exactly as before.
    # (token_row.installation_id was resolved by authenticate_agent's lookup.)
    bound_installation_id = token_row.get("installation_id")
    if bound_installation_id is not None and int(bound_installation_id) != int(installation_id):
        _reject_installation(
            token_row, installation_id, f"token is bound to installation {int(bound_installation_id)}"
        )

    installation = conn.execute(
        text(_INSTALLATION_LOOKUP_SQL),
        {
            "installation_id": installation_id,
            "customer_id": token_row["customer_id"],
            "branch_id": token_row["branch_id"],
        },
    ).mappings().first()

    if installation is None:
        _reject_installation(
            token_row, installation_id,
            "no such installation for the authenticated organization/branch",
        )
    elif installation["status"] not in INSTALLATION_HEARTBEAT_STATUSES:
        _reject_installation(token_row, installation_id, f"installation status {installation['status']!r}")

    result = conn.execute(
        text(_INSTALLATION_HEARTBEAT_SQL),
        {"installation_id": installation_id, "collector_version": version},
    )
    if result.rowcount != 1:
        _reject_installation(token_row, installation_id, "installation status changed during the heartbeat")


@app.get("/")
def root():
    return {"status": "SortView API running"}


@app.post("/upload")
@limiter.limit(UPLOAD_RATE_LIMIT, key_func=get_agent_rate_limit_key)
def upload(request: Request, data: UploadRequest, authorization: str | None = Header(default=None)):
    try:
        checkins = [row.model_dump() for row in data.checkins]
        rejects = [row.model_dump() for row in data.rejects]
        acs = [row.model_dump() for row in data.acs]

        all_rows = []
        all_rows.extend(checkins)
        all_rows.extend(rejects)
        all_rows.extend(acs)

        if not all_rows:
            raise HTTPException(status_code=400, detail="No upload rows provided")

        first_customer_id = all_rows[0]["customer_id"]
        first_branch_id = all_rows[0]["branch_id"]

        for row in all_rows:
            if row["customer_id"] != first_customer_id or row["branch_id"] != first_branch_id:
                raise HTTPException(
                    status_code=400,
                    detail="All uploaded rows must have the same customer_id and branch_id"
                )

        inserted_checkins = 0
        inserted_rejects = 0
        inserted_acs = 0

        with engine.begin() as conn:
            authenticate_agent(
                conn=conn,
                authorization=authorization,
                customer_id=first_customer_id,
                branch_id=first_branch_id,
            )

            # Phase E: ON CONFLICT DO NOTHING below is deliberately BARE
            # (no conflict target) on all three tables now, not just
            # acs_events (which already worked this way). A bare DO
            # NOTHING suppresses a violation of ANY unique/exclusion
            # constraint on the table, not just one named index -- so one
            # INSERT now transparently absorbs BOTH the pre-existing
            # semantic unique index (barcode/event_time[/error_message]
            # etc. -- unaffected, still authoritative) AND the new
            # source_event_id partial unique index (45ba2e7befbc), with
            # no way to tell from the SQL alone which one fired. That's
            # intentional: see the Phase E report's "double protection"
            # section for why callers only need "was a new row inserted,"
            # never which constraint stopped a duplicate. A legacy row
            # (source_event_id always NULL) is invisible to the partial
            # index entirely, so this is a no-op behavior change for the
            # currently deployed legacy scheduled agent.
            for row in checkins:
                result = conn.execute(text("""
                    INSERT INTO checkins (
                        customer_id, branch_id, event_time, title, barcode,
                        collection_code, call_number, shelf_code,
                        destination, bin, is_problem, message,
                        flag_1, flag_2, flag_3, source_file, source_event_id
                    )
                    VALUES (
                        :customer_id, :branch_id, :event_time, :title, :barcode,
                        :collection_code, :call_number, :shelf_code,
                        :destination, :bin, :is_problem, :message,
                        :flag_1, :flag_2, :flag_3, :source_file, :source_event_id
                    )
                    ON CONFLICT DO NOTHING
                """), row)

                inserted_checkins += result.rowcount

            for row in rejects:
                reject_row = {
                    "customer_id": row["customer_id"],
                    "branch_id": row["branch_id"],
                    "event_time": row["event_time"],
                    "barcode": row["barcode"] or "",
                    "error_message": row["message"],
                    "source_file": row["source_file"],
                    "source_event_id": row["source_event_id"],
                }

                result = conn.execute(text("""
                    INSERT INTO rejects (
                        customer_id, branch_id, event_time,
                        barcode, error_message, source_file, source_event_id
                    )
                    VALUES (
                        :customer_id, :branch_id, :event_time,
                        :barcode, :error_message, :source_file, :source_event_id
                    )
                    ON CONFLICT DO NOTHING
                """), reject_row)

                inserted_rejects += result.rowcount

            for row in acs:
                acs_row = {
                    "customer_id": row["customer_id"],
                    "branch_id": row["branch_id"],
                    "event_time": row["event_time"],
                    "message_code": row["message_code"],
                    "barcode": row["barcode"],
                    "barcode_key": row["barcode"] or "",
                    "title": row["title"],
                    "patron_id": row["patron_id"],
                    "destination": row["destination"],
                    "raw_message": row["raw_message"],
                    "source_file": row["source_file"],
                    "source_event_id": row["source_event_id"],
                }

                result = conn.execute(text("""
                    INSERT INTO acs_events (
                        customer_id, branch_id, event_time,
                        message_code, barcode, barcode_key, title,
                        patron_id, destination, raw_message, source_file,
                        source_event_id
                    )
                    VALUES (
                        :customer_id, :branch_id, :event_time,
                        :message_code, :barcode, :barcode_key, :title,
                        :patron_id, :destination, :raw_message, :source_file,
                        :source_event_id
                    )
                    ON CONFLICT DO NOTHING
                """), acs_row)

                inserted_acs += result.rowcount

        return {
            "status": "success",
            "checkins_received": len(checkins),
            "rejects_received": len(rejects),
            "acs_received": len(acs),
            "checkins_inserted": inserted_checkins,
            "rejects_inserted": inserted_rejects,
            "acs_inserted": inserted_acs
        }

    except HTTPException:
        raise
    except Exception as exc:
        # Not logger.exception: the exception text of a database error quotes the failing row.
        log_safe_exception(logger, "Upload failed", exc)
        sentry_sdk.capture_exception(exc)
        raise HTTPException(
    status_code=500,
    detail="Internal server error",
    )


@app.post("/upload-pipeline-status")
@limiter.limit(UPLOAD_RATE_LIMIT)
def upload_pipeline_status(request: Request, data: PipelineStatusRequest, authorization: str | None = Header(default=None)):
    try:
        with engine.begin() as conn:
            token_row = authenticate_agent(
                conn=conn,
                authorization=authorization,
                customer_id=data.customer_id,
                branch_id=data.branch_id,
            )

            # Only a heartbeat that explicitly names an installation is linked
            # to one; a legacy heartbeat (no installation_id) never touches
            # collector_installations. Same transaction as the upsert below:
            # a rejection here rolls back the entire heartbeat.
            if data.installation_id is not None:
                record_installation_heartbeat(
                    conn, token_row, data.installation_id, data.collector_version
                )

            sql, params = _build_pipeline_status_upsert(data)
            conn.execute(text(sql), params)

        return {
            "status": "success",
            "message": "Pipeline status uploaded"
        }

    except HTTPException:
        raise
    except Exception as exc:
        log_safe_exception(logger, "Pipeline status upload failed", exc)
        sentry_sdk.capture_exception(exc)
        raise HTTPException(
    status_code=500,
    detail="Internal server error",
    )


@app.post("/collector/enroll")
@limiter.limit(ENROLL_RATE_LIMIT)
def collector_enroll(request: Request, data: CollectorEnrollRequest):
    """Redeems a one-time enrollment code for a long-lived agent token. Takes no
    token: possession of an unexpired, unused code is the credential. One
    transaction covers locking the code, validating the tenant, marking the code
    used and inserting the token, so a failure anywhere leaves the code unused
    and no token created. Every refusal is the same generic 400; the specific
    reason is logged (never the code or a token) for operators."""
    try:
        with engine.begin() as conn:
            result = redeem_enrollment_code(
                conn,
                data.enrollment_code.get_secret_value(),
                hostname=data.hostname,
                collector_version=data.collector_version,
            )
    except EnrollmentError as exc:
        logger.warning(
            "Collector enrollment rejected | reason=%s client=%s",
            exc.reason, get_remote_address(request),
        )
        raise HTTPException(status_code=400, detail=PUBLIC_ENROLLMENT_ERROR) from None
    except Exception as exc:
        log_safe_exception(logger, "Collector enrollment failed", exc)
        sentry_sdk.capture_exception(exc)
        raise HTTPException(status_code=500, detail="Internal server error") from None

    # The body carries a credential: never cacheable.
    return JSONResponse(content=result, headers={"Cache-Control": "no-store"})


#***************************************************************
# Privacy Contract v2 (docs/contract-v2-design.md)
#
# POST /v2/upload (events) and POST /v2/status (heartbeat) run ALONGSIDE the v1 routes above, which are unchanged. They are
# dark unless SORTVIEW_V2_INGEST_ENABLED=true, and even then a request needs an ACTIVE key_id registered to the token's own
# tenant. The tenant comes only from the token; a payload has no tenant field to override it with.
#***************************************************************

V2_INGEST_ENABLED = os.getenv("SORTVIEW_V2_INGEST_ENABLED", "false").strip().lower() == "true"

# 1000 events at roughly 300 bytes each is about 300 KB; a heartbeat is a few hundred bytes. Both guards below cover
# a declared Content-Length AND a chunked request that declares none.
V2_UPLOAD_MAX_BODY_BYTES = 1024 * 1024
V2_STATUS_MAX_BODY_BYTES = 16 * 1024

V2_KEY_NOT_AUTHORIZED_DETAIL = "Ingest key is not authorized"


async def _read_bounded_body(request: Request, limit: int) -> bytes:
    """Reads the request body, refusing (413) as soon as it exceeds `limit` -- whether the client declared a size or
    streamed chunks with none. The v1 MaxBodySizeMiddleware trusts Content-Length alone; a chunked body slips past it."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from None
        if declared_size < 0 or declared_size > limit:
            raise HTTPException(status_code=413, detail="Request body too large")

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(status_code=413, detail="Request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _replay_receive(body: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _v2_route_class(limit_of: Callable[[], int]) -> type[APIRoute]:
    """A route class for the v2 endpoints: (1) 404 unless the feature flag is on -- before anything is read; (2) a bounded
    body read; (3) the body handed on to FastAPI's normal validation, so a bad request takes the ONE hardened 422 path."""

    class V2BoundedRoute(APIRoute):
        def get_route_handler(self) -> Callable:
            original = super().get_route_handler()

            async def handler(request: Request) -> Response:
                if not V2_INGEST_ENABLED:
                    raise HTTPException(status_code=404, detail="Not Found")
                body = await _read_bounded_body(request, limit_of())
                return await original(Request(request.scope, _replay_receive(body)))

            return handler

    return V2BoundedRoute


def _require_ingest_key(conn, token_row, key_id: str) -> None:
    """The request's key must be registered, ACTIVE, and the token's own tenant's. Unknown, retired and wrong-tenant keys
    get the same generic 403; the reason (and the validated, non-secret key_id) is only logged."""
    problem = ingest_key_problem(conn, token_row["customer_id"], token_row["branch_id"], key_id)
    if problem is not None:
        logger.warning(
            "Agent request rejected, ingest key not usable | token_id=%s customer_id=%s branch_id=%s key_id=%s reason=%s",
            token_row["id"], token_row["customer_id"], token_row["branch_id"], key_id, problem,
        )
        raise HTTPException(status_code=403, detail=V2_KEY_NOT_AUTHORIZED_DETAIL)


v2_upload_router = APIRouter(route_class=_v2_route_class(lambda: V2_UPLOAD_MAX_BODY_BYTES))
v2_status_router = APIRouter(route_class=_v2_route_class(lambda: V2_STATUS_MAX_BODY_BYTES))


@v2_upload_router.post("/v2/upload")
@limiter.limit(UPLOAD_RATE_LIMIT, key_func=get_agent_rate_limit_key)
def upload_v2(request: Request, data: UploadV2Request, authorization: str | None = Header(default=None)):
    token_row = None
    try:
        if not (data.checkins or data.rejects or data.acs_holds):
            raise HTTPException(status_code=400, detail="No upload events provided")

        with engine.begin() as conn:
            token_row = authenticate_agent_token(conn, authorization)
            _require_ingest_key(conn, token_row, data.key_id)
            counts = store_events(
                conn,
                customer_id=int(token_row["customer_id"]),
                branch_id=int(token_row["branch_id"]),
                key_id=data.key_id,
                checkins=data.checkins,
                rejects=data.rejects,
                acs_holds=data.acs_holds,
            )

        return {"status": "success", "contract_version": 2, **counts}

    except HTTPException:
        raise
    except EventConflict as conflict:
        # Positions and counts only -- never an event key, a value or the request body.
        logger.warning(
            "V2 upload rejected, event conflict | token_id=%s customer_id=%s branch_id=%s key_id=%s conflicts=%s",
            token_row["id"] if token_row else None,
            token_row["customer_id"] if token_row else None,
            token_row["branch_id"] if token_row else None,
            data.key_id,
            {kind: len(positions) for kind, positions in conflict.conflicts.items()},
        )
        return JSONResponse(
            status_code=409,
            content={
                "code": "event_conflict",
                "detail": "An event identity arrived with different content; nothing was stored",
                "conflicts": conflict.conflicts,
            },
        )
    except Exception as exc:
        log_safe_exception(logger, "V2 upload failed", exc)
        sentry_sdk.capture_exception(exc)
        raise HTTPException(status_code=500, detail="Internal server error") from None


@v2_status_router.post("/v2/status")
@limiter.limit(UPLOAD_RATE_LIMIT, key_func=get_agent_rate_limit_key)
def status_v2(request: Request, data: StatusV2Request, authorization: str | None = Header(default=None)):
    try:
        with engine.begin() as conn:
            token_row = authenticate_agent_token(conn, authorization)
            _require_ingest_key(conn, token_row, data.key_id)
            stored = record_heartbeat(
                conn, customer_id=int(token_row["customer_id"]), branch_id=int(token_row["branch_id"]), data=data
            )
            if not stored:  # the key was retired between the check and the write
                raise HTTPException(status_code=403, detail=V2_KEY_NOT_AUTHORIZED_DETAIL)

        return {"status": "success", "contract_version": 2}

    except HTTPException:
        raise
    except Exception as exc:
        log_safe_exception(logger, "V2 status upload failed", exc)
        sentry_sdk.capture_exception(exc)
        raise HTTPException(status_code=500, detail="Internal server error") from None


app.include_router(v2_upload_router)
app.include_router(v2_status_router)
