"""Contract v2 collector: a patron's message-64 record that arrives AFTER (or changes after) the hold it classifies.

The dashboard classifies an ACS window all at once: for every item it joins the LATEST message-64 record of the item's patron that lies in
the same window, whichever side of the item it falls on. A streaming collector emits a hold before a later profile exists, so it must be able
to CORRECT that hold. The correction is an ordinary `hold` event for the same item and the same `event_time` with the new flags; the stream's
"latest = greatest (event_time, id)" rule (docs/contract-v2-design.md section 9.4) makes it win, and because the flags are part of the
event_key it is a new row, never a conflict. No patron information is sent.

Every test reduces the emitted stream exactly as the design says readers must and compares it, item by item, with the dashboard's own
classifier (`metrics.build_acs_item_summary`) run over the whole file.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from collector_v2_support import (
    MASTER,
    NAME_COLL,
    NAME_PROG,
    PATTERN_COLL,
    RULES_DOC,
    Reply,
    ScriptedSession,
    acs_line,
    item_message,
    minutes_ago,
    patron_message,
)
from test_collector_v2_run import Env, empty_other_sources

import metrics
from agent.parser import acs as acs_parser
from collector import v2_identity

BASE = minutes_ago(300)
KEYS = v2_identity.derive_subkeys(MASTER)
P1, P2, P3, P4 = "CANARY-LATE-PATRON-1", "CANARY-LATE-PATRON-2", "CANARY-LATE-PATRON-3", "CANARY-LATE-PATRON-4"


def at(minute, second=0):
    return BASE + timedelta(minutes=minute, seconds=second)


def profile(minute, patron, name, kind="ADULT", second=0):
    return acs_line(at(minute, second), patron_message(patron, name, kind))


def hold(minute, barcode, patron, prefix="101YNY", second=0, destination="Main", extra=""):
    return acs_line(at(minute, second), item_message(barcode, patron, destination, prefix=prefix, extra=extra))


def dashboard_flags(lines):
    """barcode -> (is_ill, is_branch_services, is_collection_services) for every item the dashboard counts as a hold (its latest 101 record)."""
    frame = acs_parser.parse_lines(lines)
    summary = metrics.build_acs_item_summary(
        frame, ["Westside", "Library Express"], RULES_DOC["branch_services_names"], RULES_DOC["collection_services_names"],
        RULES_DOC["branch_services_da_patterns"], RULES_DOC["collection_services_da_patterns"])
    items = summary["items_df"]
    if items.empty:
        return {}
    return {row.barcode: (bool(row.is_ill), bool(row.is_programming), bool(row.is_collection_services))
            for row in items.itertuples() if bool(row.is_hold)}


def stream_flags(uploads, barcodes):
    """Reduce the uploaded ACS stream as the design specifies -- latest (event_time, arrival order) per item among hold/non_hold_101 --
    and return barcode -> flags for the items whose latest state is a hold."""
    by_item = {v2_identity.item_key(KEYS, b): b for b in barcodes}
    latest = {}
    arrival = 0
    for body in uploads:
        for event in body.get("acs_items", []):
            arrival += 1
            if event["state"] == "other_code10":
                continue
            rank = (event["event_time"], arrival)
            if event["item_key"] not in latest or rank > latest[event["item_key"]][0]:
                latest[event["item_key"]] = (rank, event)
    return {by_item[k]: (e["is_ill"], e["is_branch_services"], e["is_collection_services"])
            for k, (_r, e) in latest.items() if e["state"] == "hold"}


def collect(tmp_path, monkeypatch, lines, *, chunk_lines=2, runs=1):
    env = Env(tmp_path, monkeypatch, chunk_lines=chunk_lines)
    empty_other_sources(env, acs=lines)
    session = ScriptedSession()
    for _ in range(runs):
        assert env.run(session) == 0
    return env, session


SCENARIOS = {
    # the profile arrives AFTER the hold, in a later chunk: type ILL
    "ILL patron type after the hold": ([hold(0, "B-ITYPE", P1), hold(1, "B-FILL1", P4), hold(2, "B-FILL2", P4), profile(20, P1, "SOME PERSON", "ILL")]),
    "branch services name after the hold": ([hold(0, "B-PROG", P2), hold(1, "B-F1", P4), hold(2, "B-F2", P4), profile(20, P2, NAME_PROG, "STAFF")]),
    "collection services name after the hold": ([hold(0, "B-COLL", P3), hold(1, "B-F1", P4), hold(2, "B-F2", P4), profile(20, P3, NAME_COLL, "STAFF")]),
    "ILL-looking name after the hold": ([hold(0, "B-NAME", P1), hold(1, "B-F1", P4), hold(2, "B-F2", P4), profile(20, P1, "INTERLIBRARY LOAN DESK")]),
    # control: the profile is BEFORE the hold
    "profile before the hold": ([profile(0, P1, "SOME PERSON", "ILL"), hold(1, "B-CTL", P1), hold(2, "B-F1", P4), hold(3, "B-F2", P4)]),
    # a benign profile changes nothing
    "benign profile after the hold": ([hold(0, "B-BENIGN", P1), hold(1, "B-F1", P4), hold(2, "B-F2", P4), profile(20, P1, "SOME PERSON", "ADULT")]),
    # the profile changes: the dashboard uses the LATEST in the window for every item of the patron
    "profile changes from ADULT to ILL": ([profile(0, P1, "SOME PERSON", "ADULT"), hold(1, "B-CHG", P1), hold(2, "B-F1", P4),
                                           hold(3, "B-F2", P4), profile(20, P1, "SOME PERSON", "ILL")]),
    "profile changes from ILL back to ADULT": ([profile(0, P1, "SOME PERSON", "ILL"), hold(1, "B-REV", P1), hold(2, "B-F1", P4),
                                                hold(3, "B-F2", P4), profile(20, P1, "SOME PERSON", "ADULT")]),
    "A to B to A: unknown, then ILL, then ADULT again": ([hold(0, "B-ABA", P1), hold(1, "B-F1", P4), hold(2, "B-F2", P4),
                                                          profile(20, P1, "SOME PERSON", "ILL"), hold(21, "B-F3", P4), hold(22, "B-F4", P4),
                                                          profile(40, P1, "SOME PERSON", "ADULT")]),
    # several items of one patron are all corrected
    "two held items of one patron": ([hold(0, "B-ONE", P2), hold(1, "B-TWO", P2), hold(2, "B-F1", P4), hold(3, "B-F2", P4),
                                      profile(20, P2, NAME_PROG, "STAFF")]),
    # a later record for the item wins over the correction
    "a later non-hold retracts the hold before the profile arrives": ([hold(0, "B-GONE", P1), hold(5, "B-GONE", P1, prefix="101NNY"),
                                                                       hold(6, "B-F1", P4), hold(7, "B-F2", P4),
                                                                       profile(20, P1, "SOME PERSON", "ILL")]),
    "a later hold for the same item is corrected too (the dashboard applies the latest profile to the latest hold)": ([hold(0, "B-AGAIN", P1), hold(5, "B-AGAIN", P1),
                                                                                hold(6, "B-F1", P4), hold(7, "B-F2", P4),
                                                                                profile(20, P1, "SOME PERSON", "ILL")]),
    "a hold's own DA-pattern and raw ILL flags are kept when the profile is applied": (
        [hold(0, "B-PAT", P1, extra=f"|{PATTERN_COLL}|"), hold(1, "B-RAWILL", P1, extra="|DAILL"), hold(2, "B-F1", P4), hold(3, "B-F2", P4),
         profile(20, P1, NAME_PROG, "STAFF")]),
    "a non-hold item and an other-code-10 record are never corrected": (
        [hold(0, "B-NH", P1, prefix="101NNY"), hold(1, "B-O10", P1, prefix="100NUN"), hold(2, "B-F1", P4), hold(3, "B-F2", P4),
         profile(20, P1, "SOME PERSON", "ILL")]),
    "a later other-code-10 record leaves the Overview hold standing, and it is still corrected": (
        [hold(0, "B-OC", P1), hold(5, "B-OC", P1, prefix="100NUN"), hold(6, "B-F1", P4), hold(7, "B-F2", P4),
         profile(20, P1, "SOME PERSON", "ILL")]),
    "a later non-hold in the SAME second as the hold is not resurrected by the correction": (
        [hold(0, "B-SAME", P1), hold(0, "B-SAME", P1, prefix="101NNY"), hold(6, "B-F1", P4), hold(7, "B-F2", P4),
         profile(20, P1, "SOME PERSON", "ILL")]),
    "two patrons, each corrected independently": ([hold(0, "B-P1", P1), hold(1, "B-P2", P2), hold(2, "B-F1", P4), hold(3, "B-F2", P4),
                                                   profile(20, P1, "SOME PERSON", "ILL"), profile(21, P2, NAME_COLL, "STAFF")]),
    "a hold with no patron id can never change": ([hold(0, "B-NOPATRON", None), hold(1, "B-F1", P4), hold(2, "B-F2", P4),
                                                   profile(20, P1, "SOME PERSON", "ILL")]),
}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=list(SCENARIOS))
def test_the_streamed_hold_classification_equals_the_dashboards_whole_window_classification(tmp_path, monkeypatch, scenario):
    lines = SCENARIOS[scenario]
    _env, session = collect(tmp_path, monkeypatch, lines)
    barcodes = sorted({b for b in acs_parser.parse_lines(lines)["barcode"].dropna() if b})
    assert stream_flags(session.uploads(), barcodes) == dashboard_flags(lines)


def test_a_rerun_over_the_same_files_sends_nothing_new(tmp_path, monkeypatch):
    lines = SCENARIOS["ILL patron type after the hold"]
    env, _session = collect(tmp_path, monkeypatch, lines)
    again = ScriptedSession()
    assert env.run(again) == 0 and again.uploads() == []


# =====================================================================================================================
# What a correction is
# =====================================================================================================================

def item_events(session, barcode):
    key = v2_identity.item_key(KEYS, barcode)
    return [e for body in session.uploads() for e in body.get("acs_items", []) if e["item_key"] == key]


def test_a_correction_is_a_hold_for_the_same_item_and_time_with_new_flags_and_a_new_event_key(tmp_path, monkeypatch):
    _env, session = collect(tmp_path, monkeypatch, SCENARIOS["ILL patron type after the hold"])
    original, correction = item_events(session, "B-ITYPE")
    assert original["state"] == correction["state"] == "hold"
    assert original["event_time"] == correction["event_time"] and original["item_key"] == correction["item_key"]
    assert original["destination"] == correction["destination"]
    assert original["event_key"] != correction["event_key"]                     # a NEW row: never a 409 against the original
    assert (original["is_ill"], correction["is_ill"]) == (False, True)
    assert original["ruleset_id"] == correction["ruleset_id"]                   # provenance of the run that sent it
    assert set(correction) == set(original) == {"state", "event_key", "event_time", "item_key", "destination", "is_ill",
                                                "is_branch_services", "is_collection_services", "ruleset_id"}


def test_a_correction_is_sent_after_the_original_so_it_wins_the_latest_rule(tmp_path, monkeypatch):
    _env, session = collect(tmp_path, monkeypatch, SCENARIOS["ILL patron type after the hold"])
    positions = [(i, e["is_ill"]) for i, e in enumerate(e for b in session.uploads() for e in b.get("acs_items", []))
                 if e["item_key"] == v2_identity.item_key(KEYS, "B-ITYPE")]
    assert positions[0][0] < positions[1][0] and [flag for _i, flag in positions] == [False, True]


def test_an_unchanged_classification_sends_no_correction(tmp_path, monkeypatch):
    _env, session = collect(tmp_path, monkeypatch, SCENARIOS["benign profile after the hold"])
    assert len(item_events(session, "B-BENIGN")) == 1
    assert _env.status()["counters"]["acs_corrections"] == 0


def test_only_the_patrons_own_holds_are_corrected(tmp_path, monkeypatch):
    _env, session = collect(tmp_path, monkeypatch, SCENARIOS["two patrons, each corrected independently"])
    assert len(item_events(session, "B-P1")) == 2 and len(item_events(session, "B-P2")) == 2
    assert len(item_events(session, "B-F1")) == 1 and len(item_events(session, "B-F2")) == 1
    assert _env.status()["counters"]["acs_corrections"] == 2


def test_a_correction_that_returns_to_the_first_value_is_a_third_distinct_event(tmp_path, monkeypatch):
    _env, session = collect(tmp_path, monkeypatch, SCENARIOS["A to B to A: unknown, then ILL, then ADULT again"])
    first, second, third = item_events(session, "B-ABA")
    assert (first["is_ill"], second["is_ill"], third["is_ill"]) == (False, True, False)
    assert len({first["event_key"], second["event_key"], third["event_key"]}) == 3
    assert first["event_time"] == second["event_time"] == third["event_time"]


def test_corrections_carry_no_patron_information_and_leak_nothing(tmp_path, monkeypatch):
    import json

    from collector_v2_support import every_byte_under, find_leaks
    env, session = collect(tmp_path, monkeypatch, SCENARIOS["ILL patron type after the hold"])
    blob = json.dumps(session.calls).encode()
    for needle in (P1, P4, "SOME PERSON", "B-ITYPE"):
        assert needle.encode() not in blob and needle.lower().encode() not in blob
    assert find_leaks(every_byte_under(env.root) + blob) == []


# =====================================================================================================================
# The ledger
# =====================================================================================================================

def ledger(env):
    import sqlite3
    db = sqlite3.connect(env.v2.patron_cache_path)
    try:
        return {row[0]: row for row in db.execute(
            "SELECT item_key, event_time, patron_pk, destination, s_ill, s_branch, s_coll, f_ill, f_branch, f_coll, revision, event_day FROM holds")}
    finally:
        db.close()


def test_the_ledger_holds_only_hashes_and_safe_values(tmp_path, monkeypatch):
    import re
    import sqlite3
    env, _session = collect(tmp_path, monkeypatch, SCENARIOS["ILL patron type after the hold"])
    rows = ledger(env)
    key = v2_identity.item_key(KEYS, "B-ITYPE")
    assert key in rows
    for item_key, event_time, patron_pk, destination, *_flags, revision, event_day in rows.values():
        assert re.fullmatch(r"[0-9a-f]{64}", item_key) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", event_time)
        assert isinstance(patron_pk, bytes) and len(patron_pk) == 32 and re.fullmatch(r"[a-z][a-z0-9_]*", destination)
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", event_day) and revision >= 0
    assert {row[1] for row in sqlite3.connect(env.v2.patron_cache_path).execute("PRAGMA table_info(holds)")} == {
        "item_key", "event_time", "patron_pk", "destination", "s_ill", "s_branch", "s_coll", "f_ill", "f_branch", "f_coll", "revision", "event_day"}
    # the patron column is the LOCAL patron HMAC, not the item's or anything derivable from cleartext
    assert rows[key][2] == v2_identity.patron_id_hmac(KEYS, P1)


def test_the_ledger_file_contains_no_cleartext(tmp_path, monkeypatch):
    from collector_v2_support import find_leaks
    env, _session = collect(tmp_path, monkeypatch, SCENARIOS["two patrons, each corrected independently"])
    raw = env.v2.patron_cache_path.read_bytes()
    assert find_leaks(raw) == [] and b"B-P1" not in raw and b"SOME PERSON" not in raw and P1.encode() not in raw


def test_the_ledger_tracks_the_latest_hold_and_its_corrected_flags_and_revision(tmp_path, monkeypatch):
    env, _session = collect(tmp_path, monkeypatch, SCENARIOS["A to B to A: unknown, then ILL, then ADULT again"])
    row = ledger(env)[v2_identity.item_key(KEYS, "B-ABA")]
    assert row[10] == 2 and (row[7], row[8], row[9]) == (0, 0, 0)               # two corrections; back to "no flags"


def test_a_later_101_record_removes_the_hold_from_the_ledger(tmp_path, monkeypatch):
    env, _session = collect(tmp_path, monkeypatch, SCENARIOS["a later non-hold retracts the hold before the profile arrives"])
    assert v2_identity.item_key(KEYS, "B-GONE") not in ledger(env)


def test_a_later_other_code_10_record_leaves_the_hold_in_the_ledger_but_a_same_second_one_removes_it(tmp_path, monkeypatch):
    env, _session = collect(tmp_path, monkeypatch, SCENARIOS["a later other-code-10 record leaves the Overview hold standing, and it is still corrected"])
    assert v2_identity.item_key(KEYS, "B-OC") in ledger(env)
    env2, _s = collect(tmp_path / "second", monkeypatch, [hold(0, "B-TIE", P1), hold(0, "B-TIE", P1, prefix="100NUN"),
                                                          hold(6, "B-F1", P4), hold(7, "B-F2", P4)])
    assert v2_identity.item_key(KEYS, "B-TIE") not in ledger(env2)


def test_a_hold_without_a_patron_id_is_never_in_the_ledger(tmp_path, monkeypatch):
    env, _session = collect(tmp_path, monkeypatch, SCENARIOS["a hold with no patron id can never change"])
    assert v2_identity.item_key(KEYS, "B-NOPATRON") not in ledger(env)


def test_a_newer_hold_of_the_same_item_replaces_the_ledger_row(tmp_path, monkeypatch):
    env, session = collect(tmp_path, monkeypatch, SCENARIOS["a later hold for the same item is corrected too (the dashboard applies the latest profile to the latest hold)"])
    key = v2_identity.item_key(KEYS, "B-AGAIN")
    times = sorted({e["event_time"] for e in item_events(session, "B-AGAIN")})
    assert len(times) == 2 and ledger(env)[key][1] == times[-1]                 # the ledger follows the item's LATEST hold
    assert len([k for k in ledger(env) if k == key]) == 1


# =====================================================================================================================
# Replay, crashes and retention
# =====================================================================================================================

@pytest.mark.parametrize("scenario", ["ILL patron type after the hold", "two patrons, each corrected independently",
                                      "branch services name after the hold"])
def test_a_replay_after_losing_the_cursor_sends_only_events_the_server_already_has(tmp_path, monkeypatch, scenario):
    """Cursor lost, files re-read: the ledger keeps each item's identity (its revision), so every replayed event_key was already sent."""
    env, first = collect(tmp_path, monkeypatch, SCENARIOS[scenario])
    sent = {e["event_key"] for b in first.uploads() for e in b.get("acs_items", [])}
    env.v2.state_path.unlink()
    second = ScriptedSession()
    assert env.run(second) == 0
    replayed = {e["event_key"] for b in second.uploads() for e in b.get("acs_items", [])}
    assert replayed and replayed <= sent


