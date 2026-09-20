"""Test-freshness guard: catches tests (and production defaults) that quietly go stale as releases and the calendar move on.

WHY. Two failure modes recur around every Collector release, and neither shows up until a PR is already open:

  * a test that asserts "the current version is X.Y.Z" with a copied literal -- it breaks (or worse, keeps
    passing against a stale copy) on the next version bump, so every bump edits arbitrary test files;
  * a test that treats a FIXED calendar timestamp as "now" while the code under test reads the REAL clock -- it
    passes for a while, then fails on a day nobody touched anything;
  * production code (the Super Admin forms, the services behind them) that hardcodes the Collector release it
    offers as a default -- correct the day it is written, stale after the next bump, and invisible to any test.

The version authority is collector.__version__; ordinary tests compare against it (or against what a component
reports), never against a copy. Time-sensitive tests read one controlled clock (tests/controlled_clock.py).

WHAT IT CHECKS (static -- it parses the tests, imports nothing, and finishes in well under a second):

  FRESH001  an assert compares collector.__version__ (or an alias of it) with a string literal.
  FRESH002  an assert contains a string literal exactly equal to the CURRENT release version.
  FRESH003  a test/test class name embeds a specific release, e.g. test_the_next_build_reports_1_0_3.
  FRESH004  a module-level constant named like "now"/"today" holds a FIXED timestamp and the module does not use
            the shared controlled clock -- the shape that goes stale against a code path reading the real clock.
  FRESH005  a test module that must be wall-clock-free (WALL_CLOCK_FREE_TESTS) reads the real clock, or does not
            use the controlled clock at all.
  FRESH006  production code (PRODUCTION_ROOTS) hardcodes a release-shaped string as a COLLECTOR version: a
            widget default (`st.text_input("Collector version", value="1.0.3")`), a keyword/parameter default or
            assignment whose name mentions both "collector" and "version" (`collector_version="1.0.3"`), or such
            a dict entry. The fix is `from collector import __version__` -- never a second constant.
  FRESH000  a malformed `# freshness:` annotation, or a file that cannot be parsed.

WHAT IT DELIBERATELY DOES NOT DO: forbid version or date literals (FRESH006 fires only where a literal is a
COLLECTOR version -- a schema, API or agent version, or any other release-shaped text, is not matched). Historical fixtures ("an install registered at
1.0.2"), migration data, parser fixtures and explicit expiry/boundary timestamps are all fine and are not matched:
only the shapes above are. Where a match is legitimate, say so beside it, with a reason:

    NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)  # freshness: allow FRESH004 -- passed as now= at every call

The annotation goes on the flagged line (or a comment-only line directly above it) and must carry a reason.

USAGE
    python scripts/check_test_freshness.py [--repo-root PATH]      # exit 0 clean, 1 findings
Also run by CI (lint job), by tests/test_freshness_guard.py, and as part of scripts/check_release_readiness.py.
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import sys
import tokenize
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

RULES: dict[str, str] = {
    "FRESH000": "malformed freshness annotation or unparseable test file",
    "FRESH001": "release literal compared with collector.__version__",
    "FRESH002": "the current release version hardcoded in an assertion",
    "FRESH003": "test name embeds a specific release",
    "FRESH004": "fixed timestamp named as the current time",
    "FRESH005": "wall-clock read in (or missing controlled clock from) a clock-free test module",
    "FRESH006": "hardcoded Collector release in production code",
}

# Test modules whose outcome must never depend on today's date: they may not read the wall clock and must use
# the shared controlled clock. (The PostgreSQL enrollment tests are not listed: they run against a real database
# server, generate and redeem with the same real "now" a moment apart, and are opt-in.)
WALL_CLOCK_FREE_TESTS: tuple[str, ...] = ("tests/test_collector_enrollment.py",)

CONTROLLED_CLOCK_MODULE = "controlled_clock"

# Production code that offers, records or displays a Collector version. Scanned for FRESH006 (a hardcoded release
# default). Deliberately not agent/ -- the legacy continuous agent is a different product with its own version.
PRODUCTION_ROOTS: tuple[str, ...] = ("super_admin", "src", "main.py")
_WIDGET_DEFAULT_KEYWORDS = {"value", "default"}

_RELEASE_LITERAL = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?$")
_RELEASE_IN_NAME = re.compile(r"(?:^|_)v?\d{1,2}_\d{1,3}_\d{1,3}(?:_|$)")
_NOW_LIKE_NAME = re.compile(
    r"^(?:[A-Z][A-Z0-9]*_)*(?:NOW|TODAY|UTCNOW|CURRENT_TIME|CURRENT_DATE|CURRENT_DATETIME|FROZEN_TIME|FAKE_NOW)(?:_[A-Z0-9]+)*$"
)
_ISO_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")
_TIMESTAMP_CONSTRUCTORS = {"datetime", "date", "Timestamp", "fromisoformat", "strptime", "to_datetime"}
_CLOCK_READERS = {"now", "today", "utcnow"}
_WALL_CLOCK_CALLS = {
    "datetime.now", "datetime.utcnow", "datetime.today", "date.today", "Timestamp.now", "Timestamp.today",
    "time.time", "time.time_ns",
}
_ANNOTATION = re.compile(r"#\s*freshness:\s*allow\s+(FRESH\d{3})\s*--\s*(\S.{3,})$")


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.rule} {self.message}"


# --- reading the authority ----------------------------------------------------------------------


def read_release_version(repo_root: Path) -> str | None:
    """collector.__version__ as written in collector/__init__.py -- parsed, never imported, so this check
    cannot be fooled by (or slowed by) whatever else happens to be on sys.path."""
    init = repo_root / "collector" / "__init__.py"
    try:
        tree = ast.parse(init.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    return None


# --- annotations ----------------------------------------------------------------------------------


def _annotations(source: str, path: str) -> tuple[dict[int, set[str]], list[Finding], set[int]]:
    """(line -> rules allowed on it, malformed-annotation findings, comment-only lines). Real comments only:
    tokenizing means a string that merely contains the text of an annotation is not one."""
    allowed: dict[int, set[str]] = {}
    problems: list[Finding] = []
    comment_only: set[int] = set()
    lines = source.splitlines()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return allowed, problems, comment_only
    for token in tokens:
        if token.type != tokenize.COMMENT or "freshness:" not in token.string:
            continue
        line = token.start[0]
        if lines[line - 1].lstrip().startswith("#"):
            comment_only.add(line)
        match = _ANNOTATION.search(token.string)
        if match is None or match.group(1) not in RULES:
            problems.append(Finding(path, line, "FRESH000",
                                    "malformed freshness annotation; write `# freshness: allow FRESH00x -- <reason>` "
                                    "with a known rule and a reason"))
            continue
        allowed.setdefault(line, set()).add(match.group(1))
    return allowed, problems, comment_only


def _is_allowed(rule: str, node: ast.AST, allowed: dict[int, set[str]], comment_only: set[int]) -> bool:
    first = getattr(node, "lineno", 0)
    decorators = getattr(node, "decorator_list", None)
    if decorators:
        first = min(first, *(d.lineno for d in decorators))
    last = getattr(node, "end_lineno", first) or first
    if any(rule in allowed.get(line, ()) for line in range(first, last + 1)):
        return True
    above = first - 1
    return above in comment_only and rule in allowed.get(above, ())


# --- the rules ------------------------------------------------------------------------------------


def _version_aliases(tree: ast.Module) -> set[str]:
    """Names bound to collector.__version__: `__version__` itself and any `import ... as X`."""
    names = {"__version__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "__version__":
                    names.add(alias.asname or alias.name)
    return names


def _refers_to_version_authority(node: ast.expr, aliases: set[str]) -> bool:
    if isinstance(node, ast.Attribute):
        return node.attr == "__version__"
    return isinstance(node, ast.Name) and node.id in aliases


def _has_str_literal(node: ast.expr) -> bool:
    """A string literal, or a tuple/list/set containing one (`v in ("1.0.3", "1.0.4")`)."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_has_str_literal(element) for element in node.elts)
    return False


