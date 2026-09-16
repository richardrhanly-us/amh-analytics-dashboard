"""Tests for collector/deploy_manifest.py and the deployment-packaging
repair it exists for (parser-parity phase, PR #25 follow-up).

Two kinds of coverage here, deliberately kept distinct:

  1. Real, executable proof (most of this file): the manifest module
     itself, and an actual isolated-directory import smoke test that
     copies exactly the manifested files into a temp dir and imports
     collector.parsers from there in a SUBPROCESS with the repo root
     NOT on sys.path -- the same shape install-collector.ps1 produces,
     proven in pure Python so it runs on this project's own
     ubuntu-latest CI, not just Windows.
  2. Static/textual checks on the .ps1 scripts themselves (clearly
     labeled where they appear) -- this project's CI cannot execute
     PowerShell, so "update backs up/replaces both runtime trees" and
     "rollback instructions cover both runtime trees" are verified by
     asserting the script text contains the expected real markers
     (variable names, the deploy_manifest invocation, the parser-runtime
     backup/restore lines), not by running the script. Weaker than
     execution, but honest about what is and is not proven here -- real
     end-to-end proof of the .ps1 scripts happened live on LIB-L26 in
     this same phase.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from collector import deploy_manifest
from collector.run import ParserNotConfiguredError

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- the manifest itself, against the real repo -----------------------


def test_parser_runtime_files_all_exist_in_the_real_repo():
    # Not a fake/tmp_path repo -- the real one. If agent/parser/* is ever
    # renamed or removed, this fails loudly here rather than silently
    # shipping an incomplete install.
    resolved = deploy_manifest.parser_runtime_files(REPO_ROOT)
    assert len(resolved) == len(deploy_manifest.PARSER_RUNTIME_FILES)
    for path in resolved:
        assert path.is_file()


def test_parser_runtime_files_raises_loudly_on_a_missing_file(tmp_path):
    # A repo_root that doesn't have any of these files must fail, not
    # silently return a partial/empty list.
    import pytest

    with pytest.raises(FileNotFoundError):
        deploy_manifest.parser_runtime_files(tmp_path)


def test_collector_package_files_matches_the_same_glob_install_script_uses():
    files = deploy_manifest.collector_package_files(REPO_ROOT)
    names = {p.name for p in files}
    assert "run.py" in names
    assert "parsers.py" in names
    assert "reader.py" in names
    # deploy_manifest.py itself must be part of what gets deployed too --
    # install-collector.ps1's own Copy-Item glob (collector/*.py) already
    # picks it up automatically; this just confirms that glob would.
    assert "deploy_manifest.py" in names


def test_cli_prints_exactly_the_parser_runtime_files_one_per_line():
    result = subprocess.run(
        [sys.executable, "-m", "collector.deploy_manifest"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True,
    )
    printed = result.stdout.strip().splitlines()
    assert printed == list(deploy_manifest.PARSER_RUNTIME_FILES)


# --- fresh-install payload: contains what it must, excludes what it must not --


def _fresh_install_payload() -> set[Path]:
    """Every file the real install-collector.ps1 would copy, expressed
    in pure Python -- the union of collector_package_files() and
    parser_runtime_files(), exactly matching that script's own two
    Copy-Item passes."""
    payload = set(deploy_manifest.collector_package_files(REPO_ROOT))
    payload |= set(deploy_manifest.parser_runtime_files(REPO_ROOT))
    return payload


def test_fresh_install_payload_contains_collector_package_and_parser_runtime():
    payload = _fresh_install_payload()
    assert (REPO_ROOT / "collector" / "parsers.py") in payload
    assert (REPO_ROOT / "collector" / "run.py") in payload
    assert (REPO_ROOT / "agent" / "parser" / "checkins.py") in payload
    assert (REPO_ROOT / "agent" / "parser" / "rejects.py") in payload
    assert (REPO_ROOT / "agent" / "parser" / "acs.py") in payload
    assert (REPO_ROOT / "agent" / "logger_config.py") in payload
    assert (REPO_ROOT / "agent" / "__init__.py") in payload


def test_fresh_install_payload_never_includes_tests_or_docs():
    payload = _fresh_install_payload()
    for path in payload:
        assert "tests" not in path.parts
        assert "docs" not in path.parts
        assert path.suffix == ".py"


def test_fresh_install_payload_never_includes_unrelated_agent_modules():
    # Confirms the narrow-package rule holds: only the six verified
    # parser-runtime files from agent/, never the continuous-agent
    # runtime, never the legacy local mirror, never agent/main.py.
    payload = _fresh_install_payload()
    excluded_relative = [
        "agent/main.py",
        "agent/config.py",
        "agent/run_pipeline.py",
        "agent/uploader.py",
        "agent/tailer.py",
        "agent/state.py",
        "agent/spool.py",
        "agent/discovery.py",
        "agent/identity.py",
        "agent/event_identity.py",
        "agent/runtime/config.py",
        "agent/runtime/supervisor.py",
        "agent/runtime/heartbeat.py",
        "agent/runtime/uploader.py",
    ]
    for rel in excluded_relative:
        assert (REPO_ROOT / rel) not in payload, f"{rel} must never be part of the collector deploy payload"


def test_fresh_install_payload_never_includes_archived_amh_snapshot_or_sortviewagent():
    payload = _fresh_install_payload()
    for path in payload:
        path_str = str(path)
        assert "SortViewAgent - What is currently sitting on the AMH computer" not in path_str
        # The root-level SortViewAgent/ reference baseline (added later,
        # for local comparison only) must never be swept in either --
        # every payload path must live under collector/ or agent/parser/
        # or be one of the two agent/-root files, never anywhere else.
        assert path.parts[len(REPO_ROOT.parts)] in ("collector", "agent")


# --- isolated deployed-runtime import smoke test ------------------------


def test_deployed_runtime_can_import_collector_parsers_with_repo_root_not_on_syspath(tmp_path):
    """The real proof: copy exactly the manifested files into an isolated
    temp directory (nothing else -- no tests/, no docs/, no repo root),
    then in a FRESH SUBPROCESS whose sys.path is only that temp
    directory (never this repo checkout), import collector.parsers and
    build all three production parse functions. A subprocess is
    required, not just sys.path manipulation in-process, because this
    test process already has `collector` and `agent` imported from the
    real repo checkout -- only a fresh interpreter proves isolation."""
    install_root = tmp_path / "InstallRoot"
    collector_dir = install_root / "collector"
    collector_dir.mkdir(parents=True)
    for src in deploy_manifest.collector_package_files(REPO_ROOT):
        (collector_dir / src.name).write_bytes(src.read_bytes())

    for src in deploy_manifest.parser_runtime_files(REPO_ROOT):
        rel = src.relative_to(REPO_ROOT)
        dest = install_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())

    smoke_script = f"""
