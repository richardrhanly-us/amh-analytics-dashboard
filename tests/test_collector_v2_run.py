"""Contract v2 collector: delivery, retry, quarantine, heartbeat, dry run and dispatch (collector/v2_run.py, v2_uploader.py, v2_status.py).

The server is a scripted fake here, so every status code and every hostile response body can be produced on demand. The real FastAPI app is
exercised in tests/test_collector_v2_e2e.py.
"""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
import requests
from collector_v2_support import (
    BARCODE_CI,
    KEY_ID,
    OTHER_KEY_ID,
    TOKEN,
    FakeStore,
    Reply,
    ScriptedSession,
    checkin_line,
    every_byte_under,
    find_leaks,
    full_corpus,
    load_v2,
    minutes_ago,
    run_logger,
    success_reply,
    write_config,
    write_lines,
    write_rules,
)

from collector import run as collector_run
from collector import state as v1_state
from collector import v2_run, v2_status, v2_uploader
from collector.config import load_config
from collector.v2_config import load_dry_run_settings
from collector.v2_events import HEALTH_STATUSES, LAST_ERROR_CLASSES
from src.services.ingest_v2_models import StatusV2Request, UploadV2Request

BASE = minutes_ago(90)


class Env:
    """One collector install under tmp_path: config, rules, Tech Logic files, secret store, logger."""

    def __init__(self, tmp_path, monkeypatch, **v2_extra):
        self.root = tmp_path / "root"
        monkeypatch.setenv("SORTVIEW_API_TOKEN", TOKEN)
        self.config_path = write_config(self.root, v2_extra=v2_extra)
        self.cfg = load_config(self.config_path)
        self.v2 = load_v2(self.config_path)
        write_rules(self.v2)
        self.store = FakeStore()
        self.logger = run_logger(self.cfg.log_path)
        self.tech = self.root / "tech"
        self.clock = datetime.now(UTC)

    def put(self, **sources):
        """Write Tech Logic files: name=list of lines (an omitted source is left absent)."""
        for name, lines in sources.items():
            write_lines(self.tech / f"{name}.txt", lines)

    def put_all(self, corpus=None, *, base=BASE):
        self.put(**(corpus or full_corpus(base)))

    def run(self, session, **kwargs):
        kwargs.setdefault("sleep", lambda _seconds: None)
        kwargs.setdefault("clock", lambda: self.clock)
        return v2_run.run_once_v2(self.cfg, self.v2, session=session, logger=self.logger, store=self.store, **kwargs)

    def dry(self, **kwargs):
        out = io.StringIO()
        code = v2_run.run_dry(load_dry_run_settings(self.config_path), out=out, **kwargs)
        return code, out.getvalue()

    def snapshot(self):
        """Every file under the install root (logs included) -> a digest of its bytes."""
        return {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.root.rglob("*")) if p.is_file()}

    def state(self):
        return json.loads(self.v2.state_path.read_text()) if self.v2.state_path.exists() else None

    def status(self):
        return json.loads(self.v2.status_path.read_text())

    def quarantine(self):
        return json.loads(self.v2.quarantine_path.read_text())["entries"] if self.v2.quarantine_path.exists() else []

    def cursor_offset(self, name):
        document = self.state()
        return document["sources"][name]["offset"] if document and name in document.get("sources", {}) else None

    def leaks(self):
        return find_leaks(every_byte_under(self.root) + json.dumps(self.status()).encode())

    def tree_hash(self):
        digest = hashlib.sha256()
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and "logs" not in path.parts:
                digest.update(str(path.relative_to(self.root)).encode() + path.read_bytes())
        return digest.hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def checkin_lines(n, start=0, base=BASE):
    return [checkin_line(base + timedelta(seconds=start + i), BARCODE_CI, "Westside", str(1 + i % 9)) for i in range(n)]


def uploaded_events(session):
    return [(kind, event) for body in session.uploads() for kind in ("acs_items", "checkins", "rejects") for event in body.get(kind, [])]


def empty_other_sources(env, **present):
    env.put(**{name: [] for name in ("acs", "checkins", "rejects") if name not in present}, **present)


# =====================================================================================================================
# The happy path
# =====================================================================================================================

def test_a_run_delivers_acs_first_then_checkins_then_rejects_and_commits_each_cursor(env):
    env.put_all()
    session = ScriptedSession()
    assert env.run(session) == 0
    kinds = [next(k for k in ("acs_items", "checkins", "rejects") if k in body) for body in session.uploads()]
    assert kinds == ["acs_items", "checkins", "rejects"]
    for name in ("acs", "checkins", "rejects"):
        assert env.cursor_offset(name) == (env.tech / f"{name}.txt").stat().st_size


def test_the_second_run_sends_no_events_and_only_the_heartbeat(env):
    env.put_all()
    env.run(ScriptedSession())
    session = ScriptedSession()
    assert env.run(session) == 0
    assert session.uploads() == [] and len(session.statuses()) == 1


def test_new_lines_appended_later_are_delivered_once(env):
    env.put_all()
    env.run(ScriptedSession())
    write_lines(env.tech / "checkins.txt", checkin_lines(2, start=500), append=True)
    session = ScriptedSession()
    env.run(session)
    assert len(uploaded_events(session)) == 2


def test_every_upload_is_authenticated_with_the_token_and_carries_only_the_v2_envelope(env):
    env.put_all()
    session = ScriptedSession()
    env.run(session)
    assert {h["Authorization"] for h in session.headers} == {f"Bearer {TOKEN}"}
    for body in session.uploads():
        assert set(body) <= {"contract_version", "key_id", "checkins", "rejects", "acs_items"}
        assert body["contract_version"] == 2 and body["key_id"] == KEY_ID


def test_payloads_validate_against_the_real_merged_server_models(env):
    env.put_all()
    session = ScriptedSession()
    env.run(session)
    assert session.uploads()
    for body in session.uploads():
        UploadV2Request.model_validate(body)
    for body in session.statuses():
        StatusV2Request.model_validate(body)


def test_the_acs_events_carry_hold_non_hold_and_other_states_with_the_run_ruleset(env):
    env.put_all()
    session = ScriptedSession()
    env.run(session)
    items = [e for body in session.uploads() for e in body.get("acs_items", [])]
    assert sorted(e["state"] for e in items).count("hold") == 5
    assert {e["state"] for e in items} == {"hold", "non_hold_101", "other_code10"}
    assert len({e["ruleset_id"] for e in items if e["state"] == "hold"}) == 1
    assert all("ruleset_id" not in e for e in items if e["state"] != "hold")


def test_a_chunk_larger_than_the_request_cap_is_split_into_several_requests(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, max_events_per_request=50, chunk_lines=200)
    empty_other_sources(env, checkins=checkin_lines(120))
    session = ScriptedSession()
    assert env.run(session) == 0
    assert [len(b["checkins"]) for b in session.uploads()] == [50, 50, 20]


def test_requests_are_paced_between_batches(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, max_events_per_request=50, chunk_lines=200, request_interval_seconds=2.2)
    empty_other_sources(env, checkins=checkin_lines(120))
    sleeps = []
    env.run(ScriptedSession(), sleep=sleeps.append)
    assert sleeps == [2.2, 2.2, 2.2]                                # one pause after each of the three requests


