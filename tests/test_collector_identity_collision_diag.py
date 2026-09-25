"""collector/identity_collision_diag.py: the identical_identity_events collision-report diagnostic, and its
`identity-collision-diag` subcommand on the frozen dispatcher (collector/freeze/dispatcher.py).

Confirms it reproduces the same identical_identity_events count `run --v2-dry-run` reports, groups colliding events correctly,
leaks no raw or personal value (checked against the SAME canary corpus and leak-detector the rest of the v2 suite uses), and
never touches the network, the persistent secret, or any state/cursor/status file -- same safety contract as the dry run it
reuses (test_collector_v2_run.py). See that file's `Env` for the equivalent dry-run coverage this mirrors.
"""

from __future__ import annotations

import io
import json
from datetime import timedelta

import pytest
import requests
from collector_v2_support import (
    BARCODE_CI,
    BARCODE_HOLD,
    BARCODE_NON_HOLD,
    BARCODE_REJ,
    PATRON_ADULT,
    acs_line,
    checkin_line,
    find_leaks,
    full_corpus,
    item_message,
    load_v2,
    minutes_ago,
    reject_line,
    write_config,
    write_lines,
    write_rules,
)

from collector import identity_collision_diag, v2_keys, v2_run, v2_uploader
from collector.v2_config import load_dry_run_settings

BASE = minutes_ago(90)


def _env(tmp_path):
    root = tmp_path / "root"
    config_path = write_config(root)
    v2 = load_v2(config_path)
    write_rules(v2)
    return root, config_path


def _put(root, **sources):
    for name, lines in sources.items():
        write_lines(root / "tech" / f"{name}.txt", lines)


# --- basic grouping ---------------------------------------------------------------------------------------------------------

def test_reports_zero_collision_groups_for_distinct_events(tmp_path):
    root, config_path = _env(tmp_path)
    _put(root, acs=[], rejects=[],
         checkins=[checkin_line(BASE, BARCODE_CI), checkin_line(BASE + timedelta(seconds=1), BARCODE_CI)])
    out = io.StringIO()
    assert identity_collision_diag.report(str(config_path), out=out) == 0
    text = out.getvalue()
    assert "identical_identity_events=0" in text
    assert "collision_groups=0" in text


def test_groups_colliding_checkins_and_matches_the_dry_run_count(tmp_path):
    root, config_path = _env(tmp_path)
    _put(root, acs=[], rejects=[],
         checkins=[checkin_line(BASE, BARCODE_CI, "Westside", "3"), checkin_line(BASE, BARCODE_CI, "Westside", "3"),
                   checkin_line(BASE + timedelta(seconds=1), BARCODE_CI, "Westside", "3")])
    out = io.StringIO()
    assert identity_collision_diag.report(str(config_path), out=out) == 0
    text = out.getvalue()
    assert "identical_identity_events=1" in text
    assert "collision_groups=1" in text
    assert "group_size_histogram={2: 1}" in text
    assert "distinct_item_keys_in_collisions=1" in text
    assert "distinct_event_times_in_collisions=1" in text
    assert "destination=westside bin=3" in text


# --- requirement 2: the diagnostic's count matches identical_identity_events, across all three sources ----------------------

def test_diagnostic_totals_match_the_dry_runs_total_across_checkins_rejects_and_acs(tmp_path):
    root, config_path = _env(tmp_path)
    _put(root,
         acs=[acs_line(BASE, item_message(BARCODE_HOLD, PATRON_ADULT, "Main")),
              acs_line(BASE, item_message(BARCODE_HOLD, PATRON_ADULT, "Main"))],
         rejects=[reject_line(BASE, BARCODE_REJ), reject_line(BASE, BARCODE_REJ)],
         checkins=[checkin_line(BASE, BARCODE_CI, "Westside", "3"), checkin_line(BASE, BARCODE_CI, "Westside", "3")])

    dry_out = io.StringIO()
    assert v2_run.run_dry(load_dry_run_settings(config_path), out=dry_out) == 0
    dry_values = dict(line.split("=", 1) for line in dry_out.getvalue().strip().splitlines())

    diag_out = io.StringIO()
    assert identity_collision_diag.report(str(config_path), out=diag_out) == 0
    per_source = [int(line.split("=", 1)[1]) for line in diag_out.getvalue().splitlines()
                  if line.startswith("identical_identity_events=")]

    assert len(per_source) == 3  # one line per source: acs, checkins, rejects
    assert sum(per_source) == int(dry_values["identical_identity_events"]) == 3


# --- requirement 3: no prohibited PII/raw field ever appears in the output ---------------------------------------------------

