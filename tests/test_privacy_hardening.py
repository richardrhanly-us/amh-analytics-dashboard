"""Unit tests for src/services/privacy_hardening.py (security/privacy Step 2, server-side containment).

Everything here uses SYNTHETIC canary strings -- never a real patron, barcode, token or database URL. A canary
is a unique marker: if it appears anywhere in an output, the value that carried it leaked.
"""

from __future__ import annotations

import json
import logging

import pytest

from src.services import privacy_hardening as ph

PATRON = "CANARY-PATRON-ID-1001"
RAW = "64 CANARY-RAW-SIP2-MESSAGE-1002"
BARCODE = "CANARY-BARCODE-1003"
TITLE = "CANARY-ITEM-TITLE-1004"
BEARER = "CANARY-BEARER-TOKEN-1005"
DB_PASSWORD = "CANARY-DB-PASSWORD-1006"
DB_URL = f"postgresql://svc_user:{DB_PASSWORD}@db.example.invalid:5432/prod"
COOKIE = "CANARY-COOKIE-1007"
CANARIES = (PATRON, RAW, BARCODE, TITLE, BEARER, DB_PASSWORD, COOKIE)


def _dump(value) -> str:
    return json.dumps(value, default=str)


def _leaked(value) -> list[str]:
    text = _dump(value)
    return [c for c in CANARIES if c in text]


def _event() -> dict:
    """A Sentry-shaped event that carries every kind of sensitive value in every place the SDK puts them."""
    return {
        "message": f"failed for {DB_URL}",
        "logentry": {"message": "Upload failed | %s", "params": [f"Authorization: Bearer {BEARER}"]},
        "request": {
            "url": "https://api.example.invalid/upload",
            "method": "POST",
            "headers": {"Authorization": f"Bearer {BEARER}", "Cookie": f"session={COOKIE}", "Content-Type": "application/json"},
            "cookies": {"session": COOKIE},
            "data": {"acs": [{"patron_id": PATRON, "raw_message": RAW, "barcode": BARCODE, "title": TITLE}]},
            "query_string": f"debug=1&api_token={BEARER}",
        },
        "exception": {"values": [
            {"type": "RuntimeError", "module": "builtins", "value": f"could not connect to {DB_URL}",
             "stacktrace": {"frames": [{"function": "upload", "vars": {"acs_row": {"patron_id": PATRON, "raw_message": RAW}}}]}},
            {"type": "DataError", "module": "psycopg2.errors",
             "value": f'invalid input syntax for type timestamp: "{PATRON}"\nDETAIL: Failing row contains ({RAW}, {BARCODE})',
             "stacktrace": {"frames": [{"function": "do_execute", "vars": {"parameters": (PATRON, RAW)}}]}},
        ]},
        "extra": {"patron_id": PATRON, "note": "keep me"},
        "contexts": {"runtime": {"name": "CPython"}},
        "breadcrumbs": {"values": [
            {"category": "query", "message": "INSERT INTO acs_events (patron_id, raw_message) VALUES (%s, %s)",
             "data": {"db.params": [PATRON, RAW], "db.paramstyle": "format"}},
            {"category": "http", "data": {"url": DB_URL, "Authorization": f"Bearer {BEARER}"}},
        ]},
        "tags": {"environment": "production"},
    }


# --- Sentry scrubber --------------------------------------------------------------------------------------------

def test_the_scrubber_removes_every_canary_from_every_part_of_an_event():
    scrubbed = ph.scrub_sentry_event(_event())

    assert _leaked(scrubbed) == []


@pytest.mark.parametrize("key", ["patron_id", "patronId", "Patron-ID", "PATRON_ID", "raw_message", "rawMessage", "barcode",
                                 "barcode_key", "title", "Authorization", "authorization", "Proxy-Authorization", "Cookie",
                                 "Set-Cookie", "api_token", "agent_token", "SORTVIEW_API_TOKEN", "access_token", "X-Api-Key",
                                 "enrollment_code", "database_url", "DATABASE_URL", "sentry_dsn", "password", "client_secret",
                                 "db.params"])
def test_a_sensitive_key_is_filtered_whatever_its_spelling_and_wherever_it_is_nested(key):
    event = {"extra": {"deep": [{"nested": {key: "CANARY-VALUE-9999"}}]}}

    assert "CANARY-VALUE-9999" not in _dump(ph.scrub_sentry_event(event))
    assert ph.scrub_sentry_event(event)["extra"]["deep"][0]["nested"][key] == ph.FILTERED


def test_the_request_body_is_always_filtered_even_under_an_innocent_key():
    event = {"request": {"data": {"x": "CANARY-BODY-VALUE-7777"}}}

    assert ph.scrub_sentry_event(event)["request"]["data"] == ph.FILTERED