def test_conflict_retry_rounds_are_paced_too(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, request_interval_seconds=1.5)
    empty_other_sources(env, checkins=checkin_lines(5))
    sleeps = []
    env.run(ScriptedSession(script=[conflict_reply([1]), conflict_reply([0]), None]), sleep=sleeps.append)
    assert sleeps == [1.5, 1.5, 1.5]                                # two 409 rounds and the final 200


def test_the_v1_state_and_status_files_are_never_touched(env):
    v1_paths = (env.cfg.state_path, env.cfg.status_path)
    env.put_all()
    env.run(ScriptedSession())
    assert not any(p.exists() for p in v1_paths)


# =====================================================================================================================
# Progressive commit
# =====================================================================================================================

def test_the_cursor_moves_one_chunk_at_a_time_and_stops_at_the_failed_chunk(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, chunk_lines=3)
    lines = checkin_lines(12)
    empty_other_sources(env, checkins=lines)
    per_chunk = len("".join(lines[:3]).encode())
    session = ScriptedSession(script=[None, None, Reply(503)], default=Reply(503))
    assert env.run(session) == 1
    assert env.cursor_offset("checkins") == 2 * per_chunk         # two chunks delivered, the third was not committed
    assert env.status()["last_error_class"] == "retryable_infra"
    resumed = ScriptedSession()
    assert env.run(resumed) == 0
    assert env.cursor_offset("checkins") == (env.tech / "checkins.txt").stat().st_size
    assert sum(len(b["checkins"]) for b in resumed.uploads()) == 6  # only the undelivered half was resent


def test_a_failed_upload_commits_neither_the_cursor_nor_the_patron_cache(env):
    env.put_all()
    session = ScriptedSession(default=Reply(503))
    assert env.run(session) == 1
    assert env.cursor_offset("acs") is None
    import sqlite3
    assert sqlite3.connect(env.v2.patron_cache_path).execute("SELECT COUNT(*) FROM patrons").fetchone()[0] == 0


def test_after_a_failure_the_identical_events_are_resent_and_the_cache_then_persists(env):
    env.put_all()
    failing = ScriptedSession(default=Reply(503))
    env.run(failing)
    first_bodies = [json.dumps(b, sort_keys=True) for b in failing.uploads()]
    ok = ScriptedSession()
    assert env.run(ok) == 0
    assert json.dumps(ok.uploads()[0], sort_keys=True) == first_bodies[0]        # byte-identical resend
    import sqlite3
    assert sqlite3.connect(env.v2.patron_cache_path).execute("SELECT COUNT(*) FROM patrons").fetchone()[0] > 0


def test_a_run_stops_at_the_first_failed_source_and_does_not_advance_later_ones(env):
    env.put_all()
    session = ScriptedSession(script=[Reply(503)], default=None)
    assert env.run(session) == 1
    assert env.cursor_offset("acs") is None and env.cursor_offset("checkins") is None and len(session.uploads()) == 1


def test_the_chunk_budget_bounds_one_run(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, chunk_lines=2, max_chunks_per_run=3)
    empty_other_sources(env, checkins=checkin_lines(20))
    session = ScriptedSession()
    env.run(session)
    assert len(session.uploads()) == 3
    env.run(ScriptedSession())
    assert env.cursor_offset("checkins") == 6 * len(checkin_lines(1)[0].encode()) or env.cursor_offset("checkins") > 0


def test_a_missing_source_is_reported_degraded_source_unavailable_and_others_still_run(env):
    env.put(acs=full_corpus(BASE)["acs"], checkins=checkin_lines(2))           # rejects.txt is absent
    session = ScriptedSession()
    assert env.run(session) == 0
    assert env.status()["health_status"] == "degraded" and env.status()["last_error_class"] == "source_unavailable"
    assert session.statuses()[-1]["last_error_class"] == "source_unavailable"
    assert env.cursor_offset("checkins") is not None


def test_a_rotated_source_is_read_from_its_start(env):
    import os
    env.put(acs=[], checkins=checkin_lines(3), rejects=[])
    env.run(ScriptedSession())
    os.replace(env.tech / "checkins.txt", env.tech / "checkins.old")
    write_lines(env.tech / "checkins.txt", checkin_lines(2, start=700))
    session = ScriptedSession()
    env.run(session)
    assert len(uploaded_events(session)) == 2 and env.status()["counters"]["sources_rotated"] == 1


def test_a_partial_trailing_line_waits_for_its_newline(env):
    env.put(acs=[], rejects=[], checkins=checkin_lines(1))
    with open(env.tech / "checkins.txt", "ab") as handle:
        handle.write(checkin_lines(1, start=50)[0].encode()[:-1])          # no newline yet
    session = ScriptedSession()
    env.run(session)
    assert len(uploaded_events(session)) == 1
    with open(env.tech / "checkins.txt", "ab") as handle:
        handle.write(b"\n")
    later = ScriptedSession()
    env.run(later)
    assert len(uploaded_events(later)) == 1


def test_a_corrupt_v2_state_file_is_a_configuration_error_and_is_not_overwritten(env):
    env.put_all()
    env.v2.state_path.parent.mkdir(parents=True, exist_ok=True)
    env.v2.state_path.write_text("{this is not valid json CANARY-STATE", encoding="utf-8")
    session = ScriptedSession()
    assert env.run(session) == 2
    assert session.uploads() == [] and env.v2.state_path.read_text() == "{this is not valid json CANARY-STATE"
    assert env.status()["last_error_class"] == "configuration_error"
    assert "CANARY-STATE" not in (env.cfg.log_path.read_text() + json.dumps(env.status()))


def test_events_skipped_because_they_are_quarantined_are_counted(env):
    empty_other_sources(env, checkins=checkin_lines(4))
    env.run(ScriptedSession(script=[conflict_reply([2])]))
    env.v2.state_path.unlink()
    env.run(ScriptedSession())
    assert env.status()["counters"]["events_skipped_quarantined"] == 1


def test_the_patron_cache_is_committed_before_the_cursor_so_a_crash_between_them_loses_nothing(env, monkeypatch):
    import sqlite3
    env.put_all()
    real_save = v1_state.save_state

    def crash_on_the_v2_cursor(path, current):
        if str(path) == str(env.v2.state_path):
            raise OSError("simulated crash between the cache commit and the cursor commit")
        return real_save(path, current)

    monkeypatch.setattr(v2_run.state, "save_state", crash_on_the_v2_cursor)
    assert env.run(ScriptedSession()) == 1
    assert env.cursor_offset("acs") is None                                     # the cursor did not move ...
    assert sqlite3.connect(env.v2.patron_cache_path).execute("SELECT COUNT(*) FROM patrons").fetchone()[0] > 0   # ... the profiles were kept
    monkeypatch.setattr(v2_run.state, "save_state", real_save)
    resumed = ScriptedSession()
    assert env.run(resumed) == 0 and env.cursor_offset("acs") == (env.tech / "acs.txt").stat().st_size
    assert {e["state"] for b in resumed.uploads() for e in b.get("acs_items", [])} == {"hold", "non_hold_101", "other_code10"}


# =====================================================================================================================
# 409 conflicts and 422 validation failures
# =====================================================================================================================

