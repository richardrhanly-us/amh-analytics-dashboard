"""Tests for collector/bootstrap_state.py -- the production first-run
bootstrap tool (real-AMH deployment finding: a brand-new install with no
seeded state would otherwise replay every historical record in every
configured source file on its first run).
"""

from __future__ import annotations

import json

import pytest
from collector_token_support import store_api_token

from collector import bootstrap_state, state
from collector.config import load_config

CUSTOMER_ID = 1
BRANCH_ID = 1


def _write_config(tmp_path, **overrides):
    doc = {
        "customer_id": CUSTOMER_ID,
        "branch_id": BRANCH_ID,
        "api_url": "https://example.invalid",
        "sources": [
            {"name": "checkins", "path": str(tmp_path / "Checkins.txt")},
            {"name": "rejects", "path": str(tmp_path / "Rejects.txt")},
            {"name": "acs", "path": str(tmp_path / "ACS Log.txt")},
        ],
        "state_path": str(tmp_path / "state.json"),
        "status_path": str(tmp_path / "status.json"),
        "log_path": str(tmp_path / "collector.log"),
    }
    doc.update(overrides)
    path = tmp_path / "collector_config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _load(tmp_path, monkeypatch, **overrides):
    store_api_token(monkeypatch, "test-token")
    config_path = _write_config(tmp_path, **overrides)
    return load_config(config_path)


# --- complete final newline: seeds at true end of file ----------------------