def test_a_database_error_message_is_replaced_because_it_quotes_the_failing_row():
    scrubbed = ph.scrub_sentry_event(_event())
    values = scrubbed["exception"]["values"]

    assert values[1]["value"] == ph.DB_ERROR_REDACTED  # psycopg2 DataError: "... Failing row contains (...)"
    assert values[1]["type"] == "DataError"           # the error is still reported, with its type and stack
    assert values[1]["stacktrace"]["frames"][0]["function"] == "do_execute"


@pytest.mark.parametrize("module", ["sqlalchemy.exc", "psycopg2.errors", "psycopg2", "sqlite3", "asyncpg.exceptions"])
def test_every_database_driver_module_is_treated_as_a_database_error(module):
    event = {"exception": {"values": [{"type": "Whatever", "module": module, "value": "row CANARY-ROW-5555"}]}}

    assert ph.scrub_sentry_event(event)["exception"]["values"][0]["value"] == ph.DB_ERROR_REDACTED


def test_a_database_error_is_recognised_by_its_type_when_the_module_is_missing():
    event = {"exception": {"values": [{"type": "IntegrityError", "value": "row CANARY-ROW-5556"}]}}

    assert "CANARY-ROW-5556" not in _dump(ph.scrub_sentry_event(event))


def test_stack_frame_local_variables_are_removed():
    scrubbed = ph.scrub_sentry_event(_event())

    assert all("vars" not in frame for value in scrubbed["exception"]["values"] for frame in value["stacktrace"]["frames"])


def test_credentials_embedded_in_ordinary_strings_are_filtered():
    scrubbed = ph.scrub_sentry_event({"message": f"engine failed: {DB_URL} (Authorization: Bearer {BEARER})"})

    assert DB_PASSWORD not in _dump(scrubbed) and BEARER not in _dump(scrubbed)
    assert "db.example.invalid" in scrubbed["message"]  # the host is kept: it is what makes the error diagnosable


def test_a_sentry_dsn_key_inside_a_string_is_filtered():
    dsn = "https://0123456789abcdef0123456789abcdef@o1.ingest.example.invalid/42"

    assert "0123456789abcdef0123456789abcdef" not in _dump(ph.scrub_sentry_event({"message": f"init with {dsn}"}))


def test_the_scrubber_keeps_everything_that_makes_the_error_diagnosable():
    scrubbed = ph.scrub_sentry_event(_event())

    assert scrubbed["request"]["url"] == "https://api.example.invalid/upload" and scrubbed["request"]["method"] == "POST"
    assert scrubbed["request"]["headers"]["Content-Type"] == "application/json"
    assert scrubbed["exception"]["values"][0]["type"] == "RuntimeError"
    assert scrubbed["exception"]["values"][0]["stacktrace"]["frames"][0]["function"] == "upload"
    assert scrubbed["extra"]["note"] == "keep me" and scrubbed["tags"] == {"environment": "production"}
    assert scrubbed["contexts"] == {"runtime": {"name": "CPython"}}
    assert scrubbed["breadcrumbs"]["values"][0]["message"].startswith("INSERT INTO acs_events")  # the statement, no values


def test_the_scrubber_never_drops_the_event_and_never_mutates_its_input():
    event = _event()
    before = _dump(event)

    assert ph.scrub_sentry_event(event) is not None
    assert _dump(event) == before


def test_the_scrubber_survives_odd_shapes():
    for weird in ({}, {"exception": None}, {"exception": {"values": None}}, {"exception": {"values": ["not a dict"]}},
                  {"request": None}, {"request": {"data": None}}, {"breadcrumbs": None}, {"a": {"b": {"c": [1, 2, None, object]}}}):
        ph.scrub_sentry_event(weird)


def test_the_scrubber_stops_at_a_pathological_depth_instead_of_recursing_forever():
    deep: dict = {}
    node = deep
    for _ in range(200):
        node["n"] = {}
        node = node["n"]
    node["patron_id"] = "CANARY-DEEP-1"

    assert "CANARY-DEEP-1" not in _dump(ph.scrub_sentry_event(deep))


def test_the_breadcrumb_hook_scrubs_sql_parameters():
    crumb = {"category": "query", "message": "SELECT 1", "data": {"db.params": [PATRON], "url": DB_URL}}

    assert _leaked(ph.scrub_sentry_breadcrumb(crumb)) == []


# --- Sentry configuration ----------------------------------------------------------------------------------------

def test_the_sentry_options_switch_off_every_way_the_sdk_attaches_data():
    options = ph.build_sentry_options("https://key@o1.ingest.example.invalid/1", "production")

    assert options["send_default_pii"] is False
    assert options["include_local_variables"] is False
    assert options["max_request_body_size"] == "never"   # otherwise bodies (<=10 KB) ride along regardless of send_default_pii
    assert options["traces_sample_rate"] == 0.0
    assert options["before_send"] is ph.scrub_sentry_event
    assert options["before_breadcrumb"] is ph.scrub_sentry_breadcrumb
    assert options["dsn"].startswith("https://") and options["environment"] == "production"