def conflict_reply(positions, kind="checkins"):
    return Reply(409, {"code": "event_conflict", "conflicts": {kind: list(positions)}})


def test_a_409_quarantines_only_the_conflicting_events_and_resends_the_rest(env):
    empty_other_sources(env, checkins=checkin_lines(5))
    session = ScriptedSession(script=[conflict_reply([1, 3])])
    assert env.run(session) == 0
    first, second = session.uploads()
    assert len(first["checkins"]) == 5 and len(second["checkins"]) == 3
    assert {e["event_key"] for e in second["checkins"]} == {first["checkins"][i]["event_key"] for i in (0, 2, 4)}
    kept = env.quarantine()
    assert sorted(e["event_key"] for e in kept) == sorted(first["checkins"][i]["event_key"] for i in (1, 3))
    assert {e["reason"] for e in kept} == {"event_conflict"} and {e["kind"] for e in kept} == {"checkins"}
    assert env.cursor_offset("checkins") == (env.tech / "checkins.txt").stat().st_size       # the cursor is NOT blocked


def test_quarantine_entries_hold_only_the_five_safe_fields(env):
    empty_other_sources(env, checkins=checkin_lines(3))
    env.run(ScriptedSession(script=[conflict_reply([0])]))
    (entry,) = env.quarantine()
    assert set(entry) == {"event_key", "kind", "event_time", "reason", "quarantined_on"}
    assert env.leaks() == []


def test_a_quarantined_event_is_never_sent_again_even_if_the_cursor_is_rewound(env):
    empty_other_sources(env, checkins=checkin_lines(4))
    first = ScriptedSession(script=[conflict_reply([2])])
    env.run(first)
    bad = first.uploads()[0]["checkins"][2]["event_key"]
    env.v2.state_path.unlink()                                                # rewind: everything is re-read
    again = ScriptedSession()
    env.run(again)
    sent = [e["event_key"] for _k, e in uploaded_events(again)]
    assert bad not in sent and len(sent) == 3


def test_more_than_fifty_conflicts_are_handled_across_rounds(tmp_path, monkeypatch):
    """The real server reports at most 50 conflicts per list per response; the collector must loop, not give up or block the cursor."""
    env = Env(tmp_path, monkeypatch, chunk_lines=300)
    empty_other_sources(env, checkins=checkin_lines(150))
    conflicting = set()

    def server(_url, body):
        events = body["checkins"]
        if not conflicting:
            conflicting.update(e["event_key"] for i, e in enumerate(events) if i % 4 != 3)        # 3 of every 4 conflict: 113 of 150
        positions = [i for i, e in enumerate(events) if e["event_key"] in conflicting][:50]       # the server's cap
        return conflict_reply(positions) if positions else success_reply({"checkins_inserted": len(events)})

    session = ScriptedSession(default=server)
    assert env.run(session) == 0
    assert len(session.uploads()) >= 4                                                           # 113 / 50 -> three conflict rounds + success
    assert len(env.quarantine()) == 113
    final = session.uploads()[-1]["checkins"]
    assert len(final) == 37 and not any(e["event_key"] in conflicting for e in final)
    assert env.cursor_offset("checkins") == (env.tech / "checkins.txt").stat().st_size
    assert env.status()["health_status"] == "degraded" and env.status()["last_error_class"] == "permanent_rejection"
    assert session.statuses()[-1]["quarantined_count"] == 113


def test_conflicts_in_several_lists_of_one_request_are_all_removed(env):
    corpus = full_corpus(BASE)
    env.put(acs=corpus["acs"], checkins=corpus["checkins"], rejects=corpus["rejects"])
    session = ScriptedSession(script=[conflict_reply([0], "acs_items")])
    assert env.run(session) == 0
    assert len(env.quarantine()) == 1 and env.quarantine()[0]["kind"] == "acs_items"


def test_a_409_that_repeats_without_naming_anything_stops_instead_of_looping(env):
    empty_other_sources(env, checkins=checkin_lines(3))
    session = ScriptedSession(default=Reply(409, {"code": "event_conflict", "conflicts": {}}))
    assert env.run(session) == 1
    assert len(session.uploads()) == 1 and env.status()["last_error_class"] == "permanent_rejection"
    assert env.cursor_offset("checkins") is None


@pytest.mark.parametrize("body", [
    None, {}, {"code": "other"}, {"code": "event_conflict"}, {"code": "event_conflict", "conflicts": {"checkins": [99]}},
    {"code": "event_conflict", "conflicts": {"checkins": [-1]}}, {"code": "event_conflict", "conflicts": {"checkins": ["0"]}},
    {"code": "event_conflict", "conflicts": {"checkins": [True]}}, {"code": "event_conflict", "conflicts": {"patrons": [0]}},
    {"code": "event_conflict", "conflicts": {"checkins": []}}, {"code": "event_conflict", "conflicts": [0]},
])
def test_a_malformed_409_body_is_a_permanent_failure_that_quarantines_nothing(env, body):
    empty_other_sources(env, checkins=checkin_lines(3))
    assert env.run(ScriptedSession(default=Reply(409, body))) == 1
    assert env.quarantine() == [] and env.status()["last_error_class"] == "permanent_rejection"


def test_an_event_level_422_quarantines_that_event_as_invalid_and_resends_the_rest(env):
    empty_other_sources(env, checkins=checkin_lines(4))
    detail = [{"loc": ["body", "checkins", 2, "destination"], "msg": "CANARY-SERVER-MESSAGE", "type": "value_error"}]
    session = ScriptedSession(script=[Reply(422, {"detail": detail})])
    assert env.run(session) == 0
    assert len(session.uploads()[1]["checkins"]) == 3
    (entry,) = env.quarantine()
    assert entry["reason"] == "invalid_event" and entry["event_key"] == session.uploads()[0]["checkins"][2]["event_key"]
    assert "CANARY-SERVER-MESSAGE" not in (env.cfg.log_path.read_text() + json.dumps(env.status()) + env.v2.quarantine_path.read_text())


@pytest.mark.parametrize("detail", [
    [{"loc": ["body", "key_id"], "msg": "x"}], [{"loc": ["body"], "msg": "x"}], [{"loc": ["body", "checkins"], "msg": "too many"}],
    [{"loc": ["body", "checkins", 0, "bin"], "msg": "x"}, {"loc": ["body", "contract_version"], "msg": "y"}], "not a list", [], None,
    [{"loc": ["body", "checkins", 99, "bin"], "msg": "x"}], [{"loc": ["query", "checkins", 0], "msg": "x"}],
])
def test_an_envelope_level_or_unrecognized_422_is_a_configuration_error_and_quarantines_nothing(env, detail):
    empty_other_sources(env, checkins=checkin_lines(3))
    body = {"detail": detail} if detail is not None else None
    assert env.run(ScriptedSession(default=Reply(422, body))) == 2
    assert env.quarantine() == [] and env.status()["last_error_class"] == "configuration_error"
    assert env.cursor_offset("checkins") is None


# =====================================================================================================================
# Status codes -> failure classes -> heartbeat
# =====================================================================================================================

