"""Release readiness (scripts/check_release_readiness.py) and how a release build and CI use it.

collector.__version__ is the single version authority. These tests replace the old "the next build reports X.Y.Z"
literal: nothing here writes the current release number. Instead they prove that everything which REPORTS or PACKAGES
the version derives it from the authority, that no second copy exists, that no current-state test carries a stale copy,
and that a release build refuses to start from a tree that fails any of that.
"""

from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

import pytest

import collector
from collector import build_release
from scripts import check_release_readiness as readiness

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE = "7.8.9"  # the invented release of the tiny repositories below

_CLEAN_ENROLLMENT_TESTS = textwrap.dedent('''
    from controlled_clock import ControlledClock

    CLOCK = ControlledClock()

    def test_expiry():
        CLOCK.advance(timedelta(minutes=31))
''')


_PAGE = (
    "from collector import __version__ as COLLECTOR_VERSION\n"
    'installation_version = st.text_input("Collector version", value=COLLECTOR_VERSION)\n'
)


def _ready_repo(root: Path, version: str = FAKE) -> Path:
    """The smallest tree that satisfies every readiness check."""
    files = {
        "collector/__init__.py": f'__version__ = "{version}"\n',
        "collector/uploader.py": "from . import __version__\n\ndef post(): return __version__\n",
        "collector/support_info.py": "from . import __version__\n",
        "collector/build_release.py": "from . import __version__, deploy_manifest\n",
        "collector/freeze/dispatcher.py": "def main():\n    from collector import __version__\n    print(__version__)\n",
        "collector/freeze/build_frozen.ps1": '$code = "import collector; print(collector.__version__)"\n',
        "super_admin/pages/Provision_Library.py": _PAGE,
        "super_admin/pages/Manage_Libraries.py": _PAGE,
        "tests/controlled_clock.py": "",
        "tests/test_collector_enrollment.py": _CLEAN_ENROLLMENT_TESTS,
    }
    for relative, body in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return root


def _rules(findings) -> list[str]:
    return sorted({f.rule for f in findings})


# --- the real repository ---------------------------------------------------------------------------------------------

def test_the_repository_is_release_ready():
    findings = readiness.check_release_readiness(REPO_ROOT)

    assert findings == [], "\n".join(str(f) for f in findings)


def test_the_collector_version_is_a_well_formed_release_version():
    assert readiness.VERSION_PATTERN.match(collector.__version__), collector.__version__


def test_the_version_is_written_in_exactly_one_place():
    assert readiness.check_version_authority(REPO_ROOT) == []


def test_everything_that_reports_or_packages_the_version_derives_it_from_the_authority():
    assert readiness.check_tooling_derives_from_authority(REPO_ROOT) == []
    for relative in readiness.DERIVING_MODULES:
        assert (REPO_ROOT / relative).is_file(), relative


def test_the_super_admin_forms_that_default_the_collector_version_must_derive_it():
    # Named explicitly (not just "whatever the list holds"), so dropping one from the list is itself a failure.
    for page in ("super_admin/pages/Provision_Library.py", "super_admin/pages/Manage_Libraries.py"):
        assert page in readiness.DERIVING_MODULES, page


def test_the_releaser_supplies_the_exact_release_and_it_is_checked_against_the_authority():
    # The one place an exact release is asserted -- and the number comes from the person releasing, not from a test.
    assert readiness.check_expected_version(REPO_ROOT, collector.__version__) == []
    assert readiness.check_expected_version(REPO_ROOT, None) == []
    (finding,) = readiness.check_expected_version(REPO_ROOT, "0.0.0")
    assert finding.rule == "READY004" and "does not match collector.__version__" in finding.message


def test_the_full_check_reports_a_wrong_requested_release_as_not_ready():
    findings = readiness.check_release_readiness(REPO_ROOT, expected_version="0.0.0")

    assert _rules(findings) == ["READY004"]


# --- the check itself, on tiny repositories -------------------------------------------------------------------------------

def test_a_minimal_consistent_tree_is_ready(tmp_path):
    assert readiness.check_release_readiness(_ready_repo(tmp_path)) == []
    assert readiness.check_release_readiness(_ready_repo(tmp_path / "again"), expected_version=FAKE) == []