def test_never_prints_the_barcode_or_a_raw_line(tmp_path):
    root, config_path = _env(tmp_path)
    _put(root, acs=[], rejects=[],
         checkins=[checkin_line(BASE, BARCODE_CI, "Westside", "3"), checkin_line(BASE, BARCODE_CI, "Westside", "3")])
    out = io.StringIO()
    identity_collision_diag.report(str(config_path), out=out)
    assert BARCODE_CI not in out.getvalue()


def test_output_contains_none_of_the_raw_canary_values_even_with_collisions_forced(tmp_path):
    # full_corpus() plants every kind of raw value this project treats as a leak (titles, call numbers, patron
    # ids/names/addresses/emails, raw reject text -- see collector_v2_support.RAW_CANARIES). One line per source is
    # duplicated (same barcode, same second) so every collision-reporting code path -- including the category tally,
    # which prints normalized destination/bin/error_class/state labels -- actually runs while the leak check is live.
    root, config_path = _env(tmp_path)
    corpus = full_corpus(BASE)
    corpus["checkins"] = [*corpus["checkins"], corpus["checkins"][0]]
    corpus["rejects"] = [*corpus["rejects"], corpus["rejects"][0]]
    corpus["acs"] = [*corpus["acs"], corpus["acs"][5]]  # index 5: the BARCODE_HOLD item record
    _put(root, **corpus)

    out = io.StringIO()
    assert identity_collision_diag.report(str(config_path), out=out) == 0
    text = out.getvalue()
    assert "collision_groups=1" in text  # sanity: the forced duplicates actually collided somewhere
    assert find_leaks(text.encode()) == [], "a raw/personal canary value leaked into the diagnostic's output"


def test_never_prints_an_event_key_or_item_key_value(tmp_path):
    # Counts and cardinalities only (group sizes, distinct-item/-time counts) -- never the hex key itself, even
    # though item_key/event_key are already keyed HMACs safe enough to upload (see v2_events.py).
    root, config_path = _env(tmp_path)
    _put(root, acs=[], rejects=[],
         checkins=[checkin_line(BASE, BARCODE_CI, "Westside", "3"), checkin_line(BASE, BARCODE_CI, "Westside", "3")])
    out = io.StringIO()
    identity_collision_diag.report(str(config_path), out=out)
    text = out.getvalue()
    assert not any(len(token) == 64 and all(c in "0123456789abcdef" for c in token) for token in text.split())


# --- requirement 4: no network client is ever created --------------------------------------------------------------------

def test_makes_no_network_call(tmp_path):
    def forbidden(*_a, **_k):
        raise AssertionError("the diagnostic touched the network")

    root, config_path = _env(tmp_path)
    _put(root, **full_corpus(BASE))
    import collector.identity_collision_diag as module
    assert "requests" not in module.__dict__ and "v2_uploader" not in module.__dict__  # not even imported

    original_request = requests.Session.request
    original_post = requests.post
    try:
        requests.Session.request = forbidden  # type: ignore[method-assign]
        requests.post = forbidden  # type: ignore[assignment]
        v2_uploader.post_batch = forbidden  # type: ignore[assignment]
        v2_uploader.post_status = forbidden  # type: ignore[assignment]
        out = io.StringIO()
        assert identity_collision_diag.report(str(config_path), out=out) == 0
    finally:
        requests.Session.request = original_request  # type: ignore[method-assign]
        requests.post = original_post  # type: ignore[assignment]


# --- requirement 5: the persistent v2 secret is never read -----------------------------------------------------------------

def test_never_opens_the_secret_store_or_checks_its_acl(tmp_path, monkeypatch):
    root, config_path = _env(tmp_path)
    v2 = load_v2(config_path)
    v2.secret_path.parent.mkdir(parents=True, exist_ok=True)
    v2.secret_path.write_bytes(b"a persistent secret blob the diagnostic must not touch")
    before = v2.secret_path.read_bytes()

    def forbidden(*_a, **_k):
        raise AssertionError("the diagnostic touched the persistent secret or its ACL")

    for name in ("dpapi_unprotect", "dpapi_protect", "acl_state_of", "protect_directory", "initialise", "require_protected"):
        monkeypatch.setattr(v2_keys, name, forbidden)
    for name in ("exists", "load", "create", "acl_state"):
        monkeypatch.setattr(v2_keys.DpapiSecretStore, name, forbidden)

    import collector.identity_collision_diag as module
    assert "v2_keys" not in module.__dict__  # not even imported

    _put(root, **full_corpus(BASE))
    out = io.StringIO()
    assert identity_collision_diag.report(str(config_path), out=out) == 0
    assert v2.secret_path.read_bytes() == before


