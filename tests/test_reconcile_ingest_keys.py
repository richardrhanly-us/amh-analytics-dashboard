"""scripts/reconcile_ingest_keys.py with no database: how it refuses, and how E1 evidence is classified from plain rows.

CI has no PostgreSQL, so these run everywhere. tests/test_reconcile_ingest_keys_postgres.py is the same tool on a real one.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import reconcile_ingest_keys as tool

ROOT = Path(__file__).resolve().parent.parent
KEY = "3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e"
OTHER_KEY = "8d9e0f1a-2b3c-4d4e-9f5a-6b7c8d9e0f1a"
INSTANT = datetime(2026, 10, 12, 15, 30, 1, 123456, tzinfo=UTC)
LATER = INSTANT + timedelta(minutes=5)
NOTE = "initial v2 cutover | enrollment_code_id=900 installation_id=101"


def _key(key_id=KEY, **fields):
    return {"id": 1, "key_id": key_id, "customer_id": 10, "branch_id": 1, "status": "active", "created_at": INSTANT,
            "retired_at": None, "last_heartbeat_at": None, "health_status": None, "installation_id": None, **fields}


def _cutover(**fields):
    return {"id": 50, "customer_id": 10, "branch_id": 1, "cutover_at": INSTANT, "set_by": "collector-enrollment", "set_at": INSTANT,
            "note": NOTE, **fields}


def _installation(id_=101, **fields):
    return {"id": id_, "organization_id": 1, "branch_id": 1, "status": "active", "required_contract": 2, "last_seen_at": None, **fields}


def _token(**fields):
    return {"id": 7, "customer_id": 10, "branch_id": 1, "is_active": True, "installation_id": 101, "created_at": INSTANT.replace(tzinfo=None),
            "last_used_at": None, **fields}


def _code(**fields):
    return {"id": 900, "installation_id": 101, "used_at": INSTANT, "revoked_at": None, "required_contract": 2, **fields}


def _snapshot(*, keys=None, cutovers=None, codes=None, installations=None, tokens=None, events=None, branches=None):
    return {
        "keys": [_key()] if keys is None else keys,
        "cutovers": [_cutover()] if cutovers is None else cutovers,
        "codes": [_code()] if codes is None else codes,
        "installations": [_installation()] if installations is None else installations,
        "tokens": [_token()] if tokens is None else tokens,
        "events": events or [],
        "organizations": [{"id": 1, "slug": "acme", "status": "active", "operational_customer_id": 10}],
        "branches": [{"id": 1, "organization_id": 1, "status": "active", "operational_branch_id": 1}] if branches is None else branches,
        "database": {"generated_at": LATER, "name": "synthetic", "role": "operator", "server_version": "18", "superuser": False,
                     "bypass_rls": True, "alembic_revisions": ["c1e7a94f2b60"]},
        "schema": {"indexes": [], "constraints": [], "triggers": [], "policies": [], "row_level_security": []},
    }


def _e1(**snapshot):
    return tool.build_report(_snapshot(**snapshot))["keys"][0]["e1"]


def run(*argv):
    out = io.StringIO()
    return tool.main(list(argv), out=out), out.getvalue()


# --- E1 ---------------------------------------------------------------------------------------------------------------------

def test_a_complete_record_is_a_candidate_and_never_conclusive():
    e1 = _e1()

    assert e1["status"] == "candidate" and e1["candidate_installation_id"] == 101 and e1["conclusive"] is False


def test_every_e1_result_says_it_is_not_conclusive():
    for snapshot in ({}, {"cutovers": []}, {"codes": []}, {"tokens": []}):
        assert _e1(**snapshot)["conclusive"] is False


@pytest.mark.parametrize(("snapshot", "status", "reason"), [
    pytest.param({"cutovers": []}, "none", "no_enrollment_cutover", id="no-cutover"),
    pytest.param({"cutovers": [_cutover(set_by="rhanly", note="pilot")]}, "incomplete",
                 "cutover_instant_matches_without_enrollment_record", id="timestamp-equal-with-an-operator-cutover"),
    pytest.param({"cutovers": [_cutover(set_at=LATER)]}, "incomplete", "enrollment_cutover_instant_differs", id="instant-differs"),
    pytest.param({"codes": []}, "incomplete", "enrollment_code_missing", id="code-gone"),
    pytest.param({"codes": [_code(used_at=None)]}, "incomplete", "code_not_marked_used", id="code-unused"),
    pytest.param({"installations": []}, "incomplete", "installation_missing", id="installation-gone"),
    pytest.param({"tokens": []}, "incomplete", "no_token_bound_to_installation", id="no-token"),
    pytest.param({"tokens": [_token(installation_id=None)]}, "incomplete", "no_token_bound_to_installation", id="unbound-token-only"),
    pytest.param({"codes": [_code(installation_id=102)], "installations": [_installation(), _installation(102)]}, "conflicting",
                 "code_names_another_installation", id="code-for-another-installation"),
    pytest.param({"codes": [_code(used_at=LATER)]}, "conflicting", "code_used_at_differs", id="code-at-another-instant"),
    pytest.param({"codes": [_code(required_contract=1)]}, "conflicting", "code_is_not_contract_2", id="contract-1-code"),
    pytest.param({"codes": [_code(revoked_at=INSTANT)]}, "conflicting", "code_revoked", id="revoked-code"),
    pytest.param({"installations": [_installation(branch_id=3)]}, "conflicting", "installation_at_another_branch", id="other-branch"),
])
def test_anything_short_of_a_complete_record_is_not_a_candidate(snapshot, status, reason):
    e1 = _e1(**snapshot)

    assert e1["status"] == status and reason in e1["reasons"] and e1["candidate_installation_id"] is None


@pytest.mark.parametrize("note", [
    "pilot cutover",
    "",
    None,
    NOTE + " extra",
    "prefix " + NOTE,
    NOTE.replace("enrollment_code_id=900", "enrollment_code_id=abc"),
    NOTE.replace("|", "/"),
    NOTE.upper(),
])
def test_a_forged_note_of_any_other_form_is_not_an_enrollment_record(note):
    e1 = _e1(cutovers=[_cutover(note=note)])

    assert e1["status"] == "incomplete" and e1["reasons"] == ["cutover_instant_matches_without_enrollment_record"]


def test_a_note_with_the_form_but_not_the_enrollment_set_by_is_not_an_enrollment_record():
    assert _e1(cutovers=[_cutover(set_by="rhanly")])["status"] == "incomplete"


def test_two_enrollment_rows_at_the_keys_instant_are_ambiguous():
    e1 = _e1(cutovers=[_cutover(), _cutover(id=51)])

    assert e1["status"] == "ambiguous" and e1["candidate_installation_id"] is None


def test_a_second_enrollment_cutover_at_the_branch_makes_a_complete_record_ambiguous():
    e1 = _e1(cutovers=[_cutover(), _cutover(id=51, set_at=LATER)])

    assert e1["status"] == "ambiguous" and e1["reasons"] == ["several_enrollment_cutovers_at_this_branch"]


def test_a_record_that_fits_two_keys_identifies_neither():
    report = tool.build_report(_snapshot(keys=[_key(), _key(OTHER_KEY, id=2)]))

    assert [key["e1"]["status"] for key in report["keys"]] == ["ambiguous", "ambiguous"]
    assert all(key["e1"]["candidate_installation_id"] is None for key in report["keys"])


def test_a_heartbeat_written_with_another_installation_makes_a_candidate_ambiguous():
    e1 = _e1(keys=[_key(last_heartbeat_at=LATER)], installations=[_installation(), _installation(102, last_seen_at=LATER)])

    assert e1["status"] == "ambiguous" and e1["reasons"] == ["heartbeat_coincides_with_another_installation"]


def test_a_heartbeat_alone_creates_no_candidate():
    report = tool.build_report(_snapshot(keys=[_key(last_heartbeat_at=LATER)], cutovers=[], codes=[],
                                         installations=[_installation(last_seen_at=LATER)]))
    key = report["keys"][0]

    assert key["e1"]["status"] == "none" and key["heartbeat_coincides_with_installation_ids"] == [101]


def test_the_latest_retired_key_is_not_attributed_for_being_the_latest():
    report = tool.build_report(_snapshot(keys=[_key(status="retired", retired_at=LATER)], cutovers=[], codes=[]))
    key = report["keys"][0]

    assert key["owner_state"] == "unknown" and key["e1"]["status"] == "none" and key["owner_installation_id"] is None


# --- flags and states -------------------------------------------------------------------------------------------------------

def test_a_recorded_owner_is_reported_and_one_differing_from_the_candidate_is_flagged():
    agrees = tool.build_report(_snapshot(keys=[_key(installation_id=101)]))["keys"][0]
    differs = tool.build_report(_snapshot(keys=[_key(installation_id=102)]))["keys"][0]

    assert agrees["owner_state"] == "recorded" and "owner_differs_from_e1_candidate" not in agrees["flags"]
    assert "owner_differs_from_e1_candidate" in differs["flags"]


@pytest.mark.parametrize(("keys", "state"), [
    pytest.param([], "none_issued", id="none"),
    pytest.param([_key(status="retired", retired_at=LATER)], "retired_only", id="retired"),
    pytest.param([_key()], "one_active", id="one"),
    pytest.param([_key(), _key(OTHER_KEY, id=2)], "multiple_active", id="two"),
])
def test_each_branch_has_a_key_state(keys, state):
    report = tool.build_report(_snapshot(keys=keys))

    assert report["branches"][0]["key_state"] == state
    assert ("multiple_active_keys" in report["branches"][0]["flags"]) == (state == "multiple_active")


def test_token_and_installation_relationships_are_flagged_on_the_branch_and_the_key():
    report = tool.build_report(_snapshot(
        installations=[_installation(), _installation(102)],
        tokens=[_token(), _token(id=8, installation_id=102), _token(id=9, installation_id=None)],
    ))
    branch, key = report["branches"][0], report["keys"][0]

    assert {"active_unbound_tokens", "active_tokens_bound_to_several_installations", "several_live_installations"} <= set(branch["flags"])
    assert {"active_unbound_tokens_at_branch", "active_tokens_bound_to_another_installation",
            "several_live_installations_at_branch"} <= set(key["flags"])
    assert report["summary"]["tokens_active_unbound"] == 1 and report["summary"]["tokens_active_bound"] == 2


def test_a_key_outside_any_mapped_scope_is_reported_not_dropped():
    report = tool.build_report(_snapshot(branches=[]))

    assert report["keys"][0]["key_id"] == KEY and "scope_not_mapped_to_a_branch" in report["keys"][0]["flags"]
    assert report["summary"]["scopes_not_mapped_to_a_branch"] == 1


def test_events_under_another_scope_are_counted_apart():
    report = tool.build_report(_snapshot(events=[
        {"customer_id": 10, "branch_id": 1, "key_id": KEY, "events": 3, "last_received_at": LATER},
        {"customer_id": 20, "branch_id": 2, "key_id": KEY, "events": 2, "last_received_at": INSTANT},
        {"customer_id": 10, "branch_id": 1, "key_id": OTHER_KEY, "events": 1, "last_received_at": INSTANT},
    ]))

    assert report["keys"][0]["events"] == {"count": 3, "last_received_at": LATER, "count_under_another_scope": 2}
    assert report["summary"]["event_key_ids_not_in_registry"] == 1


def test_the_report_makes_no_decision_and_both_formats_render():
    report = tool.build_report(_snapshot())

    assert report["changes_made"] == 0 and "not proof" in report["notice"]
    assert json.loads(tool.render_json(report))["keys"][0]["key_id"] == KEY
    assert "requires review" in tool.render_text(report)
    assert not {"decision", "assignment", "assigned_owner", "recommended_owner"} & set(report["keys"][0])


def test_free_text_from_a_cutover_never_reaches_the_report():
    canary = "CANARY-operator-free-text"
    report = tool.build_report(_snapshot(cutovers=[_cutover(set_by=canary, note=canary)]))

    assert canary not in tool.render_json(report) and canary not in tool.render_text(report)


# --- refusals --------------------------------------------------------------------------------------------------------------

def test_no_database_url_is_refused(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    code, out = run("inventory")

    assert code == 2 and "database_url_required" in out and "SUMMARY" not in out


@pytest.mark.parametrize("url", ["sqlite:///ignored.db", "mysql://user:CANARY-password@example.invalid/db"])
def test_only_postgresql_is_accepted_and_the_url_is_not_repeated(url):
    code, out = run("inventory", "--database-url", url)

    assert code == 2 and "postgresql_required" in out and "CANARY" not in out and "ignored" not in out


def test_an_unreadable_url_is_refused_without_repeating_it():
    code, out = run("inventory", "--database-url", "CANARY-not-a-url")

    assert code == 2 and "database_url_invalid" in out and "CANARY" not in out


def test_a_refused_connection_does_not_repeat_the_url_or_password():
    code, out = run("inventory", "--database-url", "postgresql://user:CANARY-password@127.0.0.1:1/nothing", "--format", "json")
    document = json.loads(out)

    assert code == 2 and document["complete"] is False and document["error"]["code"] == "connection_failed"
    assert "CANARY" not in out and "127.0.0.1" not in out and "keys" not in document


def test_an_unexpected_failure_reports_only_a_class_name_and_no_partial_report(monkeypatch):
    def fails(_url):
        raise RuntimeError("CANARY postgresql://user:password@host/db")

    monkeypatch.setattr(tool, "run_inventory", fails)

    code, out = run("inventory", "--database-url", "postgresql://ignored/db")

    assert code == 2 and "inventory_failed" in out and "RuntimeError" in out and "CANARY" not in out and "SUMMARY" not in out


def test_a_value_json_cannot_represent_fails_closed(monkeypatch):
    monkeypatch.setattr(tool, "run_inventory", lambda _url: {"report": object()})

    code, out = run("inventory", "--database-url", "postgresql://ignored/db", "--format", "json")

    assert code == 2 and json.loads(out)["error"]["code"] == "inventory_failed"


def test_inventory_is_the_only_command():
    with pytest.raises(SystemExit) as refused:
        tool._parser().parse_args(["stamp"])

    assert refused.value.code == 2


# --- the source ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", [name for name in dir(tool) if name.endswith("_SQL")])
def test_every_statement_is_a_select(name):
    assert getattr(tool, name).lstrip().upper().startswith("SELECT")


@pytest.mark.parametrize("statement", tool._SNAPSHOT_SETTINGS)
def test_the_only_other_statements_are_transaction_local_settings(statement):
    assert statement.startswith(("SET TRANSACTION ", "SET LOCAL "))


def test_the_queries_cannot_read_a_credential_or_an_identifying_column():
    source = "\n".join(getattr(tool, name) for name in dir(tool) if name.endswith("_SQL")).lower()

    for column in ("token_hash", "code_hash", "description", "hostname", "collector_version", "item_key", "event_key", "patron", "barcode"):
        assert column not in source
    assert "select *" not in source


def test_the_source_has_no_write_statement():
    source = (ROOT / "scripts" / "reconcile_ingest_keys.py").read_text(encoding="utf-8").upper()

    for word in ("INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE", "ALTER TABLE", "CREATE ", "DROP ", "GRANT ", "COMMIT"):
        assert word not in source.replace("FOR UPDATE", "")