@pytest.mark.parametrize("bad", ["__version__ = compute()\n", '__version__ = "not-a-version"\n', "x = 1\n",
                                 '__version__ = "1.0.0"\n__version__ = "1.0.1"\n'])
def test_the_authority_must_be_a_single_well_formed_literal(tmp_path, bad):
    repo = _ready_repo(tmp_path)
    (repo / "collector" / "__init__.py").write_text(bad, encoding="utf-8")

    assert "READY001" in _rules(readiness.check_version_authority(repo))


def test_a_missing_authority_is_reported(tmp_path):
    repo = _ready_repo(tmp_path)
    (repo / "collector" / "__init__.py").unlink()

    assert _rules(readiness.check_version_authority(repo)) == ["READY001"]


@pytest.mark.parametrize(
    ("relative", "body"),
    [
        ("collector/config.py", f'DEFAULT_VERSION = "{FAKE}"\n'),                    # a quoted copy of the release
        ("collector/deploy/notes.txt", f'released as "{FAKE}"\n'),                     # ...in any collector/ file
        ("collector/freeze/sortview_collector.spec", f"version='{FAKE}'\n"),
        ("collector/other.py", 'COLLECTOR_VERSION = "1.0.0"\n'),                       # a stale copy is a copy too
        ("collector/other.py", 'class Meta:\n    VERSION: str = "2.0.1"\n'),
        ("super_admin/pages/Other.py", f'DEFAULT = "{FAKE}"\n'),                     # ...or in production code
        ("src/services/other.py", f"DEFAULT = '{FAKE}'\n"),
        ("main.py", f'DEFAULT = "{FAKE}"\n'),
    ],
)
def test_a_second_copy_of_the_release_version_is_caught(tmp_path, relative, body):
    repo = _ready_repo(tmp_path)
    extra = repo / relative
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text(body, encoding="utf-8")

    findings = readiness.check_version_authority(repo)

    assert _rules(findings) == ["READY002"] and findings[0].path == relative


def test_unrelated_numbers_and_the_authority_itself_are_not_mistaken_for_copies(tmp_path):
    repo = _ready_repo(tmp_path)
    (repo / "collector" / "state.py").write_text('STATE_SCHEMA_VERSION = 1\nPROTOCOL = "1.1"\n', encoding="utf-8")

    assert readiness.check_version_authority(repo) == []


@pytest.mark.parametrize("relative", list(readiness.DERIVING_MODULES))
def test_a_module_that_stops_deriving_the_version_is_caught(tmp_path, relative):
    repo = _ready_repo(tmp_path)
    (repo / relative).write_text('REPORTED = "hardcoded"\n', encoding="utf-8")

    findings = readiness.check_tooling_derives_from_authority(repo)

    assert [(f.path, f.rule) for f in findings] == [(relative, "READY003")]


def test_a_missing_deriving_module_is_reported_not_skipped(tmp_path):
    repo = _ready_repo(tmp_path)
    (repo / "collector" / "uploader.py").unlink()

    assert [f.path for f in readiness.check_tooling_derives_from_authority(repo)] == ["collector/uploader.py"]


@pytest.mark.parametrize(
    ("script", "expected_fragment"),
    [
        ('$ExpectedVersion = "1.2.3"\n# reads collector.__version__ too\n', "literal version"),
        ("Write-Host hello\n", "does not read collector.__version__"),
    ],
)
def test_the_frozen_build_script_must_read_the_authority_and_carry_no_literal(tmp_path, script, expected_fragment):
    repo = _ready_repo(tmp_path)
    (repo / readiness.FROZEN_BUILD_SCRIPT).write_text(script, encoding="utf-8")

    findings = readiness.check_tooling_derives_from_authority(repo)

    assert findings and all(f.rule == "READY003" for f in findings) and expected_fragment in findings[0].message


def test_historical_release_strings_in_production_code_are_not_mistaken_for_copies(tmp_path):
    repo = _ready_repo(tmp_path)
    (repo / "src" / "services").mkdir(parents=True)
    (repo / "src" / "services" / "legacy.py").write_text(
        'FIRST_SHIPPED = "1.0.0"\nSCHEMA_VERSION = "1.0.0"\nagent_version = "0.1.0"\n', encoding="utf-8"
    )

    assert readiness.check_release_readiness(repo) == []


