import hashlib
import json
import logging
import os
import re

import sentry_sdk
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import create_engine, text
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("sortview.api")

SENTRY_DSN = os.getenv("SENTRY_DSN")
SENTRY_ENVIRONMENT = os.getenv("SENTRY_ENVIRONMENT", "development")

if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=SENTRY_ENVIRONMENT,
        send_default_pii=False,
        traces_sample_rate=0.0,
    )
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

app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

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
)


#***************************************************************
# Request Models
#
# Pydantic models for the agent upload payloads. These give the
# API automatic type validation, clear 422 error responses, and
# generated OpenAPI docs instead of accepting raw dicts.
#***************************************************************

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


class RejectRow(BaseModel):
    customer_id: int
    branch_id: int
    event_time: str | None = None
    barcode: str | None = None
    message: str | None = None
    source_file: str | None = None


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


class UploadRequest(BaseModel):
    checkins: list[CheckinRow] = Field(default_factory=list)
    rejects: list[RejectRow] = Field(default_factory=list)
    acs: list[AcsRow] = Field(default_factory=list)


class PipelineStatusRequest(BaseModel):
    customer_id: int
    branch_id: int
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
    destination_breakdown: dict = Field(default_factory=dict)


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


def authenticate_agent(conn, authorization: str | None, customer_id: int, branch_id: int):
    bearer_token = get_bearer_token(authorization)

    token_row = conn.execute(
        text("""
            SELECT
                id,
                customer_id,
                branch_id,
                is_active,
                description
            FROM agent_tokens
            WHERE token_hash = encode(digest(:token, 'sha256'), 'hex')
            LIMIT 1
        """),
        {"token": bearer_token},
    ).mappings().first()

    if token_row is None:
        raise HTTPException(status_code=401, detail="Invalid agent token")

    if not token_row["is_active"]:
        raise HTTPException(status_code=403, detail="Agent token is inactive")

    if int(token_row["customer_id"]) != int(customer_id) or int(token_row["branch_id"]) != int(branch_id):
        raise HTTPException(status_code=403, detail="Token scope does not match customer_id / branch_id")

    conn.execute(
        text("""
            UPDATE agent_tokens
            SET last_used_at = CURRENT_TIMESTAMP
            WHERE id = :id
        """),
        {"id": token_row["id"]},
    )

    return token_row


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

            for row in checkins:
                result = conn.execute(text("""
                    INSERT INTO checkins (
                        customer_id, branch_id, event_time, title, barcode,
                        collection_code, call_number, shelf_code,
                        destination, bin, is_problem, message,
                        flag_1, flag_2, flag_3, source_file
                    )
                    VALUES (
                        :customer_id, :branch_id, :event_time, :title, :barcode,
                        :collection_code, :call_number, :shelf_code,
                        :destination, :bin, :is_problem, :message,
                        :flag_1, :flag_2, :flag_3, :source_file
                    )
                    ON CONFLICT (barcode, event_time) DO NOTHING
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
                }

                result = conn.execute(text("""
                    INSERT INTO rejects (
                        customer_id, branch_id, event_time,
                        barcode, error_message, source_file
                    )
                    VALUES (
                        :customer_id, :branch_id, :event_time,
                        :barcode, :error_message, :source_file
                    )
                    ON CONFLICT (barcode, event_time, error_message) DO NOTHING
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
                }

                result = conn.execute(text("""
                    INSERT INTO acs_events (
                        customer_id, branch_id, event_time,
                        message_code, barcode, barcode_key, title,
                        patron_id, destination, raw_message, source_file
                    )
                    VALUES (
                        :customer_id, :branch_id, :event_time,
                        :message_code, :barcode, :barcode_key, :title,
                        :patron_id, :destination, :raw_message, :source_file
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
        logger.exception("Upload failed")
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
            authenticate_agent(
                conn=conn,
                authorization=authorization,
                customer_id=data.customer_id,
                branch_id=data.branch_id,
            )

            conn.execute(text("""
                INSERT INTO pipeline_status (
                    customer_id,
                    branch_id,
                    last_attempt,
                    last_run,
                    status,
                    checkins_rows,
                    rejects_rows,
                    acs_rows,
                    uploaded_checkins_rows,
                    uploaded_rejects_rows,
                    uploaded_acs_rows,
                    checkins_bad_datetime_rows,
                    rejects_bad_datetime_rows,
                    acs_bad_datetime_rows,
                    transit_items,
                    problem_items,
                    destination_breakdown,
                    updated_at
                )
                VALUES (
                    :customer_id,
                    :branch_id,
                    :last_attempt,
                    :last_run,
                    :status,
                    :checkins_rows,
                    :rejects_rows,
                    :acs_rows,
                    :uploaded_checkins_rows,
                    :uploaded_rejects_rows,
                    :uploaded_acs_rows,
                    :checkins_bad_datetime_rows,
                    :rejects_bad_datetime_rows,
                    :acs_bad_datetime_rows,
                    :transit_items,
                    :problem_items,
                    CAST(:destination_breakdown AS JSONB),
                    CURRENT_TIMESTAMP
                )
                ON CONFLICT (customer_id, branch_id)
                DO UPDATE SET
                    last_attempt = EXCLUDED.last_attempt,
                    last_run = EXCLUDED.last_run,
                    status = EXCLUDED.status,
                    checkins_rows = EXCLUDED.checkins_rows,
                    rejects_rows = EXCLUDED.rejects_rows,
                    acs_rows = EXCLUDED.acs_rows,
                    uploaded_checkins_rows = EXCLUDED.uploaded_checkins_rows,
                    uploaded_rejects_rows = EXCLUDED.uploaded_rejects_rows,
                    uploaded_acs_rows = EXCLUDED.uploaded_acs_rows,
                    checkins_bad_datetime_rows = EXCLUDED.checkins_bad_datetime_rows,
                    rejects_bad_datetime_rows = EXCLUDED.rejects_bad_datetime_rows,
                    acs_bad_datetime_rows = EXCLUDED.acs_bad_datetime_rows,
                    transit_items = EXCLUDED.transit_items,
                    problem_items = EXCLUDED.problem_items,
                    destination_breakdown = EXCLUDED.destination_breakdown,
                    updated_at = CURRENT_TIMESTAMP
            """), {
                "customer_id": data.customer_id,
                "branch_id": data.branch_id,
                "last_attempt": data.last_attempt,
                "last_run": data.last_run,
                "status": data.status,
                "checkins_rows": data.checkins_rows,
                "rejects_rows": data.rejects_rows,
                "acs_rows": data.acs_rows,
                "uploaded_checkins_rows": data.uploaded_checkins_rows,
                "uploaded_rejects_rows": data.uploaded_rejects_rows,
                "uploaded_acs_rows": data.uploaded_acs_rows,
                "checkins_bad_datetime_rows": data.checkins_bad_datetime_rows,
                "rejects_bad_datetime_rows": data.rejects_bad_datetime_rows,
                "acs_bad_datetime_rows": data.acs_bad_datetime_rows,
                "transit_items": data.transit_items,
                "problem_items": data.problem_items,
                "destination_breakdown": json.dumps(data.destination_breakdown),
            })

        return {
            "status": "success",
            "message": "Pipeline status uploaded"
        }

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Pipeline status upload failed")
        sentry_sdk.capture_exception(exc)
        raise HTTPException(
    status_code=500,
    detail="Internal server error",
    )
