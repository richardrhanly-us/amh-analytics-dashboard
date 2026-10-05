"""Block 9b: the server stamps WHEN each pipeline status signal was last reported.

    POST /upload-pipeline-status
        carries `status`          -> pipeline_status.status_reported_at        = the database's clock
        carries `health_status`   -> pipeline_status.health_status_reported_at = the database's clock

Each is stamped only for the field the request actually carries; a request that carries neither stamps neither, and
no request can supply either value. The collector's and the agent's payloads are unchanged.

These drive the real endpoint through TestClient(main.app) against in-memory SQLite, so the real statement the upsert
builder produces is what runs. SQLite's CURRENT_TIMESTAMP only has whole seconds, so "this column moved and that one
did not" is shown with a SENTINEL: both columns are first set to a value no clock can produce, and a column either
still holds it afterwards or does not. Real TIMESTAMPTZ values, CURRENT_TIMESTAMP's sub-second resolution and the
database session time zone are tested on a real server in tests/test_pipeline_status_report_stamps_postgres.py; the
generated SQL itself is read in tests/test_main_api.py.

Every value is SYNTHETIC.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import main

_HASH_EXPR = "encode(digest(:token, 'sha256'), 'hex')"
SENTINEL = "1999-01-01 00:00:00"            # what a column holds until the server stamps it in these tests
CLIENT_SUPPLIED = "1988-08-08T08:08:08Z"    # what a request tries, and fails, to put there

# Every column the endpoint may write, as SQLite text, plus the two server-stamped instants.
_COLUMNS = (*main._PIPELINE_STATUS_UPDATABLE_FIELDS, "updated_at", "status_reported_at", "health_status_reported_at")

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    main.limiter.reset()


@pytest.fixture
def db(monkeypatch):
    """Two tenants -- (customer 10, branch 20) with token tok-1 and (customer 11, branch 21) with tok-2 -- and an
    installation of each state for the first."""
    assert _HASH_EXPR in main._AGENT_TOKEN_LOOKUP_SQL
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for statement in (
            "CREATE TABLE organizations (id INTEGER PRIMARY KEY, status TEXT, operational_customer_id INTEGER)",
            "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, status TEXT, operational_branch_id INTEGER)",
            ("CREATE TABLE agent_tokens (id INTEGER PRIMARY KEY, token_hash TEXT, customer_id INTEGER, branch_id INTEGER, "
             "is_active BOOLEAN, description TEXT, last_used_at TEXT, installation_id INTEGER)"),
            ("CREATE TABLE collector_installations (id INTEGER PRIMARY KEY, organization_id INTEGER, branch_id INTEGER, "
             "name TEXT, hostname TEXT, collector_version TEXT, status TEXT, installed_at TEXT, last_seen_at TEXT, "
             "created_at TEXT, updated_at TEXT)"),
            ("CREATE TABLE pipeline_status (customer_id INTEGER, branch_id INTEGER, "
             + ", ".join(f"{column} TEXT" for column in _COLUMNS) + ", UNIQUE (customer_id, branch_id))"),
            "INSERT INTO organizations VALUES (1, 'active', 10), (2, 'active', 11)",
            "INSERT INTO branches VALUES (1, 1, 'active', 20), (2, 2, 'active', 21)",
            ("INSERT INTO agent_tokens (token_hash, customer_id, branch_id, is_active, description) VALUES "
             "('tok-1', 10, 20, 1, 'x'), ('tok-2', 11, 21, 1, 'x')"),
            ("INSERT INTO collector_installations (id, organization_id, branch_id, name, status) VALUES "
             "(101, 1, 1, 'Sorter', 'active'), (104, 1, 1, 'Old sorter', 'inactive'), (201, 2, 2, 'Other', 'active')"),
        ):
            conn.execute(text(statement))
    monkeypatch.setattr(main, "engine", engine)
    monkeypatch.setattr(main, "_AGENT_TOKEN_LOOKUP_SQL", main._AGENT_TOKEN_LOOKUP_SQL.replace(_HASH_EXPR, ":token"))
    # SQLite has no JSONB cast; nothing else about the request path is altered.
    monkeypatch.setattr(main, "_pipeline_status_column_sql", lambda field: f":{field}")
    yield engine
    engine.dispose()


def post(body: dict, *, token="tok-1", customer_id=10, branch_id=20):
    return client.post("/upload-pipeline-status", json={"customer_id": customer_id, "branch_id": branch_id, **body},
                       headers={"Authorization": f"Bearer {token}"})


def row(engine, customer_id=10, branch_id=20) -> dict | None:
    with engine.connect() as conn:
        found = conn.execute(text("SELECT * FROM pipeline_status WHERE customer_id = :c AND branch_id = :b"),
                             {"c": customer_id, "b": branch_id}).mappings().first()
    return dict(found) if found else None


def stamps(engine, customer_id=10, branch_id=20) -> tuple:
    found = row(engine, customer_id, branch_id)
    return (found["status_reported_at"], found["health_status_reported_at"])


def seed_sentinels(engine, customer_id=10, branch_id=20, *, status="completed", health_status="healthy") -> None:
    """An existing row whose two stamps (and updated_at) hold the sentinel."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM pipeline_status WHERE customer_id = :c AND branch_id = :b"),
                     {"c": customer_id, "b": branch_id})
        conn.execute(text("INSERT INTO pipeline_status (customer_id, branch_id, status, health_status, updated_at, "
                          "status_reported_at, health_status_reported_at) VALUES (:c, :b, :s, :h, :t, :t, :t)"),
                     {"c": customer_id, "b": branch_id, "s": status, "h": health_status, "t": SENTINEL})