def test_a_production_default_that_hardcodes_a_collector_release_blocks_the_release(tmp_path):
    repo = _ready_repo(tmp_path)
    page = repo / "super_admin" / "pages" / "Provision_Library.py"
    page.write_text(_PAGE + 'installation_version = st.text_input("Collector version", value="1.0.3")\n', encoding="utf-8")

    findings = readiness.check_release_readiness(repo)

    assert [(f.path, f.rule) for f in findings] == [("super_admin/pages/Provision_Library.py", "FRESH006")]


def test_a_page_that_goes_back_to_a_literal_default_and_drops_the_import_is_caught_twice(tmp_path):
    repo = _ready_repo(tmp_path)
    page = repo / "super_admin" / "pages" / "Manage_Libraries.py"
    page.write_text('new_version = st.text_input("Collector version", value="1.0.3")\n', encoding="utf-8")

    findings = readiness.check_release_readiness(repo)

    assert sorted((f.path, f.rule) for f in findings) == [
        ("super_admin/pages/Manage_Libraries.py", "FRESH006"),
        ("super_admin/pages/Manage_Libraries.py", "READY003"),
    ]


def test_the_shared_controlled_clock_must_exist(tmp_path):
    repo = _ready_repo(tmp_path)
    (repo / readiness.CONTROLLED_CLOCK).unlink()

    assert _rules(readiness.check_controlled_clock_exists(repo)) == ["READY005"]


def test_a_stale_release_literal_in_a_current_state_test_blocks_the_release(tmp_path):
    repo = _ready_repo(tmp_path)
    (repo / "tests" / "test_uploader.py").write_text(
        'import collector\n\ndef test_the_next_build_reports_1_0_3():\n    assert collector.__version__ == "1.0.3"\n',
        encoding="utf-8",
    )

    findings = readiness.check_release_readiness(repo)

    assert _rules(findings) == ["FRESH001", "FRESH003"]


def test_an_enrollment_test_that_depends_on_todays_wall_clock_blocks_the_release(tmp_path):
    repo = _ready_repo(tmp_path)
    (repo / "tests" / "test_collector_enrollment.py").write_text(
        _CLEAN_ENROLLMENT_TESTS + "\ndef test_fresh():\n    assert code.expires_at > datetime.now(UTC)\n", encoding="utf-8"
    )

    findings = readiness.check_release_readiness(repo)

    assert _rules(findings) == ["FRESH005"]


def test_an_enrollment_test_module_that_drops_the_controlled_clock_blocks_the_release(tmp_path):
    repo = _ready_repo(tmp_path)
    (repo / "tests" / "test_collector_enrollment.py").write_text("def test_x():\n    pass\n", encoding="utf-8")

    assert _rules(readiness.check_release_readiness(repo)) == ["FRESH005"]


def test_the_command_line_reports_each_check_and_sets_the_exit_code(tmp_path, capsys):
    good = _ready_repo(tmp_path / "good")
    bad = _ready_repo(tmp_path / "bad")
    (bad / "collector" / "config.py").write_text(f'V = "{FAKE}"\n', encoding="utf-8")

    assert readiness.main(["--repo-root", str(good), "--expect-version", FAKE]) == 0
    out = capsys.readouterr().out
    assert out.count("[ok]") == 5 and "Ready: release" in out and FAKE in out

    assert readiness.main(["--repo-root", str(bad)]) == 1
    captured = capsys.readouterr()
    assert "[FAIL] collector.__version__ is the single version authority" in captured.out
    assert "READY002" in captured.out and "Not ready for a release build" in captured.err

    assert readiness.main(["--repo-root", str(good), "--expect-version", "0.0.0"]) == 1
    assert "READY004" in capsys.readouterr().out


# --- a release build runs the check first --------------------------------------------------------------------------------

def _refuse(root, version):
    raise build_release.BuildError("not release-ready: simulated finding")


