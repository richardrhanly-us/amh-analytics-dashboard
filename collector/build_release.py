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
  - VERSION AUTHORITY: collector.__version__ is the single authoritative
    Collector version -- the one the running Collector reports on every
    heartbeat and in support-info. The --version / `version` argument here
    is only an ASSERTION of it, never an independent source: both
    build_release and build_frozen_release raise BuildError, before any
    output directory is created or removed, unless it equals
    collector.__version__ exactly. A FROZEN bundle is additionally proven at
    the packaging boundary: the supplied runtime's SortViewCollector.exe is
    run with its config-free `version` command and must report that same
    version, so a stale PyInstaller build can never ship under a newer
    bundle name. This module never rewrites or stamps source (or any
    generated version file); the bundle directory name and
    MANIFEST.json["version"] are both derived from the validated value.
  - Never bundled: tests/, docs/, SortViewAgent/, this repo's own
    Streamlit/FastAPI backend requirements, agent/runtime/* (frozen
    continuous-agent architecture -- see agent/README.md and
    collector/__init__.py's own docstring), .git metadata, __pycache__,
    or any filled-in (non-template) config.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess  # nosec B404 -- runs only the frozen SortViewCollector.exe being packaged, see probe_frozen_runtime_version
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, deploy_manifest, v2_rules

PRODUCT_NAME = "SortView Collector"

# Explicit, not a glob of collector/*.py -- see module docstring. Keep in
# sync with tests/test_collector_build_release.py's own cross-check
# against the directory's actual contents.
#
# Government-readiness audit, Part 6 (release packaging): the 15 Privacy
# Contract v2 modules below moved here FROM BUILD_ONLY_COLLECTOR_FILES.
# collector/run.py's `contract_mode` switch (docs/collector-v2.md) already
# imports v2_run lazily and degrades gracefully (exit 2, "this build does
# not include Contract v2") if that module is ever absent -- this change
# is what makes it PRESENT instead, so a future release can actually run
# v2 when a config asks for it. This alone does NOT enable v2 in
# production: SORTVIEW_V2_INGEST_ENABLED is unchanged (still unset/false),
# no release has been rebuilt or installed anywhere, and collector_config.json's
# contract_mode still defaults to "v1" -- see the government-readiness
# report for the remaining steps (a real 1.0.6 build, an onsite dry-run
# validation, and an operator-issued ingest key) before any machine would
# actually run v2.
#
# collector/identity_collision_diag.py (the identical_identity_events
# onsite diagnostic; see its own docstring and collector/freeze/dispatcher.py's
# `identity-collision-diag` subcommand) is listed here, not in
# BUILD_ONLY_COLLECTOR_FILES, for the same reason: it is a real operator-facing
# tool that must be present on the target machine (source or frozen bundle
# alike) to run there, not a build-time-only module like this file itself.
COLLECTOR_RUNTIME_FILES: tuple[str, ...] = (
    "collector/__init__.py",
    "collector/bootstrap_state.py",
    "collector/config.py",
    "collector/identity_collision_diag.py",
    "collector/parsers.py",
    "collector/preflight.py",
    "collector/reader.py",
    "collector/run.py",
    "collector/run_audit.py",
    "collector/state.py",
    "collector/support_info.py",
    "collector/task_settings.py",
    "collector/uploader.py",
    "collector/v2_classify.py",
    "collector/v2_config.py",
    "collector/v2_events.py",
    "collector/v2_identity.py",
    "collector/v2_keys.py",
    "collector/v2_normalize.py",
    "collector/v2_patrons.py",
    "collector/v2_quarantine.py",
    "collector/v2_reader.py",
    "collector/v2_rules.py",
    "collector/v2_run.py",
    "collector/v2_safe_errors.py",
    "collector/v2_status.py",
    "collector/v2_transform.py",
    "collector/v2_uploader.py",
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
    # Guided first-install finish (token -> both preflights -> bootstrap ->
    # task registered DISABLED). Pure orchestration of the other tools in
    # this list plus SortViewCollector.exe's own subcommands; frozen
    # installs only. Copied verbatim like the rest.
    ("collector/deploy/finish-collector-install.ps1", "tools/finish-install.ps1"),
    # Contract v2 NBPL pilot preparation (dry-run only -- see its own
    # docstring). Copied verbatim like the rest of this tuple; it resolves
    # the packaged PILOT_RULES_DEST artifact from its OWN bundle root
    # (Split-Path $PSScriptRoot -Parent), never a repo-relative path.
    ("collector/deploy/prepare_v2_pilot.ps1", "tools/prepare_v2_pilot.ps1"),
    # Contract v2 production config conversion (contract_mode: v1 -> v2 on
    # the EXISTING collector_config.json; never key creation, never task
    # enabling -- see its own docstring). Copied verbatim like the rest of
    # this tuple; it resolves its own bundle's MANIFEST.json the same way
    # prepare_v2_pilot.ps1 does (Split-Path $PSScriptRoot -Parent).
    ("collector/deploy/configure_v2.ps1", "tools/configure_v2.ps1"),
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

# This 1.0.6 NBPL pilot's packaged classification rules: GENERATED AT BUILD TIME (never
# hand-copied into source control -- see _write_pilot_rules_artifact) from the current
# src/branch_settings.json via collector.v2_rules.seed_from_settings, the same
# seed/normalization logic `python -m collector.v2_rules seed` uses. The bundle-relative
# destination is deliberately under its own pilot/ folder, distinct from
# CONFIG_TEMPLATE_DEST and from the ProgramData runtime path it is installed to
# (tools/prepare_v2_pilot.ps1 copies it there) -- this file is a release-owned SEED
# artifact, not a filled-in runtime config. It holds only staff/service-account names and
# destination/DA-pattern routing labels already present in branch_settings.json -- no
# patron data, no credentials.
PILOT_RULES_SETTINGS_SOURCE = "src/branch_settings.json"
PILOT_RULES_DEST = "pilot/classification_rules.json"

# --- frozen (PyInstaller) bundle mode ---------------------------------
#
# A frozen bundle reuses DEPLOY_TOOL_FILES and CONFIG_TEMPLATE_* verbatim
# (the SAME install/update/register/preflight/uninstall/token scripts --
# each auto-detects source vs. frozen at runtime from what it finds under
# -InstallRoot/the bundle root, rather than this module shipping a second
# parallel set of scripts) -- only the Python source tree and
# requirements.txt are replaced with a copy of an already-built PyInstaller
# onedir output. See build_frozen_release below.
FROZEN_RUNTIME_DEST = "runtime"
FROZEN_EXE_NAME = "SortViewCollector.exe"
FROZEN_INTERNAL_DIR_NAME = "_internal"
# The config-free dispatcher subcommand (collector/freeze/dispatcher.py) that
# prints collector.__version__ -- what the frozen executable ACTUALLY is.
FROZEN_VERSION_SUBCOMMAND = "version"
FROZEN_VERSION_PROBE_TIMEOUT_SECONDS = 60.0
_MAX_PROBE_OUTPUT_PREVIEW_CHARS = 200

# SUPPORT_FILES minus requirements.txt -- a frozen bundle ships no
# requirements to install (there is no venv/pip step at all), but still
# needs install.ps1 itself. Derived from SUPPORT_FILES (not a separately
# maintained tuple) so it can never silently drift from it.
FROZEN_SUPPORT_FILES: tuple[tuple[str, str], ...] = tuple(
    (source_rel, dest_rel) for source_rel, dest_rel in SUPPORT_FILES if dest_rel != "requirements.txt"
)

# Files that ship at the root of FROZEN bundles ONLY. setup.ps1 is the guided,
# enrollment-driven setup: it orchestrates install.ps1 and the tools\ scripts
# and supports frozen bundles alone (it refuses a source bundle before it does
# anything), so a source bundle does not carry it.
FROZEN_ONLY_SUPPORT_FILES: tuple[tuple[str, str], ...] = (
    ("collector/deploy/setup-collector.ps1", "setup.ps1"),
)


class BuildError(Exception):
    """Raised for any build-time problem -- a missing required source
    file, an existing non-empty output directory without -Force, etc.
    Never partially writes a bundle: every source file's existence is
    verified BEFORE any copy begins (see build_release)."""


class FrozenRuntimeError(BuildError):
    """Raised when --frozen-runtime does not point at a valid PyInstaller
    onedir output directory -- see _validate_frozen_runtime_dir. A
    subclass of BuildError (not a separate exception hierarchy) so
    existing callers that only catch BuildError still catch this too."""


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

    # Same treatment for the pilot rules artifact's settings SOURCE (not copied
    # verbatim -- generated, see _write_pilot_rules_artifact): existence verified
    # up front so a missing src/branch_settings.json fails the build loudly too.
    pairs.append((repo_root / PILOT_RULES_SETTINGS_SOURCE, None))

    return pairs


def _copy_required_files(required: list[tuple[Path, str | None]], bundle_dir: Path) -> list[ManifestEntry]:
    """Shared copy-loop used by both build_release and build_frozen_release:
    copies every (src, dest_rel) pair verbatim except dest_rel is None
    entries (existence-only, written separately -- see CONFIG_TEMPLATE_SOURCE),
    returning one ManifestEntry per file actually copied."""
    entries: list[ManifestEntry] = []
    for src, dest_rel in required:
        if dest_rel is None:
            continue
        dest = bundle_dir / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        entries.append(ManifestEntry(dest_rel, _sha256_file(dest), dest.stat().st_size))
    return entries


def _write_config_template(bundle_dir: Path, repo_root: Path) -> ManifestEntry:
    """Writes collector_config.example.json (rewritten comment, not a
    byte-for-byte copy -- see _release_facing_example_config) and returns
    its ManifestEntry. Shared by both build_release and build_frozen_release
    -- every bundle, source or frozen, carries the same config template."""
    config_dest = bundle_dir / CONFIG_TEMPLATE_DEST
    config_text = _release_facing_example_config(repo_root / CONFIG_TEMPLATE_SOURCE)
    config_dest.write_text(config_text, encoding="utf-8", newline="\n")
    config_bytes = config_text.encode("utf-8")
    return ManifestEntry(CONFIG_TEMPLATE_DEST, hashlib.sha256(config_bytes).hexdigest(), len(config_bytes))


def _write_pilot_rules_artifact(bundle_dir: Path, repo_root: Path) -> ManifestEntry:
    """Generates this 1.0.6 NBPL pilot's classification_rules.json at BUILD TIME from
    src/branch_settings.json (v2_rules.seed_from_settings -- the same function
    `python -m collector.v2_rules seed` uses), rather than checking in a hand-copied
    file: the document is deterministic given the settings source, so regenerating it
    is what keeps the packaged artifact from silently drifting out of sync with the
    settings it is supposed to reflect. Written to bundle_dir/PILOT_RULES_DEST exactly
    as v2_rules.main's own `seed` command writes it (json.dumps(..., indent=2)), so a
    build's output is byte-identical to what an operator would get running that CLI
    command by hand against the same settings file. Shared by both build_release and
    build_frozen_release -- every 1.0.6 bundle, source or frozen, carries the same
    pilot artifact for tools/prepare_v2_pilot.ps1 to install."""
    settings_path = repo_root / PILOT_RULES_SETTINGS_SOURCE
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BuildError(
            f"could not read {settings_path} to generate the pilot classification rules: {exc}"
        ) from exc
    try:
        document = v2_rules.seed_from_settings(settings)
    except v2_rules.RulesError as exc:
        raise BuildError(
            f"{settings_path} is not a valid branch-settings document ({exc.code}) -- cannot generate "
            "the pilot classification rules"
        ) from exc

    dest = bundle_dir / PILOT_RULES_DEST
    dest.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2) + "\n"
    dest.write_text(text, encoding="utf-8", newline="\n")
    data = text.encode("utf-8")
    return ManifestEntry(PILOT_RULES_DEST, hashlib.sha256(data).hexdigest(), len(data))


def _write_manifest(
    bundle_dir: Path, version: str, entries: list[ManifestEntry], built_at: str | None
) -> Path:
    """Sorts entries and writes MANIFEST.json -- shared final step for
    both build_release and build_frozen_release, so the manifest's own
    shape (and how `built_at` is defaulted) can never drift between the
    two bundle kinds."""
    entries.sort(key=lambda e: e.path)
    manifest = {
        "product": PRODUCT_NAME,
        "version": version,
        "built_at": built_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "files": [{"path": e.path, "sha256": e.sha256, "size_bytes": e.size_bytes} for e in entries],
    }
    manifest_path = bundle_dir / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest_path


def _new_bundle_dir(output_dir: Path, version: str, *, force: bool) -> Path:
    """Resolves and prepares <output_dir>/SortViewCollector-<version>/,
    refusing (BuildError) if it already exists unless `force` -- shared by
    both build_release and build_frozen_release."""
    bundle_dir = output_dir / f"SortViewCollector-{version}"
    if bundle_dir.exists():
        if not force:
            raise BuildError(
                f"Output directory already exists: {bundle_dir} -- pass force=True / --force to rebuild it."
            )
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True)
    return bundle_dir


def _require_version_matches_source(version: str, repo_root: Path) -> None:
    """The release-version assertion shared by build_release and
    build_frozen_release: `version` must equal collector.__version__ EXACTLY
    (no trimming, no normalization). Called before any output directory is
    created or removed, so a mismatch can never touch an existing bundle.

    collector.__version__ is what THIS running package reports; the guard
    below makes sure that is also the package being packaged, so pointing
    --repo-root at a different checkout cannot reintroduce the drift this
    check exists to close (a source bundle ships repo_root's own
    collector/__init__.py)."""
    running_init = Path(__file__).resolve().parent / "__init__.py"
    packaged_init = repo_root / "collector" / "__init__.py"
    if packaged_init.is_file() and not packaged_init.samefile(running_init):
        raise BuildError(
            f"repo root {repo_root} contains a different collector package ({packaged_init}) than the "
            f"one running this build ({running_init}), so collector.__version__ cannot be checked "
            "against it. Run build_release from that checkout (python -m collector.build_release)."
        )
    if version != __version__:
        raise BuildError(
            f"Requested release version {version!r} does not match collector.__version__ "
            f"{__version__!r} (collector/__init__.py). collector.__version__ is the single authoritative "
            "Collector version and --version only asserts it; nothing here rewrites it. To release a "
            "different version, change collector.__version__ first (and rebuild the frozen runtime), "
            "then request that version."
        )


def _parse_frozen_version_output(stdout: str, exe_path: Path) -> str:
    """The `version` command prints exactly the version and a newline. Anything
    else -- nothing, whitespace only, several words/lines -- is malformed and
    refused rather than guessed at."""
    value = stdout.strip()
    if not value:
        raise FrozenRuntimeError(f"{exe_path} `{FROZEN_VERSION_SUBCOMMAND}` printed no version (blank output).")
    if any(ch.isspace() for ch in value):
        preview = stdout[:_MAX_PROBE_OUTPUT_PREVIEW_CHARS]
        raise FrozenRuntimeError(
            f"{exe_path} `{FROZEN_VERSION_SUBCOMMAND}` printed malformed output (expected exactly a "
            f"version): {preview!r}"
        )
    return value


def probe_frozen_runtime_version(exe_path: Path, *, timeout: float = FROZEN_VERSION_PROBE_TIMEOUT_SECONDS) -> str:
    """Runs `<exe_path> version` and returns the version it reports. Raises
    FrozenRuntimeError -- never returns a guess -- if the executable cannot
    be started, times out, exits nonzero, or prints blank/malformed output.

    Runs only the SortViewCollector.exe the caller is about to package, with
    a fixed argument list (no shell, no caller-controlled arguments)."""
    try:
        completed = subprocess.run(  # nosec B603
            [str(exe_path), FROZEN_VERSION_SUBCOMMAND],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FrozenRuntimeError(
            f"{exe_path} `{FROZEN_VERSION_SUBCOMMAND}` did not finish within {timeout:g}s."
        ) from exc
    except OSError as exc:
        raise FrozenRuntimeError(
            f"could not run {exe_path} `{FROZEN_VERSION_SUBCOMMAND}` to check its version: {exc}"
        ) from exc

    if completed.returncode != 0:
        stderr_preview = (completed.stderr or "").strip()[:_MAX_PROBE_OUTPUT_PREVIEW_CHARS]
        raise FrozenRuntimeError(
            f"{exe_path} `{FROZEN_VERSION_SUBCOMMAND}` exited with code {completed.returncode} "
            f"(expected 0). stderr: {stderr_preview!r}"
        )
    return _parse_frozen_version_output(completed.stdout or "", exe_path)


def _verify_frozen_runtime_version(
    frozen_runtime_dir: Path, expected_version: str, version_probe: Callable[[Path], str] | None
) -> None:
    """Proves the runtime being packaged NOW reports exactly `expected_version`.
    Deliberately independent of build_frozen.ps1's own check right after the
    PyInstaller build: that proves a fresh binary matched the source when it
    was built; this proves the binary handed to the packager still does."""
    probe = version_probe if version_probe is not None else probe_frozen_runtime_version
    exe_path = frozen_runtime_dir / FROZEN_EXE_NAME
    actual = probe(exe_path)
    if actual != expected_version:
        raise FrozenRuntimeError(
            f"Frozen runtime version mismatch: {exe_path} reports {actual!r} but the release version is "
            f"{expected_version!r} (collector.__version__). The PyInstaller runtime is stale or was built "
            "from different source -- rebuild it with collector/freeze/build_frozen.ps1 and pass that output."
        )


def _require_release_readiness(repo_root: Path, version: str) -> None:
    """Refuses to build from a tree that fails scripts/check_release_readiness.py: collector.__version__ is the
    one version authority, the packaging/release tooling derives from it, and no current-state test carries a
    stale release literal or depends on today's wall clock. Called from the CLI before anything is built (so
    nothing is created or removed on failure). The check itself is scripts/check_release_readiness.py -- loaded
    from `repo_root`, so there is exactly one implementation of it, shared with CI."""
    script = repo_root / "scripts" / "check_release_readiness.py"
    if not script.is_file():
        raise BuildError(
            f"Refusing to build -- the release-readiness check ({script}) is missing, so the release cannot be "
            "verified against collector.__version__ and the test-freshness rules."
        )
    spec = importlib.util.spec_from_file_location("sortview_check_release_readiness", script)
    if spec is None or spec.loader is None:
        raise BuildError(f"Refusing to build -- {script} could not be loaded.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolves string annotations through sys.modules
    try:
        spec.loader.exec_module(module)
        findings = module.check_release_readiness(repo_root, expected_version=version)
    finally:
        sys.modules.pop(spec.name, None)
    if findings:
        raise BuildError(
            "Refusing to build -- the tree is not release-ready (python scripts/check_release_readiness.py "
            "shows the same):\n" + "\n".join(f"  {finding}" for finding in findings)
        )


def build_release(
    repo_root: Path,
    output_dir: Path,
    version: str,
    *,
    force: bool = False,
    built_at: str | None = None,
) -> BuildResult:
    """Builds a standalone SOURCE release bundle at
    <output_dir>/SortViewCollector-<version>/ -- a Python source tree
    plus requirements.txt, installed by install.ps1 into a fresh venv. See
    build_frozen_release for the PyInstaller (no-Python-required) bundle kind.

    `version` is an ASSERTION, not a source of truth: it must equal
    collector.__version__ exactly or BuildError is raised (see
    _require_version_matches_source). The bundle directory name and
    MANIFEST.json["version"] are derived from that validated value.

    Fails loudly (BuildError) and writes NOTHING if the version does not
    match or any required source file is missing -- both are checked up
    front, before the target directory is even created (or, with force,
    removed), so a partially-built bundle is never left behind by a
    mid-build failure and an existing bundle is never destroyed by a
    failing build. Deterministic given a fixed `built_at` (accepted so tests
    don't depend on wall-clock time); defaults to the current UTC time.
    """
    if not version.strip():
        raise BuildError("version must not be empty")
    _require_version_matches_source(version, repo_root)

    required = _required_source_files(repo_root)
    missing = [str(src) for src, _dest in required if not src.is_file()]
    if missing:
        raise BuildError(
            "Refusing to build -- required source file(s) missing:\n" + "\n".join(f"  {m}" for m in missing)
        )

    bundle_dir = _new_bundle_dir(output_dir, version, force=force)

    entries = _copy_required_files(required, bundle_dir)
    entries.append(_write_config_template(bundle_dir, repo_root))
    entries.append(_write_pilot_rules_artifact(bundle_dir, repo_root))

    manifest_path = _write_manifest(bundle_dir, version, entries, built_at)

    return BuildResult(bundle_dir=bundle_dir, manifest_path=manifest_path, entries=tuple(entries))


def _validate_frozen_runtime_dir(frozen_runtime_dir: Path) -> None:
    """Fails loudly (FrozenRuntimeError) unless frozen_runtime_dir looks
    like a real PyInstaller onedir output directory -- checked BEFORE any
    bundle output directory is created, same fail-closed contract as
    build_release's own missing-source check. Deliberately does NOT
    build PyInstaller itself (see build_frozen_release's own docstring for
    why) -- this only validates an ALREADY-built runtime."""
    if not frozen_runtime_dir.is_dir():
        raise FrozenRuntimeError(f"--frozen-runtime is not a directory: {frozen_runtime_dir}")
    if not any(frozen_runtime_dir.iterdir()):
        raise FrozenRuntimeError(f"--frozen-runtime directory is empty: {frozen_runtime_dir}")
    exe_path = frozen_runtime_dir / FROZEN_EXE_NAME
    if not exe_path.is_file():
        raise FrozenRuntimeError(
            f"--frozen-runtime does not contain {FROZEN_EXE_NAME}: {frozen_runtime_dir} -- "
            "build it first with collector/freeze/build_frozen.ps1, then pass its dist\\SortViewCollector\\ "
            "output directory here."
        )
    internal_dir = frozen_runtime_dir / FROZEN_INTERNAL_DIR_NAME
    if not internal_dir.is_dir():
        raise FrozenRuntimeError(
            f"--frozen-runtime does not contain a {FROZEN_INTERNAL_DIR_NAME}\\ directory: {frozen_runtime_dir} -- "
            "this does not look like a real PyInstaller onedir output."
        )


def _copy_frozen_runtime(frozen_runtime_dir: Path, bundle_dir: Path) -> list[ManifestEntry]:
    """Copies the already-validated frozen runtime directory's ENTIRE
    contents into <bundle_dir>/runtime/, deterministically (shutil.copytree
    onto a destination that does not yet exist -- no overlay, no stale
    files possible since bundle_dir was just freshly created), and returns
    one ManifestEntry per file, hashed from the COPIED destination (not
    the source) so the manifest reflects exactly what shipped."""
    dest_runtime_dir = bundle_dir / FROZEN_RUNTIME_DEST
    shutil.copytree(frozen_runtime_dir, dest_runtime_dir)

    entries: list[ManifestEntry] = []
    for path in sorted(dest_runtime_dir.rglob("*")):
        if path.is_file():
            rel = path.relative_to(bundle_dir).as_posix()
            entries.append(ManifestEntry(rel, _sha256_file(path), path.stat().st_size))
    return entries


def _required_frozen_deploy_files(repo_root: Path) -> list[tuple[Path, str | None]]:
    """Same shape as _required_source_files, but for a FROZEN bundle's
    non-runtime files only: the same deploy tool scripts (DEPLOY_TOOL_FILES
    -- each one auto-detects source vs. frozen at runtime, see this
    module's own docstring), install.ps1 but NOT requirements.txt
    (FROZEN_SUPPORT_FILES), and the config template. Deliberately does NOT
    include COLLECTOR_RUNTIME_FILES or deploy_manifest.PARSER_RUNTIME_FILES
    -- a frozen bundle never ships the Python source tree at all."""
    pairs: list[tuple[Path, str | None]] = []

    for source_rel, dest_rel in DEPLOY_TOOL_FILES:
        pairs.append((repo_root / source_rel, dest_rel))

    for source_rel, dest_rel in FROZEN_SUPPORT_FILES + FROZEN_ONLY_SUPPORT_FILES:
        pairs.append((repo_root / source_rel, dest_rel))

    pairs.append((repo_root / CONFIG_TEMPLATE_SOURCE, None))
    pairs.append((repo_root / PILOT_RULES_SETTINGS_SOURCE, None))

    return pairs


def build_frozen_release(
    repo_root: Path,
    output_dir: Path,
    version: str,
    frozen_runtime_dir: Path,
    *,
    force: bool = False,
    built_at: str | None = None,
    version_probe: Callable[[Path], str] | None = None,
) -> BuildResult:
    """Builds a standalone FROZEN release bundle at
    <output_dir>/SortViewCollector-<version>/ -- a PyInstaller onedir
    runtime (no Python/pip/venv required on the target machine at all)
    plus the same deploy tooling as build_release.

    REQUIRES an already-built PyInstaller onedir output (frozen_runtime_dir,
    e.g. dist\\SortViewCollector\\ produced by collector/freeze/build_frozen.ps1)
    -- this function deliberately never invokes PyInstaller itself. Building
    PyInstaller takes ~25s, needs its own isolated packaging venv (built
    from collector/deploy/requirements.txt, never this repo's dev .venv --
    see collector/freeze/build_frozen.ps1's own docstring), and is a
    distinct, independently-verifiable step; collapsing it into this
    function would make a release build silently depend on whichever
    Python happens to be on THIS machine's PATH at build_release.py
    invocation time, exactly the kind of hidden environment coupling the
    isolated packaging venv exists to avoid.

    `version` is an ASSERTION, not a source of truth (see build_release): it
    must equal collector.__version__ exactly. Because this function packages
    an ALREADY-BUILT runtime, that alone is not enough -- the runtime could
    have been built from older source -- so the supplied SortViewCollector.exe
    is also run with its config-free `version` command and must report that
    same version (`version_probe` replaces that subprocess call, for tests).

    Fails loudly and writes NOTHING if the version does not match, the
    runtime reports a different/blank/malformed version or fails to run,
    frozen_runtime_dir is invalid (see _validate_frozen_runtime_dir), or any
    deploy-tool source file is missing -- all checked before any output
    directory is created (or, with force, removed), same fail-closed
    contract as build_release.
    """
    if not version.strip():
        raise BuildError("version must not be empty")
    _require_version_matches_source(version, repo_root)

    _validate_frozen_runtime_dir(frozen_runtime_dir)

    required = _required_frozen_deploy_files(repo_root)
    missing = [str(src) for src, _dest in required if not src.is_file()]
    if missing:
        raise BuildError(
            "Refusing to build -- required source file(s) missing:\n" + "\n".join(f"  {m}" for m in missing)
        )

    _verify_frozen_runtime_version(frozen_runtime_dir, version, version_probe)

    bundle_dir = _new_bundle_dir(output_dir, version, force=force)

    entries = _copy_required_files(required, bundle_dir)
    entries.extend(_copy_frozen_runtime(frozen_runtime_dir, bundle_dir))
    entries.append(_write_config_template(bundle_dir, repo_root))
    entries.append(_write_pilot_rules_artifact(bundle_dir, repo_root))

    manifest_path = _write_manifest(bundle_dir, version, entries, built_at)

    return BuildResult(bundle_dir=bundle_dir, manifest_path=manifest_path, entries=tuple(entries))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a self-contained SortView Collector release bundle -- no Git, "
        "no repository checkout, and no development environment required on the target "
        "machine to install from the result."
    )
    parser.add_argument("--output", required=True, help="Directory to build the release bundle into")
    parser.add_argument(
        "--version",
        required=True,
        help="Release version ASSERTION -- must exactly equal collector.__version__ (the single "
        "authoritative Collector version, in collector/__init__.py), or the build fails before "
        "creating anything. It never changes the version; it only confirms you are releasing "
        "what the source says.",
    )
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parent.parent),
        help="Repository root (defaults to two levels up from this file)",
    )
    parser.add_argument("--force", action="store_true", help="Remove and rebuild an existing output directory")
    parser.add_argument(
        "--frozen-runtime",
        help="Path to an ALREADY-BUILT PyInstaller onedir output directory (e.g. "
        "dist\\SortViewCollector\\, produced separately by collector/freeze/build_frozen.ps1) -- "
        "if given, builds a FROZEN bundle (no Python/pip/venv required on the target machine) "
        "instead of the default source bundle. This flag never triggers a PyInstaller build itself.",
    )
    args = parser.parse_args(argv)

    try:
        # Version assertion first (its message is the specific one), then the release-readiness check --
        # both before either build function creates or removes anything.
        _require_version_matches_source(args.version, Path(args.repo_root))
        _require_release_readiness(Path(args.repo_root), args.version)
        if args.frozen_runtime:
            result = build_frozen_release(
                Path(args.repo_root), Path(args.output), args.version,
                Path(args.frozen_runtime), force=args.force,
            )
        else:
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