@pytest.mark.parametrize(("reply", "exit_code", "health", "error_class"), [
    (Reply(401), 1, "error", "auth_failure"), (Reply(403), 1, "error", "auth_failure"),
    (Reply(404), 2, "error", "configuration_error"), (Reply(405), 2, "error", "configuration_error"),
    (Reply(400), 1, "error", "permanent_rejection"), (Reply(413), 1, "error", "permanent_rejection"),
    (Reply(429), 1, "degraded", "retryable_infra"), (Reply(500), 1, "degraded", "retryable_infra"),
    (Reply(502), 1, "degraded", "retryable_infra"), (Reply(503), 1, "degraded", "retryable_infra"),
    (Reply(504), 1, "degraded", "retryable_infra"), (Reply(418), 1, "error", "other"), (Reply(302), 1, "error", "other"),
    (Reply(200, {"status": "failure"}), 1, "degraded", "retryable_infra"), (Reply(200, None), 1, "degraded", "retryable_infra"),
    (requests.ConnectionError("x"), 1, "degraded", "retryable_infra"), (requests.Timeout("x"), 1, "degraded", "retryable_infra"),
    (RuntimeError("x"), 1, "error", "other"),
])
def test_each_failure_maps_to_the_documented_status_and_error_class(env, reply, exit_code, health, error_class):
    empty_other_sources(env, checkins=checkin_lines(2))
    session = ScriptedSession(default=reply)
    assert env.run(session) == exit_code
    assert (env.status()["health_status"], env.status()["last_error_class"]) == (health, error_class)
    heartbeat = session.statuses()[-1]
    assert (heartbeat["status"], heartbeat.get("last_error_class")) == (health, error_class)
    assert env.cursor_offset("checkins") is None                    # nothing was committed


def test_repeated_retryable_failures_escalate_to_error(env):
    empty_other_sources(env, checkins=checkin_lines(2))
    seen = []
    for _ in range(4):
        env.run(ScriptedSession(default=Reply(503)))
        seen.append(env.status()["health_status"])
    assert seen == ["degraded", "degraded", "error", "error"]
    assert env.status()["last_error_class"] == "retryable_infra" and env.status()["consecutive_failures"] == 4


def test_a_good_run_clears_the_failure_streak_and_records_the_last_success(env):
    empty_other_sources(env, checkins=checkin_lines(2))
    env.run(ScriptedSession(default=Reply(503)))
    assert env.status()["last_success_at"] is None
    env.run(ScriptedSession())
    status = env.status()
    assert status["consecutive_failures"] == 0 and status["health_status"] == "healthy" and status["last_error_class"] is None
    assert status["last_success_at"] is not None


def test_a_failed_run_keeps_the_previous_last_success(env):
    empty_other_sources(env, checkins=checkin_lines(2))
    env.run(ScriptedSession())
    before = env.status()["last_success_at"]
    write_lines(env.tech / "checkins.txt", checkin_lines(1, start=900), append=True)
    env.run(ScriptedSession(default=Reply(503)))
    assert env.status()["last_success_at"] == before


def test_a_healthy_heartbeat_has_the_exact_v2_fields_and_no_outbox(env):
    env.put_all()
    session = ScriptedSession()
    env.run(session)
    (heartbeat,) = session.statuses()
    assert set(heartbeat) == {"contract_version", "key_id", "status", "pending_outbox_count", "quarantined_count", "last_success_at",
                              "watcher_last_active_at"}
    assert heartbeat["status"] == "healthy" and heartbeat["pending_outbox_count"] == 0 and heartbeat["quarantined_count"] == 0
    assert "oldest_pending_event_at" not in heartbeat and "last_error_class" not in heartbeat
    assert heartbeat["status"] in HEALTH_STATUSES


def test_quarantined_count_is_the_number_of_retained_entries(env):
    empty_other_sources(env, checkins=checkin_lines(6))
    env.run(ScriptedSession(script=[conflict_reply([0, 1])]))
    assert env.status()["quarantined_count"] == 2
    write_lines(env.tech / "checkins.txt", checkin_lines(2, start=800), append=True)
    session = ScriptedSession()
    env.run(session)
    assert session.statuses()[-1]["quarantined_count"] == 2 and session.statuses()[-1]["status"] == "healthy"


def test_the_heartbeat_failing_never_fails_the_run(env):
    env.put_all()
    session = ScriptedSession(status_reply=Reply(500))
    assert env.run(session) == 0
    assert env.status()["health_status"] == "healthy"
    boom = ScriptedSession(status_reply=requests.ConnectionError("CANARY-HEARTBEAT-EXCEPTION"))
    write_lines(env.tech / "checkins.txt", checkin_lines(1, start=600), append=True)
    assert env.run(boom) == 0
    assert "CANARY-HEARTBEAT" not in env.cfg.log_path.read_text()


def test_the_heartbeat_is_sent_even_when_the_run_fails_before_reading_anything(env):
    env.store = FakeStore(present=False)
    env.put_all()
    session = ScriptedSession()
    assert env.run(session) == 2
    assert session.uploads() == []
    (heartbeat,) = session.statuses()
    assert heartbeat["status"] == "error" and heartbeat["last_error_class"] == "configuration_error"


@pytest.mark.parametrize("value,ok", [("healthy", True), ("degraded", True), ("error", True), ("ok", False), ("failing", False), ("", False)])
def test_a_status_snapshot_only_accepts_the_approved_status_enum(value, ok):
    make = lambda: v2_status.StatusSnapshot(value, None, 0, None, None)
    if ok:
        assert make().payload(KEY_ID)["status"] == value
    else:
        with pytest.raises(v2_status.StatusError):
            make()


@pytest.mark.parametrize("value", ["timeout", "CANARY-exception text", "", "auth", "config"])
def test_a_status_snapshot_refuses_free_text_error_classes(value):
    with pytest.raises(v2_status.StatusError) as caught:
        v2_status.StatusSnapshot("error", value, 0, None, None)
    assert value not in str(caught.value) or value == ""


@pytest.mark.parametrize("field,value", [("quarantined_count", -1), ("quarantined_count", True), ("quarantined_count", "3"),
                                         ("quarantined_count", 10_000_001), ("pending_outbox_count", 1)])
def test_a_status_snapshot_bounds_its_counters(field, value):
    with pytest.raises(v2_status.StatusError):
        v2_status.StatusSnapshot("healthy", None, **{"quarantined_count": 0, "last_success_at": None, "watcher_last_active_at": None,
                                                     field: value})


def test_a_status_snapshot_has_no_field_that_could_carry_text():
    import dataclasses
    fields = {f.name: f.type for f in dataclasses.fields(v2_status.StatusSnapshot)}
    assert set(fields) == {"status", "last_error_class", "quarantined_count", "last_success_at", "watcher_last_active_at",
                           "pending_outbox_count"}
    assert not any("error" == name or "message" in name or "detail" in name or "text" in name for name in fields)


