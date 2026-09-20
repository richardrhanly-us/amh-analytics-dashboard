"""Release-readiness validation for a Collector release: run BEFORE a release is built.

collector.__version__ (collector/__init__.py) is the ONE authoritative Collector version. This check never records
the version anywhere else; it proves the rest of the repository still agrees with the authority instead:

  READY001  the authority exists and is a single, well-formed string literal.
  READY002  no second copy of the release version lives in the Collector's code or tooling (a quoted copy of it
            in any collector/ file or production module, or a version-named constant with a release-shaped
            literal inside collector/).
  READY003  packaging and release tooling DERIVE from the authority: the runtime modules that report the version
            (uploader heartbeat, support-info, the frozen `version` command), the build tools and the Super Admin
            forms that default the Collector version import it, and the frozen-build script reads
            collector.__version__ rather than carrying a literal.
  READY004  --expect-version, when given, equals the authority. This is the one place an exact release number is
            asserted (the assertion is supplied by the person releasing, never copied into the tests).
  READY005  the shared controlled clock the time-sensitive tests rely on exists.
  FRESH00x  the freshness guard (scripts/check_test_freshness.py) is clean: no current-state test carries a
            conflicting release literal or a release-named test, the time-sensitive enrollment tests use the
            controlled clock and never read today's wall clock, and no production default (e.g. the Super Admin
            "Collector version" fields) hardcodes a Collector release.

USAGE
    python scripts/check_release_readiness.py [--repo-root PATH] [--expect-version X.Y.Z]   # exit 0 ready, 1 not
collector/build_release.py runs this before it builds anything (the same check, so a release cannot be built from a
tree that fails it), and collector/freeze/build_frozen.ps1 runs it before PyInstaller. Fast (AST only) and
deterministic: no network, no clock, no imports of the code under check.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# Runnable as a script (scripts/ is then sys.path[0]), as `scripts.check_release_readiness`, or loaded by path
# from collector/build_release.py: make the sibling module importable in every case.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import check_test_freshness as freshness

Finding = freshness.Finding

AUTHORITY = "collector/__init__.py"
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?$")
VERSION_NAMES = {"__version__", "VERSION", "COLLECTOR_VERSION", "RELEASE_VERSION", "SOURCE_VERSION", "CURRENT_VERSION"}

# Modules that must obtain the version from the authority (import it) instead of holding their own.
DERIVING_MODULES = (
    "collector/uploader.py",          # reported on every heartbeat
    "collector/support_info.py",      # reported by support-info
    "collector/freeze/dispatcher.py",  # the config-free `version` command of the frozen executable
    "collector/build_release.py",     # the --version assertion and the MANIFEST
    "super_admin/pages/Provision_Library.py",  # the default of the "Collector version" field
    "super_admin/pages/Manage_Libraries.py",   # ...and of the "Add installation" form
)
FROZEN_BUILD_SCRIPT = "collector/freeze/build_frozen.ps1"
CONTROLLED_CLOCK = "tests/controlled_clock.py"
_TEXT_SUFFIXES = {".py", ".ps1", ".spec", ".json", ".txt", ".cfg", ".toml", ".ini", ".xml"}
_PS_VERSION_LITERAL = re.compile(r"\$\w*[Vv]ersion\w*\s*=\s*['\"]\d+\.\d+\.\d+")


@dataclass(frozen=True)
class CheckResult:
    name: str
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not self.findings


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def check_version_authority(repo_root: Path) -> list[Finding]:
    """READY001 + READY002."""
    init = repo_root / AUTHORITY
    source = _read(init)
    if source is None:
        return [Finding(AUTHORITY, 1, "READY001", "the version authority is missing or unreadable")]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Finding(AUTHORITY, exc.lineno or 1, "READY001", f"cannot be parsed: {exc.msg}")]

    definitions = [
        n for n in tree.body
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__version__" for t in n.targets)
    ]
    if len(definitions) != 1:
        return [Finding(AUTHORITY, 1, "READY001", f"expected exactly one module-level __version__ assignment, found {len(definitions)}")]
    value = definitions[0].value
    if not (isinstance(value, ast.Constant) and isinstance(value.value, str) and VERSION_PATTERN.match(value.value)):
        return [Finding(AUTHORITY, definitions[0].lineno, "READY001",
                        "__version__ must be a plain string literal like '1.2.3' (it is read without importing or evaluating)")]
    version = value.value

    findings: list[Finding] = []
    quoted = re.compile(r"[\"']" + re.escape(version) + r"[\"']")
    candidates = [
        p for p in sorted((repo_root / "collector").rglob("*"))
        if p.is_file() and p.suffix in _TEXT_SUFFIXES and "__pycache__" not in p.parts
    ]
    candidates += freshness.production_files(repo_root)  # a quoted copy of the release in production code is a copy too
    for path in candidates:
        relative = path.relative_to(repo_root).as_posix()
        if relative == AUTHORITY:
            continue
        text = _read(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if quoted.search(line):
                findings.append(Finding(relative, number, "READY002",
                                        f"copies the release version {version!r}; collector.__version__ is the only place it is written"))
        if path.suffix == ".py" and relative.startswith("collector/"):
            try:
                module = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(module):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                assigned = node.value
                if (
                    isinstance(assigned, ast.Constant) and isinstance(assigned.value, str)
                    and VERSION_PATTERN.match(assigned.value)
                    and any(isinstance(t, ast.Name) and t.id in VERSION_NAMES for t in targets)
                ):
                    findings.append(Finding(relative, node.lineno, "READY002",
                                            "defines its own release-version constant; import collector.__version__ instead"))
    return findings


def _imports_the_authority(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in (None, "", "collector") and any(a.name == "__version__" for a in node.names):
            return True
    return False


def check_tooling_derives_from_authority(repo_root: Path) -> list[Finding]:
    """READY003."""
    findings: list[Finding] = []
    for relative in DERIVING_MODULES:
        source = _read(repo_root / relative)
        if source is None:
            findings.append(Finding(relative, 1, "READY003", "is missing or unreadable, so it cannot be shown to derive from collector.__version__"))
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            findings.append(Finding(relative, exc.lineno or 1, "READY003", f"cannot be parsed: {exc.msg}"))
            continue
        if not _imports_the_authority(tree):
            findings.append(Finding(relative, 1, "READY003", "does not import __version__ from the collector package"))

    script = _read(repo_root / FROZEN_BUILD_SCRIPT)
    if script is None:
        findings.append(Finding(FROZEN_BUILD_SCRIPT, 1, "READY003", "is missing or unreadable"))
    else:
        if "collector.__version__" not in script:
            findings.append(Finding(FROZEN_BUILD_SCRIPT, 1, "READY003", "does not read collector.__version__"))
        for number, line in enumerate(script.splitlines(), start=1):
            if _PS_VERSION_LITERAL.search(line):
                findings.append(Finding(FROZEN_BUILD_SCRIPT, number, "READY003", "assigns a literal version instead of reading collector.__version__"))
    return findings


def check_expected_version(repo_root: Path, expected_version: str | None) -> list[Finding]:
    """READY004: the exact release assertion, supplied by the releaser (never stored in the tests)."""
    if expected_version is None:
        return []
    actual = freshness.read_release_version(repo_root)
    if actual is None:
        return [Finding(AUTHORITY, 1, "READY004", "the release version cannot be read, so it cannot be compared")]
    if expected_version != actual:
        return [Finding(AUTHORITY, 1, "READY004",
                        f"the requested release {expected_version!r} does not match collector.__version__ {actual!r}; "
                        "change collector.__version__ first, then request that version")]
    return []


def check_controlled_clock_exists(repo_root: Path) -> list[Finding]:
    """READY005."""
    if (repo_root / CONTROLLED_CLOCK).is_file():
        return []
    return [Finding(CONTROLLED_CLOCK, 1, "READY005", "the shared controlled clock the time-sensitive tests use is missing")]


def check_tests_are_fresh(repo_root: Path) -> list[Finding]:
    """FRESH00x: stale release literals, release-named tests, fixed 'now' constants, wall-clock reads, and
    hardcoded Collector releases in production defaults."""
    return freshness.scan_repository(repo_root)


def run_checks(repo_root: Path, *, expected_version: str | None = None) -> list[CheckResult]:
    steps: list[tuple[str, Callable[[], list[Finding]]]] = [
        ("collector.__version__ is the single version authority", lambda: check_version_authority(repo_root)),
        ("packaging and release tooling derive from it", lambda: check_tooling_derives_from_authority(repo_root)),
        ("the requested release matches it", lambda: check_expected_version(repo_root, expected_version)),
        ("the shared controlled clock exists", lambda: check_controlled_clock_exists(repo_root)),
        ("tests and production defaults carry no stale release literal; tests no wall-clock dependence", lambda: check_tests_are_fresh(repo_root)),
    ]
    return [CheckResult(name, tuple(sorted(step()))) for name, step in steps]


def check_release_readiness(repo_root: Path, *, expected_version: str | None = None) -> list[Finding]:
    """Every finding from every check (empty means the tree is ready for a release build)."""
    return [finding for result in run_checks(repo_root, expected_version=expected_version) for finding in result.findings]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the repository is ready for a Collector release build.")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--expect-version", help="the release you intend to build; must equal collector.__version__")
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()

    results = run_checks(repo_root, expected_version=args.expect_version)
    for result in results:
        print(f"[{'ok' if result.ok else 'FAIL'}] {result.name}")
        for finding in result.findings:
            print(f"       {finding}")
    failed = [r for r in results if not r.ok]
    if failed:
        print(f"\nNot ready for a release build: {len(failed)} check(s) failed.", file=sys.stderr)
        return 1
    print(f"\nReady: release {freshness.read_release_version(repo_root)} (collector.__version__) passes every check.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