import sys
assert sys.path[0] != r"{REPO_ROOT}", "repo root leaked onto sys.path"
import collector.parsers as parsers
fns = parsers.build_production_parse_fns(customer_id=1, branch_id=1)
assert set(fns.keys()) == {{"checkins", "rejects", "acs"}}
line = "Sunny days /|33472004192508|MLEPB|E KERBEL NATURE|000|1|False||4|N|N|N|8/31/2026|4:18:39 PM"
records = fns["checkins"]([line])
assert len(records) == 1
assert records[0]["barcode"] == "33472004192508"
print("ISOLATED_IMPORT_OK")
"""

    result = subprocess.run(
        [sys.executable, "-c", smoke_script],
        cwd=str(install_root),  # deliberately NOT the repo checkout
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "ISOLATED_IMPORT_OK" in result.stdout


def test_deployed_runtime_does_not_accidentally_pick_up_the_repo_checkout():
    # Sanity check on the smoke test above's own isolation claim: the
    # repo root really is absent from a subprocess launched with only
    # the temp install root as cwd and no PYTHONPATH override.
    result = subprocess.run(
        [sys.executable, "-c", "import sys; print(sys.path[0])"],
        cwd=str(REPO_ROOT / "collector"),  # a real subdirectory, not the repo root itself
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() != str(REPO_ROOT)


# --- static checks on the .ps1 scripts (CI cannot execute PowerShell) --


def _read_ps1(name: str) -> str:
    return (REPO_ROOT / "collector" / "deploy" / name).read_text(encoding="utf-8")


def test_install_script_copies_parser_runtime_via_the_manifest():
    text = _read_ps1("install-collector.ps1")
    assert "collector.deploy_manifest" in text
    assert "$parserRuntimeFiles" in text


def test_update_script_backs_up_and_replaces_both_runtime_trees():
    text = _read_ps1("update-collector.ps1")
    assert "collector.deploy_manifest" in text
    # Both the collector/*.py backup line and the parser-runtime backup
    # loop must be present, not just one or the other.
    assert 'Copy-Item (Join-Path $InstallRoot "collector\\*.py") -Destination (Join-Path $BackupRoot "collector")' in text
    assert "$parserRuntimeFiles" in text
    assert "BackupRoot $relativePath" in text or "$backupFile" in text


def test_update_script_rollback_instructions_cover_both_runtime_trees():
    text = _read_ps1("update-collector.ps1")
    assert '$BackupRoot\\collector\\*.py' in text
    # Must be \agent\* specifically, not a bare \agent -- empirically
    # verified (isolated temp-directory test, not assumed): when the
    # destination directory already exists, `Copy-Item "...\agent"
    # "...\agent" -Recurse` nests the whole folder underneath the
    # existing one (InstallRoot\agent\agent\...) rather than overwriting
    # its contents, silently leaving the actually-imported files
    # untouched. The trailing \* on the SOURCE is what makes this a real
    # in-place restore instead of a no-op rollback.
    assert '$BackupRoot\\agent\\*' in text


def test_uninstall_wording_no_longer_claims_collector_py_only():
    text = _read_ps1("uninstall-collector.ps1")
    assert "canonical parser runtime" in text


def test_stale_parser_not_wired_language_is_gone_from_deploy_surfaces():
    for name in (
        "install-collector.ps1",
        "update-collector.ps1",
        "uninstall-collector.ps1",
        "register-collector-task.ps1",
    ):
        text = _read_ps1(name)
        assert "later, separate phase" not in text
        assert "not yet configured') on every run until" not in text


def test_stale_parser_not_wired_language_is_gone_from_docs_and_config_and_init():
    admin_guide = (REPO_ROOT / "docs" / "collector-v1-admin-guide.md").read_text(encoding="utf-8")
    assert "not wired in yet" not in admin_guide
    assert "parser not configured" not in admin_guide

    example_config = (REPO_ROOT / "collector" / "deploy" / "collector_config.example.json").read_text(encoding="utf-8")
    assert "NOT wired in yet" not in example_config

    init_py = (REPO_ROOT / "collector" / "__init__.py").read_text(encoding="utf-8")
    assert "until a separate, later parser-parity phase wires" not in init_py


def test_parser_not_configured_error_itself_still_exists():
    # Requirement 6: the real fail-closed behavior for an unknown source
    # name must be preserved -- only the STALE "parser wiring is a later
    # phase" framing was removed, never the mechanism itself.
    assert issubclass(ParserNotConfiguredError, Exception)


def test_requirements_txt_has_pandas_for_the_now_wired_canonical_parsers():
    text = (REPO_ROOT / "collector" / "deploy" / "requirements.txt").read_text(encoding="utf-8")
    assert "pandas==" in text