def _stamped(value) -> bool:
    """A value the server's clock put there: present, and neither the sentinel nor anything a client sent."""
    return value is not None and value != SENTINEL and "1988" not in str(value)


RUN_REPORT = {"status": "completed", "last_attempt": "2026-10-05T18:45:00Z", "last_run": "2026-10-05T18:45:03Z",
              "checkins_rows": 4, "rejects_rows": 0, "acs_rows": 2, "uploaded_checkins_rows": 4}
HEARTBEAT = {"health_status": "healthy", "pending_outbox_count": 0, "quarantined_count": 0,
             "last_success_at": "2026-10-05T18:44:00Z", "watcher_last_active_at": "2026-10-05T18:45:00Z"}
# Exactly the probe collector/preflight.py::_check_auth_and_scope posts at install and update time.
PREFLIGHT_PROBE = {"status": "preflight_check", "last_attempt": "2026-10-05T18:45:00.000000Z",
                   "checkins_rows": 0, "rejects_rows": 0, "acs_rows": 0}


# =====================================================================================================================
# Each signal stamps its own column, on an existing row
# =====================================================================================================================

def test_a_status_only_report_advances_status_reported_at_and_not_the_health_stamp(db):
    seed_sentinels(db)

    assert post(RUN_REPORT).status_code == 200

    status_at, health_at = stamps(db)
    assert _stamped(status_at)
    assert health_at == SENTINEL            # untouched: this request carried no health_status
    assert row(db)["health_status"] == "healthy"   # and the other writer's value is still its own


def test_a_health_status_only_report_advances_health_status_reported_at_and_not_the_status_stamp(db):
    seed_sentinels(db)

    assert post(HEARTBEAT).status_code == 200

    status_at, health_at = stamps(db)
    assert _stamped(health_at)
    assert status_at == SENTINEL            # untouched: this request carried no status
    assert row(db)["status"] == "completed"


def test_a_report_carrying_both_signals_advances_both_stamps_to_the_same_instant(db):
    seed_sentinels(db)

    assert post({**RUN_REPORT, **HEARTBEAT}).status_code == 200

    status_at, health_at = stamps(db)
    assert _stamped(status_at) and _stamped(health_at)
    assert status_at == health_at           # one statement, one reading of the clock


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"checkins_rows": 9, "last_attempt": "2026-10-05T18:45:00Z", "last_run": "2026-10-05T18:45:03Z"},
        {"pending_outbox_count": 3, "quarantined_count": 1, "watcher_last_active_at": "2026-10-05T18:45:00Z"},
        {"last_error": None, "last_failure_category": "retryable_infra"},
        {"destination_breakdown": {"Main": 4}},
        {"installation_id": 101, "collector_version": "0.0.1-synthetic"},
    ],
    ids=["nothing but the tenant", "run fields without status", "heartbeat fields without health_status",
         "error fields", "destination breakdown", "installation linkage only"],
)
def test_a_report_carrying_neither_signal_advances_neither_stamp(db, body):
    seed_sentinels(db)

    assert post(body).status_code == 200

    assert stamps(db) == (SENTINEL, SENTINEL)     # another column changing is not a report of either signal
    assert row(db)["updated_at"] != SENTINEL      # ...though the row WAS written, as before