@pytest.mark.parametrize("scenario", ["A to B to A: unknown, then ILL, then ADULT again", "profile changes from ADULT to ILL",
                                      "profile changes from ILL back to ADULT"])
def test_a_replay_when_a_profile_changed_may_add_correction_rows_but_the_final_state_is_still_right(tmp_path, monkeypatch, scenario):
    """The one non-idempotent replay: a patron whose profile CHANGED within the re-read range. Re-reading walks the profile through its earlier
    values again, which sends higher-revision corrections. They are new rows, the LAST one restores the right answer, and the reduced stream
    still equals the dashboard's."""
    lines = SCENARIOS[scenario]
    env, first = collect(tmp_path, monkeypatch, lines)
    env.v2.state_path.unlink()
    second = ScriptedSession()
    assert env.run(second) == 0
    barcodes = sorted({b for b in acs_parser.parse_lines(lines)["barcode"].dropna() if b})
    assert stream_flags(first.uploads() + second.uploads(), barcodes) == dashboard_flags(lines)


def test_a_failed_delivery_of_a_correction_changes_neither_the_cursor_nor_the_ledger(tmp_path, monkeypatch):
    env = Env(tmp_path, monkeypatch, chunk_lines=2)
    empty_other_sources(env, acs=SCENARIOS["ILL patron type after the hold"])
    failing = ScriptedSession(script=[None], default=Reply(503))
    assert env.run(failing) == 1
    key = v2_identity.item_key(KEYS, "B-ITYPE")
    assert ledger(env)[key][10] == 0 and (ledger(env)[key][7:10]) == (0, 0, 0)          # the first chunk committed; the correction did not
    resumed = ScriptedSession()
    assert env.run(resumed) == 0
    assert ledger(env)[key][10] == 1 and ledger(env)[key][7] == 1
    assert len(item_events(resumed, "B-ITYPE")) == 1                                       # exactly the correction was (re)sent