def test_uses_a_fresh_random_throwaway_key_every_time(tmp_path, monkeypatch):
    masters = []
    real = identity_collision_diag.derive_subkeys
    monkeypatch.setattr(identity_collision_diag, "derive_subkeys", lambda master: masters.append(master) or real(master))
    root, config_path = _env(tmp_path)
    _put(root, **full_corpus(BASE))
    identity_collision_diag.report(str(config_path), out=io.StringIO())
    identity_collision_diag.report(str(config_path), out=io.StringIO())
    assert len(masters) == 2 and masters[0] != masters[1] and all(len(m) == 32 for m in masters)


# --- requirement 6: no cursor/state/status/quarantine/cache file is ever written ---------------------------------------------

def test_writes_nothing_under_the_install_root(tmp_path):
    root, config_path = _env(tmp_path)
    _put(root, **full_corpus(BASE))
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    out = io.StringIO()
    identity_collision_diag.report(str(config_path), out=out)
    after = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    assert before == after


def test_writes_no_state_cache_quarantine_or_status_file(tmp_path):
    root, config_path = _env(tmp_path)
    v2 = load_v2(config_path)
    _put(root, **full_corpus(BASE))
    identity_collision_diag.report(str(config_path), out=io.StringIO())
    for path in (v2.state_path, v2.status_path, v2.patron_cache_path, v2.quarantine_path):
        assert not path.exists(), path


# --- requirement 7: a malformed or missing config fails closed (via the CLI main(), not just report()) -----------------------

def test_main_fails_closed_on_a_nonexistent_config(tmp_path, capsys):
    missing = tmp_path / "does-not-exist.json"
    assert identity_collision_diag.main(["--config", str(missing)]) == 2
    assert "Configuration error" in capsys.readouterr().err


def test_main_fails_closed_on_malformed_json(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    assert identity_collision_diag.main(["--config", str(bad)]) == 2
    assert "Configuration error" in capsys.readouterr().err


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("v2"),
    lambda d: d["v2"].pop("timezone"),
    lambda d: d["v2"].update(timezone="Mars/Base"),
    lambda d: d.update(sources=[]),
])
def test_main_fails_closed_on_a_config_missing_what_the_diagnostic_needs(tmp_path, capsys, mutate):
    _root, config_path = _env(tmp_path)
    document = json.loads(config_path.read_text(encoding="utf-8"))
    mutate(document)
    config_path.write_text(json.dumps(document), encoding="utf-8")

    assert identity_collision_diag.main(["--config", str(config_path)]) == 2
    assert "Configuration error" in capsys.readouterr().err


def test_main_requires_the_config_flag():
    with pytest.raises(SystemExit):
        identity_collision_diag.main([])


# --- requirement 8: release packaging carries this module -- see also test_collector_build_release.py and
# test_collector_freeze.py for the bundle/dispatcher/spec side of this same guarantee ------------------------------------------

def test_module_is_listed_in_the_collector_runtime_files_for_release_packaging():
    from collector import build_release
    assert "collector/identity_collision_diag.py" in build_release.COLLECTOR_RUNTIME_FILES
    assert "collector/identity_collision_diag.py" not in build_release.BUILD_ONLY_COLLECTOR_FILES


# =====================================================================================
# The bounded identity-collision acceptance gate (replaces a flat identical_identity_events == 0
# requirement -- see the module docstring's GATE RULES for the full rationale). Each test drives
# report() end to end against synthetic Tech Logic lines and reads the printed `=== gate ===`
# block, exactly as collector/deploy/prepare_v2_pilot.ps1's Test-IdentityCollisionGatePassed does.
# =====================================================================================

def _gate_fields(text: str) -> dict[str, str]:
    block = text.split("=== gate ===", 1)[1]
    return dict(line.split("=", 1) for line in block.strip().splitlines())


def _reject_pair(local, error_message: str, barcode: str = "") -> list[str]:
    return [reject_line(local, barcode, error_message), reject_line(local, barcode, error_message)]


def _filler_rejects(start_seconds: int, count: int) -> list[str]:
    """`count` reject lines, each at its own distinct second, each with its own barcode -- none of
    them can collide with each other or with anything else, by construction."""
    return [reject_line(BASE + timedelta(seconds=start_seconds + i), f"FILLER-BARCODE-{i}", "item not found")
            for i in range(count)]