# --- exception logging ---------------------------------------------------------------------------------------------

class _DriverError(Exception):
    """Stands in for a psycopg2 error: it carries the SQLSTATE and quotes the row in its message."""

    pgcode = "22007"


def _raise_driver_error():
    raise _DriverError(f'invalid input syntax for type timestamp: "{PATRON}" DETAIL: Failing row contains ({RAW})')


def test_the_exception_summary_names_the_type_the_sqlstate_and_the_location_but_never_the_message():
    try:
        _raise_driver_error()
    except _DriverError as exc:
        summary = ph.safe_exception_summary(exc)

    assert "error_type=" in summary and "_DriverError" in summary
    assert "sqlstate=22007" in summary
    assert "_raise_driver_error" in summary and "test_privacy_hardening.py" in summary
    assert _leaked(summary) == []


def test_the_summary_reads_the_sqlstate_from_a_wrapped_driver_error():
    class Wrapper(Exception):
        def __init__(self):
            super().__init__(f"({PATRON}) wrapped")
            self.orig = _DriverError("x")

    assert "sqlstate=22007" in ph.safe_exception_summary(Wrapper())


def test_a_message_that_is_not_a_sqlstate_is_not_reported_as_one():
    class Odd(Exception):
        pgcode = f"{PATRON} not a code"

    assert "sqlstate=" not in ph.safe_exception_summary(Odd("x"))


def test_the_cause_type_is_named_without_its_message():
    try:
        try:
            raise _DriverError(PATRON)
        except _DriverError as inner:
            raise RuntimeError(BARCODE) from inner
    except RuntimeError as exc:
        summary = ph.safe_exception_summary(exc)

    assert "cause_type=" in summary and "_DriverError" in summary and _leaked(summary) == []


def test_log_safe_exception_writes_one_error_record_without_exc_info_or_message(caplog):
    logger = logging.getLogger("sortview.test.privacy")
    try:
        _raise_driver_error()
    except _DriverError as exc:
        with caplog.at_level(logging.DEBUG, logger="sortview.test.privacy"):
            ph.log_safe_exception(logger, "Upload failed", exc)

    (record,) = caplog.records
    assert record.levelno == logging.ERROR and record.exc_info is None
    assert record.getMessage().startswith("Upload failed | error_type=")
    assert record.msg == "Upload failed | %s"          # the fixed text is the log template (what an error tracker groups on)
    assert _leaked(caplog.text) == []


def test_a_percent_sign_in_the_fixed_message_cannot_break_the_log_format(caplog):
    logger = logging.getLogger("sortview.test.privacy")
    with caplog.at_level(logging.DEBUG, logger="sortview.test.privacy"):
        ph.log_safe_exception(logger, "100% failure", RuntimeError("x"))

    assert caplog.records[0].getMessage().startswith("100% failure | error_type=")


# --- validation errors ---------------------------------------------------------------------------------------------

def _error(kind="missing", loc=("body", "acs", 0, "customer_id"), **extra):
    return {"type": kind, "loc": loc, "msg": f"framework text quoting {PATRON}", "input": {"patron_id": PATRON, "raw_message": RAW},
            "ctx": {"error": RAW}, "url": "https://errors.pydantic.dev/x", **extra}


def test_a_safe_validation_error_says_where_and_what_kind_and_never_the_value():
    (safe,) = ph.safe_validation_errors([_error()])

    assert safe == {"loc": ["body", "acs", 0, "customer_id"], "type": "missing", "msg": "Field required"}
    assert _leaked(safe) == []


def test_no_field_the_framework_supplies_is_forwarded():
    (safe,) = ph.safe_validation_errors([_error()])

    assert set(safe) == {"loc", "type", "msg"}  # no `input`, `ctx`, `url`, and the framework's own `msg` text


@pytest.mark.parametrize("kind", ["int_parsing", "bool_parsing", "string_type", "string_too_long", "string_pattern_mismatch",
                                  "literal_error", "greater_than_equal", "less_than_equal", "json_invalid", "extra_forbidden",
                                  "too_long", "model_attributes_type"])
def test_every_known_error_type_has_a_fixed_message(kind):
    (safe,) = ph.safe_validation_errors([_error(kind)])

    assert safe["type"] == kind and safe["msg"] != "Invalid value" and _leaked(safe) == []


def test_an_unknown_or_custom_error_type_gets_the_generic_message():
    (safe,) = ph.safe_validation_errors([_error("value_error")])

    assert safe["msg"] == "Invalid value"


def test_a_hostile_error_type_string_is_not_forwarded():
    (safe,) = ph.safe_validation_errors([_error(f"weird {PATRON}")])

    assert safe["type"] == "invalid" and _leaked(safe) == []