def test_holds_older_than_the_retention_window_are_purged_and_can_no_longer_be_corrected(tmp_path):
    from datetime import date

    from collector.v2_patrons import HoldRow, PatronCache
    today = date(2026, 6, 15)  # freshness: allow FRESH004 -- passed as today= to a cache that never reads the real clock
    cache = PatronCache(tmp_path / "p.db", hold_days=90, hold_max_rows=1000, today=today)
    old = HoldRow("a" * 64, "2026-01-01T10:00:00Z", b"p" * 32, "main", (False,) * 3, (False,) * 3)
    edge = HoldRow("b" * 64, "2026-03-17T10:00:00Z", b"p" * 32, "main", (False,) * 3, (False,) * 3)      # exactly 90 days old
    fresh = HoldRow("c" * 64, "2026-06-14T10:00:00Z", b"p" * 32, "main", (False,) * 3, (False,) * 3)
    for row in (old, edge, fresh):
        cache.hold_put(row)
    cache.commit()
    assert cache.purge_holds() == 1
    assert [r.item_key for r in cache.holds_for_patron(b"p" * 32)] == ["b" * 64, "c" * 64]
    cache.close()


def test_the_ledger_row_cap_drops_the_oldest_first(tmp_path):
    from datetime import date

    from collector.v2_patrons import HoldRow, PatronCache
    cache = PatronCache(tmp_path / "p.db", hold_days=3650, hold_max_rows=3, today=date(2026, 6, 15))  # freshness: allow FRESH004 -- today=
    for n in range(6):
        cache.hold_put(HoldRow(f"{n:064x}", f"2026-06-{10 + n:02d}T10:00:00Z", b"p" * 32, "main", (False,) * 3, (False,) * 3))
    cache.commit()
    assert cache.purge_holds() == 3
    assert [r.item_key for r in cache.holds_for_patron(b"p" * 32)] == [f"{n:064x}" for n in (3, 4, 5)]
    cache.close()


