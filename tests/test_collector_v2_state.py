"""Contract v2 collector: the local stores and inputs -- patron cache, ruleset registry, quarantine, rules, config, reader."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import date, timedelta

import pytest
from collector_v2_support import (
    KEY_ID,
    MASTER,
    NAME_COLL,
    NAME_PROG,
    PATRON_ADULT,
    RULES_DOC,
    find_leaks,
    write_config,
    write_lines,
)

from collector import (
    v2_config,
    v2_events,
    v2_identity,
    v2_reader,
    v2_rules,
)
from collector.config import ConfigError
from collector.v2_patrons import PatronCache
from collector.v2_quarantine import Quarantine, QuarantineEntry, QuarantineEntryError
from collector.v2_rules import RulesError

KEYS = v2_identity.derive_subkeys(MASTER)
TODAY = date(2026, 6, 15)  # freshness: allow FRESH004 -- passed as today= to a cache/quarantine that never reads the real clock


def pk(text):
    return v2_identity.patron_id_hmac(KEYS, text)


# =====================================================================================================================
# Patron cache
# =====================================================================================================================

@pytest.fixture
def cache(tmp_path):
    c = PatronCache(tmp_path / "patrons.db", today=TODAY)
    yield c
    c.close()


def test_a_committed_profile_can_be_looked_up_by_its_hmac(cache):
    cache.stage(pk(PATRON_ADULT), name_k=v2_identity.name_hmac(KEYS, "SOME NAME"), type_ill=True, name_ill=False, profile=True)
    cache.commit()
    info = cache.lookup(pk(PATRON_ADULT))
    assert info.type_ill is True and info.name_ill is False and info.name_k == v2_identity.name_hmac(KEYS, "some name")
    assert cache.contains(pk(PATRON_ADULT)) and not cache.contains(pk("someone else"))


def test_staged_changes_are_invisible_to_the_file_until_committed_and_discard_drops_them(tmp_path):
    path = tmp_path / "p.db"
    cache = PatronCache(path, today=TODAY)
    cache.stage(pk("A"), profile=True, name_k=None)
    assert cache.contains(pk("A"))                       # visible to this run's guard
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM patrons").fetchone()[0] == 0
    cache.discard()
    assert not cache.contains(pk("A"))
    cache.stage(pk("B"), profile=True)
    cache.commit()
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM patrons").fetchone()[0] == 1
    cache.close()


def test_a_plain_sighting_never_replaces_a_staged_or_stored_profile(cache):
    cache.stage(pk("P"), name_k=b"k" * 32, type_ill=True, name_ill=True, profile=True)
    cache.stage(pk("P"))                                  # staged plain sighting after a staged profile
    cache.commit()
    cache.stage(pk("P"))                                  # plain sighting after a stored profile
    cache.commit()
    info = cache.lookup(pk("P"))
    assert (info.type_ill, info.name_ill, info.name_k) == (True, True, b"k" * 32)


def test_the_cache_file_holds_no_cleartext_identifier_or_name(tmp_path):
    path = tmp_path / "p.db"
    cache = PatronCache(path, today=TODAY)
    for i in range(50):
        identifier, name = f"CANARY-PATRON-{i:04d}", f"CANARY-NAME-{i:04d}"
        cache.stage(pk(identifier), name_k=v2_identity.name_hmac(KEYS, name), type_ill=bool(i % 2), name_ill=False, profile=True)
    cache.commit()
    cache.purge()
    cache.close()
    every_file = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
    assert find_leaks(every_file, ("CANARY-PATRON", "CANARY-NAME", "CANARY-")) == []
    # ... and its schema has no column that could hold one
    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(patrons)")}
    assert columns == {"pk", "name_k", "type_ill", "name_ill", "first_seen", "last_seen"}


def test_dates_are_whole_days_only(cache):
    cache.stage(pk("A"), profile=True)
    cache.commit()
    first, last = cache._db.execute("SELECT first_seen, last_seen FROM patrons").fetchone()
    assert first == last == TODAY.isoformat() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", first)


def test_ttl_is_a_180_day_sliding_window(tmp_path):
    path = tmp_path / "p.db"
    day_one = PatronCache(path, today=TODAY)
    day_one.stage(pk("KEPT"), profile=True)
    day_one.stage(pk("STALE"), profile=True)
    day_one.commit()
    day_one.close()

    later = PatronCache(path, today=TODAY + timedelta(days=100))
    later.stage(pk("KEPT"))                                # seen again on day 100: the window slides
    later.commit()
    later.close()

    expiry = PatronCache(path, today=TODAY + timedelta(days=181))
    expired, evicted = expiry.purge()
    assert (expired, evicted) == (1, 0)                    # STALE (last seen day 0) is gone; KEPT (last seen day 100) is not
    assert expiry.contains(pk("KEPT")) and not expiry.contains(pk("STALE"))
    expiry.close()

    much_later = PatronCache(path, today=TODAY + timedelta(days=281))
    assert much_later.purge() == (1, 0) and not much_later.contains(pk("KEPT"))
    much_later.close()


def test_a_row_exactly_at_the_ttl_boundary_is_kept(tmp_path):
    path = tmp_path / "p.db"
    c = PatronCache(path, today=TODAY)
    c.stage(pk("EDGE"), profile=True)
    c.commit()
    c.close()
    boundary = PatronCache(path, ttl_days=180, today=TODAY + timedelta(days=180))
    assert boundary.purge() == (0, 0) and boundary.contains(pk("EDGE"))
    boundary.close()


def test_the_row_cap_evicts_the_least_recently_seen(tmp_path):
    path = tmp_path / "p.db"
    for offset, name in enumerate(("OLDEST", "MIDDLE", "NEWEST")):
        c = PatronCache(path, today=TODAY + timedelta(days=offset), max_rows=1000)
        c.stage(pk(name), profile=True)
        c.commit()
        c.close()
    capped = PatronCache(path, today=TODAY + timedelta(days=3), max_rows=2)
    expired, evicted = capped.purge()
    assert (expired, evicted) == (0, 1)
    assert not capped.contains(pk("OLDEST")) and capped.contains(pk("MIDDLE")) and capped.contains(pk("NEWEST"))
    capped.close()


def test_a_read_only_cache_never_creates_or_changes_a_file(tmp_path):
    missing = tmp_path / "sub" / "none.db"
    ro = PatronCache(missing, today=TODAY, read_only=True)
    ro.stage(pk("A"), profile=True)
    assert ro.contains(pk("A"))                            # in-memory overlay still works for the run's own guard
    ro.commit()
    assert ro.purge() == (0, 0)
    ro.ruleset_id_for(b"f" * 32)
    ro.close()
    assert not missing.exists() and not missing.parent.exists()

    real = tmp_path / "real.db"
    c = PatronCache(real, today=TODAY)
    c.stage(pk("KEEP"), profile=True)
    c.commit()
    c.close()
    before = hashlib.sha256(real.read_bytes()).hexdigest()
    ro = PatronCache(real, today=TODAY + timedelta(days=999), read_only=True)
    ro.stage(pk("NEW"), profile=True)
    ro.commit()
    ro.purge()
    ro.ruleset_id_for(b"z" * 32)
    assert ro.contains(pk("KEEP"))
    ro.close()
    assert hashlib.sha256(real.read_bytes()).hexdigest() == before


# --- ruleset registry ---------------------------------------------------------------------------------------------------

def test_a_ruleset_id_is_a_random_uuid4_that_is_stable_for_one_fingerprint(cache):
    first = cache.ruleset_id_for(b"a" * 32)
    assert uuid.UUID(first).version == 4 and re.fullmatch(v2_events.UUID4_PATTERN, first)
    assert cache.ruleset_id_for(b"a" * 32) == first
    assert cache.ruleset_id_for(b"b" * 32) != first


def test_a_ruleset_id_survives_a_restart_and_is_not_derived_from_the_fingerprint(tmp_path):
    fingerprint = b"c" * 32
    one = PatronCache(tmp_path / "a.db", today=TODAY)
    first = one.ruleset_id_for(fingerprint)
    one.close()
    again = PatronCache(tmp_path / "a.db", today=TODAY)
    assert again.ruleset_id_for(fingerprint) == first
    again.close()
    other = PatronCache(tmp_path / "b.db", today=TODAY)
    assert other.ruleset_id_for(fingerprint) != first     # same fingerprint, another install: unrelated id
    other.close()
    assert fingerprint.hex() not in first and hashlib.sha256(fingerprint).hexdigest()[:8] not in first


def test_the_ruleset_id_changes_when_the_rules_change_and_returns_when_they_return(tmp_path):
    a = v2_rules.compile_rules(RULES_DOC, KEYS)
    b = v2_rules.compile_rules({**RULES_DOC, "collection_services_names": [NAME_COLL, "NEW ACCOUNT"]}, KEYS)
    c = PatronCache(tmp_path / "p.db", today=TODAY)
    ida, idb = c.ruleset_id_for(a.fingerprint), c.ruleset_id_for(b.fingerprint)
    assert ida != idb and c.ruleset_id_for(a.fingerprint) == ida
    c.close()


def test_old_rulesets_are_capped(tmp_path):
    c = PatronCache(tmp_path / "p.db", today=TODAY)
    for i in range(80):
        c.ruleset_id_for(i.to_bytes(32, "big"))
    c.purge()
    assert c._db.execute("SELECT COUNT(*) FROM rulesets").fetchone()[0] <= 50
    c.close()


# =====================================================================================================================
# Quarantine
# =====================================================================================================================

def entry(n=1, kind="checkins", reason="event_conflict", on="2026-06-15"):
    return QuarantineEntry(hashlib.sha256(str(n).encode()).hexdigest(), kind, "2026-06-01T10:00:00Z", reason, on)


def test_a_quarantine_entry_holds_exactly_five_safe_fields():
    assert set(entry().as_dict()) == {"event_key", "kind", "event_time", "reason", "quarantined_on"}
    assert set(QuarantineEntry.__dataclass_fields__) == {"event_key", "kind", "event_time", "reason", "quarantined_on"}


@pytest.mark.parametrize("field,value", [
    ("event_key", "CANARY-BARCODE-XYZ"), ("event_key", "A" * 64), ("event_key", "a" * 63), ("kind", "patrons"), ("kind", "CANARY-KIND"),
    ("event_time", "2026-06-01 10:00:00"), ("event_time", "CANARY"), ("reason", "CANARY-RAW reject text"), ("reason", "409 conflict"),
    ("quarantined_on", "June 1"), ("quarantined_on", "2026-06-01T00:00:00Z"),
])
def test_an_unsafe_quarantine_entry_is_refused_without_echoing_the_value(field, value):
    fields = entry().as_dict()
    fields[field] = value
    with pytest.raises(QuarantineEntryError) as caught:
        QuarantineEntry(**fields)
    assert value not in str(caught.value) and field in str(caught.value)


def test_a_quarantine_entry_cannot_carry_an_extra_field():
    with pytest.raises(TypeError):
        QuarantineEntry(**entry().as_dict(), barcode="CANARY-BARCODE-XYZ")


def test_quarantine_persists_deduplicates_and_counts_retained_entries(tmp_path):
    path = tmp_path / "q.json"
    q = Quarantine(path, today=TODAY)
    q.add([entry(1), entry(2), entry(1)])
    assert q.count == 2 and len(q) == 2
    reopened = Quarantine(path, today=TODAY)
    assert reopened.count == 2 and reopened.contains("checkins", entry(1).event_key)
    assert reopened.keys_for("checkins") == {entry(1).event_key, entry(2).event_key} and reopened.keys_for("rejects") == set()
    assert json.loads(path.read_text())["schema_version"] == 1


def test_the_same_event_key_under_another_kind_is_a_different_entry(tmp_path):
    q = Quarantine(tmp_path / "q.json", today=TODAY)
    q.add([entry(1, "checkins"), entry(1, "rejects")])
    assert q.count == 2 and q.keys_for("acs_items") == set()


def test_entries_expire_after_retention_and_are_pruned(tmp_path):
    path = tmp_path / "q.json"
    Quarantine(path, today=TODAY).add([entry(1, on="2026-01-01"), entry(2, on=TODAY.isoformat())])
    later = Quarantine(path, today=TODAY, retention_days=90)
    assert later.count == 1 and later.contains("checkins", entry(2).event_key)


def test_the_store_is_capped_and_drops_the_oldest_first(tmp_path):
    q = Quarantine(tmp_path / "q.json", max_entries=3, today=TODAY)
    q.add([entry(n) for n in range(1, 6)])
    assert q.count == 3 and [e.event_key for e in q.entries] == [entry(n).event_key for n in (3, 4, 5)]


def test_the_file_contains_only_safe_fields_and_no_canary(tmp_path):
    path = tmp_path / "q.json"
    Quarantine(path, today=TODAY).add([entry(n) for n in range(20)])
    document = json.loads(path.read_text())
    assert set(document) == {"schema_version", "entries"}
    assert all(set(item) == {"event_key", "kind", "event_time", "reason", "quarantined_on"} for item in document["entries"])
    assert find_leaks(path.read_bytes()) == []


def test_a_corrupt_quarantine_file_is_set_aside_and_the_store_starts_empty(tmp_path):
    path = tmp_path / "q.json"
    path.write_text('{"schema_version": 1, "entries": [{"event_key": "CANARY-BARCODE-XYZ"}]}', encoding="utf-8")
    q = Quarantine(path, today=TODAY)
    assert q.count == 0 and q.reset_from_corrupt_file
    assert (tmp_path / "q.json.corrupt").exists() and not path.exists()
    path.write_text("not json at all", encoding="utf-8")
    assert Quarantine(path, today=TODAY).reset_from_corrupt_file


def test_a_read_only_quarantine_never_writes_or_moves_a_file(tmp_path):
    path = tmp_path / "q.json"
    Quarantine(path, today=TODAY).add([entry(1)])
    before = path.read_bytes()
    ro = Quarantine(path, today=TODAY, read_only=True)
    ro.add([entry(2)])
    ro.save()
    ro.prune_and_save()
    assert path.read_bytes() == before and ro.count == 1
    path.write_text("garbage", encoding="utf-8")
    assert Quarantine(path, today=TODAY, read_only=True).reset_from_corrupt_file
    assert path.read_text() == "garbage" and not (tmp_path / "q.json.corrupt").exists()


def test_a_missing_file_is_an_empty_quarantine_and_creates_nothing(tmp_path):
    q = Quarantine(tmp_path / "nothing" / "q.json", today=TODAY)
    assert q.count == 0 and not (tmp_path / "nothing").exists()


# =====================================================================================================================
# Rules
# =====================================================================================================================

def test_rules_compile_names_to_hmacs_and_keep_no_name_text():
    rules = v2_rules.compile_rules(RULES_DOC, KEYS)
    assert v2_identity.name_hmac(KEYS, NAME_PROG) in rules.branch_name_hmacs
    assert v2_identity.name_hmac(KEYS, NAME_COLL) in rules.collection_name_hmacs
    assert NAME_PROG.lower().encode() not in repr(rules).lower().encode() or "hmac" in repr(rules).lower()
    assert all(isinstance(h, bytes) and len(h) == 32 for h in rules.branch_name_hmacs | rules.collection_name_hmacs)


def test_rule_names_are_compared_the_dashboard_way_stripped_and_upper_cased():
    rules = v2_rules.compile_rules({**RULES_DOC, "branch_services_names": ["  mixed Case Name  "]}, KEYS)
    assert v2_identity.name_hmac(KEYS, "MIXED CASE NAME") in rules.branch_name_hmacs


def test_the_fingerprint_is_keyed_stable_and_order_independent():
    a = v2_rules.compile_rules({**RULES_DOC, "branch_services_names": ["B", "A"]}, KEYS)
    b = v2_rules.compile_rules({**RULES_DOC, "branch_services_names": ["A", "B", "a"]}, KEYS)
    other_master = v2_rules.compile_rules({**RULES_DOC, "branch_services_names": ["A", "B"]}, v2_identity.derive_subkeys(bytes(32)))
    assert a.fingerprint == b.fingerprint != other_master.fingerprint and len(a.fingerprint) == 32


def test_da_patterns_become_delimited_upper_case_markers():
    rules = v2_rules.compile_rules({**RULES_DOC, "collection_services_da_patterns": ["dafoo bar"]}, KEYS)
    assert rules.collection_da_markers == ("|DAFOO BAR|",)


@pytest.mark.parametrize("bad", [
    None, [], "text", {"schema_version": 2}, {"schema_version": 1, "destinations": "x"},
    {"schema_version": 1, "destinations": [{"slug": "Bad Slug", "contains": "x"}]},
    {"schema_version": 1, "destinations": [{"slug": "ok", "contains": ""}]},
    {"schema_version": 1, "branch_services_names": "not a list"},
    {"schema_version": 1, "branch_services_names": [1, 2]},
])
def test_an_invalid_rules_document_is_a_fixed_code_error(bad):
    with pytest.raises(RulesError) as caught:
        v2_rules.compile_rules(bad, KEYS)
    assert caught.value.code == "rules_invalid" and str(caught.value) == "rules_invalid"


def test_a_missing_rules_file_is_an_error_never_an_empty_ruleset(tmp_path):
    with pytest.raises(RulesError) as caught:
        v2_rules.load_rules(tmp_path / "missing.json", KEYS)
    assert caught.value.code == "rules_missing"


def test_a_malformed_rules_file_does_not_echo_its_content(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text('{"schema_version": 1, "branch_services_names": ["CANARY-NAME"', encoding="utf-8")
    with pytest.raises(RulesError) as caught:
        v2_rules.load_rules(path, KEYS)
    assert "CANARY" not in str(caught.value) + repr(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


SETTINGS = {
    "transit": {"destinations": [{"key": "westside", "label": "Westside", "enabled": True},
                                 {"key": "west annex", "label": "West Annex", "enabled": True},
                                 {"key": "off", "label": "Disabled", "enabled": False},
                                 {"key": "9", "label": "Nine", "enabled": True}]},
    "internal_routing": {"branch_services_names": ["Prog Acct ", "prog acct"], "collection_services_names": ["Cat Acct"],
                         "branch_services_da_patterns": ["dabranch"], "collection_services_da_patterns": ["dacoll"]},
}


def test_the_rules_are_seeded_from_the_current_dashboard_settings():
    document = v2_rules.seed_from_settings(SETTINGS)
    assert [d["slug"] for d in document["destinations"]] == ["westside", "west_annex", "d_9"]
    assert document["branch_services_names"] == ["PROG ACCT"] and document["collection_services_names"] == ["CAT ACCT"]
    assert document["branch_services_da_patterns"] == ["DABRANCH"] and document["collection_services_da_patterns"] == ["DACOLL"]
    v2_rules.compile_rules(document, KEYS)                  # what it seeds is valid


def test_the_seed_command_writes_a_local_file_prints_only_counts_and_refuses_to_overwrite(tmp_path, capsys):
    settings, out = tmp_path / "settings.json", tmp_path / "config" / "rules.json"
    settings.write_text(json.dumps(SETTINGS), encoding="utf-8")
    assert v2_rules.main(["seed", "--settings", str(settings), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "PROG ACCT" not in printed and "Prog Acct" not in printed and "destinations=3" in printed
    assert json.loads(out.read_text())["branch_services_names"] == ["PROG ACCT"]
    assert v2_rules.main(["seed", "--settings", str(settings), "--out", str(out)]) == 2
    assert v2_rules.main(["seed", "--settings", str(tmp_path / "nope.json"), "--out", str(tmp_path / "x.json")]) == 1


# =====================================================================================================================
# Configuration
# =====================================================================================================================

def test_a_config_without_contract_mode_is_v1(tmp_path):
    assert v2_config.read_contract_mode(write_config(tmp_path, contract_mode=None)) == "v1"


@pytest.mark.parametrize("mode", ["v1", "v2"])
def test_contract_mode_reads_v1_and_v2(tmp_path, mode):
    assert v2_config.read_contract_mode(write_config(tmp_path, contract_mode=mode)) == mode


@pytest.mark.parametrize("mode", ["V2", "2", "v3", "", None, 2, True])
def test_any_other_contract_mode_is_refused(tmp_path, mode):
    path = write_config(tmp_path, contract_mode="v2")
    document = json.loads(path.read_text())
    document["contract_mode"] = mode
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ConfigError):
        v2_config.read_contract_mode(path)


def test_v2_settings_load_with_sibling_defaults_and_a_zone(tmp_path):
    cfg = v2_config.load_v2_config(write_config(tmp_path / "root"), require=True)
    assert cfg.key_id == KEY_ID and cfg.timezone == "America/Chicago" and str(cfg.zone()) == "America/Chicago"
    assert cfg.state_path.name == "state_v2.json" and cfg.patron_cache_path.name == "patron_cache.db"
    v1_state = (tmp_path / "root" / "state" / "state.json").resolve()
    assert v1_state not in {cfg.state_path.resolve(), cfg.status_path.resolve(), cfg.quarantine_path.resolve(), cfg.patron_cache_path.resolve()}
    assert cfg.secret_path.name == "v2_key.dpapi" and cfg.rules_path.name == "classification_rules.json"


def test_v2_uses_its_own_state_and_status_files_never_the_v1_ones(tmp_path):
    from collector.config import load_config
    path = write_config(tmp_path / "root")
    os.environ["SORTVIEW_API_TOKEN"] = "x"
    v1, v2 = load_config(path), v2_config.load_v2_config(path, require=True)
    assert v2.state_path != v1.state_path and v2.status_path != v1.status_path


def test_no_v2_section_is_none_unless_required(tmp_path):
    path = write_config(tmp_path)
    document = json.loads(path.read_text())
    del document["v2"]
    path.write_text(json.dumps(document), encoding="utf-8")
    assert v2_config.load_v2_config(path) is None
    with pytest.raises(ConfigError):
        v2_config.load_v2_config(path, require=True)


@pytest.mark.parametrize("mutation,setting", [
    ({"key_id": "not-a-uuid"}, "key_id"), ({"key_id": "3F2B8C1E-4D5A-4B6C-8D7E-9F0A1B2C3D4E"}, "key_id"), ({"key_id": None}, "key_id"),
    ({"timezone": None}, "timezone"), ({"timezone": ""}, "timezone"), ({"timezone": "Mars/Olympus_Mons"}, "timezone"),
    ({"timezone": "../../etc/passwd"}, "timezone"), ({"chunk_lines": 0}, "chunk_lines"), ({"chunk_lines": "many"}, "chunk_lines"),
    ({"max_events_per_request": 1001}, "max_events_per_request"), ({"max_events_per_request": True}, "max_events_per_request"),
    ({"request_interval_seconds": -1}, "request_interval_seconds"), ({"patron_ttl_days": 0}, "patron_ttl_days"),
    ({"secret_path": ""}, "secret_path"),
])
def test_bad_v2_settings_are_refused_naming_the_setting_not_the_value(tmp_path, mutation, setting):
    path = write_config(tmp_path)
    document = json.loads(path.read_text())
    document["v2"].update(mutation)
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        v2_config.load_v2_config(path, require=True)
    message = str(caught.value)
    assert setting in message
    for value in mutation.values():
        if isinstance(value, str) and len(value) > 3:
            assert value not in message


def test_the_timezone_is_required_there_is_no_default(tmp_path):
    path = write_config(tmp_path)
    document = json.loads(path.read_text())
    del document["v2"]["timezone"]
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ConfigError, match="timezone"):
        v2_config.load_v2_config(path, require=True)


def test_config_errors_do_not_leak_file_content(tmp_path):
    path = tmp_path / "c.json"
    path.write_text('{"contract_mode": "v2", "CANARY-SECRET": ', encoding="utf-8")
    with pytest.raises(ConfigError) as caught:
        v2_config.read_contract_mode(path)
    assert "CANARY" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


# =====================================================================================================================
# Reader
# =====================================================================================================================

def lines_n(n, start=0):
    return [f"line-{i:05d}\n" for i in range(start, start + n)]


def test_a_chunk_is_bounded_and_the_cursor_advances_by_true_bytes(tmp_path):
    path = tmp_path / "f.txt"
    write_lines(path, lines_n(10))
    first = v2_reader.read_chunk(str(path), None, 4)
    assert first.lines == lines_n(4) and first.more and first.cursor.offset == 4 * len(lines_n(1)[0])
    second = v2_reader.read_chunk(str(path), first.cursor, 4)
    assert second.lines == lines_n(4, 4) and second.more
    third = v2_reader.read_chunk(str(path), second.cursor, 4)
    assert third.lines == lines_n(2, 8) and not third.more
    assert v2_reader.read_chunk(str(path), third.cursor, 4).lines == []


def test_a_partial_trailing_line_is_never_consumed_until_it_is_complete(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"complete-1\ncomplete-2\npartial-no-newline")
    chunk = v2_reader.read_chunk(str(path), None, 100)
    assert chunk.lines == ["complete-1\n", "complete-2\n"] and chunk.cursor.offset == len(b"complete-1\ncomplete-2\n")
    with open(path, "ab") as handle:
        handle.write(b"-now-done\n")
    assert v2_reader.read_chunk(str(path), chunk.cursor, 100).lines == ["partial-no-newline-now-done\n"]


def test_crlf_lines_are_normalized_and_offsets_stay_true_byte_counts(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    chunk = v2_reader.read_chunk(str(path), None, 2)
    assert chunk.lines == ["one\n", "two\n"] and chunk.cursor.offset == len(b"one\r\ntwo\r\n")


def test_multibyte_and_invalid_utf8_do_not_desynchronize_offsets(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes("café-1\n".encode() + b"bad-\xff\xfe-bytes\n" + b"after\n")
    chunk = v2_reader.read_chunk(str(path), None, 1)
    assert chunk.cursor.offset == len("café-1\n".encode())
    rest = v2_reader.read_chunk(str(path), chunk.cursor, 10)
    assert len(rest.lines) == 2 and rest.lines[1] == "after\n" and rest.cursor.offset == path.stat().st_size


def test_a_missing_file_reports_it_missing_and_keeps_the_cursor(tmp_path):
    cursor = v2_events.Cursor((1, 2), 10)
    chunk = v2_reader.read_chunk(str(tmp_path / "gone.txt"), cursor, 5)
    assert not chunk.existed and chunk.lines == [] and chunk.cursor == cursor


def test_a_rotated_file_is_read_from_the_start(tmp_path):
    path = tmp_path / "f.txt"
    write_lines(path, lines_n(5))
    first = v2_reader.read_chunk(str(path), None, 100)
    os.replace(path, tmp_path / "f.old")
    write_lines(path, lines_n(3, 100))
    after = v2_reader.read_chunk(str(path), first.cursor, 100)
    assert after.rotated and after.lines == lines_n(3, 100)


def test_a_truncated_file_is_read_from_the_start(tmp_path):
    path = tmp_path / "f.txt"
    write_lines(path, lines_n(50))
    first = v2_reader.read_chunk(str(path), None, 1000)
    write_lines(path, lines_n(2, 900))                     # same file, now shorter than the cursor
    after = v2_reader.read_chunk(str(path), first.cursor, 1000)
    assert after.truncated and after.lines == lines_n(2, 900)


def test_the_tail_is_bounded_complete_lines_only_and_changes_nothing(tmp_path):
    path = tmp_path / "f.txt"
    body = "".join(lines_n(1000))
    path.write_bytes(body.encode() + b"unfinished")
    before = path.read_bytes()
    tail = v2_reader.read_tail(str(path), 500)
    assert tail and all(line.endswith("\n") and line.startswith("line-") for line in tail)
    assert tail[-1] == "line-00999\n" and "unfinished" not in "".join(tail)
    assert len("".join(tail)) <= 500
    assert path.read_bytes() == before
    assert v2_reader.read_tail(str(tmp_path / "gone"), 500) == []
    assert v2_reader.read_tail(str(path), 10_000_000)[0] == "line-00000\n"


def test_the_v1_state_is_unchanged_by_reading(tmp_path):
    path = tmp_path / "f.txt"
    write_lines(path, lines_n(3))
    stat = path.stat()
    v2_reader.read_chunk(str(path), None, 10)
    v2_reader.read_tail(str(path), 100)
    assert path.stat().st_mtime_ns == stat.st_mtime_ns and path.stat().st_size == stat.st_size