def _last_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _dotted_tail(node: ast.expr) -> str:
    """'datetime.now' for datetime.now / enrollment.datetime.now / dt.datetime.now."""
    if isinstance(node, ast.Attribute):
        owner = _last_name(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    return _last_name(node) or ""


def _is_fixed_timestamp(value: ast.expr) -> bool:
    """A timestamp spelled out in the source -- datetime(2026, 9, 20, ...), Timestamp("2026-03-30"),
    "2026-09-20T12:00:00Z" -- with no read of the clock in it."""
    fixed = False
    for node in ast.walk(value):
        if isinstance(node, ast.Call):
            name = _last_name(node.func)
            if name in _CLOCK_READERS:
                return False
            if name in _TIMESTAMP_CONSTRUCTORS and node.args and isinstance(node.args[0], ast.Constant):
                fixed = True
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and _ISO_TIMESTAMP.match(node.value):
            fixed = True
    return fixed


def _uses_controlled_clock(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == CONTROLLED_CLOCK_MODULE:
            return True
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] == CONTROLLED_CLOCK_MODULE for a in node.names):
            return True
    return False


def scan_source(
    source: str,
    path: str = "<source>",
    *,
    current_version: str | None = None,
    wall_clock_free: bool = False,
) -> list[Finding]:
    """All findings for one test module's source text."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 1, "FRESH000", f"cannot be parsed: {exc.msg}")]

    allowed, findings, comment_only = _annotations(source, path)

    def report(rule: str, node: ast.AST, message: str) -> None:
        if not _is_allowed(rule, node, allowed, comment_only):
            findings.append(Finding(path, getattr(node, "lineno", 1), rule, message))

    aliases = _version_aliases(tree)
    uses_clock = _uses_controlled_clock(tree)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            flagged_literals: set[int] = set()
            for compare in (n for n in ast.walk(node.test) if isinstance(n, ast.Compare)):
                operands = [compare.left, *compare.comparators]
                if any(_refers_to_version_authority(o, aliases) for o in operands):
                    for operand in operands:
                        if _has_str_literal(operand):
                            flagged_literals.update(id(c) for c in ast.walk(operand) if isinstance(c, ast.Constant))
                            report("FRESH001", compare,
                                   "compares collector.__version__ with a literal; compare it with what the component "
                                   "reports, never with a copy of the number (an exact release assertion belongs in "
                                   "scripts/check_release_readiness.py --expect-version)")
            if current_version:
                for constant in (n for n in ast.walk(node.test) if isinstance(n, ast.Constant)):
                    if constant.value == current_version and id(constant) not in flagged_literals:
                        report("FRESH002", constant,
                               f"asserts against the literal {current_version!r}, the current release; use "
                               "collector.__version__ (or a clearly older/newer fixture value) so a version bump "
                               "does not require editing this test")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name.startswith(("test", "Test")) and _RELEASE_IN_NAME.search(node.name):
                report("FRESH003", node,
                       f"the name {node.name!r} embeds a specific release; name what is checked, not the number, "
                       "so it does not become misleading with every release")

    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None or uses_clock:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and _NOW_LIKE_NAME.match(target.id) and _is_fixed_timestamp(value):
                report("FRESH004", node,
                       f"{target.id} is a fixed timestamp standing in for the current time, but nothing pins the code "
                       "under test to it, so it goes stale as the calendar moves. Use the shared controlled clock "
                       "(tests/controlled_clock.py) or, if every use injects it explicitly, annotate why")

    if wall_clock_free:
        if not uses_clock:
            findings.append(Finding(path, 1, "FRESH005",
                                    "this module must be wall-clock-free but does not use the shared controlled clock "
                                    "(tests/controlled_clock.py)"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _dotted_tail(node.func) in _WALL_CLOCK_CALLS:
                report("FRESH005", node,
                       f"{_dotted_tail(node.func)}() reads the real clock in a module whose result must not depend "
                       "on today's date; read the controlled clock (CLOCK.instant / CLOCK.advance) instead")

    return sorted(findings)


def _mentions_collector_version(text: str | None) -> bool:
    lowered = (text or "").lower()
    return "collector" in lowered and "version" in lowered


def _is_release_literal(node: ast.expr | None) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and bool(_RELEASE_LITERAL.match(node.value))


def _assigned_names(target: ast.expr) -> list[str]:
    """`x` -> x, `self.x` -> x, `payload["x"]` -> x."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Attribute):
        return [target.attr]
    if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and isinstance(target.slice.value, str):
        return [target.slice.value]
    return []