def test_seeds_at_end_of_file_when_last_line_is_complete(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_bytes(b"line one\nline two\n")
    (tmp_path / "Rejects.txt").write_bytes(b"reject one\n")
    (tmp_path / "ACS Log.txt").write_bytes(b"acs one\n")
    cfg = _load(tmp_path, monkeypatch)

    result = bootstrap_state.bootstrap_state(cfg)

    checkins_size = (tmp_path / "Checkins.txt").stat().st_size
    assert result.sources["checkins"].offset == checkins_size
    assert result.sources["rejects"].offset == (tmp_path / "Rejects.txt").stat().st_size
    assert result.sources["acs"].offset == (tmp_path / "ACS Log.txt").stat().st_size


# --- incomplete trailing line: the exact production finding -----------------


def test_incomplete_trailing_line_is_not_seeded_past(tmp_path, monkeypatch):
    # Mirrors the real AMH finding: current_size=8230605, safe offset
    # 8230599 (6 trailing unconsumed bytes) -- Tech Logic mid-write.
    complete_part = b"line one\nline two\n"
    incomplete_tail = b"partial-no-newline-yet"
    (tmp_path / "Checkins.txt").write_bytes(complete_part + incomplete_tail)
    (tmp_path / "Rejects.txt").write_bytes(b"reject one\n")
    (tmp_path / "ACS Log.txt").write_bytes(b"acs one\n")
    cfg = _load(tmp_path, monkeypatch)

    result = bootstrap_state.bootstrap_state(cfg)

    # Seeded EXACTLY at the end of the last complete line -- never past
    # the trailing partial bytes, and never at the raw file size.
    assert result.sources["checkins"].offset == len(complete_part)
    full_size = len(complete_part) + len(incomplete_tail)
    assert result.sources["checkins"].offset < full_size


def test_seeded_offset_matches_what_an_ordinary_first_run_would_stop_at(tmp_path, monkeypatch):
    # Cross-check against collector.reader directly, not just this
    # module's own math -- proves bootstrap uses the SAME boundary logic
    # collector.run already relies on, not a reimplementation.
    from collector import reader

    (tmp_path / "Checkins.txt").write_bytes(b"line one\nline two\npartial-tail")
    (tmp_path / "Rejects.txt").write_bytes(b"reject one\n")
    (tmp_path / "ACS Log.txt").write_bytes(b"acs one\n")
    cfg = _load(tmp_path, monkeypatch)

    expected = reader.read_new_lines(str(tmp_path / "Checkins.txt"), None)
    result = bootstrap_state.bootstrap_state(cfg)

    assert result.sources["checkins"].offset == expected.cursor.offset


# --- missing source: fail closed, no partial seed ----------------------------


def test_missing_source_raises_and_writes_no_state_file(tmp_path, monkeypatch):
    # Checkins.txt deliberately never created.
    (tmp_path / "Rejects.txt").write_bytes(b"reject one\n")
    (tmp_path / "ACS Log.txt").write_bytes(b"acs one\n")
    cfg = _load(tmp_path, monkeypatch)

    with pytest.raises(bootstrap_state.BootstrapError, match="checkins"):
        bootstrap_state.bootstrap_state(cfg)

    assert not cfg.state_path.exists()


# --- existing state: ALWAYS refuse, no override of any kind -----------------


def test_refuses_to_overwrite_existing_state(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_bytes(b"line one\n")
    (tmp_path / "Rejects.txt").write_bytes(b"reject one\n")
    (tmp_path / "ACS Log.txt").write_bytes(b"acs one\n")
    cfg = _load(tmp_path, monkeypatch)

    existing = state.with_source(state.empty_state(), "checkins", state.SourceState(identity=None, offset=999))
    state.save_state(cfg.state_path, existing)
    existing_bytes = cfg.state_path.read_bytes()

    with pytest.raises(bootstrap_state.ExistingStateError):
        bootstrap_state.bootstrap_state(cfg)

    # Untouched -- byte-for-byte identical to the pre-existing (real)
    # cursor, not overwritten in any way.
    assert cfg.state_path.read_bytes() == existing_bytes
    reloaded = state.load_state(cfg.state_path)
    assert reloaded.sources["checkins"].offset == 999


def test_bootstrap_state_has_no_force_parameter():
    # Structural guarantee, not just a behavioral test: there is no way
    # to call this function and bypass the existing-state refusal --
    # the parameter simply does not exist.
    import inspect

    signature = inspect.signature(bootstrap_state.bootstrap_state)
    assert "force" not in signature.parameters
    assert list(signature.parameters) == ["cfg"]


def test_existing_state_error_message_explains_the_risk_and_gives_no_override(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_bytes(b"line one\n")
    (tmp_path / "Rejects.txt").write_bytes(b"reject one\n")
    (tmp_path / "ACS Log.txt").write_bytes(b"acs one\n")
    cfg = _load(tmp_path, monkeypatch)
    state.save_state(cfg.state_path, state.empty_state())

    with pytest.raises(bootstrap_state.ExistingStateError) as exc_info:
        bootstrap_state.bootstrap_state(cfg)

    message = str(exc_info.value)
    assert "already been initialized" in message
    assert "skip" in message.lower()
    assert "no override" in message.lower()
    assert "investigate" in message.lower()


# --- all three configured source names ---------------------------------------


def test_seeds_all_three_configured_sources(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_bytes(b"checkins line\n")
    (tmp_path / "Rejects.txt").write_bytes(b"rejects line\n")
    (tmp_path / "ACS Log.txt").write_bytes(b"acs line\n")
    cfg = _load(tmp_path, monkeypatch)

    result = bootstrap_state.bootstrap_state(cfg)

    assert set(result.sources.keys()) == {"checkins", "rejects", "acs"}
    for name in ("checkins", "rejects", "acs"):
        assert result.sources[name].identity is not None
        assert result.sources[name].offset > 0


# --- never touches source files, never parses/uploads ------------------------


def test_never_modifies_source_files(tmp_path, monkeypatch):
    checkins_content = b"line one\nline two\npartial-tail"
    (tmp_path / "Checkins.txt").write_bytes(checkins_content)
    (tmp_path / "Rejects.txt").write_bytes(b"reject one\n")
    (tmp_path / "ACS Log.txt").write_bytes(b"acs one\n")
    cfg = _load(tmp_path, monkeypatch)

    bootstrap_state.bootstrap_state(cfg)

    assert (tmp_path / "Checkins.txt").read_bytes() == checkins_content


def test_module_never_imports_uploader_or_parsers():
    # Structural guarantee, not just a behavioral test: this tool cannot
    # make a network call or construct an upload record, because it
    # never imports the modules that would let it.
    import collector.bootstrap_state as mod

    assert not hasattr(mod, "uploader")
    assert not hasattr(mod, "parsers")


# --- persists through collector.state, not hand-written JSON -----------------


def test_result_is_loadable_via_collector_state_load_state(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_bytes(b"line one\n")
    (tmp_path / "Rejects.txt").write_bytes(b"reject one\n")
    (tmp_path / "ACS Log.txt").write_bytes(b"acs one\n")
    cfg = _load(tmp_path, monkeypatch)

    written = bootstrap_state.bootstrap_state(cfg)
    reloaded = state.load_state(cfg.state_path)

    assert reloaded == written


# --- CLI main() ---------------------------------------------------------------


def test_main_returns_0_and_prints_seeded_offsets(tmp_path, monkeypatch, capsys):
    (tmp_path / "Checkins.txt").write_bytes(b"line one\n")
    (tmp_path / "Rejects.txt").write_bytes(b"reject one\n")
    (tmp_path / "ACS Log.txt").write_bytes(b"acs one\n")
    store_api_token(monkeypatch, "test-token")
    config_path = _write_config(tmp_path)

    exit_code = bootstrap_state.main(["--config", str(config_path)])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "checkins" in out
    assert "rejects" in out
    assert "acs" in out


def test_main_returns_2_for_invalid_config_path():
    exit_code = bootstrap_state.main(["--config", "does-not-exist.json"])
    assert exit_code == 2


def test_main_returns_2_when_state_already_exists(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_bytes(b"line one\n")
    (tmp_path / "Rejects.txt").write_bytes(b"reject one\n")
    (tmp_path / "ACS Log.txt").write_bytes(b"acs one\n")
    store_api_token(monkeypatch, "test-token")
    config_path = _write_config(tmp_path)
    cfg = load_config(config_path)
    state.save_state(cfg.state_path, state.empty_state())
    existing_bytes = cfg.state_path.read_bytes()

    exit_code = bootstrap_state.main(["--config", str(config_path)])

    assert exit_code == 2
    # Byte-for-byte unchanged -- the CLI path refuses just as completely
    # as the library function itself.
    assert cfg.state_path.read_bytes() == existing_bytes


def test_main_has_no_force_flag(tmp_path, monkeypatch):
    (tmp_path / "Checkins.txt").write_bytes(b"line one\n")
    (tmp_path / "Rejects.txt").write_bytes(b"reject one\n")
    (tmp_path / "ACS Log.txt").write_bytes(b"acs one\n")
    store_api_token(monkeypatch, "test-token")
    config_path = _write_config(tmp_path)

    with pytest.raises(SystemExit):
        bootstrap_state.main(["--config", str(config_path), "--force"])


def test_main_returns_1_for_missing_source(tmp_path, monkeypatch):
    (tmp_path / "Rejects.txt").write_bytes(b"reject one\n")
    (tmp_path / "ACS Log.txt").write_bytes(b"acs one\n")
    store_api_token(monkeypatch, "test-token")
    config_path = _write_config(tmp_path)

    exit_code = bootstrap_state.main(["--config", str(config_path)])

    assert exit_code == 1



# =============================================================================
# Contract v2 (`contract_mode: "v2"`): the same command seeds the v2 cursor.
# Everything above is v1 and must keep passing untouched.
# =============================================================================

import requests
from collector_v2_support import (
    BARCODE_CI,
    TOKEN,
    FakeStore,
    ScriptedSession,
    checkin_line,
    full_corpus,
    load_v2,
    minutes_ago,
    run_logger,
    write_lines,
    write_rules,
)
from collector_v2_support import write_config as write_v2_config

from collector import reader, v2_reader, v2_run
from collector.v2_events import Cursor

V2_SOURCES = ("acs", "checkins", "rejects")


def _v2_install(tmp_path, monkeypatch, *, contents=None, contract_mode="v2", **config_kwargs):
    """A collector root with a v2 config and the three Tech Logic files (`contents`: name -> bytes, None = absent)."""
    store_api_token(monkeypatch, TOKEN)
    root = tmp_path / "root"
    config_path = write_v2_config(root, contract_mode=contract_mode, **config_kwargs)
    (root / "tech").mkdir(parents=True, exist_ok=True)
    for name in V2_SOURCES:
        data = (contents or {}).get(name, b"")
        if data is not None:
            (root / "tech" / f"{name}.txt").write_bytes(data)
    return root, config_path


def _edit_config(config_path, change):
    document = json.loads(config_path.read_text(encoding="utf-8"))
    change(document)
    config_path.write_text(json.dumps(document), encoding="utf-8")


def _v1_state(root):
    return root / "state" / "state.json"


def _v2_state(root):
    return root / "data" / "v2" / "state_v2.json"


def _collector_output_files(root):
    """Every file under the root except the inputs (Tech Logic files, the config)."""
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "tech" not in path.relative_to(root).parts and path.name != "config.json"
    }


# --- contract selection -----------------------------------------------------


@pytest.mark.parametrize("contract_mode", [None, "v1"])
def test_absent_or_v1_contract_mode_keeps_the_v1_bootstrap_even_with_a_v2_section(tmp_path, monkeypatch, contract_mode):
    root, config_path = _v2_install(tmp_path, monkeypatch, contract_mode=contract_mode, contents={"acs": b"a\nb\n"})

    assert bootstrap_state.main(["--config", str(config_path)]) == 0

    assert state.load_state(_v1_state(root)).sources.keys() == set(V2_SOURCES)
    assert state.load_state(_v1_state(root)).sources["acs"].offset == 4
    assert not _v2_state(root).exists()


@pytest.mark.parametrize("contract_mode", [None, "v1"])
def test_a_v1_bootstrap_still_refuses_an_existing_state_with_exit_2(tmp_path, monkeypatch, contract_mode):
    _root, config_path = _v2_install(tmp_path, monkeypatch, contract_mode=contract_mode)
    assert bootstrap_state.main(["--config", str(config_path)]) == 0

    assert bootstrap_state.main(["--config", str(config_path)]) == 2  # v1's own code, unchanged: never 3


def test_an_unsupported_contract_mode_is_a_configuration_failure(tmp_path, monkeypatch, capsys):
    root, config_path = _v2_install(tmp_path, monkeypatch, contract_mode="v3")

    assert bootstrap_state.main(["--config", str(config_path)]) == 2

    assert "contract_mode" in capsys.readouterr().err
    assert _collector_output_files(root) == set()


# --- fresh v2 bootstrap -----------------------------------------------------


def test_v2_seeds_three_empty_files_at_offset_zero_with_their_identity(tmp_path, monkeypatch, capsys):
    root, config_path = _v2_install(tmp_path, monkeypatch)

    assert bootstrap_state.main(["--config", str(config_path)]) == 0

    seeded = state.load_state(_v2_state(root))
    assert seeded.sources.keys() == set(V2_SOURCES)
    for name in V2_SOURCES:
        assert seeded.sources[name].offset == 0
        assert seeded.sources[name].identity == reader.identify(str(root / "tech" / f"{name}.txt")).token
    assert "Seeded v2 state at" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("content", "expected_offset"),
    [
        (b"one\ntwo\n", 8),                 # LF, complete: end of file
        (b"one\r\ntwo\r\n", 10),            # CRLF, complete: end of file
        (b"one\ntwo\nthr", 8),              # incomplete final record: after the last complete newline
        (b"one\r\ntwo\r\nthr", 10),
        (b"no newline at all", 0),          # nothing complete yet
        (b"", 0),
    ],
    ids=["lf", "crlf", "lf-partial", "crlf-partial", "no-newline", "empty"],
)
def test_v2_seeds_at_the_line_safe_end_and_exactly_where_the_v2_reader_would_stop(tmp_path, monkeypatch, content, expected_offset):
    root, config_path = _v2_install(tmp_path, monkeypatch, contents={name: content for name in V2_SOURCES})

    assert bootstrap_state.main(["--config", str(config_path)]) == 0

    seeded = state.load_state(_v2_state(root))
    for name in V2_SOURCES:
        path = str(root / "tech" / f"{name}.txt")
        entry = seeded.sources[name]
        assert entry.offset == expected_offset

        # Parity: a v2 run that had read the whole file from byte 0 would hold exactly this cursor ...
        first_read = v2_reader.read_chunk(path, None, 1_000_000)
        assert first_read.cursor == Cursor(entry.identity, entry.offset)
        # ... and a v2 run resuming from the seeded cursor has nothing to read and sees no rotation or truncation.
        resumed = v2_reader.read_chunk(path, Cursor(entry.identity, entry.offset), 1_000_000)
        assert (resumed.lines, resumed.rotated, resumed.truncated, resumed.existed) == ([], False, False, True)
        assert resumed.cursor == Cursor(entry.identity, entry.offset)


def test_v2_writes_only_the_cursor_file_and_never_modifies_a_source(tmp_path, monkeypatch):
    content = b"one\ntwo\npartial"
    root, config_path = _v2_install(tmp_path, monkeypatch, contents={name: content for name in V2_SOURCES})

    assert bootstrap_state.main(["--config", str(config_path)]) == 0

    # No v1 state, patron cache, quarantine, status file, rules, secret or log.
    assert _collector_output_files(root) == {"data/v2/state_v2.json"}
    for name in V2_SOURCES:
        assert (root / "tech" / f"{name}.txt").read_bytes() == content


def test_v2_seeds_only_the_configured_sources(tmp_path, monkeypatch):
    root, config_path = _v2_install(tmp_path, monkeypatch, sources=False)  # the support config with ONE source: acs

    assert bootstrap_state.main(["--config", str(config_path)]) == 0

    assert state.load_state(_v2_state(root)).sources.keys() == {"acs"}


def test_v2_opens_no_network_session_and_parses_or_uploads_nothing(tmp_path, monkeypatch):
    from collector import uploader, v2_transform, v2_uploader

    def forbidden(*_args, **_kwargs):
        raise AssertionError("the v2 bootstrap must not do this")

    monkeypatch.setattr(requests.Session, "send", forbidden)
    monkeypatch.setattr(uploader, "build_session", forbidden)
    monkeypatch.setattr(v2_uploader, "post_batch", forbidden)
    monkeypatch.setattr(v2_uploader, "post_status", forbidden)
    monkeypatch.setattr(v2_transform, "transform_lines", forbidden)
    monkeypatch.setattr(v2_transform, "read_next_chunk", forbidden)
    monkeypatch.setattr(v2_run, "build_runtime", forbidden)
    root, config_path = _v2_install(tmp_path, monkeypatch, contents={name: b"one\ntwo\n" for name in V2_SOURCES})

    assert bootstrap_state.main(["--config", str(config_path)]) == 0
    assert _v2_state(root).exists()


# --- refusals: exit 3, nothing changed --------------------------------------


def test_v2_refuses_an_existing_v2_state_with_exit_3_and_leaves_it_untouched(tmp_path, monkeypatch, capsys):
    root, config_path = _v2_install(tmp_path, monkeypatch, contents={name: b"one\ntwo\n" for name in V2_SOURCES})
    _v2_state(root).parent.mkdir(parents=True)
    _v2_state(root).write_bytes(b'{"schema_version": 1, "sources": {"acs": {"identity": [1, 2], "offset": 3}}}')
    before = _v2_state(root).read_bytes()

    assert bootstrap_state.main(["--config", str(config_path)]) == bootstrap_state.EXIT_REFUSED == 3

    assert _v2_state(root).read_bytes() == before
    assert "reason=v2_state_exists" in capsys.readouterr().err
    assert _collector_output_files(root) == {"data/v2/state_v2.json"}


def test_v2_refuses_even_a_corrupt_or_empty_v2_state_file(tmp_path, monkeypatch):
    root, config_path = _v2_install(tmp_path, monkeypatch)
    _v2_state(root).parent.mkdir(parents=True)
    _v2_state(root).write_bytes(b"")

    assert bootstrap_state.main(["--config", str(config_path)]) == 3
    assert _v2_state(root).read_bytes() == b""


def test_v2_refuses_when_a_legacy_v1_state_exists_and_no_v2_state_does(tmp_path, monkeypatch, capsys):
    """A machine that has run v1 may be a v1-to-v2 conversion: seeding at end-of-file would skip everything between the
    v1 cursor and the end of each file, so the tool refuses rather than guess."""
    root, config_path = _v2_install(tmp_path, monkeypatch, contents={name: b"one\ntwo\n" for name in V2_SOURCES})
    _v1_state(root).parent.mkdir(parents=True)
    _v1_state(root).write_bytes(b'{"schema_version": 1, "sources": {"acs": {"identity": [1, 2], "offset": 4}}}')
    before = _v1_state(root).read_bytes()

    assert bootstrap_state.main(["--config", str(config_path)]) == 3

    err = capsys.readouterr().err
    assert "reason=legacy_state_exists" in err
    assert "will not infer whether this is a fresh install or a v1-to-v2 conversion" in err
    assert _v1_state(root).read_bytes() == before
    assert not _v2_state(root).exists()
    assert _collector_output_files(root) == {"state/state.json"}


def test_v2_refusals_have_no_override_flag(tmp_path, monkeypatch):
    _root, config_path = _v2_install(tmp_path, monkeypatch)
    assert bootstrap_state.main(["--config", str(config_path)]) == 0

    with pytest.raises(SystemExit):
        bootstrap_state.main(["--config", str(config_path), "--force"])
    assert "force" not in bootstrap_state.bootstrap_v2_state.__code__.co_varnames


def test_v2_can_be_retried_after_a_failure_that_wrote_no_state(tmp_path, monkeypatch):
    root, config_path = _v2_install(tmp_path, monkeypatch, contents={"rejects": None})
    assert bootstrap_state.main(["--config", str(config_path)]) == 1
    assert not _v2_state(root).exists()

    (root / "tech" / "rejects.txt").write_bytes(b"")
    assert bootstrap_state.main(["--config", str(config_path)]) == 0
    assert bootstrap_state.main(["--config", str(config_path)]) == 3  # and now it is initialized


# --- failures: exit 1 or 2, no state file -----------------------------------


def test_v2_missing_source_fails_with_exit_1_and_writes_no_state(tmp_path, monkeypatch, capsys):
    root, config_path = _v2_install(tmp_path, monkeypatch, contents={"checkins": None})

    assert bootstrap_state.main(["--config", str(config_path)]) == 1

    assert "'checkins' not found or unreadable" in capsys.readouterr().err
    assert _collector_output_files(root) == set()


def test_v2_unreadable_source_fails_with_exit_1_and_writes_no_state(tmp_path, monkeypatch):
    root, config_path = _v2_install(tmp_path, monkeypatch, contents={name: b"one\n" for name in V2_SOURCES})
    real_read = reader._read_complete_lines

    def denied(path, start_offset):
        if path.endswith("rejects.txt"):
            raise PermissionError(13, "Access is denied")
        return real_read(path, start_offset)

    monkeypatch.setattr(reader, "_read_complete_lines", denied)

    assert bootstrap_state.main(["--config", str(config_path)]) == 1
    assert _collector_output_files(root) == set()


def test_v2_unknown_source_name_is_a_configuration_failure(tmp_path, monkeypatch, capsys):
    root, config_path = _v2_install(tmp_path, monkeypatch)
    (root / "tech" / "holds.txt").write_bytes(b"")
    _edit_config(config_path, lambda d: d["sources"].append({"name": "holds", "path": str(root / "tech" / "holds.txt")}))

    assert bootstrap_state.main(["--config", str(config_path)]) == 2

    assert "holds" in capsys.readouterr().err
    assert _collector_output_files(root) == set()


@pytest.mark.parametrize(
    "damage",
    [
        lambda d: d.pop("v2"),
        lambda d: d.__setitem__("v2", "not an object"),
        lambda d: d["v2"].__setitem__("key_id", "NOT-A-KEY-ID"),
        lambda d: d["v2"].pop("timezone"),
        lambda d: d["v2"].__setitem__("timezone", "Mars/Olympus_Mons"),
        lambda d: d["v2"].__setitem__("chunk_lines", 0),
    ],
    ids=["no-section", "section-not-object", "bad-key-id", "no-timezone", "unknown-timezone", "bad-limit"],
)
def test_v2_malformed_v2_config_is_a_configuration_failure(tmp_path, monkeypatch, capsys, damage):
    root, config_path = _v2_install(tmp_path, monkeypatch)
    _edit_config(config_path, damage)

    assert bootstrap_state.main(["--config", str(config_path)]) == 2

    err = capsys.readouterr().err
    assert err.startswith("Configuration error:")
    assert "NOT-A-KEY-ID" not in err and "Mars" not in err  # names the setting, never the value
    assert _collector_output_files(root) == set()


def test_v2_fails_when_a_source_is_replaced_while_it_is_being_read(tmp_path, monkeypatch, capsys):
    root, config_path = _v2_install(tmp_path, monkeypatch, contents={name: b"one\ntwo\n" for name in V2_SOURCES})
    real_identify = reader.identify
    calls: dict[str, int] = {}

    def identify(path):
        calls[path] = calls.get(path, 0) + 1
        # The reader identifies a source once; bootstrap's own second look sees a different file.
        if path.endswith("checkins.txt") and calls[path] > 1:
            return reader.FileIdentity(token=(999_999, 999_999))
        return real_identify(path)

    monkeypatch.setattr(reader, "identify", identify)

    assert bootstrap_state.main(["--config", str(config_path)]) == 1

    assert "'checkins' was replaced or truncated" in capsys.readouterr().err
    assert _collector_output_files(root) == set()


def test_v2_fails_when_a_source_disappears_while_it_is_being_read(tmp_path, monkeypatch):
    root, config_path = _v2_install(tmp_path, monkeypatch, contents={name: b"one\ntwo\n" for name in V2_SOURCES})
    real_read = reader.read_new_lines

    def read_then_delete(path, cursor):
        result = real_read(path, cursor)
        if path.endswith("acs.txt"):
            (root / "tech" / "acs.txt").unlink()
        return result

    monkeypatch.setattr(reader, "read_new_lines", read_then_delete)

    assert bootstrap_state.main(["--config", str(config_path)]) == 1
    assert _collector_output_files(root) == set()


def test_v2_fails_when_a_source_is_truncated_while_it_is_being_read(tmp_path, monkeypatch, capsys):
    root, config_path = _v2_install(tmp_path, monkeypatch, contents={name: b"one\ntwo\n" for name in V2_SOURCES})
    real_read = reader.read_new_lines

    def read_then_truncate(path, cursor):
        result = real_read(path, cursor)
        if path.endswith("rejects.txt"):
            with open(path, "r+b") as handle:  # same file (same identity), now shorter than the chosen offset
                handle.truncate(2)
        return result

    monkeypatch.setattr(reader, "read_new_lines", read_then_truncate)

    assert bootstrap_state.main(["--config", str(config_path)]) == 1

    assert "'rejects' was replaced or truncated" in capsys.readouterr().err
    assert _collector_output_files(root) == set()


def test_v2_a_source_that_only_grew_while_it_was_read_is_not_a_failure(tmp_path, monkeypatch):
    root, config_path = _v2_install(tmp_path, monkeypatch, contents={name: b"one\ntwo\n" for name in V2_SOURCES})
    real_read = reader.read_new_lines

    def read_then_append(path, cursor):
        result = real_read(path, cursor)
        with open(path, "ab") as handle:
            handle.write(b"three\n")
        return result

    monkeypatch.setattr(reader, "read_new_lines", read_then_append)

    assert bootstrap_state.main(["--config", str(config_path)]) == 0
    # Seeded BEFORE the appended record, so the first run reads it rather than skipping it.
    assert state.load_state(_v2_state(root)).sources["acs"].offset == 8


def test_v2_atomic_write_failure_leaves_no_state_file_and_no_temp_file(tmp_path, monkeypatch, capsys):
    root, config_path = _v2_install(tmp_path, monkeypatch, contents={name: b"one\n" for name in V2_SOURCES})

    def failing_replace(_src, _dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(state.os, "replace", failing_replace)

    assert bootstrap_state.main(["--config", str(config_path)]) == 1

    assert "could not write the v2 state file" in capsys.readouterr().err
    assert not _v2_state(root).exists()
    assert _collector_output_files(root) == set()  # an empty data\v2 folder may remain; no file does


def test_v2_writes_the_state_exactly_once(tmp_path, monkeypatch):
    root, config_path = _v2_install(tmp_path, monkeypatch, contents={name: b"one\n" for name in V2_SOURCES})
    writes = []
    real_save = state.save_state
    monkeypatch.setattr(state, "save_state", lambda path, doc: (writes.append(path), real_save(path, doc))[1])

    assert bootstrap_state.main(["--config", str(config_path)]) == 0

    assert writes == [_v2_state(root)]


# --- the point of it all: the first v2 run after a fresh bootstrap ----------


def _install_with_history(tmp_path, monkeypatch):
    """A v2 install whose Tech Logic files already hold history, as a real machine's do before the Collector arrives."""
    store_api_token(monkeypatch, TOKEN)
    root = tmp_path / "root"
    config_path = write_v2_config(root)
    for name, lines in full_corpus(minutes_ago(90)).items():
        write_lines(root / "tech" / f"{name}.txt", lines)
    cfg, v2 = load_config(config_path), load_v2(config_path)
    write_rules(v2)

    def run(session):
        return v2_run.run_once_v2(cfg, v2, session=session, logger=run_logger(cfg.log_path), store=FakeStore(),
                                  sleep=lambda _seconds: None)

    return root, config_path, run


def test_a_v2_run_after_a_fresh_bootstrap_sends_no_historical_event_and_then_only_new_ones(tmp_path, monkeypatch):
    root, config_path, run = _install_with_history(tmp_path, monkeypatch)

    assert bootstrap_state.main(["--config", str(config_path)]) == 0
    first = ScriptedSession()
    assert run(first) == 0
    assert first.uploads() == []
    assert len(first.statuses()) == 1

    write_lines(root / "tech" / "checkins.txt", [checkin_line(minutes_ago(1), BARCODE_CI + "-NEW")], append=True)
    second = ScriptedSession()
    assert run(second) == 0
    assert [len(body.get("checkins", [])) for body in second.uploads()] == [1]


def test_without_a_bootstrap_the_first_v2_run_does_upload_the_history(tmp_path, monkeypatch):
    """The problem itself, pinned: with no v2 state every source is read from byte 0."""
    _root, _config_path, run = _install_with_history(tmp_path, monkeypatch)
    session = ScriptedSession()

    assert run(session) == 0
    assert session.uploads() != []
