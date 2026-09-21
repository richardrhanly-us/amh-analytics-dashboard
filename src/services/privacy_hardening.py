"""Server-side privacy containment helpers (security/privacy Step 2).

The v1 Collector contract still sends `patron_id` and `raw_message`, and the API still accepts and
stores them until Contract v2 and the collector cutover (later steps). Until then this module keeps the
server from creating SECONDARY exposure paths for whatever a request carries:

  * scrub_sentry_event / build_sentry_options -- what may reach the error tracker;
  * safe_exception_summary / log_safe_exception -- what an exception may write to the application log;
  * install_streamlit_log_scrubber -- what Streamlit's own log of an UNCAUGHT page exception may contain (and, through
    enforce_streamlit_error_details, that the browser sees only the generic error whatever the host configures);
  * safe_validation_errors / safe_validation_body -- what a 422 response may say back to the caller;
  * api_docs_kwargs -- whether the API publishes its own schema.

Deliberately framework-free (standard library only) so each behaviour is unit-testable without FastAPI,
Sentry or a database. Nothing here removes data from the database or changes what the API accepts.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sysconfig
import threading
import traceback
from collections.abc import Mapping, Sequence
from typing import Any

FILTERED = "[Filtered]"
DB_ERROR_REDACTED = "[Filtered: database error text can contain row values]"

# --- what counts as sensitive -----------------------------------------------------------------------------------

# Keys are compared normalised (lower-case, letters and digits only), so `patron_id`, `patronId`, `Patron-ID`
# and `PATRON_ID` are the same key.
_SENSITIVE_EXACT = frozenset({
    # patron / raw record content (the v1 ACS contract)
    "patronid", "patronname", "patrontype", "rawmessage", "rawline", "barcode", "barcodekey", "title",
    # credentials and secrets
    "authorization", "proxyauthorization", "cookie", "cookies", "setcookie", "xapikey", "apikey", "bearer",
    "enrollmentcode", "databaseurl", "dsn", "sentrydsn", "password", "passwd", "secret",
    # bound SQL parameters recorded by observability tooling
    "dbparams", "parameters",
})
_SENSITIVE_SUFFIXES = ("token", "password", "secret", "apikey", "authorization", "cookie", "databaseurl")

_NON_ALNUM = re.compile(r"[^a-z0-9]")

# credentials embedded in a URL (postgresql://user:password@host/db), a Sentry DSN key, a bearer token
_URL_CREDENTIALS = re.compile(r"(://)[^/\s:@]+:[^/\s@]+@")
_DSN_KEY = re.compile(r"(https?://)[0-9a-fA-F]{16,}@")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=\-]{8,}")
# `name=value` pairs (query strings, "key=value" text) whose NAME says the value is sensitive
_SENSITIVE_PAIR = re.compile(r"(?i)\b([\w.\-]*(?:token|password|secret|api[_-]?key|patron[_-]?id|raw[_-]?message|barcode))=([^&\s]+)")

_DB_MODULES = frozenset({"sqlalchemy", "psycopg2", "psycopg", "asyncpg", "sqlite3", "pg8000"})
_DB_TYPE_NAMES = frozenset({
    "DBAPIError", "StatementError", "OperationalError", "IntegrityError", "DataError", "ProgrammingError",
    "InternalError", "DatabaseError", "NotSupportedError", "InterfaceError",
})

_MAX_DEPTH = 40


def _is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalised = _NON_ALNUM.sub("", key.lower())
    return normalised in _SENSITIVE_EXACT or normalised.endswith(_SENSITIVE_SUFFIXES)


def _scrub_text(text: str) -> str:
    text = _URL_CREDENTIALS.sub(r"\1" + FILTERED + "@", text)
    text = _DSN_KEY.sub(r"\1" + FILTERED + "@", text)
    text = _BEARER.sub(r"\1 " + FILTERED, text)
    return _SENSITIVE_PAIR.sub(r"\1=" + FILTERED, text)


def _scrub(value: Any, key: object = None, depth: int = 0) -> Any:
    if _is_sensitive_key(key):
        return FILTERED
    if depth > _MAX_DEPTH:
        return FILTERED
    if isinstance(value, Mapping):
        return {k: _scrub(v, k, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, None, depth + 1) for v in value]
    if isinstance(value, str):
        return _scrub_text(value)
    return value


# --- Sentry -------------------------------------------------------------------------------------------------------

def _is_database_exception(exception_value: Mapping[str, Any]) -> bool:
    module = str(exception_value.get("module") or "").split(".")[0]
    return module in _DB_MODULES or str(exception_value.get("type") or "") in _DB_TYPE_NAMES


def scrub_sentry_event(event: dict[str, Any], hint: Any = None) -> dict[str, Any]:
    """`before_send` hook: returns the event with every sensitive value replaced, never dropping it (the error
    is still reported, with its type, location and stack).

      * values under sensitive keys (patron_id, raw_message, barcode, Authorization, tokens, database URLs,
        cookies, bound SQL parameters) are filtered anywhere in the event;
      * the request body is always filtered (an upload body IS patron data under the v1 contract);
      * credentials embedded in any string (postgresql://user:pw@..., Bearer ..., DSN keys) are filtered;
      * the message of a database error is replaced -- DBAPI errors quote the failing row and the offending value;
      * stack-frame local variables are removed.
    """
    scrubbed: dict[str, Any] = _scrub(event)

    request = scrubbed.get("request")
    if isinstance(request, dict) and "data" in request:
        request["data"] = FILTERED

    exception = scrubbed.get("exception")
    if isinstance(exception, dict):
        for item in exception.get("values") or []:
            if not isinstance(item, dict):
                continue
            if _is_database_exception(item):
                item["value"] = DB_ERROR_REDACTED
            stacktrace = item.get("stacktrace")
            if isinstance(stacktrace, dict):
                for frame in stacktrace.get("frames") or []:
                    if isinstance(frame, dict):
                        frame.pop("vars", None)
    return scrubbed


def scrub_sentry_breadcrumb(crumb: dict[str, Any], hint: Any = None) -> dict[str, Any]:
    """`before_breadcrumb` hook: the same scrubbing, applied before a breadcrumb is even stored."""
    scrubbed: dict[str, Any] = _scrub(crumb)
    return scrubbed


def build_sentry_options(dsn: str, environment: str) -> dict[str, Any]:
    """The complete `sentry_sdk.init(...)` options for the API. Every privacy-relevant switch is explicit here
    (rather than left to an SDK default that can change):

      send_default_pii=False           no user/IP/cookie data;
      include_local_variables=False    exception frames carry no local variables (they hold whole upload rows);
      max_request_body_size="never"    the SDK otherwise attaches request bodies (up to 10 KB) to every event,
                                       independent of send_default_pii -- a v1 upload body is patron data;
      before_send / before_breadcrumb  scrub_sentry_event / scrub_sentry_breadcrumb (defence in depth).
    """
    return {
        "dsn": dsn,
        "environment": environment,
        "send_default_pii": False,
        "include_local_variables": False,
        "max_request_body_size": "never",
        "traces_sample_rate": 0.0,
        "before_send": scrub_sentry_event,
        "before_breadcrumb": scrub_sentry_breadcrumb,
    }


# --- application log ----------------------------------------------------------------------------------------------

_SQLSTATE = re.compile(r"^[0-9A-Z]{5}$")


def _sqlstate(exc: BaseException) -> str | None:
    for candidate in (exc, getattr(exc, "orig", None), exc.__cause__):
        for attribute in ("pgcode", "sqlstate"):
            code = getattr(candidate, attribute, None)
            if isinstance(code, str) and _SQLSTATE.match(code):
                return code
    return None


_STDLIB_DIR = sysconfig.get_paths().get("stdlib", "").replace("\\", "/")


def _is_application_frame(filename: str) -> bool:
    """A frame from SortView's own code, as opposed to a library (site-packages), the standard library, or generated code."""
    path = filename.replace("\\", "/")
    if path.startswith("<") or "/site-packages/" in path or "/dist-packages/" in path:
        return False
    return not (_STDLIB_DIR and path.startswith(_STDLIB_DIR + "/"))


def _frame_text(frame: traceback.FrameSummary) -> str:
    return f"{os.path.basename(frame.filename)}:{frame.lineno}:{frame.name}"


def safe_exception_summary(exc: BaseException, *, max_frames: int = 5, app_frames: int = 5) -> str:
    """Type, SQLSTATE and code location of an exception -- NEVER its message. Database drivers put the failing
    row and the offending value in the message (`DETAIL: Failing row contains (...)`, `invalid input syntax for
    type timestamp: "<value>"`), and SQLAlchemy's `hide_parameters` only hides its own `[parameters: ...]` block,
    not the driver's text. So the message is not logged at all.

    `at=` is the innermost `max_frames` frames. A database failure is usually 10+ SQLAlchemy/driver frames deep, so
    those alone would never name the SortView code that made the call: `app=` therefore adds up to `app_frames` of
    SortView's OWN frames (not site-packages, not the standard library) from further out, innermost first."""
    parts = [f"error_type={type(exc).__module__}.{type(exc).__qualname__}"]
    state = _sqlstate(exc)
    if state:
        parts.append(f"sqlstate={state}")
    cause = exc.__cause__ or exc.__context__
    if cause is not None:
        parts.append(f"cause_type={type(cause).__module__}.{type(cause).__qualname__}")
    every_frame = traceback.extract_tb(exc.__traceback__)
    frames = every_frame[-max_frames:]
    if frames:
        parts.append("at=" + " < ".join(_frame_text(f) for f in reversed(frames)))
    outer = every_frame[: len(every_frame) - len(frames)]
    own = [f for f in outer if _is_application_frame(f.filename)][-app_frames:] if app_frames > 0 else []
    if own:
        parts.append("app=" + " < ".join(_frame_text(f) for f in reversed(own)))
    return " ".join(parts)


def log_safe_exception(logger: logging.Logger, message: str, exc: BaseException) -> None:
    """ERROR-level log of a failure without `exc_info` (so no exception text or traceback message reaches the log
    handlers or the error tracker's logging integration)."""
    # `message` is a fixed string chosen by the caller, so it stays part of the log TEMPLATE (which is what the
    # error tracker groups on); the variable part is only the summary.
    logger.error(message.replace("%", "%%") + " | %s", safe_exception_summary(exc))


# --- Streamlit's own log of an uncaught app exception ---------------------------------------------------------------

# Streamlit logs every uncaught exception in a page script -- message, traceback and all -- before it decides what
# the browser may see (`client.showErrorDetails`), and that log line is not ours: `_LOGGER.error("Uncaught app
# execution", exc_info=ex)` on the `streamlit.error_util` logger (stderr), or, when the optional `rich` package is
# installed, a console print that never touches `logging` (stdout). A database driver's message quotes SQL, bound
# values and the failing row, so an unwrapped query failure would put them in the server log ("Manage app" on
# Streamlit Cloud). The scrubber below rewrites those records before any handler formats them.

_SCRUBBER_MARK = "_sortview_streamlit_exception_scrubber"
# An uncaught app exception usually passes through many library frames before it reaches the page's own, so the summary
# lists the innermost few (`at=`) AND SortView's own frames further out (`app=`, see safe_exception_summary).
_UNCAUGHT_SUMMARY_FRAMES = 4
_UNCAUGHT_APP_FRAMES = 6


def _is_streamlit_logger(name: str) -> bool:
    return name == "streamlit" or name.startswith("streamlit.")


def _attached_exception(record: logging.LogRecord) -> BaseException | None:
    info: Any = record.exc_info
    if isinstance(info, BaseException):
        return info
    if isinstance(info, tuple) and len(info) == 3 and isinstance(info[1], BaseException):
        return info[1]
    return None


def scrub_streamlit_log_record(record: logging.LogRecord) -> logging.LogRecord:
    """For a record from one of Streamlit's own loggers that carries an exception, drop the exception (and so its message
    and traceback text) and append `safe_exception_summary` -- type, SQLSTATE, code location -- to the log message. Every
    other record is returned untouched."""
    exc = _attached_exception(record) if _is_streamlit_logger(record.name) else None
    if exc is not None:
        summary = safe_exception_summary(exc, max_frames=_UNCAUGHT_SUMMARY_FRAMES, app_frames=_UNCAUGHT_APP_FRAMES)
        record.msg = f"{record.getMessage()} | {summary}"
        record.args = None
        record.exc_info = None
        record.exc_text = None
    return record


def is_streamlit_log_scrubber_installed() -> bool:
    """Whether this process's log-record factory is the scrubbing one (for the redaction canary's diagnostics)."""
    return bool(getattr(logging.getLogRecordFactory(), _SCRUBBER_MARK, False))


# --- what the browser may see of an uncaught page exception ---------------------------------------------------------

# `client.showErrorDetails` decides what Streamlit sends the browser for an uncaught exception. "none" shows only the
# generic message; the legacy "false" (which Streamlit Community Cloud forces at startup, over `.streamlit/config.toml`)
# is "stacktrace": it still shows the exception TYPE and the traceback -- server file paths and source lines. A
# `STREAMLIT_CLIENT_SHOW_ERROR_DETAILS` variable or a `--client.showErrorDetails` flag can do the same. The value is read
# in-process each time an exception is marshalled, and `client.showErrorDetails` is one of the options a script may set,
# so every entry script pins it to "none" here. It is unconditional: there is deliberately no way to turn details back on.
_ERROR_DETAILS_OPTION = "client.showErrorDetails"
_ERROR_DETAILS_ENFORCED = "none"
_error_details_lock = threading.Lock()
# What the option was, and where it was defined, the first time this process enforced it: the platform's value (e.g.
# Community Cloud's "false"), kept because enforcement overwrites it. None until the first call.
_error_details_startup: tuple[Any, str] | None = None


def streamlit_error_details_startup() -> tuple[Any, str] | None:
    """`(value, where_defined)` of `client.showErrorDetails` as it was before this process first enforced "none", or None
    if enforcement has not run (or could not read it). For the redaction canary's diagnostics; not a secret."""
    return _error_details_startup


def enforce_streamlit_error_details() -> bool:
    """Force `client.showErrorDetails` to "none" through Streamlit's supported `st.set_option`, whatever the platform, an
    environment variable or a flag set it to. Idempotent (setting the same value again changes nothing) and safe to call
    on every script run. Returns whether the effective value is "none" afterwards.

    It never raises, and it never logs a configuration value or exception text: if the option cannot be set it writes a
    fixed warning line and returns False (the repository's `.streamlit/config.toml` is still "none").
    """
    global _error_details_startup
    enforced = False
    try:
        import streamlit as st
        from streamlit import config

        with _error_details_lock:
            if _error_details_startup is None:
                _error_details_startup = (config.get_option(_ERROR_DETAILS_OPTION), config.get_where_defined(_ERROR_DETAILS_OPTION))
            st.set_option(_ERROR_DETAILS_OPTION, _ERROR_DETAILS_ENFORCED)
            enforced = config.get_option(_ERROR_DETAILS_OPTION) == _ERROR_DETAILS_ENFORCED
    except Exception:
        enforced = False
    if not enforced:
        logging.getLogger(__name__).warning("Could not enforce Streamlit client.showErrorDetails=none")
    return enforced


def install_streamlit_log_scrubber() -> None:
    """Keep uncaught-exception text out of the process's log output, and out of the browser. Idempotent; call it first in
    every Streamlit entry script (a page opened by its own URL runs only that page's script, never `app.py`).

    1. Wraps the log-record factory so Streamlit's records are scrubbed (`scrub_streamlit_log_record`) before any
       handler sees them. It is scoped by logger name, so it does not depend on which Streamlit module logs, and it
       cannot be bypassed by Streamlit's per-logger handlers or `propagate = False`.
    2. Turns Streamlit's rich-traceback console print off (`logger.enableRich`), because that path writes the exception
       straight to stdout and bypasses `logging`, so nothing above could scrub it.
    3. Pins `client.showErrorDetails` to "none" (`enforce_streamlit_error_details`) on EVERY call, outside the
       already-installed check above, so a later script run re-applies it.

    It never raises: a failure here must not stop the page.
    """
    factory = logging.getLogRecordFactory()
    if not getattr(factory, _SCRUBBER_MARK, False):
        def scrubbing_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
            return scrub_streamlit_log_record(factory(*args, **kwargs))

        setattr(scrubbing_factory, _SCRUBBER_MARK, True)
        logging.setLogRecordFactory(scrubbing_factory)

    with contextlib.suppress(Exception):
        from streamlit import config

        config.set_option("logger.enableRich", False)

    enforce_streamlit_error_details()


# --- validation errors ---------------------------------------------------------------------------------------------

# Fixed messages, keyed by the framework's error TYPE. The framework's own `msg`/`input`/`ctx` are never
# forwarded: some embed the submitted value, and a custom validator's message can carry anything.
_SAFE_MESSAGES = {
    "missing": "Field required",
    "int_parsing": "Input should be a valid integer",
    "int_type": "Input should be a valid integer",
    "int_from_float": "Input should be a valid integer",
    "float_parsing": "Input should be a valid number",
    "float_type": "Input should be a valid number",
    "bool_parsing": "Input should be a valid boolean",
    "bool_type": "Input should be a valid boolean",
    "string_type": "Input should be a valid string",
    "string_too_long": "String is too long",
    "string_too_short": "String is too short",
    "string_pattern_mismatch": "String does not match the required format",
    "literal_error": "Input is not one of the allowed values",
    "enum": "Input is not one of the allowed values",
    "greater_than": "Value is too small",
    "greater_than_equal": "Value is too small",
    "less_than": "Value is too large",
    "less_than_equal": "Value is too large",
    "too_long": "Too many items",
    "too_short": "Too few items",
    "dict_type": "Input should be an object",
    "list_type": "Input should be a list",
    "model_attributes_type": "Input should be an object",
    "model_type": "Input should be an object",
    "json_invalid": "Request body is not valid JSON",
    "extra_forbidden": "Unexpected field",
    "datetime_parsing": "Input should be a valid datetime",
    "datetime_type": "Input should be a valid datetime",
    # Contract v2 (services/ingest_v2_models.py): custom error types, each with a fixed message.
    "timestamp_format": "Timestamp must be ISO-8601 with a UTC offset",
    "timestamp_range": "Timestamp is outside the accepted range",
    "too_many_events": "Too many events in one request",
}
_GENERIC_MESSAGE = "Invalid value"

_SAFE_LOC_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_MAX_REPORTED_ERRORS = 20


def _safe_loc(loc: Any, kind: str = "") -> list[int | str]:
    """A location is field names and list indexes. A string that is not a plain identifier could be a
    caller-supplied dictionary key or field name, so it is not echoed.

    For an `extra_forbidden` error the LAST element is, by definition, the name the CALLER supplied for a field the model
    does not have -- and a plain-looking one (`P2300000000003`, a card number used as a key) passes the identifier test
    above. It is never echoed, whatever it looks like: the parents in the location are the server's own field names."""
    safe: list[int | str] = []
    for element in loc if isinstance(loc, (list, tuple)) else []:
        if isinstance(element, bool):
            continue
        if isinstance(element, int) or (isinstance(element, str) and _SAFE_LOC_NAME.match(element)):
            safe.append(element)
        else:
            safe.append("<key>")
    if kind == "extra_forbidden" and safe:
        safe[-1] = "<key>"
    return safe


def safe_validation_errors(errors: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Support-safe form of a validation failure: where (`loc`) and what kind (`type`, fixed `msg`) -- never the
    submitted value. At most 20 are reported (an upload can hold thousands of rows)."""
    safe = []
    for error in list(errors)[:_MAX_REPORTED_ERRORS]:
        kind = str(error.get("type") or "")
        safe.append({
            "loc": _safe_loc(error.get("loc"), kind),
            "type": kind if re.fullmatch(r"[a-z_0-9.]{1,64}", kind) else "invalid",
            "msg": _SAFE_MESSAGES.get(kind, _GENERIC_MESSAGE),
        })
    return safe


def safe_validation_body(errors: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The 422 response body. `detail` keeps the framework's shape (a list of {loc, type, msg}) so clients that
    read it keep working; `code` is the stable machine-readable error code."""
    return {"code": "validation_error", "detail": safe_validation_errors(errors)}


# --- API schema exposure -------------------------------------------------------------------------------------------

def api_docs_kwargs(enabled: bool) -> dict[str, Any]:
    """FastAPI constructor arguments for the interactive docs and the OpenAPI schema. They publish every field the
    API accepts, so they are OFF unless SORTVIEW_API_DOCS_ENABLED=true (development)."""
    if enabled:
        return {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}
