"""Server-side privacy containment (security/privacy Step 2), exercised through the REAL FastAPI app.

The v1 Collector contract still sends `patron_id` and `raw_message`, and the API still stores them. These tests
prove the server no longer creates SECONDARY exposure paths for what a request carries:

  * database errors carry no bound values (SQLAlchemy `hide_parameters`) and no driver text in the log;
  * the error tracker (real Sentry SDK pipeline, in-memory transport) receives no request body, local variables,
    credentials or row values;
  * 422 responses do not echo the submitted value;
  * the API does not publish its own schema in production;
  * and the currently deployed v1 collector still works.

Every value below is a SYNTHETIC canary. If a canary appears in an output, the value that carried it leaked.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sentry_sdk
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DataError, OperationalError
from sqlalchemy.pool import StaticPool

import main
from collector.uploader import FailureCategory, _post_json
from src.services import privacy_hardening as ph

ROOT = Path(__file__).resolve().parent.parent

PATRON = "CANARY-PATRON-ID-2001"
RAW = "64 CANARY-RAW-SIP2-MESSAGE-2002"
BARCODE = "CANARY-BARCODE-2003"
TITLE = "CANARY-ITEM-TITLE-2004"
TOKEN = "CANARY-BEARER-TOKEN-2005"
ERROR_TEXT = "CANARY-LAST-ERROR-2006"
DB_PASSWORD = "CANARY-DB-PASSWORD-2007"
ENROLL = "CANARY-ENROLLMENT-CODE-2008"
CANARIES = (PATRON, RAW, BARCODE, TITLE, TOKEN, ERROR_TEXT, DB_PASSWORD, ENROLL)

_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"
CUSTOMER, BRANCH = 10, 1

client = TestClient(main.app, raise_server_exceptions=False)


def leaked(*outputs) -> list[str]:
    haystack = "\n".join(str(o) for o in outputs)
    return [canary for canary in CANARIES if canary in haystack]


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main.limiter.reset()


# --- a real (SQLite) database behind the real endpoints ----------------------------------------------------------

_FACT_TABLES = (
    (
        "CREATE TABLE checkins (customer_id INTEGER, branch_id INTEGER, event_time TEXT, title TEXT, barcode TEXT,"
        " collection_code TEXT, call_number TEXT, shelf_code TEXT, destination TEXT, bin TEXT, is_problem BOOLEAN,"
        " message TEXT, flag_1 TEXT, flag_2 TEXT, flag_3 TEXT, source_file TEXT, source_event_id TEXT)"
    ),
    (
        "CREATE TABLE rejects (customer_id INTEGER, branch_id INTEGER, event_time TEXT, barcode TEXT, error_message TEXT,"
        " source_file TEXT, source_event_id TEXT)"
    ),
    (
        "CREATE TABLE acs_events (customer_id INTEGER, branch_id INTEGER, event_time TEXT, message_code TEXT, barcode TEXT,"
        " barcode_key TEXT, title TEXT, patron_id TEXT, destination TEXT, raw_message TEXT, source_file TEXT,"
        " source_event_id TEXT)"
    ),
)


def _build_engine(monkeypatch, *, fact_tables: bool):
    # Built with the API engine's OWN hide_parameters setting -- i.e. what production does to a database error.
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False},
        hide_parameters=main.engine.hide_parameters,
    )

    @event.listens_for(engine, "connect")
    def _register_sha256(dbapi_connection, _record):
        dbapi_connection.create_function("sha256hex", 1, lambda v: hashlib.sha256(v.encode("utf-8")).hexdigest())

    ddl = [
        "CREATE TABLE organizations (id INTEGER PRIMARY KEY, slug TEXT, status TEXT, operational_customer_id INTEGER)",
        "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, status TEXT, operational_branch_id INTEGER)",
        "CREATE TABLE collector_installations (id INTEGER PRIMARY KEY, organization_id INTEGER, branch_id INTEGER, status TEXT)",
        (
            "CREATE TABLE agent_tokens (id INTEGER PRIMARY KEY AUTOINCREMENT, token_hash TEXT, customer_id INTEGER,"
            " branch_id INTEGER, description TEXT, is_active BOOLEAN, last_used_at TEXT, installation_id INTEGER)"
        ),
        *(_FACT_TABLES if fact_tables else ()),
    ]
    with engine.begin() as conn:
        for statement in ddl:
            conn.execute(text(statement))
        conn.execute(text("INSERT INTO organizations VALUES (1, 'lib', 'active', :c)"), {"c": CUSTOMER})
        conn.execute(text("INSERT INTO branches VALUES (1, 1, 'active', :b)"), {"b": BRANCH})
        conn.execute(
            text("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, description, is_active)"
                 " VALUES (:h, :c, :b, 'test token', 1)"),
            {"h": hashlib.sha256(TOKEN.encode("utf-8")).hexdigest(), "c": CUSTOMER, "b": BRANCH},
        )
    monkeypatch.setattr(main, "engine", engine)
    assert _HASH_EXPR in main._AGENT_TOKEN_LOOKUP_SQL
    monkeypatch.setattr(main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, "sha256hex(:token)"))
    return engine


@pytest.fixture
def api_db(monkeypatch):
    """Tokens and tenant tables only: an ACS insert therefore fails inside the database, like any real DB error."""
    return _build_engine(monkeypatch, fact_tables=False)


@pytest.fixture
def api_db_with_facts(monkeypatch):
    return _build_engine(monkeypatch, fact_tables=True)


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def v1_acs_row(**overrides):
    row = {"customer_id": CUSTOMER, "branch_id": BRANCH, "event_time": "2026-01-01 10:00:00", "message_code": "64",
           "barcode": BARCODE, "title": TITLE, "patron_id": PATRON, "destination": "Main", "raw_message": RAW,
           "source_file": "ACS Log.txt"}
    row.update(overrides)
    return row


# --- 1. SQLAlchemy parameter hiding ------------------------------------------------------------------------------------

def test_the_api_engine_hides_bound_parameters():
    assert main.engine.hide_parameters is True


def test_the_dashboard_engine_hides_bound_parameters(monkeypatch):
    import database

    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@localhost/none")
    monkeypatch.setattr(database, "_engine", None)  # restored by monkeypatch; nothing connects (engines are lazy)

    assert database.get_engine().hide_parameters is True


def _engine_call_sites():
    """(path, line, call) for every create_engine / engine_from_config call in production Python code."""
    sources = [ROOT / "main.py", ROOT / "alembic" / "env.py"]
    for directory in ("src", "scripts", "super_admin", "collector", "agent"):
        sources += sorted((ROOT / directory).rglob("*.py"))
    for path in sources:
        if "SortViewAgent" in path.parts or "__pycache__" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name in ("create_engine", "engine_from_config"):
                    yield path.relative_to(ROOT).as_posix(), node.lineno, node


def test_every_production_database_engine_is_created_with_hidden_parameters():
    sites = list(_engine_call_sites())

    assert {path for path, _, _ in sites} >= {"main.py", "src/database.py", "alembic/env.py",
                                              "scripts/check_pipeline_health.py", "scripts/create_agent_token.py",
                                              "scripts/verify_db_snapshot.py"}  # the scan really sees them all
    for path, line, call in sites:
        hidden = [k for k in call.keywords if k.arg == "hide_parameters" and isinstance(k.value, ast.Constant) and k.value.value is True]
        assert hidden, f"{path}:{line} creates a database engine without hide_parameters=True"


def _failing_insert(engine):
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO no_such_table (a, b) VALUES (:a, :b)"), {"a": PATRON, "b": RAW})


def test_a_database_error_from_an_engine_with_the_production_setting_carries_no_bound_value():
    engine = create_engine("sqlite://", hide_parameters=main.engine.hide_parameters)

    with pytest.raises(OperationalError) as excinfo:
        _failing_insert(engine)

    assert leaked(excinfo.value, repr(excinfo.value)) == []
    assert "no such table" in str(excinfo.value)  # still diagnosable


def test_control_the_same_error_without_the_setting_does_expose_the_values():
    with pytest.raises(OperationalError) as excinfo:
        _failing_insert(create_engine("sqlite://"))  # SQLAlchemy's default

    assert PATRON in str(excinfo.value) and RAW in str(excinfo.value)  # proves the canary check above can fail


# --- 2. database failure through the real endpoints: logs, response, driver text -----------------------------------------

def test_a_database_failure_during_a_v1_upload_leaks_nothing_to_the_log_or_the_response(api_db, caplog):
    with caplog.at_level(logging.DEBUG):
        response = client.post("/upload", json={"acs": [v1_acs_row()]}, headers=AUTH)

    assert response.status_code == 500 and response.json() == {"detail": "Internal server error"}
    assert leaked(response.text, response.headers, caplog.text) == []
    assert "Upload failed | error_type=sqlalchemy.exc.OperationalError" in caplog.text  # still observable
    assert "traceback" not in caplog.text.lower()                                    # no exc_info, so no message-bearing trace


def test_a_database_failure_during_a_status_heartbeat_leaks_nothing(api_db, caplog):
    body = {"customer_id": CUSTOMER, "branch_id": BRANCH, "status": "failed", "last_error": ERROR_TEXT}

    with caplog.at_level(logging.DEBUG):
        response = client.post("/upload-pipeline-status", json=body, headers=AUTH)  # no pipeline_status table

    assert response.status_code == 500
    assert leaked(response.text, caplog.text) == []
    assert "Pipeline status upload failed | error_type=" in caplog.text


class _PgError(Exception):
    """What psycopg2 raises: the SQLSTATE plus a message that QUOTES the failing row and the offending value."""

    pgcode = "22007"


class _ExplodingEngine:
    def __init__(self, exc: Exception):
        self._exc = exc

    def begin(self):
        raise self._exc


def _driver_error() -> DataError:
    orig = _PgError(f'invalid input syntax for type timestamp: "{PATRON}"\nDETAIL: Failing row contains ({RAW}, {BARCODE})')
    return DataError("INSERT INTO acs_events (...) VALUES (...)", {"patron_id": PATRON}, orig)


def test_driver_error_text_that_quotes_the_row_never_reaches_the_log_even_with_hidden_parameters(monkeypatch, caplog):
    monkeypatch.setattr(main, "engine", _ExplodingEngine(_driver_error()))
    assert PATRON in str(_driver_error())  # the raw exception text DOES carry it (hide_parameters cannot hide the driver's part)

    with caplog.at_level(logging.DEBUG):
        response = client.post("/upload", json={"acs": [v1_acs_row()]}, headers=AUTH)

    assert response.status_code == 500
    assert leaked(response.text, caplog.text) == []
    assert "sqlstate=22007" in caplog.text and "DataError" in caplog.text  # the triage facts survive


def test_an_enrollment_failure_logs_no_message_text(monkeypatch, caplog):
    def explode(*_a, **_k):
        raise RuntimeError(f"failed while handling {ENROLL} for host {DB_PASSWORD}")

    monkeypatch.setattr(main, "engine", type("E", (), {"begin": lambda self: _NullTransaction()})())
    monkeypatch.setattr(main, "redeem_enrollment_code", explode)

    with caplog.at_level(logging.DEBUG):
        response = client.post("/collector/enroll", json={"enrollment_code": "SV-ABCD-EFGH-JKLM-NPQR"})

    assert response.status_code == 500 and leaked(response.text, caplog.text) == []
    assert "Collector enrollment failed | error_type=builtins.RuntimeError" in caplog.text


class _NullTransaction:
    def __enter__(self):
        return object()

    def __exit__(self, *exc):
        return False


# --- 3. Sentry: the real SDK pipeline into an in-memory transport --------------------------------------------------------

class _CaptureTransport(Transport):
    def __init__(self, options=None):
        super().__init__(options)
        self.events: list[dict] = []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.headers.get("type") == "event":
                self.events.append(item.payload.json)


DSN = "https://0123456789abcdef0123456789abcdef@o1.ingest.example.invalid/1"


@pytest.fixture
def start_sentry():
    def start(options: dict) -> _CaptureTransport:
        transport = _CaptureTransport()
        sentry_sdk.init(transport=transport, **options)
        main.app.middleware_stack = None  # rebuild, so the SDK's ASGI/FastAPI integration wraps the app
        return transport

    yield start
    sentry_sdk.get_client().close()
    sentry_sdk.init()  # back to the disabled client the rest of the suite expects
    main.app.middleware_stack = None


def test_sentry_receives_no_patron_data_token_or_body_from_a_failing_upload(api_db, start_sentry):
    transport = start_sentry(ph.build_sentry_options(DSN, "test"))

    response = client.post("/upload", json={"acs": [v1_acs_row()]}, headers=AUTH)
    sentry_sdk.flush()

    assert response.status_code == 500
    assert transport.events, "the failure must still be reported"
    assert leaked(json.dumps(transport.events)) == []
    exception_types = {v.get("type") for e in transport.events for v in (e.get("exception") or {}).get("values", [])}
    assert "OperationalError" in exception_types  # observability kept: the type, and (below) the stack


def test_sentry_events_keep_their_stack_but_carry_no_local_variables_or_request_body(api_db, start_sentry):
    transport = start_sentry(ph.build_sentry_options(DSN, "test"))

    client.post("/upload", json={"acs": [v1_acs_row()]}, headers=AUTH)
    sentry_sdk.flush()

    frames = [f for e in transport.events for v in (e.get("exception") or {}).get("values", [])
              for f in (v.get("stacktrace") or {}).get("frames", [])]
    assert any(f.get("function") == "upload" for f in frames)          # the code location is kept
    assert all("vars" not in f for f in frames)                         # local variables are not
    assert all((e.get("request") or {}).get("data") in (None, ph.FILTERED) for e in transport.events)


def test_sentry_receives_no_database_url_or_authorization_from_a_connection_failure(monkeypatch, start_sentry):
    transport = start_sentry(ph.build_sentry_options(DSN, "test"))
    url = f"postgresql://svc_user:{DB_PASSWORD}@db.example.invalid:5432/prod"
    monkeypatch.setattr(main, "engine", _ExplodingEngine(RuntimeError(f"could not connect to {url} using Bearer {TOKEN}")))

    response = client.post("/upload", json={"acs": [v1_acs_row()]}, headers=AUTH)
    sentry_sdk.flush()

    assert response.status_code == 500
    assert leaked(json.dumps(transport.events)) == []
    assert any(v.get("type") == "RuntimeError" for e in transport.events for v in (e.get("exception") or {}).get("values", []))


def test_the_sdk_client_is_configured_with_every_privacy_switch(start_sentry):
    start_sentry(ph.build_sentry_options(DSN, "test"))
    options = sentry_sdk.get_client().options

    assert options["send_default_pii"] is False
    assert options["include_local_variables"] is False
    assert options["max_request_body_size"] == "never"
    assert options["before_send"] is ph.scrub_sentry_event


def test_control_the_pre_hardening_options_did_attach_the_request_body_and_credentials(api_db, start_sentry):
    # The initialisation main.py used before Step 2, verbatim. This proves the checks above can fail.
    transport = start_sentry({"dsn": DSN, "environment": "test", "send_default_pii": False, "traces_sample_rate": 0.0})

    client.post("/upload", json={"acs": [v1_acs_row()]}, headers=AUTH)
    sentry_sdk.flush()

    assert {PATRON, RAW, BARCODE} <= set(leaked(json.dumps(transport.events)))


def test_main_initialises_sentry_with_the_hardened_options():
    script = (
        "import json, sentry_sdk, main\n"
        "o = sentry_sdk.get_client().options\n"
        "print(json.dumps({'pii': o['send_default_pii'], 'locals': o['include_local_variables'],"
        " 'body': o['max_request_body_size'], 'before_send': o['before_send'].__name__}))\n"
    )
    env = {**os.environ, "DATABASE_URL": "postgresql://user:pw@localhost/none", "SENTRY_DSN": DSN,
           "PYTHONPATH": str(ROOT), "SENTRY_ENVIRONMENT": "test"}

    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, check=False)  # nosec B603

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "pii": False, "locals": False, "body": "never", "before_send": "scrub_sentry_event"}


# --- 4. validation errors do not echo what was submitted -------------------------------------------------------------------

def _assert_safe_422(response):
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert isinstance(body["detail"], list) and body["detail"]
    for item in body["detail"]:
        assert set(item) == {"loc", "type", "msg"}  # no `input`, `ctx` or `url`
    assert leaked(response.text) == []


def test_a_row_missing_a_required_field_does_not_echo_the_row():
    row = v1_acs_row()
    del row["customer_id"]  # the framework's default response then quotes the WHOLE row

    response = client.post("/upload", json={"acs": [row]}, headers=AUTH)

    _assert_safe_422(response)
    assert response.json()["detail"][0]["loc"] == ["body", "acs", 0, "customer_id"]
    assert response.json()["detail"][0]["type"] == "missing"


def test_a_wrongly_typed_field_does_not_echo_its_value():
    body = {"checkins": [{"customer_id": CUSTOMER, "branch_id": BRANCH, "event_time": "2026-01-01 10:00:00",
                          "barcode": BARCODE, "is_problem": f"{PATRON}-not-a-bool"}]}

    response = client.post("/upload", json=body, headers=AUTH)

    _assert_safe_422(response)
    assert response.json()["detail"][0]["loc"][-1] == "is_problem"


def test_a_malformed_source_event_id_does_not_echo_its_value():
    response = client.post("/upload", json={"rejects": [{"customer_id": CUSTOMER, "branch_id": BRANCH, "barcode": BARCODE,
                                                          "source_event_id": f"{RAW}-not-a-hash"}]}, headers=AUTH)

    _assert_safe_422(response)
    assert response.json()["detail"][0]["type"] == "string_pattern_mismatch"


def test_an_invalid_heartbeat_value_does_not_echo_its_value():
    body = {"customer_id": CUSTOMER, "branch_id": BRANCH, "health_status": ERROR_TEXT, "installation_id": f"{PATRON}-x"}

    response = client.post("/upload-pipeline-status", json=body, headers=AUTH)

    _assert_safe_422(response)


def test_an_invalid_enrollment_code_is_not_echoed_although_it_is_a_secret():
    response = client.post("/collector/enroll", json={"enrollment_code": ENROLL * 4})  # longer than the 64-character limit

    _assert_safe_422(response)
    assert response.json()["detail"][0]["loc"] == ["body", "enrollment_code"]


def test_a_body_that_is_not_json_does_not_echo_its_content():
    response = client.post("/upload", content=f'{{"acs": [{{"patron_id": "{PATRON}", "raw_message": "{RAW}"'.encode(),
                           headers={**AUTH, "Content-Type": "application/json"})

    _assert_safe_422(response)
    assert response.json()["detail"][0]["type"] == "json_invalid"


def test_the_authorization_header_never_appears_in_any_error_response(caplog):
    with caplog.at_level(logging.DEBUG):
        responses = [
            client.post("/upload", json={"acs": [{"patron_id": PATRON}]}, headers=AUTH),
            client.post("/upload-pipeline-status", json={}, headers=AUTH),
            client.post("/upload", json={"acs": [v1_acs_row()]}, headers={"Authorization": f"Bearer {TOKEN}x"}),
        ]

    assert [r.status_code for r in responses][:2] == [422, 422]
    assert leaked(*[r.text for r in responses], *[r.headers for r in responses], caplog.text) == []


def test_only_the_first_twenty_validation_errors_are_reported():
    rows = [{"patron_id": PATRON, "raw_message": RAW} for _ in range(200)]  # 200 rows, each missing two required fields

    response = client.post("/upload", json={"acs": rows}, headers=AUTH)

    assert response.status_code == 422 and len(response.json()["detail"]) == 20 and leaked(response.text) == []


def test_the_validation_failure_is_logged_with_locations_and_kinds_only(caplog):
    with caplog.at_level(logging.DEBUG, logger="sortview.api"):
        client.post("/upload", json={"acs": [{"patron_id": PATRON, "raw_message": RAW}]}, headers=AUTH)

    assert "Request validation failed | path=/upload" in caplog.text and "customer_id:missing" in caplog.text
    assert leaked(caplog.text) == []


def test_the_collector_logs_and_reports_a_rejected_upload_without_the_submitted_values():
    """The audit's loop: the Collector previews the first 500 characters of an unexpected response body into its own
    log and `last_error` (which is then uploaded). The server's 422 no longer contains anything to preview."""
    row = v1_acs_row()
    del row["customer_id"]
    response = client.post("/upload", json={"acs": [row]}, headers=AUTH)

    class _Response:
        status_code = response.status_code
        text = response.text

        def json(self):
            return response.json()

    class _Session:
        def post(self, *_a, **_k):
            return _Response()

    outcome = _post_json(_Session(), "https://api.example.invalid/upload", {}, headers=AUTH, timeout=(1, 1))

    assert outcome.success is False and outcome.status_code == 422
    assert outcome.category is FailureCategory.RETRYABLE_INFRA  # unchanged: the deployed collector's behaviour is not altered
    assert leaked(outcome.error) == []


