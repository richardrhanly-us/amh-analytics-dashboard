import math
import os

import pandas as pd
import requests
from config import load_config
from urllib3.util.retry import Retry

from logger_config import get_logger

logger = get_logger("uploader")
config = load_config()

API_URL = config["api_url"].rstrip("/")
API_TOKEN = config["api_token"]

CUSTOMER_ID = config["customer_id"]
BRANCH_ID = config["branch_id"]

CONNECT_TIMEOUT = int(os.getenv("SORTVIEW_HTTP_CONNECT_TIMEOUT", "10"))
UPLOAD_READ_TIMEOUT = int(os.getenv("SORTVIEW_HTTP_UPLOAD_READ_TIMEOUT", "300"))
STATUS_READ_TIMEOUT = int(os.getenv("SORTVIEW_HTTP_STATUS_READ_TIMEOUT", "60"))
MAX_RECORDS_PER_REQUEST = max(1, int(os.getenv("SORTVIEW_MAX_RECORDS_PER_REQUEST", "1000")))
MAX_LOG_RESPONSE_CHARS = max(200, int(os.getenv("SORTVIEW_MAX_LOG_RESPONSE_CHARS", "500")))
HTTP_RETRY_TOTAL = max(0, int(os.getenv("SORTVIEW_HTTP_RETRY_TOTAL", "3")))
HTTP_RETRY_BACKOFF_FACTOR = float(os.getenv("SORTVIEW_HTTP_RETRY_BACKOFF_FACTOR", "1.0"))
HTTP_RETRY_STATUS_CODES = (429, 500, 502, 503, 504)

retry_strategy = Retry(
    total=HTTP_RETRY_TOTAL,
    connect=HTTP_RETRY_TOTAL,
    read=HTTP_RETRY_TOTAL,
    status=HTTP_RETRY_TOTAL,
    backoff_factor=HTTP_RETRY_BACKOFF_FACTOR,
    status_forcelist=HTTP_RETRY_STATUS_CODES,
    allowed_methods=frozenset(["POST"]),
    raise_on_status=False,
)

session = requests.Session()
adapter = requests.adapters.HTTPAdapter(
    pool_connections=5,
    pool_maxsize=5,
    max_retries=retry_strategy,
)
session.mount("https://", adapter)
session.mount("http://", adapter)

def auth_headers():
    return {
        "Authorization": f"Bearer {API_TOKEN}",
        "Content-Type": "application/json",
    }


def make_json_safe(value):
    if value is None:
        return None

    # pd.isna raises on some array-like inputs; every ordinary scalar
    # (str, int, etc.) falls through here, so this guards a duck-type
    # check rather than a real fault.
    try:
        if pd.isna(value):
            return None
    except Exception:  # nosec B110
        pass

    # Same rationale: logging here would fire on every non-NaN-checkable value.
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
    except Exception:  # nosec B110
        pass

    if isinstance(value, (str, int, float, bool)):
        return value

    return str(value)


def _truncate_response_text(text):
    if text is None:
        return ""

    text = str(text)
    if len(text) <= MAX_LOG_RESPONSE_CHARS:
        return text

    return text[:MAX_LOG_RESPONSE_CHARS] + "...<truncated>"


