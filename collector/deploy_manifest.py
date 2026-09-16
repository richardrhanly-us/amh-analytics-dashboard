"""Deployment file manifest (parser-parity deployment repair).

Single source of truth for which files a Collector v1 install actually
needs on disk -- consumed by collector/deploy/install-collector.ps1 and
collector/deploy/update-collector.ps1 so the required file list can
never silently drift between the two scripts (same pattern
collector/task_settings.py already established for Scheduled Task XML:
one Python module is authoritative, PowerShell shells out to it rather
than duplicating the list). Pure Python, no third-party imports --
deliberately runnable under a bare `python`, before the collector venv
(and therefore pandas) exists yet, since install-collector.ps1 needs
this list BEFORE it creates the venv.

PARSER_RUNTIME_FILES is the verified (not assumed) transitive import
closure of collector/parsers.py's `from agent.parser import ...`
statements, confirmed by reading every file in the chain:

    collector/parsers.py
      -> agent/parser/checkins.py  -> pandas (venv dep) + agent/logger_config.py
      -> agent/parser/rejects.py   -> pandas (venv dep) + agent/logger_config.py
      -> agent/parser/acs.py       -> pandas (venv dep) + re (stdlib) + agent/logger_config.py
      -> agent/parser/__init__.py  -> (docstring only, no imports -- still required as a package marker)
    agent/__init__.py is required too -- empty, but Python cannot import
    `agent.parser.*` as a package at all without it existing on disk.

Nothing beyond this is copied -- explicitly NOT agent/config.py,
agent/runtime/*, agent/tailer.py, agent/state.py, agent/spool.py,
agent/main.py, agent/discovery.py, agent/identity.py,
agent/event_identity.py, the legacy agent/run_pipeline.py mirror, or the
archived "agent/SortViewAgent - What is currently sitting on the AMH
computer/" snapshot -- collector/parsers.py imports none of them, and
copying them would violate the narrow-deploy-package architecture
collector/__init__.py's own docstring has maintained since Phase 4a.
Also never the root-level SortViewAgent/ reference baseline (unrelated
to this package entirely, added later for local comparison only).
"""

from __future__ import annotations

import argparse
from pathlib import Path

# Repo-root-relative; forward slashes here, normalized to the OS path
# separator by callers via Path(...) / PowerShell's own Join-Path.
PARSER_RUNTIME_FILES: tuple[str, ...] = (
    "agent/__init__.py",
    "agent/logger_config.py",
    "agent/parser/__init__.py",
    "agent/parser/checkins.py",
    "agent/parser/rejects.py",
    "agent/parser/acs.py",
)


def collector_package_files(repo_root: Path) -> list[Path]:
    """Every *.py file directly under collector/ -- the same glob
    install-collector.ps1 has always used for the collector package
    itself, expressed here too so a test can verify it without shelling
    out to PowerShell."""
    return sorted((repo_root / "collector").glob("*.py"))


def parser_runtime_files(repo_root: Path) -> list[Path]:
    """Resolves PARSER_RUNTIME_FILES against repo_root, verifying each
    actually exists -- so a future rename/removal in agent/parser/*
    breaks this manifest loudly (a test failure, or a nonzero exit from
    this module's own CLI) rather than silently shipping an incomplete
    runtime to a real install."""
    resolved = []
    for rel in PARSER_RUNTIME_FILES:
        path = repo_root / Path(rel)
        if not path.is_file():
            raise FileNotFoundError(f"deploy manifest lists a file that does not exist: {path}")
        resolved.append(path)
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print the required canonical-parser runtime file list "
        "(repo-root-relative, one per line) -- consumed by "
        "collector/deploy/install-collector.ps1 and update-collector.ps1."
    )
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Repository root (defaults to two levels up from this file, which is "
        "correct when run via -m from a real checkout regardless of the caller's "
        "own working directory).",
    )
    args = parser.parse_args(argv)
    repo_root = Path(args.repo_root)

    for path in parser_runtime_files(repo_root):
        print(path.relative_to(repo_root).as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