# --- 5. the currently deployed (v1) collector still works ---------------------------------------------------------------------

def _v1_payload():
    return {
        "checkins": [{"customer_id": CUSTOMER, "branch_id": BRANCH, "event_time": "2026-01-01 10:00:00", "title": "T",
                      "barcode": "B-1", "collection_code": "C", "call_number": "N", "shelf_code": "S", "destination": "Main",
                      "bin": "1", "is_problem": False, "message": "", "flag_1": "0", "flag_2": "0", "flag_3": "0",
                      "source_file": "Checkins.txt"}],
        "rejects": [{"customer_id": CUSTOMER, "branch_id": BRANCH, "event_time": "2026-01-01 10:01:00", "barcode": "B-2",
                     "message": "Item not found", "source_file": "Rejects.txt"}],
        "acs": [v1_acs_row(message_code="10", raw_message="101YNY|AB" + BARCODE)],
    }


def test_a_full_v1_collector_payload_is_still_accepted_and_stored(api_db_with_facts):
    response = client.post("/upload", json=_v1_payload(), headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"status": "success", "checkins_received": 1, "rejects_received": 1, "acs_received": 1,
                               "checkins_inserted": 1, "rejects_inserted": 1, "acs_inserted": 1}
    with api_db_with_facts.connect() as conn:
        stored = conn.execute(text("SELECT patron_id, raw_message, barcode FROM acs_events")).one()
    # DOCUMENTED, TEMPORARY: Step 2 is containment only. The v1 fields are still accepted and stored until Contract v2
    # and the collector cutover (a later step); this assertion is what that step will deliberately change.
    assert tuple(stored) == (PATRON, "101YNY|AB" + BARCODE, BARCODE)