def test_a_second_status_report_advances_its_stamp_again_and_leaves_the_health_stamp_as_it_was(db):
    assert post(HEARTBEAT).status_code == 200                 # the health stamp is set by a real heartbeat first
    assert post(RUN_REPORT).status_code == 200
    _, health_before = stamps(db)
    with db.begin() as conn:      # wind the status stamp back, so that a second stamping is visible within one second
        conn.execute(text("UPDATE pipeline_status SET status_reported_at = :t"), {"t": SENTINEL})

    assert post({**RUN_REPORT, "status": "completed_no_new_rows"}).status_code == 200

    status_at, health_at = stamps(db)
    assert _stamped(status_at)
    assert health_at == health_before and _stamped(health_at)   # the earlier heartbeat's instant, byte for byte


def test_a_second_health_report_advances_its_stamp_again_and_leaves_the_status_stamp_as_it_was(db):
    assert post(RUN_REPORT).status_code == 200
    assert post(HEARTBEAT).status_code == 200
    status_before, _ = stamps(db)
    with db.begin() as conn:
        conn.execute(text("UPDATE pipeline_status SET health_status_reported_at = :t"), {"t": SENTINEL})

    assert post({**HEARTBEAT, "health_status": "degraded"}).status_code == 200

    status_at, health_at = stamps(db)
    assert _stamped(health_at)
    assert status_at == status_before and _stamped(status_at)


def test_the_two_writers_alternating_each_move_only_their_own_stamp(db):
    # The scheduled collector and the continuous agent against one row, as during a parallel-validation window.
    seed_sentinels(db)
    moved = []
    for body in (RUN_REPORT, HEARTBEAT, HEARTBEAT, RUN_REPORT, {"checkins_rows": 1}, {**RUN_REPORT, **HEARTBEAT}):
        with db.begin() as conn:
            conn.execute(text("UPDATE pipeline_status SET status_reported_at = :t, health_status_reported_at = :t"),
                         {"t": SENTINEL})
        assert post(body).status_code == 200
        moved.append(tuple(_stamped(value) for value in stamps(db)))

    assert moved == [(True, False), (False, True), (False, True), (True, False), (False, False), (True, True)]


# =====================================================================================================================
# The first report for a branch (the INSERT branch of the upsert)
# =====================================================================================================================

def test_a_first_ever_status_report_creates_the_row_with_only_the_status_stamp(db):
    assert row(db) is None

    assert post(RUN_REPORT).status_code == 200

    status_at, health_at = stamps(db)
    assert _stamped(status_at) and health_at is None    # never reported: NULL, not a stamp


def test_a_first_ever_heartbeat_creates_the_row_with_only_the_health_stamp(db):
    assert post(HEARTBEAT).status_code == 200

    status_at, health_at = stamps(db)
    assert _stamped(health_at) and status_at is None


def test_a_first_ever_report_carrying_neither_signal_creates_the_row_with_no_stamp(db):
    assert post({"checkins_rows": 2}).status_code == 200

    assert stamps(db) == (None, None)
    assert row(db)["updated_at"] is not None


# =====================================================================================================================
# The install / update preflight probe
# =====================================================================================================================

def test_a_preflight_probe_advances_the_status_stamp_and_never_the_health_stamp(db):
    seed_sentinels(db)

    assert post(PREFLIGHT_PROBE).status_code == 200

    status_at, health_at = stamps(db)
    assert _stamped(status_at)              # it carries `status`, so it is a report of the run status: intentional
    assert health_at == SENTINEL
    assert row(db)["status"] == "preflight_check"


def test_a_preflight_probe_on_a_branch_that_never_reported_leaves_the_health_stamp_null(db):
    assert post({**PREFLIGHT_PROBE, "installation_id": 101, "collector_version": "0.0.1-synthetic"}).status_code == 200

    status_at, health_at = stamps(db)
    assert _stamped(status_at) and health_at is None


# =====================================================================================================================
# Presence, not value
# =====================================================================================================================