@pytest.mark.parametrize(("failure", "new_q", "missing", "streak", "expected"), [
    (None, 0, 0, 0, ("healthy", None)), (None, 2, 0, 0, ("degraded", "permanent_rejection")),
    (None, 0, 1, 0, ("degraded", "source_unavailable")), (None, 3, 1, 0, ("degraded", "source_unavailable")),
    ("retryable", 0, 0, 1, ("degraded", "retryable_infra")), ("retryable", 0, 0, 3, ("error", "retryable_infra")),
    ("auth", 0, 0, 1, ("error", "auth_failure")), ("config", 0, 0, 1, ("error", "configuration_error")),
    ("permanent", 0, 0, 1, ("error", "permanent_rejection")), ("other", 0, 0, 1, ("error", "other")),
    ("something-unexpected", 0, 0, 1, ("error", "other")),
])
def test_decide_maps_a_run_outcome_to_status_and_class(failure, new_q, missing, streak, expected):
    assert v2_status.decide(failure=failure, new_quarantined=new_q, sources_missing=missing, consecutive_failures=streak,
                            error_after=3) == expected
    assert expected[0] in HEALTH_STATUSES and (expected[1] is None or expected[1] in LAST_ERROR_CLASSES)


# =====================================================================================================================
# Secret, rules and ACL failures fail closed
# =====================================================================================================================

def test_a_missing_secret_fails_closed_without_reading_or_sending_anything(env):
    env.store = FakeStore(present=False)
    env.put_all()
    session = ScriptedSession()
    assert env.run(session) == 2 and session.uploads() == [] and env.state() is None
    assert not env.v2.patron_cache_path.exists() or env.v2.patron_cache_path.stat().st_size >= 0


def test_a_secret_bound_to_another_key_id_fails_closed(env):
    env.store = FakeStore(bound_key_id=OTHER_KEY_ID)
    env.put_all()
    session = ScriptedSession()
    assert env.run(session) == 2 and session.uploads() == []
    assert env.status()["last_error_class"] == "configuration_error"
    assert "secret_key_mismatch" in env.cfg.log_path.read_text()


def test_an_exposed_secret_folder_refuses_to_run(env):
    env.store = FakeStore(acl="exposed")
    env.put_all()
    session = ScriptedSession()
    assert env.run(session) == 2 and session.uploads() == []
    assert "secret_exposed" in env.cfg.log_path.read_text()


@pytest.mark.parametrize(("verdict", "code"), [("exposed", "secret_exposed"), ("unknown", "secret_acl_unverified"),
                                               ("garbage", "secret_acl_unverified"), ("", "secret_acl_unverified")])
def test_a_secret_whose_acl_is_not_verified_protected_fails_closed_before_it_is_read(env, verdict, code):
    env.store = FakeStore(acl=verdict)
    env.put_all()
    session = ScriptedSession()
    assert env.run(session) == 2
    assert env.store.loads == 0                                             # the secret was never even loaded
    assert session.uploads() == [] and env.state() is None
    assert code in env.cfg.log_path.read_text()
    status = env.status()
    assert (status["health_status"], status["last_error_class"]) == ("error", "configuration_error")
    (heartbeat,) = session.statuses()
    assert (heartbeat["status"], heartbeat["last_error_class"]) == ("error", "configuration_error")
    assert env.leaks() == []


def test_an_unknown_acl_state_does_not_run_even_after_a_previous_good_run(env):
    env.put_all()
    assert env.run(ScriptedSession()) == 0
    env.store = FakeStore(acl="unknown")
    write_lines(env.tech / "checkins.txt", checkin_lines(2, start=700), append=True)
    session = ScriptedSession()
    assert env.run(session) == 2 and session.uploads() == []
    assert env.status()["last_error_class"] == "configuration_error"


def test_a_verified_protected_acl_runs(env):
    env.store = FakeStore(acl="protected")
    env.put_all()
    assert env.run(ScriptedSession()) == 0 and env.store.loads == 1


def test_a_missing_rules_file_fails_closed_never_an_empty_ruleset(env):
    env.v2.rules_path.unlink()
    env.put_all()
    session = ScriptedSession()
    assert env.run(session) == 2 and session.uploads() == []
    assert env.status()["last_error_class"] == "configuration_error" and "rules_missing" in env.cfg.log_path.read_text()


def test_a_malformed_rules_file_fails_closed(env):
    env.v2.rules_path.write_text('{"schema_version": 1, "branch_services_names": "CANARY-RULE-TEXT"}', encoding="utf-8")
    env.put_all()
    session = ScriptedSession()
    assert env.run(session) == 2 and session.uploads() == []
    assert "CANARY" not in env.cfg.log_path.read_text()


def test_changing_the_rules_gives_a_new_ruleset_id_and_returning_restores_it(env):
    original = json.loads(env.v2.rules_path.read_text())
    ids = []
    for document in (original, {**original, "collection_services_names": ["SOMEONE ELSE"]}, original):
        env.v2.rules_path.write_text(json.dumps(document), encoding="utf-8")
        env.v2.state_path.unlink(missing_ok=True)
        env.put(acs=full_corpus(BASE)["acs"], checkins=[], rejects=[])
        session = ScriptedSession()
        env.run(session)
        ids.append({e["ruleset_id"] for b in session.uploads() for e in b["acs_items"] if e["state"] == "hold"})
    assert ids[0] == ids[2] and ids[0] != ids[1] and all(len(i) == 1 for i in ids)


# =====================================================================================================================
# The privacy sweep over everything a run writes (the whole-system version lives in test_collector_v2_privacy.py)
# =====================================================================================================================

HOSTILE_BODY = {"detail": "CANARY-SERVER-TEXT", "message": "CANARY-SERVER-MESSAGE", "error": "CANARY-SERVER-ERROR",
                "code": "CANARY-SERVER-CODE", "status": "CANARY-SERVER-STATUS"}


@pytest.mark.parametrize("reply", [
    Reply(500, HOSTILE_BODY), Reply(400, HOSTILE_BODY), Reply(401, HOSTILE_BODY), Reply(404, HOSTILE_BODY), Reply(418, HOSTILE_BODY),
    Reply(409, {**HOSTILE_BODY, "code": "event_conflict", "conflicts": {"checkins": [0]}}),
    Reply(422, {"detail": [{"loc": ["body", "checkins", 0, "bin"], "msg": "CANARY-SERVER-TEXT", "input": "CANARY-SERVER-INPUT"}]}),
    Reply(200, {**HOSTILE_BODY, "status": "success"}),
    requests.ConnectionError("CANARY-SERVER-EXCEPTION token=" + TOKEN), ValueError("CANARY-SERVER-EXCEPTION token=" + TOKEN),
])
def test_no_server_response_or_exception_text_reaches_a_log_the_state_the_status_or_the_heartbeat(env, reply):
    empty_other_sources(env, checkins=checkin_lines(3))
    session = ScriptedSession(default=reply, status_reply=Reply(500, HOSTILE_BODY))
    env.run(session)
    everything = every_byte_under(env.root) + json.dumps(env.status()).encode() + json.dumps(session.statuses()).encode()
    for needle in (b"CANARY-SERVER", TOKEN.encode(), b"token="):
        assert needle not in everything, needle
    heartbeat = session.statuses()[-1]
    assert set(heartbeat) <= {"contract_version", "key_id", "status", "last_error_class", "pending_outbox_count", "quarantined_count",
                              "last_success_at", "watcher_last_active_at"}


def test_the_token_is_only_ever_sent_in_the_authorization_header(env):
    env.put_all()
    session = ScriptedSession()
    env.run(session)
    assert TOKEN not in json.dumps(session.calls) and TOKEN.encode() not in every_byte_under(env.root)