def test_the_release_build_cli_refuses_a_tree_that_is_not_ready_and_creates_nothing(monkeypatch, tmp_path, capsys):
    built: list[str] = []
    monkeypatch.setattr(build_release, "_require_release_readiness", _refuse)
    monkeypatch.setattr(build_release, "build_release", lambda *a, **k: built.append("source"))
    monkeypatch.setattr(build_release, "build_frozen_release", lambda *a, **k: built.append("frozen"))
    output = tmp_path / "out"

    for extra in ([], ["--frozen-runtime", str(tmp_path)]):
        exit_code = build_release.main(["--output", str(output), "--version", collector.__version__, *extra])

        assert exit_code == 1
        assert "Build failed: not release-ready: simulated finding" in capsys.readouterr().err
    assert built == [] and not output.exists()


def test_the_release_build_cli_passes_the_requested_version_to_the_readiness_check(monkeypatch, tmp_path):
    seen: list[tuple[Path, str]] = []

    def record(root, version):
        seen.append((root, version))
        raise build_release.BuildError("stop after recording")

    monkeypatch.setattr(build_release, "_require_release_readiness", record)

    build_release.main(["--output", str(tmp_path / "out"), "--version", collector.__version__])

    assert seen == [(Path(build_release.__file__).resolve().parent.parent, collector.__version__)]


def test_the_version_assertion_still_fails_first_with_its_own_message(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(build_release, "_require_release_readiness", _refuse)

    assert build_release.main(["--output", str(tmp_path / "out"), "--version", "0.0.0"]) == 1

    err = capsys.readouterr().err
    assert "does not match collector.__version__" in err and "not release-ready" not in err


def test_the_readiness_check_runs_from_the_repository_being_released_and_receives_the_version(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "check_release_readiness.py").write_text(
        "def check_release_readiness(repo_root, *, expected_version=None):\n"
        "    return [f'stub finding for {expected_version} in {repo_root.name}']\n",
        encoding="utf-8",
    )

    with pytest.raises(build_release.BuildError) as excinfo:
        build_release._require_release_readiness(tmp_path, "9.9.9")

    assert "stub finding for 9.9.9" in str(excinfo.value) and "not release-ready" in str(excinfo.value)


def test_a_release_cannot_be_built_without_the_readiness_check(tmp_path):
    with pytest.raises(build_release.BuildError, match="release-readiness check .* is missing"):
        build_release._require_release_readiness(tmp_path, "1.2.3")


def test_the_real_readiness_check_passes_through_the_build_hook_and_leaves_no_module_behind():
    build_release._require_release_readiness(REPO_ROOT, collector.__version__)

    assert "sortview_check_release_readiness" not in sys.modules


def test_the_frozen_build_script_checks_readiness_before_it_builds_anything():
    script = (REPO_ROOT / "collector" / "freeze" / "build_frozen.ps1").read_text(encoding="utf-8")

    assert "scripts\\check_release_readiness.py" in script
    assert script.index("check_release_readiness.py") < script.index("-m PyInstaller")
    assert "throw \"Release readiness check failed" in script


# --- CI --------------------------------------------------------------------------------------------------------------------

def _lint_job() -> str:
    text = (REPO_ROOT / ".github" / "workflows" / "python-tests.yml").read_text(encoding="utf-8")
    return text[text.index("\n  lint:\n"): text.index("\n  security:\n")]


def test_ci_runs_the_freshness_guard_and_the_readiness_check_on_every_pull_request():
    workflow = (REPO_ROOT / ".github" / "workflows" / "python-tests.yml").read_text(encoding="utf-8")
    lint = _lint_job()

    assert "pull_request:" in workflow
    assert "python scripts/check_test_freshness.py" in lint
    assert "python scripts/check_release_readiness.py" in lint
    for step in ("Check test freshness", "Check release readiness"):
        block = lint[lint.index(f"- name: {step}"):]
        block = block[: block.index("\n\n") if "\n\n" in block else len(block)]
        assert "continue-on-error" not in block, f"{step} must block the merge"


def test_the_checks_need_nothing_installed_so_ci_runs_them_without_dependencies():
    allowed = set(sys.stdlib_module_names) | {"check_test_freshness"}  # each other, and the standard library only
    for script in ("check_test_freshness.py", "check_release_readiness.py"):
        tree = ast.parse((REPO_ROOT / "scripts" / script).read_text(encoding="utf-8"))
        imported = {
            (alias.name if isinstance(node, ast.Import) else node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in (node.names if isinstance(node, ast.Import) else [ast.alias(name=node.module or "")])
        }
        assert imported <= allowed, (script, sorted(imported - allowed))