def scan_production_source(source: str, path: str = "<source>") -> list[Finding]:
    """FRESH006 for one production module: a release-shaped literal used as a COLLECTOR version."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 1, "FRESH000", f"cannot be parsed: {exc.msg}")]

    allowed, findings, comment_only = _annotations(source, path)

    def report(node: ast.AST, literal: ast.expr) -> None:
        if not _is_allowed("FRESH006", node, allowed, comment_only):
            findings.append(Finding(
                path, getattr(node, "lineno", 1), "FRESH006",
                f"hardcodes the Collector release {getattr(literal, 'value', '?')!r}; use `from collector import "
                "__version__` (collector.__version__ is the only place a release is written) so the next version "
                "bump cannot leave this stale",
            ))

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            labels = [a.value for a in node.args[:1] if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            labels += [k.value.value for k in node.keywords
                       if k.arg == "label" and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str)]
            label_is_collector_version = any(_mentions_collector_version(label) for label in labels)
            for keyword in node.keywords:
                if keyword.arg is not None and _is_release_literal(keyword.value) and (
                    _mentions_collector_version(keyword.arg)
                    or (label_is_collector_version and keyword.arg in _WIDGET_DEFAULT_KEYWORDS)
                ):
                    report(keyword.value, keyword.value)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is not None and _is_release_literal(node.value) and any(
                _mentions_collector_version(name) for target in targets for name in _assigned_names(target)
            ):
                report(node, node.value)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant) and isinstance(key.value, str)
                    and _mentions_collector_version(key.value) and _is_release_literal(value)
                ):
                    report(value, value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            spec = node.args
            positional = [*spec.posonlyargs, *spec.args]
            pairs = list(zip(positional[len(positional) - len(spec.defaults):], spec.defaults, strict=True))
            pairs += [(a, d) for a, d in zip(spec.kwonlyargs, spec.kw_defaults, strict=True) if d is not None]
            for argument, default in pairs:
                if _mentions_collector_version(argument.arg) and _is_release_literal(default):
                    report(default, default)

    return sorted(findings)


# --- scanning the repository -----------------------------------------------------------------------


def _test_files(repo_root: Path) -> Iterable[Path]:
    tests = repo_root / "tests"
    if not tests.is_dir():
        return []
    return sorted(p for p in tests.rglob("*.py") if "__pycache__" not in p.parts)


def production_files(repo_root: Path) -> list[Path]:
    files: list[Path] = []
    for root in PRODUCTION_ROOTS:
        path = repo_root / root
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts))
    return files


def scan_production(repo_root: Path) -> list[Finding]:
    """FRESH006 across the production roots of `repo_root`."""
    findings: list[Finding] = []
    for path in production_files(repo_root):
        relative = path.relative_to(repo_root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            findings.append(Finding(relative, 1, "FRESH000", f"cannot be read: {exc}"))
            continue
        findings.extend(scan_production_source(source, relative))
    return sorted(findings)


def scan_tests(
    repo_root: Path,
    *,
    current_version: str | None = None,
    wall_clock_free: Iterable[str] = WALL_CLOCK_FREE_TESTS,
) -> list[Finding]:
    """Every finding across tests/**/*.py of `repo_root` (the current release read from its collector package)."""
    version = current_version if current_version is not None else read_release_version(repo_root)
    clock_free = set(wall_clock_free)
    findings: list[Finding] = []
    for path in _test_files(repo_root):
        relative = path.relative_to(repo_root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            findings.append(Finding(relative, 1, "FRESH000", f"cannot be read: {exc}"))
            continue
        findings.extend(scan_source(source, relative, current_version=version, wall_clock_free=relative in clock_free))
    for missing in sorted(c for c in clock_free if not (repo_root / c).is_file()):
        findings.append(Finding(missing, 1, "FRESH005", "is listed as a wall-clock-free test module but does not exist"))
    return sorted(findings)


def scan_repository(
    repo_root: Path,
    *,
    current_version: str | None = None,
    wall_clock_free: Iterable[str] = WALL_CLOCK_FREE_TESTS,
) -> list[Finding]:
    """Tests (FRESH000-005) and production defaults (FRESH006): everything this guard checks."""
    return sorted([
        *scan_tests(repo_root, current_version=current_version, wall_clock_free=wall_clock_free),
        *scan_production(repo_root),
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect tests and production defaults that go stale as releases and the calendar move on."
    )
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent.parent))
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root).resolve()

    version = read_release_version(repo_root)
    findings = scan_repository(repo_root, current_version=version)
    for finding in findings:
        print(finding)
    if findings:
        print(f"\n{len(findings)} freshness finding(s). See the docstring of scripts/check_test_freshness.py "
              "for the rules and the annotation for a legitimate exception.", file=sys.stderr)
        return 1
    print(f"Test freshness OK (current release {version or 'unknown'}; {len(list(_test_files(repo_root)))} test files, "
          f"{len(production_files(repo_root))} production files).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