def test_only_plain_identifiers_and_indexes_appear_in_a_location():
    (safe,) = ph.safe_validation_errors([_error(loc=("body", "acs", 3, f"key with {PATRON}", "customer_id", True))])

    # a caller-supplied dictionary key or field name is not echoed; booleans are not indexes
    assert safe["loc"] == ["body", "acs", 3, "<key>", "customer_id"]


def test_a_location_that_is_not_a_sequence_becomes_empty():
    assert ph.safe_validation_errors([_error(loc=PATRON)])[0]["loc"] == []


def test_the_number_of_reported_errors_is_capped():
    safe = ph.safe_validation_errors([_error(loc=("body", "acs", i, "customer_id")) for i in range(1000)])

    assert len(safe) == 20


def test_the_validation_body_keeps_the_frameworks_detail_shape_and_adds_a_stable_code():
    body = ph.safe_validation_body([_error()])

    assert body["code"] == "validation_error"
    assert isinstance(body["detail"], list) and body["detail"][0]["loc"][-1] == "customer_id"
    assert _leaked(body) == []


# --- the location of a deep failure --------------------------------------------------------------------------------

@pytest.mark.parametrize("filename, expected", [
    (r"C:\Users\dev\Projects\amh\src\services\auth_service.py", True),
    ("/app/src/services/auth_service.py", True),
    ("/mount/src/amh-analytics-dashboard/src/app.py", True),
    (r"C:\Users\dev\Projects\amh\.venv\Lib\site-packages\sqlalchemy\engine\base.py", False),
    ("/home/adminuser/venv/lib/python3.11/site-packages/streamlit/runtime/scriptrunner/exec_code.py", False),
    ("/usr/lib/python3/dist-packages/psycopg2/__init__.py", False),
    ("<frozen importlib._bootstrap>", False),
    ("<string>", False),
])
def test_only_sortviews_own_frames_count_as_application_frames(filename, expected):
    assert ph._is_application_frame(filename) is expected


def test_a_standard_library_frame_is_not_an_application_frame():
    import json as stdlib_module

    assert ph._is_application_frame(stdlib_module.__file__) is False


# A stand-in for SQLAlchemy/psycopg2: code whose FILENAME is under site-packages, ten frames deep.
_LIBRARY_SOURCE = "def _library_call(depth, error):\n    if depth == 0:\n        raise error\n    _library_call(depth - 1, error)\n"
_library_namespace: dict = {}
exec(  # nosec B102 - test-only, fixed source
    compile(_LIBRARY_SOURCE, "/venv/lib/python3.11/site-packages/fakedb/engine.py", "exec"), _library_namespace
)


def _application_entry_point():
    # this frame is far outside the innermost few, as SQLAlchemy's connect path makes it
    _library_namespace["_library_call"](9, _DriverError(f'invalid input syntax: "{PATRON}" Failing row contains ({RAW})'))


def test_the_summary_names_sortview_code_even_when_the_failure_is_many_frames_deep():
    try:
        _application_entry_point()
    except _DriverError as exc:
        summary = ph.safe_exception_summary(exc)

    assert "at=" in summary and "_library_call" in summary
    assert "app=" in summary and "_application_entry_point" in summary  # the caller is named although 10 frames out
    assert _leaked(summary) == []


def test_a_real_database_connection_failure_is_located_in_the_calling_code_without_its_message():
    from sqlalchemy import create_engine, text

    def load_the_settings_page():
        engine = create_engine(f"postgresql://canary_user:{DB_PASSWORD}@127.0.0.1:1/canary_db", connect_args={"connect_timeout": 2})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))

    try:
        load_the_settings_page()
    except Exception as exc:
        summary = ph.safe_exception_summary(exc)

    assert "error_type=sqlalchemy.exc.OperationalError" in summary and "cause_type=psycopg2.OperationalError" in summary
    assert "app=" in summary and "load_the_settings_page" in summary  # SQLAlchemy's own frames alone would not say this
    assert _leaked(summary) == [] and "canary_user" not in summary and "canary_db" not in summary


def test_the_application_frame_count_can_be_turned_off_and_the_at_field_is_unchanged():
    try:
        _application_entry_point()
    except _DriverError as exc:
        with_app = ph.safe_exception_summary(exc)
        without_app = ph.safe_exception_summary(exc, app_frames=0)

    assert "app=" not in without_app
    assert with_app.split(" app=")[0] == without_app  # the existing `at=` field is byte-for-byte what it was


# --- API schema exposure -------------------------------------------------------------------------------------------

def test_the_api_docs_are_off_by_default_and_on_only_when_asked():
    assert ph.api_docs_kwargs(False) == {"docs_url": None, "redoc_url": None, "openapi_url": None}
    assert ph.api_docs_kwargs(True) == {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}
