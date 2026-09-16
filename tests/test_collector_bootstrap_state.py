"""Tests for collector/bootstrap_state.py -- the production first-run
bootstrap tool (real-AMH deployment finding: a brand-new install with no
seeded state would otherwise replay every historical record in every
configured source file on its first run).
"""

from __future__ import annotations

import json

import pytest

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
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
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
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
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
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
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
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)

    with pytest.raises(SystemExit):
        bootstrap_state.main(["--config", str(config_path), "--force"])


def test_main_returns_1_for_missing_source(tmp_path, monkeypatch):
    (tmp_path / "Rejects.txt").write_bytes(b"reject one\n")
    (tmp_path / "ACS Log.txt").write_bytes(b"acs one\n")
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    config_path = _write_config(tmp_path)

    exit_code = bootstrap_state.main(["--config", str(config_path)])

    assert exit_code == 1