def test_a_signal_sent_as_an_explicit_null_was_still_reported(db):
    seed_sentinels(db)

    assert post({"status": None}).status_code == 200

    status_at, health_at = stamps(db)
    assert _stamped(status_at) and health_at == SENTINEL
    assert row(db)["status"] is None        # the existing partial-update rule: an explicit null clears the value


def test_an_omitted_signal_is_never_stamped_on_the_strength_of_its_stored_value(db):
    # The row already HOLDS a status and a health_status. Neither is in the request, so neither stamp moves.
    seed_sentinels(db, status="failed_upload", health_status="degraded")

    assert post({"rejects_rows": 1}).status_code == 200

    assert stamps(db) == (SENTINEL, SENTINEL)
    assert (row(db)["status"], row(db)["health_status"]) == ("failed_upload", "degraded")


# =====================================================================================================================
# The server's clock, never the client's
# =====================================================================================================================

def test_a_client_cannot_supply_either_stamp(db):
    seed_sentinels(db)

    response = post({**RUN_REPORT, **HEARTBEAT, "status_reported_at": CLIENT_SUPPLIED,
                     "health_status_reported_at": CLIENT_SUPPLIED})

    assert response.status_code == 200      # the model's policy for an undeclared key is unchanged: ignored
    status_at, health_at = stamps(db)
    assert _stamped(status_at) and _stamped(health_at)
    assert "1988" not in str(row(db))       # the supplied value is nowhere in the row


def test_supplying_a_stamp_without_its_signal_stamps_nothing(db):
    seed_sentinels(db)

    assert post({"checkins_rows": 1, "status_reported_at": CLIENT_SUPPLIED,
                 "health_status_reported_at": CLIENT_SUPPLIED}).status_code == 200

    assert stamps(db) == (SENTINEL, SENTINEL)


def test_none_of_the_clients_own_timestamps_becomes_a_stamp(db):
    seed_sentinels(db)
    body = {**RUN_REPORT, **HEARTBEAT, "last_attempt": "1988-01-01T00:00:00Z", "last_run": "1988-01-01T00:00:01Z",
            "last_success_at": "1988-01-01T00:00:02Z", "watcher_last_active_at": "1988-01-01T00:00:03Z",
            "oldest_pending_event_at": "1988-01-01T00:00:04Z"}

    assert post(body).status_code == 200

    found = row(db)
    assert found["last_run"] == "1988-01-01T00:00:01Z"      # stored as sent, as before...
    assert _stamped(found["status_reported_at"]) and _stamped(found["health_status_reported_at"])   # ...and not here


# =====================================================================================================================
# What is unchanged
# =====================================================================================================================

def test_updated_at_still_moves_on_every_accepted_report_whatever_it_carries(db):
    for body in ({}, {"checkins_rows": 1}, RUN_REPORT, HEARTBEAT, {**RUN_REPORT, **HEARTBEAT}, PREFLIGHT_PROBE):
        seed_sentinels(db)

        assert post(body).status_code == 200

        assert row(db)["updated_at"] != SENTINEL, body


def test_the_response_is_exactly_what_it_was(db):
    for body in ({}, RUN_REPORT, HEARTBEAT, {**RUN_REPORT, **HEARTBEAT}, PREFLIGHT_PROBE):
        response = post(body)

        assert response.status_code == 200
        assert response.json() == {"status": "success", "message": "Pipeline status uploaded"}   # no stamp is echoed


def test_every_other_column_is_written_exactly_as_before(db):
    assert post({**RUN_REPORT, **HEARTBEAT}).status_code == 200

    found = row(db)
    for field, value in {**RUN_REPORT, **HEARTBEAT}.items():
        assert str(found[field]) == str(value), field     # the stand-in table stores everything as text
    # A partial update still leaves the other writer's columns alone.
    assert post({"health_status": "degraded"}).status_code == 200
    assert row(db)["status"] == "completed" and row(db)["checkins_rows"] == found["checkins_rows"]


def test_one_tenants_report_never_stamps_another_tenants_row(db):
    seed_sentinels(db, 10, 20)
    seed_sentinels(db, 11, 21)

    assert post({**RUN_REPORT, **HEARTBEAT}, token="tok-1").status_code == 200

    assert all(_stamped(value) for value in stamps(db, 10, 20))
    assert stamps(db, 11, 21) == (SENTINEL, SENTINEL)
    assert row(db, 11, 21)["updated_at"] == SENTINEL