def test_a_legacy_heartbeat_without_installation_fields_is_still_accepted(api_db_with_facts, monkeypatch):
    columns = ", ".join(f"{name} TEXT" for name in main._PIPELINE_STATUS_UPDATABLE_FIELDS)
    with api_db_with_facts.begin() as conn:
        conn.execute(text(f"CREATE TABLE pipeline_status (customer_id INTEGER, branch_id INTEGER, {columns},"
                          " updated_at TEXT, UNIQUE (customer_id, branch_id))"))
    # SQLite has no JSONB cast; nothing else about the request path is altered.
    monkeypatch.setattr(main, "_pipeline_status_column_sql", lambda field: f":{field}")

    response = client.post("/upload-pipeline-status", headers=AUTH,
                           json={"customer_id": CUSTOMER, "branch_id": BRANCH, "status": "completed", "checkins_rows": 3})

    assert response.status_code == 200 and response.json()["status"] == "success"


# --- 6. the API does not publish its own schema in production ---------------------------------------------------------------

@pytest.mark.skipif(os.getenv("SORTVIEW_API_DOCS_ENABLED", "").lower() == "true", reason="a developer enabled the API docs locally")
def test_the_docs_and_the_openapi_schema_are_not_served_by_default():
    assert main.API_DOCS_ENABLED is False
    assert (main.app.docs_url, main.app.redoc_url, main.app.openapi_url) == (None, None, None)
    for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        assert client.get(path).status_code == 404, path
    assert client.get("/").json() == {"status": "SortView API running"}  # the uptime check's endpoint is untouched


def _docs_status(flag: str | None) -> dict:
    script = (
        "import json, main\n"
        "from fastapi.testclient import TestClient\n"
        "c = TestClient(main.app)\n"
        "print(json.dumps({p: c.get(p).status_code for p in ('/docs', '/redoc', '/openapi.json', '/')}))\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "SORTVIEW_API_DOCS_ENABLED"}
    env.update({"DATABASE_URL": "postgresql://user:pw@localhost/none", "PYTHONPATH": str(ROOT)})
    if flag is not None:
        env["SORTVIEW_API_DOCS_ENABLED"] = flag
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, check=False)  # nosec B603
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_a_developer_can_opt_in_to_the_docs():
    assert _docs_status("true") == {"/docs": 200, "/redoc": 200, "/openapi.json": 200, "/": 200}


@pytest.mark.parametrize("flag", [None, "", "false", "0", "no", "TRUE-ish"])
def test_anything_but_true_keeps_the_docs_off(flag):
    assert _docs_status(flag) == {"/docs": 404, "/redoc": 404, "/openapi.json": 404, "/": 200}