def test_real_nbpl_pattern_91_keyless_reject_pairs_of_1024_passes(tmp_path):
    root, config_path = _env(tmp_path)
    classes = ["ACS returned failure", "Multiple RFID tags detected"]
    pairs: list[str] = []
    for i in range(91):
        pairs.extend(_reject_pair(BASE + timedelta(seconds=i), classes[i % 2]))
    rejects = pairs + _filler_rejects(10_000, 1024 - len(pairs))
    assert len(rejects) == 1024
    _put(root, acs=[], checkins=[], rejects=rejects)

    out = io.StringIO()
    assert identity_collision_diag.report(str(config_path), out=out) == 0
    text = out.getvalue()
    assert "identical_identity_events=91" in text  # still printed, never hidden
    gate = _gate_fields(text)
    assert gate["identity_collision_gate"] == "pass"
    assert 0.0 < float(gate["keyless_reject_collision_rate"]) <= 0.12
    assert gate["max_collision_group_size"] == "2"
    assert float(gate["collision_time_spread"]) >= 0.90
    assert gate["unexpected_item_keyed_collision_groups"] == "0"
    assert gate["unexpected_keyless_collision_groups"] == "0"


def test_same_shape_above_12_percent_fails(tmp_path):
    root, config_path = _env(tmp_path)
    # 20 permitted keyless-reject pairs (40 events) against a total of 100 -- a 20% rate, comfortably over the 12% ceiling.
    classes = ["ACS returned failure", "Multiple RFID tags detected"]
    pairs: list[str] = []
    for i in range(20):
        pairs.extend(_reject_pair(BASE + timedelta(seconds=i), classes[i % 2]))
    rejects = pairs + _filler_rejects(10_000, 100 - len(pairs))
    _put(root, acs=[], checkins=[], rejects=rejects)

    out = io.StringIO()
    identity_collision_diag.report(str(config_path), out=out)
    gate = _gate_fields(out.getvalue())
    assert gate["identity_collision_gate"] == "fail"
    assert float(gate["keyless_reject_collision_rate"]) > 0.12


def test_collision_group_size_3_fails(tmp_path):
    root, config_path = _env(tmp_path)
    same_second = [reject_line(BASE, "", "ACS returned failure") for _ in range(3)]
    _put(root, acs=[], checkins=[], rejects=same_second)

    out = io.StringIO()
    identity_collision_diag.report(str(config_path), out=out)
    text = out.getvalue()
    gate = _gate_fields(text)
    assert gate["identity_collision_gate"] == "fail"
    assert gate["max_collision_group_size"] == "3"
    assert "group_size_histogram={3: 1}" in text


def test_collision_time_spread_below_0_90_fails(tmp_path):
    root, config_path = _env(tmp_path)
    # 10 distinct seconds, each carrying BOTH permitted classes at once -- 20 collision groups but
    # only 10 distinct times (spread = 0.5), comfortably clear of the group-size and rate ceilings.
    rejects: list[str] = []
    for i in range(10):
        when = BASE + timedelta(seconds=i)
        rejects.extend(_reject_pair(when, "ACS returned failure"))
        rejects.extend(_reject_pair(when, "Multiple RFID tags detected"))
    rejects += _filler_rejects(10_000, 1000)
    _put(root, acs=[], checkins=[], rejects=rejects)

    out = io.StringIO()
    identity_collision_diag.report(str(config_path), out=out)
    gate = _gate_fields(out.getvalue())
    assert gate["identity_collision_gate"] == "fail"
    assert gate["max_collision_group_size"] == "2"
    assert float(gate["keyless_reject_collision_rate"]) <= 0.12  # isolates the failure to spread, not rate
    assert float(gate["collision_time_spread"]) < 0.90


def test_expected_acs_hold_duplicate_pair_passes(tmp_path):
    root, config_path = _env(tmp_path)
    hold = item_message(BARCODE_HOLD, PATRON_ADULT, "Main")
    _put(root, acs=[acs_line(BASE, hold), acs_line(BASE, hold)], checkins=[], rejects=[])

    out = io.StringIO()
    identity_collision_diag.report(str(config_path), out=out)
    text = out.getvalue()
    gate = _gate_fields(text)
    assert "state=hold" in text
    assert gate["identity_collision_gate"] == "pass"
    assert gate["unexpected_item_keyed_collision_groups"] == "0"