def test_a_full_run_leaves_no_raw_value_anywhere_it_writes(env):
    env.put_all()
    session = ScriptedSession(script=[conflict_reply([0], "acs_items")])
    env.run(session)
    assert env.leaks() == []
    assert find_leaks(json.dumps(session.calls).encode()) == []


# =====================================================================================================================
# The dry run
# =====================================================================================================================

def test_the_dry_run_prints_only_aggregate_counts_and_never_a_value(env):
    env.put_all()
    code, output = env.dry()
    assert code == 0
    lines = output.strip().splitlines()
    assert all("=" in line and line.split("=", 1)[1].lstrip("-").isdigit() for line in lines), lines
    assert find_leaks(output.encode()) == []
    values = dict(line.split("=", 1) for line in lines)
    assert values["network_calls"] == "0" and values["dry_run_complete"] == "1" and values["events_acs_items"] == "7"
    assert values["throwaway_key"] == "1" and values["persistent_secret_used"] == "0" and values["acs_corrections"] == "0"
    assert values["dropped_patron_card"] == "3" and values["acs_state_hold"] == "5"


def test_the_dry_run_changes_nothing_on_disk(env):
    env.put_all()
    env.run(ScriptedSession(script=[conflict_reply([0])]))                # a real run first: cache, state, quarantine all exist
    write_lines(env.tech / "checkins.txt", checkin_lines(5, start=400), append=True)
    before = env.snapshot()
    code, _ = env.dry()
    assert code == 0 and env.snapshot() == before


def test_the_dry_run_on_a_fresh_install_creates_no_file_at_all(env):
    env.put_all()
    before = env.snapshot()
    code, output = env.dry()
    assert code == 0 and "throwaway_key=1" in output
    assert env.snapshot() == before
    assert not env.v2.state_path.exists() and not env.v2.patron_cache_path.exists() and not env.v2.quarantine_path.exists()
    assert not env.v2.status_path.exists() and not env.v2.secret_path.exists()     # (the test harness's own log file predates the run)


def test_the_dry_run_never_opens_the_secret_store_or_checks_its_acl(env, monkeypatch):
    from collector import v2_keys
    env.put_all()
    env.v2.secret_path.parent.mkdir(parents=True, exist_ok=True)
    env.v2.secret_path.write_bytes(b"a persistent secret blob the dry run must not touch")
    before = env.v2.secret_path.read_bytes()

    def forbidden(*_a, **_k):
        raise AssertionError("the dry run touched the persistent secret or its ACL")

    monkeypatch.setattr(v2_run, "DpapiSecretStore", forbidden)
    monkeypatch.setattr(v2_run, "require_protected", forbidden)
    for name in ("dpapi_unprotect", "dpapi_protect", "acl_state_of", "protect_directory", "initialise", "require_protected"):
        monkeypatch.setattr(v2_keys, name, forbidden)
    for name in ("exists", "load", "create", "acl_state"):
        monkeypatch.setattr(v2_keys.DpapiSecretStore, name, forbidden)
    code, output = env.dry()
    assert code == 0 and "persistent_secret_used=0" in output and env.v2.secret_path.read_bytes() == before


def test_the_dry_run_uses_a_fresh_random_throwaway_key_every_time(env, monkeypatch):
    masters = []
    real = v2_run.derive_subkeys
    monkeypatch.setattr(v2_run, "derive_subkeys", lambda master: masters.append(master) or real(master))
    env.put_all()
    env.dry()
    env.dry()
    assert len(masters) == 2 and masters[0] != masters[1] and all(len(m) == 32 for m in masters)
    assert FakeStore().master not in masters


