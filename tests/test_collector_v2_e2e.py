"""Contract v2 collector: end to end against the REAL FastAPI app (main.py) on the merged v2 server code.

The collector's `run_once_v2` talks to the actual `/v2/upload` and `/v2/status` endpoints through Starlette's TestClient, over the same
SQLite-backed harness the server's own tests use (tests/test_ingest_v2_api.py: tenant and key tables, v2 tables, SQLite copies of the v1
triggers). Nothing is scripted: real validation, real dedup/conflict detection, real 409/422 bodies, real heartbeat persistence.

What the real PostgreSQL adds (constraints, the unique index under concurrency) is proved in tests/test_ingest_v2_postgres.py.
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta

import pytest
from collector_v2_support import (
    BARCODE_CI,
    FakeStore,
    acs_line,
    find_leaks,
    full_corpus,
    item_message,
    minutes_ago,
    patron_message,
    write_lines,
)
from sqlalchemy import text
from test_collector_v2_run import Env, checkin_lines, empty_other_sources
from test_ingest_v2_api import (  # noqa: F401 -- the autouse limiter reset is a fixture this module reuses
    BRANCH,
    CUSTOMER,
    KEY,
    OTHER_TENANT_KEY,
    OTHER_TOKEN,
    RETIRED_KEY,
    UNKNOWN_KEY,
    _reset_rate_limiter,
    client,
)
from test_ingest_v2_api import (
    TOKEN as SERVER_TOKEN,
)
from test_ingest_v2_api import (
    db as server_db,  # noqa: F401 -- the server harness's database fixture, requested by name below
)

import main
from collector import v2_run

BASE = minutes_ago(120)
NEW_TABLES = ("checkin_events", "reject_events", "acs_item_events")


class ServerSession:
    """A `requests.Session` stand-in that hands every POST to the real FastAPI app."""

    def __init__(self, token=SERVER_TOKEN):
        self.token = token
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        del timeout
        headers = {**(headers or {}), "Authorization": f"Bearer {self.token}"}
        response = client.post(url.removeprefix("http://testserver"), json=json, headers=headers)
        self.calls.append((url, json, response.status_code))
        return response

    def uploads(self):
        return [(body, code) for url, body, code in self.calls if url.endswith("/v2/upload")]

    def statuses(self):
        return [(body, code) for url, body, code in self.calls if url.endswith("/v2/status")]


@pytest.fixture
def e2e(tmp_path, monkeypatch, server_db):  # noqa: F811 -- a pytest fixture is requested by its (imported) name
    from collector.config import load_config

    env = Env(tmp_path, monkeypatch)
    monkeypatch.setenv("SORTVIEW_API_TOKEN", SERVER_TOKEN)
    env.cfg = load_config(env.config_path)
    assert env.v2.key_id == KEY                                   # the key the server test harness registered for this tenant
    env.db = server_db
    return env


def all_rows(database):
    return {table: database.rows(table) for table in NEW_TABLES}


def heartbeat_row(database):
    columns = ("health_status, last_error_class, pending_outbox_count, quarantined_count, oldest_pending_event_at, last_success_at, "
               "watcher_last_active_at, last_heartbeat_at")
    (row,) = database.rows("ingest_key_ids", columns, f"WHERE key_id = '{KEY}'")
    return dict(zip(columns.split(", "), row, strict=True))


# =====================================================================================================================
# A real delivery
# =====================================================================================================================

def test_a_full_run_is_accepted_by_the_real_server_and_lands_in_the_v2_tables(e2e):
    e2e.put_all(full_corpus(BASE))
    session = ServerSession()
    assert e2e.run(session) == 0
    assert [code for _b, code in session.uploads()] == [200, 200, 200] and [code for _b, code in session.statuses()] == [200]
    assert e2e.db.count("acs_item_events") == 7 and e2e.db.count("checkin_events") == 3 and e2e.db.count("reject_events") == 1
    states = sorted(row[0] for row in e2e.db.rows("acs_item_events", "state"))
    assert states == ["hold"] * 5 + ["non_hold_101", "other_code10"]


def test_the_v1_tables_and_triggers_are_never_touched(e2e):
    e2e.put_all(full_corpus(BASE))
    e2e.run(ServerSession())
    assert e2e.db.v1_rows() == 0 and e2e.db.v1_statements() == []


def test_the_stored_v2_rows_carry_no_raw_value(e2e):
    e2e.put_all(full_corpus(BASE))
    e2e.run(ServerSession())
    blob = json.dumps(all_rows(e2e.db), default=str).encode() + json.dumps(e2e.db.rows("ingest_key_ids"), default=str).encode()
    assert find_leaks(blob) == []
    # ... including the hex/base64 spellings of a barcode the row could hold by mistake
    assert BARCODE_CI.encode().hex().encode() not in blob and base64.b64encode(BARCODE_CI.encode()) not in blob


def test_the_item_key_the_server_stores_is_not_the_barcode_and_is_stable_for_the_same_item(e2e):
    corpus = full_corpus(BASE)
    e2e.put_all(corpus)
    e2e.run(ServerSession())
    keys = {row[0] for row in e2e.db.rows("checkin_events", "item_key") if row[0]}
    rejects = {row[0] for row in e2e.db.rows("reject_events", "item_key") if row[0]}
    assert all(len(k) == 64 and BARCODE_CI not in k for k in keys | rejects)


def test_the_heartbeat_is_persisted_with_the_v2_fields(e2e):
    e2e.put_all(full_corpus(BASE))
    e2e.run(ServerSession())
    row = heartbeat_row(e2e.db)
    assert row["health_status"] == "healthy" and row["last_error_class"] is None
    assert row["pending_outbox_count"] == 0 and row["quarantined_count"] == 0 and row["oldest_pending_event_at"] is None
    assert row["last_success_at"] and row["watcher_last_active_at"] and row["last_heartbeat_at"]


def test_resending_the_same_events_is_idempotent(e2e):
    e2e.put_all(full_corpus(BASE))
    session = ServerSession()
    e2e.run(session)
    before = all_rows(e2e.db)
    e2e.v2.state_path.unlink()                                    # cursor lost: everything is re-read and re-sent
    again = ServerSession()
    assert e2e.run(again) == 0
    assert all(code == 200 for _b, code in again.uploads()) and all_rows(e2e.db) == before
    assert e2e.quarantine() == [] and e2e.status()["health_status"] == "healthy"


def test_the_ruleset_id_is_stable_across_runs_so_a_resend_is_a_duplicate_not_a_conflict(e2e):
    e2e.put_all(full_corpus(BASE))
    e2e.run(ServerSession())
    ids = {row[0] for row in e2e.db.rows("acs_item_events", "ruleset_id", "WHERE state = 'hold'")}
    e2e.v2.state_path.unlink()
    e2e.run(ServerSession())
    assert {row[0] for row in e2e.db.rows("acs_item_events", "ruleset_id", "WHERE state = 'hold'")} == ids and len(ids) == 1


def test_appended_lines_are_delivered_incrementally(e2e):
    e2e.put_all(full_corpus(BASE))
    e2e.run(ServerSession())
    write_lines(e2e.tech / "checkins.txt", checkin_lines(3, start=300, base=BASE), append=True)
    session = ServerSession()
    e2e.run(session)
    assert sum(len(b["checkins"]) for b, _c in session.uploads()) == 3 and e2e.db.count("checkin_events") == 6


def test_a_later_non_hold_record_for_the_same_item_is_delivered_as_a_separate_retraction_event(e2e):
    hold = acs_line(BASE, item_message("CANARY-BARCODE-RETRACT", "CANARY-PATRON-A", "Main"))
    retract = acs_line(BASE + timedelta(minutes=5), item_message("CANARY-BARCODE-RETRACT", "CANARY-PATRON-A", "Main", prefix="101NNY"))
    e2e.put(acs=[acs_line(BASE - timedelta(seconds=5), patron_message("CANARY-PATRON-A", "CANARY-NAME", "ADULT")), hold, retract],
            checkins=[], rejects=[])
    assert e2e.run(ServerSession()) == 0
    rows = e2e.db.rows("acs_item_events", "state, item_key, event_time", "ORDER BY event_time")
    assert [r[0] for r in rows] == ["hold", "non_hold_101"] and rows[0][1] == rows[1][1]


# =====================================================================================================================
# Real 409 conflicts
# =====================================================================================================================

def corrupt_rows(db, table, column, value, limit=None):
    """Make stored rows disagree with what the collector will re-send (same event_key, different content): a real conflict."""
    with db.engine.begin() as conn:
        ids = [r[0] for r in conn.execute(text(f"SELECT id FROM {table} ORDER BY id"))][:limit]  # nosec B608 - test SQL, fixed names
        for row_id in ids:
            conn.execute(text(f"UPDATE {table} SET {column} = :v WHERE id = :i"), {"v": value, "i": row_id})  # nosec B608
    return len(ids)


def test_a_real_409_is_quarantined_and_the_remaining_events_are_delivered(e2e):
    empty_other_sources(e2e, checkins=checkin_lines(6, base=BASE))
    e2e.run(ServerSession())
    corrupt_rows(e2e.db, "checkin_events", "destination", "main", limit=2)
    e2e.v2.state_path.unlink()
    session = ServerSession()
    assert e2e.run(session) == 0
    codes = [code for _b, code in session.uploads()]
    assert codes[0] == 409 and codes[-1] == 200
    assert len(e2e.quarantine()) == 2 and e2e.db.count("checkin_events") == 6
    assert e2e.cursor_offset("checkins") == (e2e.tech / "checkins.txt").stat().st_size
    row = heartbeat_row(e2e.db)
    assert (row["health_status"], row["last_error_class"], row["quarantined_count"]) == ("degraded", "permanent_rejection", 2)


def test_more_than_fifty_real_conflicts_are_resolved_across_rounds(tmp_path, monkeypatch, server_db):  # noqa: F811 -- a pytest fixture is requested by its (imported) name
    from collector.config import load_config

    env = Env(tmp_path, monkeypatch, chunk_lines=400)
    monkeypatch.setenv("SORTVIEW_API_TOKEN", SERVER_TOKEN)
    env.cfg = load_config(env.config_path)
    empty_other_sources(env, checkins=checkin_lines(130, base=BASE))
    env.run(ServerSession())
    assert server_db.count("checkin_events") == 130
    assert corrupt_rows(server_db, "checkin_events", "destination", "main", limit=120) == 120
    env.v2.state_path.unlink()
    session = ServerSession()
    assert env.run(session) == 0
    codes = [code for _b, code in session.uploads()]
    assert codes.count(409) >= 3 and codes[-1] == 200                    # 120 conflicts / 50 per response = at least three 409 rounds
    assert len(env.quarantine()) == 120 and server_db.count("checkin_events") == 130
    assert env.cursor_offset("checkins") == (env.tech / "checkins.txt").stat().st_size
    assert heartbeat_row(server_db)["quarantined_count"] == 120


def test_a_quarantined_event_is_not_sent_again_on_the_next_run(e2e):
    empty_other_sources(e2e, checkins=checkin_lines(4, base=BASE))
    e2e.run(ServerSession())
    corrupt_rows(e2e.db, "checkin_events", "destination", "main", limit=1)
    e2e.v2.state_path.unlink()
    e2e.run(ServerSession())
    e2e.v2.state_path.unlink()
    third = ServerSession()
    assert e2e.run(third) == 0
    assert [code for _b, code in third.uploads()] == [200]               # no 409 round: the conflicting event is skipped locally
    assert sum(len(b["checkins"]) for b, _c in third.uploads()) == 3


# =====================================================================================================================
# Real failures
# =====================================================================================================================

def test_an_unregistered_key_is_refused_and_nothing_is_committed(e2e):
    e2e.v2 = e2e.v2.__class__(**{**e2e.v2.__dict__, "key_id": UNKNOWN_KEY})
    e2e.store = FakeStore(bound_key_id=UNKNOWN_KEY)
    e2e.put_all(full_corpus(BASE))
    session = ServerSession()
    assert e2e.run(session) == 1
    assert session.uploads()[0][1] == 403 and e2e.cursor_offset("acs") is None
    assert e2e.status()["last_error_class"] == "auth_failure" and e2e.db.v2_rows() == 0


def test_a_retired_key_is_refused(e2e):
    e2e.db.retire(KEY)
    e2e.put_all(full_corpus(BASE))
    assert e2e.run(ServerSession()) == 1
    assert e2e.status()["last_error_class"] == "auth_failure" and e2e.db.v2_rows() == 0


def test_another_tenants_key_is_refused_and_no_row_is_written_for_either_tenant(e2e):
    e2e.v2 = e2e.v2.__class__(**{**e2e.v2.__dict__, "key_id": OTHER_TENANT_KEY})
    e2e.store = FakeStore(bound_key_id=OTHER_TENANT_KEY)
    e2e.put_all(full_corpus(BASE))
    assert e2e.run(ServerSession()) == 1
    assert e2e.db.v2_rows() == 0 and e2e.status()["last_error_class"] == "auth_failure"


def test_a_bad_token_is_an_auth_failure(e2e):
    e2e.put_all(full_corpus(BASE))
    assert e2e.run(ServerSession(token="CANARY-WRONG-TOKEN-XYZ")) == 1
    assert e2e.status()["health_status"] == "error" and e2e.status()["last_error_class"] == "auth_failure"
    assert b"CANARY-WRONG-TOKEN" not in e2e.cfg.log_path.read_bytes()


def test_another_tenants_token_cannot_deliver_this_installs_key(e2e):
    e2e.put_all(full_corpus(BASE))
    assert e2e.run(ServerSession(token=OTHER_TOKEN)) == 1 and e2e.db.v2_rows() == 0


def test_v2_ingest_switched_off_on_the_server_is_a_configuration_error(e2e, monkeypatch):
    monkeypatch.setattr(main, "V2_INGEST_ENABLED", False)
    e2e.put_all(full_corpus(BASE))
    session = ServerSession()
    assert e2e.run(session) == 2
    assert session.uploads()[0][1] == 404 and e2e.status()["last_error_class"] == "configuration_error"
    assert e2e.cursor_offset("acs") is None


def test_a_retired_key_id_in_the_secret_store_binding_fails_before_any_request(e2e):
    e2e.store = FakeStore(bound_key_id="6b7c8d9e-0f1a-4b2c-9d3e-4f5a6b7c8d9e")
    e2e.put_all(full_corpus(BASE))
    session = ServerSession()
    assert e2e.run(session) == 2 and session.calls[0][0].endswith("/v2/status") and len(session.calls) == 1
    assert RETIRED_KEY == "6b7c8d9e-0f1a-4b2c-9d3e-4f5a6b7c8d9e"


def test_a_server_side_v2_failure_is_retryable_and_resent_identically(e2e, monkeypatch):
    e2e.put_all(full_corpus(BASE))
    real = main.engine

    class Boom:
        def begin(self):
            raise RuntimeError("CANARY-DATABASE-DOWN")

        def connect(self):
            raise RuntimeError("CANARY-DATABASE-DOWN")

    monkeypatch.setattr(main, "engine", Boom())
    failing = ServerSession()
    assert e2e.run(failing) == 1
    assert e2e.status()["last_error_class"] in ("retryable_infra", "auth_failure", "other")
    assert e2e.cursor_offset("acs") is None
    assert b"CANARY-DATABASE-DOWN" not in e2e.cfg.log_path.read_bytes() + e2e.v2.status_path.read_bytes()
    monkeypatch.setattr(main, "engine", real)
    assert e2e.run(ServerSession()) == 0 and e2e.db.count("acs_item_events") == 7


def test_the_dry_run_sends_nothing_to_the_real_server(e2e):
    e2e.put_all(full_corpus(BASE))
    before = len(e2e.db.sent)
    code, output = e2e.dry()
    assert code == 0 and "network_calls=0" in output and len(e2e.db.sent) == before and e2e.db.v2_rows() == 0


def test_main_entry_with_a_real_config_file_delivers_to_the_real_server(e2e, monkeypatch):
    from collector import run as collector_run
    e2e.put_all(full_corpus(BASE))
    session = ServerSession()
    monkeypatch.setattr(v2_run, "DpapiSecretStore", lambda _path: FakeStore())
    monkeypatch.setattr(v2_run.uploader, "build_session", lambda: session)
    assert collector_run.main(["--config", str(e2e.config_path)]) == 0
    assert e2e.db.count("acs_item_events") == 7 and heartbeat_row(e2e.db)["health_status"] == "healthy"


def test_events_from_two_runs_with_two_different_secrets_do_not_collide_or_link(tmp_path, monkeypatch, server_db):  # noqa: F811 -- a pytest fixture is requested by its (imported) name
    """A NEW key_id (a new secret) is a fresh identity space: the same log under another secret shares no event_key or item_key."""
    from collector.config import load_config
    from collector.v2_config import load_v2_config

    env = Env(tmp_path, monkeypatch)
    monkeypatch.setenv("SORTVIEW_API_TOKEN", SERVER_TOKEN)
    env.cfg = load_config(env.config_path)
    env.put_all(full_corpus(BASE))
    env.run(ServerSession())
    first = {row[0] for row in server_db.rows("checkin_events", "item_key") if row[0]}

    document = json.loads(env.config_path.read_text())
    document["v2"]["key_id"] = "5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d"       # the tenant's second active key in the harness
    document["v2"]["state_path"] = str(env.root / "data" / "v2" / "state_second.json")
    env.config_path.write_text(json.dumps(document))
    env.v2 = load_v2_config(env.config_path, require=True)
    env.store = FakeStore(master=bytes(reversed(range(32))), bound_key_id="5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d")
    assert env.run(ServerSession()) == 0
    second = {row[0] for row in server_db.rows("checkin_events", "item_key", "WHERE key_id = '5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d'") if row[0]}
    assert first and second and first.isdisjoint(second)
    assert CUSTOMER == 10 and BRANCH == 1


# =====================================================================================================================
# Late message-64 corrections, through the real server
# =====================================================================================================================

def small_chunk_env(tmp_path, monkeypatch, server_db, **v2_extra):  # noqa: F811 -- the imported `server_db` fixture's value
    from collector.config import load_config

    env = Env(tmp_path, monkeypatch, chunk_lines=2, **v2_extra)
    monkeypatch.setenv("SORTVIEW_API_TOKEN", SERVER_TOKEN)
    env.cfg = load_config(env.config_path)
    env.db = server_db
    return env


def stored_flags(db, barcodes):
    """Reduce the PERSISTED stream as docs/contract-v2-design.md section 9.4 says readers must: the greatest (event_time, id) per item among
    hold/non_hold_101 events; barcode -> flags for the items whose latest state is a hold."""
    from collector_v2_support import MASTER

    from collector import v2_identity
    keys = v2_identity.derive_subkeys(MASTER)
    by_item = {v2_identity.item_key(keys, b): b for b in barcodes}
    latest = {}
    for row_id, when, item_key, state, ill, branch, collection in db.rows(
            "acs_item_events", "id, event_time, item_key, state, is_ill, is_branch_services, is_collection_services", "ORDER BY id"):
        if state == "other_code10":
            continue
        rank = (str(when), row_id)
        if item_key not in latest or rank > latest[item_key][0]:
            latest[item_key] = (rank, state, (bool(ill), bool(branch), bool(collection)))
    return {by_item[k]: flags for k, (_r, state, flags) in latest.items() if state == "hold"}


@pytest.mark.parametrize("scenario", [
    "ILL patron type after the hold", "branch services name after the hold", "collection services name after the hold",
    "profile changes from ADULT to ILL", "profile changes from ILL back to ADULT", "two held items of one patron",
    "A to B to A: unknown, then ILL, then ADULT again", "two patrons, each corrected independently",
    "a later non-hold retracts the hold before the profile arrives",
])
def test_the_real_servers_stored_stream_reduces_to_the_dashboards_classification(tmp_path, monkeypatch, server_db, scenario):  # noqa: F811
    from test_collector_v2_late_profile import SCENARIOS, dashboard_flags

    from agent.parser import acs as acs_parser
    lines = SCENARIOS[scenario]
    env = small_chunk_env(tmp_path, monkeypatch, server_db)
    empty_other_sources(env, acs=lines)
    session = ServerSession()
    assert env.run(session) == 0
    assert {code for _b, code in session.uploads()} == {200} and env.quarantine() == []
    barcodes = sorted({b for b in acs_parser.parse_lines(lines)["barcode"].dropna() if b})
    assert stored_flags(server_db, barcodes) == dashboard_flags(lines)
    assert server_db.v1_rows() == 0


def test_a_flip_flopping_profile_is_not_swallowed_by_the_servers_dedup(tmp_path, monkeypatch, server_db):  # noqa: F811
    """A -> B -> A: the third event has the first one's flags. Without the revision it would share the first event's event_key and the server
    would call it a duplicate, leaving the item stuck on B. With it, three rows are stored and the last one wins."""
    from test_collector_v2_late_profile import SCENARIOS
    env = small_chunk_env(tmp_path, monkeypatch, server_db)
    empty_other_sources(env, acs=SCENARIOS["A to B to A: unknown, then ILL, then ADULT again"])
    assert env.run(ServerSession()) == 0
    from collector_v2_support import MASTER

    from collector import v2_identity
    target = v2_identity.item_key(v2_identity.derive_subkeys(MASTER), "B-ABA")
    item_rows = [(r[0], bool(r[1]), r[2], r[3]) for r in server_db.rows(
        "acs_item_events", "id, is_ill, event_key, event_time", f"WHERE item_key = '{target}' ORDER BY id")]
    assert [flag for _i, flag, _k, _t in item_rows] == [False, True, False]
    assert len({k for _i, _f, k, _t in item_rows}) == 3 and len({t for _i, _f, _k, t in item_rows}) == 1


def test_a_correction_never_conflicts_with_its_original_on_the_real_server(tmp_path, monkeypatch, server_db):  # noqa: F811
    from test_collector_v2_late_profile import SCENARIOS
    env = small_chunk_env(tmp_path, monkeypatch, server_db)
    empty_other_sources(env, acs=SCENARIOS["ILL patron type after the hold"])
    session = ServerSession()
    assert env.run(session) == 0
    assert 409 not in {code for _b, code in session.uploads()}
    assert env.status()["counters"]["acs_corrections"] == 1 and heartbeat_row(server_db)["health_status"] == "healthy"


def test_the_stored_stream_carries_no_patron_information_after_corrections(tmp_path, monkeypatch, server_db):  # noqa: F811
    from test_collector_v2_late_profile import P1, SCENARIOS
    env = small_chunk_env(tmp_path, monkeypatch, server_db)
    empty_other_sources(env, acs=SCENARIOS["ILL patron type after the hold"])
    env.run(ServerSession())
    stored = json.dumps(all_rows(server_db), default=str).encode()
    for needle in (P1, "SOME PERSON", "B-ITYPE"):
        assert needle.encode() not in stored and needle.encode().hex().encode() not in stored
    assert find_leaks(stored) == []


# =====================================================================================================================
# The ruleset_id is provenance -- and the server compares it
# =====================================================================================================================

def test_the_same_classification_resent_under_a_new_ruleset_id_is_idempotent(
        tmp_path, monkeypatch, server_db):  # noqa: F811
    import json as _json

    from collector_v2_support import RULES_DOC
    from test_collector_v2_late_profile import SCENARIOS

    from collector.config import load_config

    env = Env(tmp_path, monkeypatch)
    monkeypatch.setenv("SORTVIEW_API_TOKEN", SERVER_TOKEN)
    env.cfg = load_config(env.config_path)
    env.db = server_db

    empty_other_sources(env, acs=SCENARIOS["profile before the hold"])

    assert env.run(ServerSession()) == 0

    before = server_db.rows(
        "acs_item_events",
        "event_key, ruleset_id",
        "ORDER BY id",
    )
    first_ruleset = {r[1] for r in before}

    assert len(first_ruleset) == 1
    assert env.quarantine() == []

    env.v2.rules_path.write_text(
        _json.dumps(
            {
                **RULES_DOC,
                "collection_services_names": [
                    *RULES_DOC["collection_services_names"],
                    "ANOTHER",
                ],
            }
        ),
        encoding="utf-8",
    )

    env.v2.state_path.unlink()

    session = ServerSession()

    assert env.run(session) == 0

    assert {code for _b, code in session.uploads()} == {200}
    assert env.quarantine() == []

    after = server_db.rows(
        "acs_item_events",
        "event_key, ruleset_id",
        "ORDER BY id",
    )

    assert after == before