# =====================================================================================================================
# A refused or failed request stamps nothing
# =====================================================================================================================

REFUSED = {
    "no authorization header": (lambda: client.post("/upload-pipeline-status",
                                                    json={"customer_id": 10, "branch_id": 20, **RUN_REPORT, **HEARTBEAT}), 401),
    "unknown token": (lambda: post({**RUN_REPORT, **HEARTBEAT}, token="no-such-token"), 401),
    "another tenant's token": (lambda: post({**RUN_REPORT, **HEARTBEAT}, token="tok-2"), 403),
    "an installation that is not active": (lambda: post({**RUN_REPORT, **HEARTBEAT, "installation_id": 104}), 403),
    "another tenant's installation": (lambda: post({**RUN_REPORT, **HEARTBEAT, "installation_id": 201}), 403),
    "an unknown installation": (lambda: post({**RUN_REPORT, **HEARTBEAT, "installation_id": 999}), 403),
    "an invalid health_status": (lambda: post({**RUN_REPORT, "health_status": "excellent"}), 422),
    "a malformed installation_id": (lambda: post({**RUN_REPORT, **HEARTBEAT, "installation_id": "abc"}), 422),
}


@pytest.mark.parametrize("name", list(REFUSED))
def test_a_refused_report_stamps_nothing_on_an_existing_row(db, name):
    seed_sentinels(db)
    before = row(db)
    send, expected_status = REFUSED[name]

    response = send()

    assert response.status_code == expected_status
    assert stamps(db) == (SENTINEL, SENTINEL)
    assert row(db) == before                # not updated_at, not a value: the row is exactly as it was


@pytest.mark.parametrize("name", list(REFUSED))
def test_a_refused_report_creates_no_row(db, name):
    send, expected_status = REFUSED[name]

    assert send().status_code == expected_status
    assert row(db) is None


def test_a_suspended_organizations_report_stamps_nothing(db):
    seed_sentinels(db)
    with db.begin() as conn:
        conn.execute(text("UPDATE organizations SET status = 'suspended' WHERE id = 1"))

    assert post({**RUN_REPORT, **HEARTBEAT}).status_code == 403
    assert stamps(db) == (SENTINEL, SENTINEL)


class _RollsBackAtCommit:
    """main.engine, except that the transaction is rolled back -- and an error raised -- at the moment it would have
    committed: everything the endpoint did, the upsert included, ran first."""

    def __init__(self, engine):
        self._engine = engine
        self.statements: list[str] = []

    def begin(self):
        outer, inner = self, self._engine.begin()

        class _Context:
            def __enter__(self):
                self.conn = inner.__enter__()
                return _Recording(self.conn, outer.statements)

            def __exit__(self, *_exc):
                failure = RuntimeError("synthetic failure at commit")
                inner.__exit__(RuntimeError, failure, None)     # rolls the transaction back
                raise failure

        return _Context()


class _Recording:
    def __init__(self, conn, statements):
        self._conn, self._statements = conn, statements

    def execute(self, statement, params=None):
        self._statements.append(" ".join(str(statement).split()))
        return self._conn.execute(statement, params)


def test_a_report_whose_transaction_does_not_commit_stamps_nothing(db, monkeypatch):
    seed_sentinels(db)
    before = row(db)
    failing = _RollsBackAtCommit(db)
    monkeypatch.setattr(main, "engine", failing)

    response = post({**RUN_REPORT, **HEARTBEAT, "installation_id": 101})

    assert response.status_code == 500 and response.json() == {"detail": "Internal server error"}
    # The stamping statement really ran inside the transaction...
    upserts = [sql for sql in failing.statements if sql.startswith("INSERT INTO pipeline_status")]
    assert len(upserts) == 1
    assert "status_reported_at = CURRENT_TIMESTAMP" in upserts[0]
    assert "health_status_reported_at = CURRENT_TIMESTAMP" in upserts[0]
    # ...and none of it survived: the stamps are part of the same transaction as everything else.
    assert row(db) == before and stamps(db) == (SENTINEL, SENTINEL)


def test_a_failed_first_report_leaves_no_row_and_so_no_stamp(db, monkeypatch):
    monkeypatch.setattr(main, "engine", _RollsBackAtCommit(db))

    assert post({**RUN_REPORT, **HEARTBEAT}).status_code == 500
    assert row(db) is None