def _post_json(endpoint, payload, timeout, purpose):
    url = f"{API_URL}{endpoint}"

    try:
        response = session.post(
            url,
            json=payload,
            headers=auth_headers(),
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.exception(
            "HTTP request failed | purpose=%s url=%s",
            purpose,
            url,
        )
        raise RuntimeError(f"{purpose} request failed: {exc}") from exc

    if not response.ok:
        response_preview = _truncate_response_text(response.text)
        logger.error(
            "HTTP request returned error | purpose=%s url=%s status=%s response=%s",
            purpose,
            url,
            response.status_code,
            response_preview,
        )
        raise RuntimeError(
            f"{purpose} failed with status {response.status_code}: {response_preview}"
        )

    try:
        result = response.json()
    except ValueError as exc:
        response_preview = _truncate_response_text(response.text)
        logger.error(
            "HTTP response was not valid JSON | purpose=%s url=%s status=%s response=%s",
            purpose,
            url,
            response.status_code,
            response_preview,
        )
        raise RuntimeError(
            f"{purpose} returned non-JSON response: {response_preview}"
        ) from exc

    logger.info(
        "HTTP request complete | purpose=%s url=%s status=%s",
        purpose,
        url,
        response.status_code,
    )
    return result


def _slice_batch(records, batch_index, batch_size):
    start = batch_index * batch_size
    end = start + batch_size
    return records[start:end]


def _iter_upload_payloads(checkins_records, rejects_records, acs_records, batch_size):
    total_batches = max(
        math.ceil(len(checkins_records) / batch_size) if checkins_records else 0,
        math.ceil(len(rejects_records) / batch_size) if rejects_records else 0,
        math.ceil(len(acs_records) / batch_size) if acs_records else 0,
    )

    if total_batches == 0:
        yield {
            "checkins": [],
            "rejects": [],
            "acs": [],
        }
        return

    for batch_index in range(total_batches):
        payload = {
            "checkins": _slice_batch(checkins_records, batch_index, batch_size),
            "rejects": _slice_batch(rejects_records, batch_index, batch_size),
            "acs": _slice_batch(acs_records, batch_index, batch_size),
        }

        if payload["checkins"] or payload["rejects"] or payload["acs"]:
            yield payload


def build_checkins_payload(df):
    records = df.to_dict(orient="records")
    safe_records = []

    for row in records:
        safe_records.append({
            "customer_id": CUSTOMER_ID,
            "branch_id": BRANCH_ID,
            "event_time": make_json_safe(row.get("datetime")),
            "title": make_json_safe(row.get("title")),
            "barcode": make_json_safe(row.get("barcode")),
            "collection_code": make_json_safe(row.get("collection_code")),
            "call_number": make_json_safe(row.get("call_number")),
            "shelf_code": make_json_safe(row.get("shelf_code")),
            "destination": make_json_safe(row.get("destination")),
            "bin": make_json_safe(row.get("bin")),
            "is_problem": make_json_safe(row.get("is_problem")),
            "message": make_json_safe(row.get("message")),
            "flag_1": make_json_safe(row.get("flag_1")),
            "flag_2": make_json_safe(row.get("flag_2")),
            "flag_3": make_json_safe(row.get("flag_3")),
            "source_file": "Checkins.txt",
        })

    return safe_records


def build_rejects_payload(df):
    records = df.to_dict(orient="records")
    safe_records = []

    for row in records:
        safe_records.append({
            "customer_id": CUSTOMER_ID,
            "branch_id": BRANCH_ID,
            "event_time": make_json_safe(row.get("datetime")),
            "barcode": make_json_safe(row.get("barcode")),
            "message": make_json_safe(row.get("error_message")),
            "source_file": "Rejects.txt",
        })

    return safe_records


def build_acs_payload(df):
    records = df.to_dict(orient="records")
    safe_records = []
    skipped_bad_datetime = 0

    for row in records:
        event_time = make_json_safe(row.get("datetime"))

        if event_time is None:
            skipped_bad_datetime += 1
            continue

        safe_records.append({
            "customer_id": CUSTOMER_ID,
            "branch_id": BRANCH_ID,
            "event_time": event_time,
            "message_code": make_json_safe(row.get("message_code")),
            "barcode": make_json_safe(row.get("barcode")),
            "title": make_json_safe(row.get("title")),
            "patron_id": make_json_safe(row.get("patron_id")),
            "destination": make_json_safe(row.get("destination")),
            "raw_message": make_json_safe(row.get("raw_message")),
            "source_file": "ACS Log.txt",
        })

    logger.info(
        "Built ACS payload | rows=%s skipped_bad_datetime=%s",
        len(safe_records),
        skipped_bad_datetime
    )

    return safe_records


def upload_checkins_and_rejects(checkins_df, rejects_df, acs_df):
    checkins_records = build_checkins_payload(checkins_df)
    rejects_records = build_rejects_payload(rejects_df)
    acs_records = build_acs_payload(acs_df)

    totals = {
        "uploaded_checkins": 0,
        "uploaded_rejects": 0,
        "uploaded_acs": 0,
    }

    batch_count = 0

    for batch_number, payload in enumerate(
        _iter_upload_payloads(
            checkins_records,
            rejects_records,
            acs_records,
            MAX_RECORDS_PER_REQUEST,
        ),
        start=1,
    ):
        batch_count += 1

        logger.info(
            "Uploading batch | batch_number=%s checkins=%s rejects=%s acs=%s",
            batch_number,
            len(payload["checkins"]),
            len(payload["rejects"]),
            len(payload["acs"]),
        )

        result = _post_json(
            endpoint="/upload",
            payload=payload,
            timeout=(CONNECT_TIMEOUT, UPLOAD_READ_TIMEOUT),
            purpose=f"upload batch {batch_number}",
        )

        totals["uploaded_checkins"] += int(result.get("checkins_inserted", 0))
        totals["uploaded_rejects"] += int(result.get("rejects_inserted", 0))
        totals["uploaded_acs"] += int(result.get("acs_inserted", 0))

        logger.info(
            "Batch upload complete | batch_number=%s inserted_checkins=%s inserted_rejects=%s inserted_acs=%s",
            batch_number,
            int(result.get("checkins_inserted", 0)),
            int(result.get("rejects_inserted", 0)),
            int(result.get("acs_inserted", 0)),
        )

    logger.info(
        "API upload complete | batches=%s uploaded_checkins=%s uploaded_rejects=%s uploaded_acs=%s",
        batch_count,
        totals["uploaded_checkins"],
        totals["uploaded_rejects"],
        totals["uploaded_acs"],
    )

    return totals


def upload_pipeline_status(status):
    try:
        payload = {
            "customer_id": CUSTOMER_ID,
            "branch_id": BRANCH_ID,
            "last_attempt": status.get("last_attempt"),
            "last_run": status.get("last_run"),
            "status": status.get("status"),

            "checkins_rows": status.get("checkins_rows"),
            "rejects_rows": status.get("rejects_rows"),
            "acs_rows": status.get("acs_rows"),

            "uploaded_checkins_rows": status.get("uploaded_checkins_rows"),
            "uploaded_rejects_rows": status.get("uploaded_rejects_rows"),
            "uploaded_acs_rows": status.get("uploaded_acs_rows"),

            "checkins_bad_datetime_rows": status.get("checkins_bad_datetime_rows"),
            "rejects_bad_datetime_rows": status.get("rejects_bad_datetime_rows"),
            "acs_bad_datetime_rows": status.get("acs_bad_datetime_rows"),

            "transit_items": status.get("transit_items"),
            "problem_items": status.get("problem_items"),
            "destination_breakdown": status.get("destination_breakdown", {}),
        }

        _post_json(
            endpoint="/upload-pipeline-status",
            payload=payload,
            timeout=(CONNECT_TIMEOUT, STATUS_READ_TIMEOUT),
            purpose="pipeline status upload",
        )

        logger.info(
            "Pipeline status uploaded via API | status=%s last_attempt=%s last_run=%s",
            status.get("status"),
            status.get("last_attempt"),
            status.get("last_run"),
        )
        return True

    except Exception as e:
        logger.error("Pipeline status upload failed: %s", e)
        return False