def test_staged_ledger_changes_reach_the_file_only_on_commit_and_discard_drops_them(tmp_path):
    import sqlite3
    from datetime import date

    from collector.v2_patrons import HoldRow, PatronCache
    path = tmp_path / "p.db"
    cache = PatronCache(path, today=date(2026, 6, 15))  # freshness: allow FRESH004 -- today=
    row = HoldRow("a" * 64, "2026-06-14T10:00:00Z", b"p" * 32, "main", (False,) * 3, (True, False, False), 1)
    cache.hold_put(row)
    assert cache.hold_get("a" * 64) == row and [r.item_key for r in cache.holds_for_patron(b"p" * 32)] == ["a" * 64]
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM holds").fetchone()[0] == 0
    cache.discard()
    assert cache.hold_get("a" * 64) is None
    cache.hold_put(row)
    cache.commit()
    cache.hold_delete("a" * 64)
    assert cache.hold_get("a" * 64) is None and cache.holds_for_patron(b"p" * 32) == []
    cache.commit()
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM holds").fetchone()[0] == 0
    cache.close()


def test_a_read_only_cache_keeps_its_ledger_in_memory_only(tmp_path):
    from datetime import date

    from collector.v2_patrons import HoldRow, PatronCache
    missing = tmp_path / "sub" / "p.db"
    cache = PatronCache(missing, today=date(2026, 6, 15), read_only=True)  # freshness: allow FRESH004 -- today=
    cache.hold_put(HoldRow("a" * 64, "2026-06-14T10:00:00Z", b"p" * 32, "main", (False,) * 3, (False,) * 3))
    assert cache.holds_for_patron(b"p" * 32)
    cache.commit()
    assert cache.purge_holds() == 0 and cache.hold_count() == 0
    cache.close()
    assert not missing.exists() and not missing.parent.exists()