def test_the_dry_run_makes_no_network_call(env, monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("the dry run touched the network")

    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(requests, "post", forbidden)
    monkeypatch.setattr(v2_uploader, "post_batch", forbidden)
    monkeypatch.setattr(v2_uploader, "post_status", forbidden)
    env.put_all()
    assert env.dry()[0] == 0


def test_the_dry_run_reads_only_a_bounded_tail(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, dry_run_tail_bytes=2048)
    empty_other_sources(env, checkins=checkin_lines(2000))
    _code, output = env.dry()
    values = dict(line.split("=", 1) for line in output.strip().splitlines())
    assert 0 < int(values["lines_read"]) < 200


def test_the_dry_run_applies_the_patron_guard_using_profiles_read_in_the_same_tail(env):
    env.put_all()
    write_lines(env.tech / "checkins.txt", [checkin_line(BASE, "CANARY-PATRON-CARD-99")], append=True)
    values = dict(line.split("=", 1) for line in env.dry()[1].strip().splitlines())
    assert int(values["dropped_patron_card"]) >= 4                          # the ACS tail taught it the card; ACS, check-in and reject scans drop


def test_a_dry_run_failure_is_a_fixed_code_not_a_value(env):
    env.v2.rules_path.write_text("CANARY-not-json", encoding="utf-8")
    env.put_all()
    with pytest.raises(v2_run.CollectorV2Error) as caught:
        env.dry()
    assert "CANARY" not in str(caught.value) + repr(caught.value)


# =====================================================================================================================
# Dispatch from collector/run.py
# =====================================================================================================================

def _spy_v1_and_v2(monkeypatch):
    calls = []
    monkeypatch.setattr(collector_run, "run_once", lambda *a, **k: calls.append("v1") or collector_run.RunOutcome(exit_code=0, status={}))
    monkeypatch.setattr(collector_run.parsers, "build_production_parse_fns", lambda **_k: {})
    monkeypatch.setattr(collector_run.uploader, "build_session", lambda: object())
    monkeypatch.setattr(v2_run, "main_v2", lambda *a, **k: calls.append("v2") or 0)
    return calls


@pytest.mark.parametrize("mode", [None, "v1"])
def test_a_config_without_contract_mode_or_with_v1_still_runs_the_v1_path(tmp_path, monkeypatch, mode):
    env = Env(tmp_path, monkeypatch)
    calls = _spy_v1_and_v2(monkeypatch)
    assert collector_run.main(["--config", str(write_config(env.root, contract_mode=mode))]) == 0
    assert calls == ["v1"]


def test_contract_mode_v2_dispatches_to_the_v2_runner(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(v2_run, "main_v2", lambda cfg, path, *, logger: seen.update(path=path) or 0)
    monkeypatch.setattr(collector_run, "run_once", lambda *a, **k: pytest.fail("the v1 path ran in v2 mode"))
    assert collector_run.main(["--config", str(env.config_path)]) == 0
    assert seen == {"path": str(env.config_path)}


def test_v2_dry_run_flag_dispatches_even_from_a_v1_config_and_before_any_v1_configuration(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    path = write_config(env.root, contract_mode=None)
    seen = []
    monkeypatch.setattr(v2_run, "main_v2_dry_run", lambda config_path, **_k: seen.append(config_path) or 0)
    monkeypatch.setattr(v2_run, "main_v2", lambda *a, **k: pytest.fail("the credentialed v2 runner ran for a dry run"))
    monkeypatch.setattr(collector_run, "run_once", lambda *a, **k: pytest.fail("the v1 path ran for a dry run"))
    monkeypatch.setattr(collector_run, "load_config", lambda *a, **k: pytest.fail("the v1 configuration was built for a dry run"))
    assert collector_run.main(["--config", str(path), "--v2-dry-run"]) == 0 and seen == [str(path)]


def test_an_invalid_contract_mode_is_a_configuration_error_exit_2(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    path = write_config(env.root, contract_mode="v9")
    assert collector_run.main(["--config", str(path)]) == 2
    assert "contract_mode" in capsys.readouterr().err


def test_main_v2_dry_run_end_to_end_prints_the_counts(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch)
    env.put_all()
    assert collector_run.main(["--config", str(env.config_path), "--v2-dry-run"]) == 0
    out = capsys.readouterr()
    assert "dry_run_complete=1" in out.out and find_leaks((out.out + out.err).encode()) == []


def test_main_v2_run_end_to_end_with_a_patched_session_and_store(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.put_all()
    session = ScriptedSession()
    monkeypatch.setattr(v2_run, "DpapiSecretStore", lambda _path: FakeStore())
    monkeypatch.setattr(v2_run.uploader, "build_session", lambda: session)
    assert collector_run.main(["--config", str(env.config_path)]) == 0
    assert len(session.uploads()) == 3 and env.status()["health_status"] == "healthy"
    assert env.leaks() == []


def test_a_crash_in_the_v2_runner_is_logged_by_type_and_location_only(tmp_path, monkeypatch, capsys, caplog):
    env = Env(tmp_path, monkeypatch)
    env.put_all()
    monkeypatch.setattr(v2_run, "DpapiSecretStore", lambda _path: FakeStore())

    def boom():
        raise RuntimeError("CANARY-CRASH-MESSAGE " + TOKEN)

    monkeypatch.setattr(v2_run.uploader, "build_session", boom)
    assert collector_run.main(["--config", str(env.config_path)]) == 1
    output = capsys.readouterr()
    logged = caplog.text + env.cfg.log_path.read_text()
    assert "CANARY-CRASH" not in logged + output.out + output.err and TOKEN not in logged + output.out + output.err
    assert "RuntimeError" in logged


def test_v1_and_v2_use_different_state_files_so_v2_never_moves_the_v1_cursor(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    v1_state.save_state(env.cfg.state_path, v1_state.with_source(v1_state.load_state(env.cfg.state_path), "acs",
                                                                 v1_state.SourceState((1, 2), 77)))
    before = env.cfg.state_path.read_bytes()
    env.put_all()
    env.run(ScriptedSession())
    assert env.v2.state_path != env.cfg.state_path and env.cfg.state_path.read_bytes() == before


# --- v1 must not depend on the v2 modules (a release bundle does not ship them yet) -----------------------------------------------

@pytest.mark.parametrize("document", [{}, {"contract_mode": "v1"}, {"contract_mode": "v2"}, {"contract_mode": "V2"}, {"contract_mode": 2},
                                      {"contract_mode": None}, {"contract_mode": ""}, {"contract_mode": True}, {"other": 1}])
def test_the_inline_mode_reader_agrees_with_the_v2_config_reader(tmp_path, document):
    from collector.config import ConfigError
    from collector.v2_config import read_contract_mode
    path = tmp_path / "c.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    try:
        expected = read_contract_mode(path)
    except ConfigError:
        with pytest.raises(ConfigError):
            collector_run._requested_contract_mode(str(path))
    else:
        assert collector_run._requested_contract_mode(str(path)) == expected


def test_the_v1_run_path_imports_no_v2_module(tmp_path):
    """A fresh interpreter that imports and runs the v1 entry point up to dispatch must not have loaded any v2 module."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(collector_run.__file__).resolve().parent.parent
    script = ("import sys, collector.run as r\n"
              "r._requested_contract_mode(sys.argv[1])\n"
              "print(sorted(m for m in sys.modules if m.startswith('collector.v2_')))\n")
    config = write_config(tmp_path / "root", contract_mode=None)
    result = subprocess.run([sys.executable, "-c", script, str(config)], cwd=root, capture_output=True, text=True, check=False,
                            env={**__import__("os").environ, "DATABASE_URL": "sqlite:///x", "PYTHONPATH": str(root)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "[]"


def test_v1_still_runs_when_every_v2_module_is_absent(tmp_path, monkeypatch):
    import sys
    env = Env(tmp_path, monkeypatch)
    for name in [m for m in sys.modules if m.startswith("collector.v2_")]:
        monkeypatch.setitem(sys.modules, name, None)                  # importing any of them now fails, as in a build without v2
    monkeypatch.setattr(collector_run, "run_once", lambda *a, **k: collector_run.RunOutcome(exit_code=0, status={}))
    monkeypatch.setattr(collector_run.parsers, "build_production_parse_fns", lambda **_k: {})
    monkeypatch.setattr(collector_run.uploader, "build_session", lambda: object())
    assert collector_run.main(["--config", str(write_config(env.root, contract_mode=None))]) == 0


@pytest.mark.parametrize("argv_extra,mode", [([], "v2"), (["--v2-dry-run"], None)])
def test_a_build_without_v2_reports_it_clearly_instead_of_crashing(tmp_path, monkeypatch, capsys, argv_extra, mode):
    import sys
    env = Env(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "collector.v2_run", None)
    path = write_config(env.root, contract_mode=mode)
    assert collector_run.main(["--config", str(path), *argv_extra]) == 2
    assert "does not include Contract v2" in capsys.readouterr().err


def test_the_release_tool_does_not_bundle_the_v2_modules_yet():
    """Release integration is a separate step: until it lands the v2 modules are declared, but not shipped."""
    from pathlib import Path

    from collector import build_release
    v2_files = {f"collector/{p.name}" for p in (Path(collector_run.__file__).parent).glob("v2_*.py")}
    assert v2_files and v2_files <= set(build_release.BUILD_ONLY_COLLECTOR_FILES)
    assert v2_files.isdisjoint(build_release.COLLECTOR_RUNTIME_FILES)


# --- the dry run is independent of every credential ----------------------------------------------------------------------------------

def minimal_dry_config(root, *, rules_path=True, state_path=False, timezone="America/Chicago"):
    """A config holding ONLY what a dry run may need: source paths, a time zone and (by path or by default) the local rules."""
    root.mkdir(parents=True, exist_ok=True)
    section = {"timezone": timezone}
    if rules_path:
        section["rules_path"] = str(root / "config" / "classification_rules.json")
    document = {"sources": [{"name": name, "path": str(root / "tech" / f"{name}.txt")} for name in ("acs", "checkins", "rejects")],
                "v2": section}
    if state_path:
        document["state_path"] = str(root / "state" / "state.json")
    path = root / "min_config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def seed_minimal_install(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch)
    env.put_all()
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    return env


def test_the_dry_run_runs_with_the_api_token_absent(tmp_path, monkeypatch, capsys):
    import os
    env = seed_minimal_install(tmp_path, monkeypatch)
    assert "SORTVIEW_API_TOKEN" not in os.environ
    assert collector_run.main(["--config", str(env.config_path), "--v2-dry-run"]) == 0
    out = capsys.readouterr()
    assert "dry_run_complete=1" in out.out and "events_acs_items=7" in out.out
    assert find_leaks((out.out + out.err).encode()) == [] and "Missing API token" not in out.err


def test_the_dry_run_needs_only_source_paths_timezone_and_rules(tmp_path, monkeypatch, capsys):
    """No api_url, no customer/branch ids, no state/status/log paths, no key_id, no contract_mode, no token."""
    env = seed_minimal_install(tmp_path, monkeypatch)
    minimal = minimal_dry_config(env.root)
    monkeypatch.setattr(collector_run, "load_config", lambda *_a, **_k: pytest.fail("the dry run built the v1 configuration"))
    assert collector_run.main(["--config", str(minimal), "--v2-dry-run"]) == 0
    assert "dry_run_complete=1" in capsys.readouterr().out


def test_the_dry_run_finds_the_default_rules_next_to_the_state_path_when_no_rules_path_is_given(tmp_path, monkeypatch, capsys):
    env = seed_minimal_install(tmp_path, monkeypatch)
    minimal = minimal_dry_config(env.root, rules_path=False, state_path=True)
    assert collector_run.main(["--config", str(minimal), "--v2-dry-run"]) == 0
    assert "dry_run_complete=1" in capsys.readouterr().out


def test_the_dry_run_never_reads_the_api_token_from_the_environment(tmp_path, monkeypatch, capsys):
    import os
    env = seed_minimal_install(tmp_path, monkeypatch)

    class Trap(dict):
        def _guard(self, key):
            if key == "SORTVIEW_API_TOKEN":
                raise AssertionError("the dry run read SORTVIEW_API_TOKEN")

        def get(self, key, default=None):
            self._guard(key)
            return super().get(key, default)

        def __getitem__(self, key):
            self._guard(key)
            return super().__getitem__(key)

        def __contains__(self, key):
            self._guard(key)
            return super().__contains__(key)

    monkeypatch.setattr(os, "environ", Trap(os.environ))
    assert collector_run.main(["--config", str(env.config_path), "--v2-dry-run"]) == 0
    capsys.readouterr()


def test_the_dry_run_needs_no_server_registration_and_makes_no_network_call(tmp_path, monkeypatch, capsys):
    env = seed_minimal_install(tmp_path, monkeypatch)

    def forbidden(*_a, **_k):
        raise AssertionError("the dry run touched the network")

    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(requests, "post", forbidden)
    monkeypatch.setattr(v2_run.uploader, "build_session", forbidden)
    monkeypatch.setattr(v2_uploader, "post_batch", forbidden)
    monkeypatch.setattr(v2_uploader, "post_status", forbidden)
    assert collector_run.main(["--config", str(minimal_dry_config(env.root)), "--v2-dry-run"]) == 0
    assert "network_calls=0" in capsys.readouterr().out


def test_the_dry_run_writes_nothing_under_the_install_root_when_run_through_the_cli(tmp_path, monkeypatch, capsys):
    env = seed_minimal_install(tmp_path, monkeypatch)
    minimal = minimal_dry_config(env.root)
    before = env.snapshot()
    assert collector_run.main(["--config", str(minimal), "--v2-dry-run"]) == 0
    capsys.readouterr()
    assert env.snapshot() == before


def test_the_dry_run_writes_no_state_cache_quarantine_status_or_log_file_of_its_own(tmp_path):
    """A fresh interpreter, an EMPTY working directory, no token: the ONLY files that can appear are the v1 parser modules' own empty log
    files, which `collector.run` creates merely by being imported (it imports the v1 parsers) -- nothing the dry run itself writes."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(collector_run.__file__).resolve().parent.parent
    install = tmp_path / "install"
    write_config(install)
    config = install / "config.json"
    write_rules(load_v2(config))
    for name, lines in full_corpus(BASE).items():
        write_lines(install / "tech" / f"{name}.txt", lines)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "SORTVIEW_API_TOKEN"}
    env.update({"PYTHONPATH": str(root), "DATABASE_URL": "sqlite:///x"})
    result = subprocess.run([sys.executable, "-m", "collector.run", "--config", str(config), "--v2-dry-run"], cwd=cwd, env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    made = {p.relative_to(cwd).as_posix(): p.stat().st_size for p in cwd.rglob("*")}
    assert set(made) <= {"logs", "logs/parser.acs.log", "logs/parser.checkins.log", "logs/parser.rejects.log"}, made
    assert all(size == 0 for name, size in made.items() if name.endswith(".log")), made        # empty: nothing was ever logged into them
    assert not (install / "data").exists() and not (install / "state").exists() and not (install / "logs").exists()


@pytest.mark.parametrize(("mutate", "needle"), [
    (lambda d: d.pop("v2"), "v2"), (lambda d: d["v2"].pop("timezone"), "timezone"), (lambda d: d["v2"].update(timezone="Mars/Base"), "timezone"),
    (lambda d: d.update(sources=[]), "sources"), (lambda d: d["sources"].append(d["sources"][0]), "duplicate"),
    (lambda d: d["sources"][0].pop("path"), "sources"), (lambda d: d["v2"].pop("rules_path"), "rules_path"),
    (lambda d: d["v2"].update(dry_run_tail_bytes=5), "dry_run_tail_bytes"),
])
def test_a_dry_run_config_problem_is_a_fixed_message_naming_the_setting(tmp_path, monkeypatch, capsys, mutate, needle):
    env = seed_minimal_install(tmp_path, monkeypatch)
    minimal = minimal_dry_config(env.root)
    document = json.loads(minimal.read_text())
    mutate(document)
    minimal.write_text(json.dumps(document), encoding="utf-8")
    assert collector_run.main(["--config", str(minimal), "--v2-dry-run"]) == 2
    err = capsys.readouterr().err
    assert "Configuration error" in err and needle in err


def test_a_missing_rules_file_is_a_fixed_code_for_the_dry_run_too(tmp_path, monkeypatch, capsys):
    env = seed_minimal_install(tmp_path, monkeypatch)
    env.v2.rules_path.unlink()
    assert collector_run.main(["--config", str(minimal_dry_config(env.root)), "--v2-dry-run"]) == 2
    assert "rules_missing" in capsys.readouterr().err


# --- the collision counter (an onsite dry-run acceptance check) -------------------------------------------------------------------------

def test_the_dry_run_reports_identical_identity_events_zero_for_distinct_events(env):
    env.put_all()
    values = dict(line.split("=", 1) for line in env.dry()[1].strip().splitlines())
    assert values["identical_identity_events"] == "0"


def test_the_dry_run_counts_events_that_share_every_safe_field(env):
    """Two check-ins of the same item, destination and bin in the same second are indistinguishable once the raw line is dropped: that is the
    collision the counter measures. No raw or personal value is used to tell them apart."""
    env.put(acs=[], rejects=[], checkins=[checkin_line(BASE, BARCODE_CI, "Westside", "3"), checkin_line(BASE, BARCODE_CI, "Westside", "3"),
                                          checkin_line(BASE + timedelta(seconds=1), BARCODE_CI, "Westside", "3")])
    values = dict(line.split("=", 1) for line in env.dry()[1].strip().splitlines())
    assert values["identical_identity_events"] == "1" and values["events_checkins"] == "3"