def test_unexpected_item_keyed_checkin_pair_fails(tmp_path):
    root, config_path = _env(tmp_path)
    _put(root, acs=[], rejects=[],
         checkins=[checkin_line(BASE, BARCODE_CI, "Westside", "3"), checkin_line(BASE, BARCODE_CI, "Westside", "3")])

    out = io.StringIO()
    identity_collision_diag.report(str(config_path), out=out)
    gate = _gate_fields(out.getvalue())
    assert gate["identity_collision_gate"] == "fail"
    assert gate["unexpected_item_keyed_collision_groups"] == "1"


def test_unexpected_item_keyed_reject_pair_fails(tmp_path):
    root, config_path = _env(tmp_path)
    # A barcode IS present here (unlike the permitted keyless case) -- item-keyed, and rejects are
    # never a permitted item-keyed source (only acs/hold is), so this must fail.
    rejects = [reject_line(BASE, BARCODE_REJ, "ACS returned failure"), reject_line(BASE, BARCODE_REJ, "ACS returned failure")]
    _put(root, acs=[], checkins=[], rejects=rejects)

    out = io.StringIO()
    identity_collision_diag.report(str(config_path), out=out)
    gate = _gate_fields(out.getvalue())
    assert gate["identity_collision_gate"] == "fail"
    assert gate["unexpected_item_keyed_collision_groups"] == "1"


def test_unexpected_item_keyed_acs_non_hold_pair_fails(tmp_path):
    root, config_path = _env(tmp_path)
    non_hold = item_message(BARCODE_NON_HOLD, PATRON_ADULT, "Main", prefix="101NNY")
    _put(root, acs=[acs_line(BASE, non_hold), acs_line(BASE, non_hold)], checkins=[], rejects=[])

    out = io.StringIO()
    identity_collision_diag.report(str(config_path), out=out)
    text = out.getvalue()
    gate = _gate_fields(text)
    assert "state=non_hold_101" in text or "state=" in text  # sanity: not misclassified as a hold
    assert "state=hold" not in text
    assert gate["identity_collision_gate"] == "fail"
    assert gate["unexpected_item_keyed_collision_groups"] == "1"


def test_unexpected_keyless_reject_class_fails(tmp_path):
    root, config_path = _env(tmp_path)
    # "item not found" -> error_class=item_not_found, which is NOT in PERMITTED_KEYLESS_REJECT_ERROR_CLASSES.
    rejects = [reject_line(BASE, "", "item not found"), reject_line(BASE, "", "item not found")]
    _put(root, acs=[], checkins=[], rejects=rejects)

    out = io.StringIO()
    identity_collision_diag.report(str(config_path), out=out)
    gate = _gate_fields(out.getvalue())
    assert gate["identity_collision_gate"] == "fail"
    assert gate["unexpected_keyless_collision_groups"] == "1"


def test_zero_collisions_passes(tmp_path):
    root, config_path = _env(tmp_path)
    _put(root, **full_corpus(BASE))  # the stock corpus has no forced duplicates anywhere

    out = io.StringIO()
    identity_collision_diag.report(str(config_path), out=out)
    text = out.getvalue()
    gate = _gate_fields(text)
    assert gate["identity_collision_gate"] == "pass"
    assert gate["max_collision_group_size"] == "0"
    assert gate["unexpected_item_keyed_collision_groups"] == "0"
    assert gate["unexpected_keyless_collision_groups"] == "0"


def test_gate_output_and_every_permitted_scenario_together_leak_no_raw_canary(tmp_path):
    # Combines a permitted ACS-hold pair, a permitted keyless-reject pair, and the full canary
    # corpus in one run, so every printing code path (per-source detail AND the gate block) is
    # exercised at once while checking for leaks.
    root, config_path = _env(tmp_path)
    corpus = full_corpus(BASE)
    hold = item_message(BARCODE_HOLD, PATRON_ADULT, "Main")
    corpus["acs"] = [*corpus["acs"], acs_line(BASE + timedelta(seconds=500), hold), acs_line(BASE + timedelta(seconds=500), hold)]
    corpus["rejects"] = [*corpus["rejects"], reject_line(BASE + timedelta(seconds=600), "", "ACS returned failure"),
                         reject_line(BASE + timedelta(seconds=600), "", "ACS returned failure")]
    _put(root, **corpus)

    out = io.StringIO()
    assert identity_collision_diag.report(str(config_path), out=out) == 0
    text = out.getvalue()
    assert "=== gate ===" in text
    assert find_leaks(text.encode()) == [], "a raw/personal canary value leaked into the gate-inclusive diagnostic output"
    # the gate block itself is only pass/fail strings and numbers -- never a hex key
    gate_block = text.split("=== gate ===", 1)[1]
    assert not any(len(tok) == 64 and all(c in "0123456789abcdef" for c in tok) for tok in gate_block.split())