def test_a_cache_file_from_before_the_ledger_existed_opens_read_only_without_error(tmp_path):
    import sqlite3
    from datetime import date

    from collector.v2_patrons import PatronCache
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE patrons (pk BLOB PRIMARY KEY, name_k BLOB, type_ill INT, name_ill INT, first_seen TEXT, last_seen TEXT)")
    db.commit()
    db.close()
    ro = PatronCache(path, today=date(2026, 6, 15), read_only=True)  # freshness: allow FRESH004 -- today=
    assert ro.holds_for_patron(b"x" * 32) == [] and ro.hold_get("a" * 64) is None and ro.hold_count() == 0
    ro.close()
    upgraded = PatronCache(path, today=date(2026, 6, 15))  # freshness: allow FRESH004 -- today=
    assert upgraded.hold_count() == 0
    upgraded.close()


def test_the_hold_ledger_settings_are_configurable_and_bounded(tmp_path):
    from collector_v2_support import write_config

    from collector.config import ConfigError
    from collector.v2_config import load_v2_config
    cfg = load_v2_config(write_config(tmp_path / "a"), require=True)
    assert (cfg.hold_ledger_days, cfg.hold_ledger_max_rows) == (90, 250_000)
    custom = load_v2_config(write_config(tmp_path / "b", v2_extra={"hold_ledger_days": 30, "hold_ledger_max_rows": 5000}), require=True)
    assert (custom.hold_ledger_days, custom.hold_ledger_max_rows) == (30, 5000)
    for number, bad in enumerate(({"hold_ledger_days": 0}, {"hold_ledger_days": "many"}, {"hold_ledger_max_rows": 10}, {"hold_ledger_days": True})):
        path = write_config(tmp_path / f"bad{number}", v2_extra=bad)
        with pytest.raises(ConfigError, match="hold_ledger"):
            load_v2_config(path, require=True)


