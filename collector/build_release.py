"""Builds a self-contained SortView Collector release bundle from a repo
checkout -- BUILD-TIME ONLY, never shipped in (or run from) the bundle it
produces.

WHY THIS EXISTS: collector/deploy/install-collector.ps1 and
update-collector.ps1 both assumed they were being run from inside a real
Git checkout (repo-root-relative paths, a `python -m collector.deploy_manifest`
subprocess call to enumerate the canonical-parser runtime slice). That is
correct for a developer/QA install directly against a checkout, but breaks
entirely for a customer machine that has no Git, no GitHub access, and no
repository -- exactly the real deployment target. This module produces a
flat, self-contained folder (a "release bundle") that already contains
every file collector/deploy/install-release.ps1 needs, laid out exactly
where that script expects to find them -- so the installer never needs to
resolve a repo root or shell out to Python before its own venv exists.

WHAT GOES IN THE BUNDLE (see COLLECTOR_RUNTIME_FILES /
deploy_manifest.PARSER_RUNTIME_FILES / DEPLOY_TOOL_FILES /
SUPPORT_FILES below) vs. what is deliberately excluded:

  - collector/deploy_manifest.py and collector/build_release.py itself are
    NEVER bundled -- both are build-time tools; the release installer
    copies a fixed, already-curated file set instead of dynamically
    enumerating one, so neither module is needed at install/update time
    anymore. COLLECTOR_RUNTIME_FILES is therefore an explicit list, not a
    directory glob -- a glob would silently sweep up any future build-only
    module dropped into collector/ (as deploy_manifest.py and this module
    already are) the same way it would a real runtime file; an explicit
    list instead requires deliberately updating it, and
    tests/test_collector_build_release.py cross-checks it against the
    directory contents so a forgotten update fails loudly instead of
    silently shipping an incomplete (or bloated) bundle.
  - agent.parser.* runtime slice: reused from deploy_manifest.PARSER_RUNTIME_FILES
    verbatim (single source of truth, unchanged) -- never duplicated here.
  - Deployment/support tooling: register-collector-task.ps1,
    run-preflight-as-system.ps1, and uninstall-collector.ps1 are copied
    VERBATIM (see DEPLOY_TOOL_FILES) -- all three were found, on
    inspection, to already resolve every path from their own
    -InstallRoot/-ConfigPath parameters with zero repo-root assumption,
    so no release-specific fork of any of them was needed. install-release.ps1,
    update-release.ps1, and set-collector-api-token.ps1 ARE
    release-specific (their repo-checkout equivalents -- install-collector.ps1,
    update-collector.ps1, agent/deploy/set-sortview-api-token.ps1 -- keep
    resolving relative to a checkout, for the separate developer/QA
    install-from-checkout flow, which this module does not replace).
  - Never bundled: tests/, docs/, SortViewAgent/, this repo's own
    Streamlit/FastAPI backend requirements, agent/runtime/* (frozen
    continuous-agent architecture -- see agent/README.md and
    collector/__init__.py's own docstring), .git metadata, __pycache__,
    or any filled-in (non-template) config.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import deploy_manifest

PRODUCT_NAME = "SortView Collector"

# Explicit, not a glob of collector/*.py -- see module docstring. Keep in
# sync with tests/test_collector_build_release.py's own cross-check
# against the directory's actual contents.
COLLECTOR_RUNTIME_FILES: tuple[str, ...] = (
    "collector/__init__.py",
    "collector/bootstrap_state.py",
    "collector/config.py",
    "collector/parsers.py",
    "collector/preflight.py",
    "collector/reader.py",
    "collector/run.py",
    "collector/state.py",
    "collector/support_info.py",
    "collector/task_settings.py",
    "collector/uploader.py",
)

# Files known to exist under collector/ that are deliberately NEVER
# bundled -- build-time-only tooling. Used only by this module's own
# tests to prove COLLECTOR_RUNTIME_FILES plus this set together account
# for every *.py file collector/ actually contains (catches drift in
# either direction: a forgotten addition, or an accidental inclusion).
BUILD_ONLY_COLLECTOR_FILES: tuple[str, ...] = (
    "collector/deploy_manifest.py",
    "collector/build_release.py",
)

# (repo-relative source, bundle-relative destination) -- copied verbatim,
# no content changes. See module docstring for why these three specifically
# needed no release-specific fork.
DEPLOY_TOOL_FILES: tuple[tuple[str, str], ...] = (
    ("collector/deploy/register-collector-task.ps1", "tools/register-task.ps1"),
    ("collector/deploy/run-preflight-as-system.ps1", "tools/preflight-system.ps1"),
    ("collector/deploy/update-release.ps1", "tools/update.ps1"),
    ("collector/deploy/uninstall-collector.ps1", "tools/uninstall.ps1"),
    ("collector/deploy/set-collector-api-token.ps1", "tools/set-api-token.ps1"),
)

# (repo-relative source, bundle-relative destination) for files that land
# at the bundle ROOT rather than under tools/.
SUPPORT_FILES: tuple[tuple[str, str], ...] = (
    ("collector/deploy/install-release.ps1", "install.ps1"),
    ("collector/deploy/requirements.txt", "requirements.txt"),
)

# collector_config.example.json is handled separately (not listed here)
# because its _comment_token field is rewritten at build time to point at
# the bundle's own tools/set-api-token.ps1 and install.ps1 instead of
# repo-relative paths that do not exist in a standalone bundle -- see
# _release_facing_example_config below.
CONFIG_TEMPLATE_SOURCE = "collector/deploy/collector_config.example.json"
CONFIG_TEMPLATE_DEST = "collector_config.example.json"


class BuildError(Exception):
    """Raised for any build-time problem -- a missing required source
    file, an existing non-empty output directory without -Force, etc.
    Never partially writes a bundle: every source file's existence is
    verified BEFORE any copy begins (see build_release)."""


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class BuildResult:
    bundle_dir: Path
    manifest_path: Path
    entries: tuple[ManifestEntry, ...]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _release_facing_example_config(source_path: Path) -> str:
    """Loads the repo's config template and rewrites only its
    _comment_token field to reference the bundle's own tools/set-api-token.ps1
    and install.ps1, instead of repo-relative paths (collector/deploy/...)
    that do not exist in a standalone bundle. Every other field, including
    every other comment, is passed through unchanged -- parsed and
    re-serialized as JSON (not string search/replace) so the result is
    guaranteed to still be valid JSON regardless of the source wording."""
    doc = json.loads(source_path.read_text(encoding="utf-8"))
    # bandit B105 (hardcoded-password heuristic) false-positives on this --
    # it is doc prose EXPLAINING that no token belongs in this file, not a
    # credential itself. See agent/uploader.py for this repo's established
    # inline-nosec convention for a genuine tool false positive.
    doc["_comment_token"] = (  # nosec B105
        "SORTVIEW_API_TOKEN is READ FROM THE ENVIRONMENT ONLY -- it must never appear in "
        "this file. Set it as a Machine-scope environment variable via install.ps1's token "
        "step (or tools\\set-api-token.ps1 directly) before running the collector or its "
        "preflight check."
    )
    return json.dumps(doc, indent=2) + "\n"


def _required_source_files(repo_root: Path) -> list[tuple[Path, str | None]]:
    """Every (absolute_source_path, bundle_relative_dest) pair this build
    needs -- computed up front so every one can be verified to exist
    BEFORE any copying starts (see BuildError's docstring). dest is None
    for a source that is verified here but written separately, not
    copied verbatim by the generic loop below (see CONFIG_TEMPLATE_SOURCE)."""
    pairs: list[tuple[Path, str | None]] = []

    for rel in COLLECTOR_RUNTIME_FILES:
        pairs.append((repo_root / rel, rel))

    # Resolved from the raw tuple, not deploy_manifest.parser_runtime_files()
    # (which raises FileNotFoundError on the first missing entry) -- so a
    # missing parser-runtime file is reported the same uniform way as any
    # other missing source below (all of them collected and reported
    # together as a single BuildError), not as a different exception type.
    for rel in deploy_manifest.PARSER_RUNTIME_FILES:
        pairs.append((repo_root / rel, rel))

    for source_rel, dest_rel in DEPLOY_TOOL_FILES:
        pairs.append((repo_root / source_rel, dest_rel))

    for source_rel, dest_rel in SUPPORT_FILES:
        pairs.append((repo_root / source_rel, dest_rel))

    # collector_config.example.json is deliberately NOT included in this
    # list's copy step -- it is rewritten (not copied verbatim), see
    # _release_facing_example_config -- but its source existence is still
    # verified here up front, same as everything else, so a missing
    # template fails the build loudly before any output directory is
    # created rather than surfacing later as an unhandled read error.
    pairs.append((repo_root / CONFIG_TEMPLATE_SOURCE, None))

    return pairs


def build_release(
    repo_root: Path,
    output_dir: Path,
    version: str,
    *,
    force: bool = False,
    built_at: str | None = None,
) -> BuildResult:
    """Builds a standalone release bundle at
    <output_dir>/SortViewCollector-<version>/.

    Fails loudly (BuildError) and writes NOTHING if any required source
    file is missing -- every source file's existence is checked up front,
    before the target directory is even created, so a partially-built
    bundle is never left behind by a mid-build failure. Deterministic
    given a fixed `built_at` (accepted so tests don't depend on wall-clock
    time); defaults to the current UTC time.
    """
    if not version.strip():
        raise BuildError("version must not be empty")

    required = _required_source_files(repo_root)
    missing = [str(src) for src, _dest in required if not src.is_file()]
    if missing:
        raise BuildError(
            "Refusing to build -- required source file(s) missing:\n" + "\n".join(f"  {m}" for m in missing)
        )

    bundle_dir = output_dir / f"SortViewCollector-{version}"
    if bundle_dir.exists():
        if not force:
            raise BuildError(
                f"Output directory already exists: {bundle_dir} -- pass force=True / --force to rebuild it."
            )
        shutil.rmtree(bundle_dir)

    bundle_dir.mkdir(parents=True)

    entries: list[ManifestEntry] = []
    for src, dest_rel in required:
        if dest_rel is None:
            # collector_config.example.json's source -- existence already
            # verified above; written separately below, never copied verbatim.
            continue
        dest = bundle_dir / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        entries.append(ManifestEntry(dest_rel, _sha256_file(dest), dest.stat().st_size))

    # collector_config.example.json is written separately (rewritten
    # comment, not a byte-for-byte copy) -- see _release_facing_example_config.
    config_dest = bundle_dir / CONFIG_TEMPLATE_DEST
    config_text = _release_facing_example_config(repo_root / CONFIG_TEMPLATE_SOURCE)
    config_dest.write_text(config_text, encoding="utf-8", newline="\n")
    entries.append(ManifestEntry(CONFIG_TEMPLATE_DEST, hashlib.sha256(config_text.encode("utf-8")).hexdigest(), len(config_text.encode("utf-8"))))

    entries.sort(key=lambda e: e.path)

    manifest = {
        "product": PRODUCT_NAME,
        "version": version,
        "built_at": built_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "files": [{"path": e.path, "sha256": e.sha256, "size_bytes": e.size_bytes} for e in entries],
    }
    manifest_path = bundle_dir / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")

    return BuildResult(bundle_dir=bundle_dir, manifest_path=manifest_path, entries=tuple(entries))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a self-contained SortView Collector release bundle -- no Git, "
        "no repository checkout, and no development environment required on the target "
        "machine to install from the result."
    )
    parser.add_argument("--output", required=True, help="Directory to build the release bundle into")
    parser.add_argument("--version", required=True, help='Release version, e.g. "1.0.0"')
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Repository root (defaults to two levels up from this file)",
    )
    parser.add_argument("--force", action="store_true", help="Remove and rebuild an existing output directory")
    args = parser.parse_args(argv)

    try:
        result = build_release(
            Path(args.repo_root), Path(args.output), args.version, force=args.force
        )
    except BuildError as exc:
        print(f"Build failed: {exc}", file=sys.stderr)
        return 1

    print(f"Built release bundle: {result.bundle_dir}")
    print(f"Manifest: {result.manifest_path} ({len(result.entries)} file(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