def test_purging_runs_at_the_start_of_every_run(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from collector.v2_patrons import HoldRow, PatronCache
    env = Env(tmp_path, monkeypatch, hold_ledger_days=30)
    empty_other_sources(env, acs=[], checkins=[])
    old_day = (datetime.now(UTC).date() - timedelta(days=45)).isoformat()
    cache = PatronCache(env.v2.patron_cache_path, today=datetime.now(UTC).date())
    cache.hold_put(HoldRow("d" * 64, f"{old_day}T10:00:00Z", b"p" * 32, "main", (False,) * 3, (False,) * 3))
    cache.commit()
    cache.close()
    assert "d" * 64 in ledger(env)
    assert env.run(ScriptedSession()) == 0
    assert "d" * 64 not in ledger(env)


# =====================================================================================================================
# The real server
# =====================================================================================================================

def test_the_dry_run_keeps_its_ledger_in_memory_and_writes_no_file(tmp_path, monkeypatch, capsys):
    env = Env(tmp_path, monkeypatch, chunk_lines=2)
    empty_other_sources(env, acs=SCENARIOS["ILL patron type after the hold"])
    before = env.snapshot()
    code, output = env.dry()
    assert code == 0 and "acs_corrections=0" in output and env.snapshot() == before
    capsys.readouterr()
