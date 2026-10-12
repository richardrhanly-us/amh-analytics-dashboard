"""Production first-run bootstrap (real-AMH deployment finding).

Seeds collector/state.py's state.json at the current, complete-line-safe
end of each configured source file -- WITHOUT ever reading a record's
fields, constructing an upload payload, making a network call, or
writing to a source file. Run this ONCE, explicitly, by an operator,
before the very first scheduled/manual `collector.run` on a real
production install.

WHY THIS EXISTS: Collector v1 has no equivalent of the continuous
agent's NORMAL bootstrap mode (agent/README.md: "seeds at the source
file's current end-of-file. Historical content already in the file is
never read."). Absent this tool, a brand-new install's first
`collector.run` starts every source with cursor=None ->
collector/reader.py's read_new_lines(path, None) -> start_offset = 0 --
and would parse and upload the ENTIRE existing historical content of
every configured Tech Logic file, not just records going forward. This
was discovered on the real AMH machine before the first production run
and worked around by hand; this module is the supported replacement for
that manual step.

WHY NOT SEED TO THE RAW CURRENT FILE SIZE (os.path.getsize()): Tech
Logic can be mid-write at the exact moment this runs -- a real, observed
condition on the AMH machine (ACS Log.txt sampled with 6 trailing
unconsumed bytes: current_size=8230605, the safe complete-line offset
was 8230599). Seeding to the raw file size would silently skip whatever
complete record eventually lands inside that trailing partial line once
Tech Logic finishes writing it, since the persisted offset would already
be past those bytes. This module calls
collector.reader.read_new_lines(path, None) -- the EXACT SAME
complete-line-safe boundary calculation an ordinary collector.run
already performs on a source with no prior cursor -- so the seeded
offset is precisely where a normal first run would have stopped, never
past a byte that might still be mid-write. Partial-line safety is
therefore inherited, not reimplemented.

FAIL CLOSED on any configured source that is missing or unreadable at
bootstrap time: raises rather than silently seeding a partial state, so
a wrong source path is caught by the operator running this command
before production traffic starts, not discovered later as silently-
missing data.

ALWAYS REFUSES TO OVERWRITE an existing state.json -- unconditionally,
with no override flag of any kind. If state.json exists, the Collector
has already been initialized on this install: either it has already run
and collected real data, or it was bootstrapped once before. In either
case, re-bootstrapping would recompute a fresh safe-EOF position and
overwrite the real, already-committed cursor -- silently skipping
whatever legitimate, unprocessed data sits between the old committed
offset and the new one. This is a first-run tool for a genuinely
uninitialized install ONLY, not a reset tool. There is deliberately no
--force, no confirmation-prompt bypass, and no re-bootstrap option in
this module -- an operator who believes state.json is genuinely wrong
(corrupt, from a failed install, etc.) must investigate and recover it
as its own deliberate action (see collector/state.py's own
quarantine_corrupt_file for the pattern that already exists for a
corrupt file specifically), never by re-running this command over it.

CONTRACT V2 (`contract_mode: "v2"`): the same command seeds the v2 cursor
instead -- `v2.state_path` (default <root>\\data\\v2\\state_v2.json), in
the same collector/state.py document, at the same complete-line-safe
offsets. Everything above is v1 and is unchanged; an absent
`contract_mode` is still v1. The v2 path differs in four ways:

  - it REFUSES (exit 3) when the v2 state file already exists, AND when
    it does not but the legacy v1 state.json does. The second case is a
    machine that has run v1: it may be an in-place v1-to-v2 conversion
    that needs continuity from the v1 cursor, and seeding at end-of-file
    would silently skip every record between that cursor and the end of
    the file. This tool does not guess which it is. Neither file is
    read, changed or removed;
  - only the v2 source names (acs, checkins, rejects) are accepted --
    the same rule a v2 run enforces (`unknown_source`);
  - each source is identified again after it was read, and a source that
    was replaced or shrank in the meantime fails the bootstrap;
  - it writes nothing but the cursor: no patron cache, quarantine, status
    file, rules or secret, and it opens no network session.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import reader, state
from .config import CollectorConfig, ConfigError, load_config

# v2 only: a state file that makes a fresh initialization unsafe (or already done). v1 keeps exit 2 for its own refusal.
EXIT_REFUSED = 3


def _source_state_from_cursor(cursor: reader.SourceCursor) -> state.SourceState:
    """Same conversion as collector/run.py's identically-named private
    function -- duplicated deliberately (four lines, independently
    tested in both places) rather than importing a private name across
    modules. Matches this package's own established preference for
    small, independently-verified primitives over cross-module coupling
    (see collector/__init__.py's architecture docstring)."""
    identity = cursor.identity.token if cursor.identity is not None else None
    return state.SourceState(identity=identity, offset=cursor.offset)


class BootstrapError(Exception):
    """Base class for every error this module raises deliberately."""


class ExistingStateError(BootstrapError):
    """Raised, always, whenever state.json already exists. See module
    docstring's ALWAYS REFUSES TO OVERWRITE section -- there is no
    override for this, in this module, in any form."""


def bootstrap_state(cfg: CollectorConfig) -> state.CollectorState:
    """Seeds a fresh CollectorState for every source in `cfg`, each at
    that source's current complete-line-safe offset, and persists it via
    collector.state.save_state (the same atomic-write primitive
    collector.run itself uses). Returns the state that was written.

    Raises ExistingStateError, unconditionally, if cfg.state_path already
    exists -- see module docstring. Raises BootstrapError if any
    configured source is missing or unreadable -- never seeds a partial
    result.
    """
    if cfg.state_path.exists():
        raise ExistingStateError(
            f"state file already exists: {cfg.state_path}\n"
            "\n"
            "This means the Collector has already been initialized on this install --\n"
            "either it has already run and collected real data, or it was bootstrapped\n"
            "once before. collector.bootstrap_state is only for a genuinely uninitialized\n"
            "first install: it is refusing to run rather than recompute a fresh safe-EOF\n"
            "position and overwrite the real, already-committed cursor, which could\n"
            "silently skip legitimate unprocessed data sitting between the old committed\n"
            "offset and a newly-computed one.\n"
            "\n"
            "There is no override flag for this. If you believe this state.json is\n"
            "genuinely wrong (e.g. left over from a failed install, never a real,\n"
            "currently-collecting install), investigate and recover it yourself as a\n"
            "separate, deliberate action -- do not re-run this command over it."
        )

    new_state = state.empty_state()
    for source_cfg in cfg.sources:
        result = reader.read_new_lines(source_cfg.path, None)
        if not result.existed:
            raise BootstrapError(
                f"source {source_cfg.name!r} not found or unreadable: {source_cfg.path} -- "
                "refusing to seed a partial bootstrap. Fix the source path and retry."
            )
        new_state = state.with_source(new_state, source_cfg.name, _source_state_from_cursor(result.cursor))

    state.save_state(cfg.state_path, new_state)
    return new_state


class UnsafeStateError(BootstrapError):
    """Contract v2 only: an existing state file makes a fresh v2 initialization unsafe or already done. `reason` is a
    fixed code -- `v2_state_exists` or `legacy_state_exists` -- and there is no override, exactly as for v1."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def bootstrap_v2_state(cfg: CollectorConfig, v2_state_path: Path) -> state.CollectorState:
    """Contract v2's bootstrap_state: seeds `v2_state_path` -- and nothing else -- with every source in `cfg` at its
    current complete-line-safe offset, found by the same reader call as v1. See the module docstring's CONTRACT V2
    section. The whole document is built in memory and written once, atomically: any failure leaves no state file.

    Raises ConfigError for a source name v2 does not know, UnsafeStateError when the v2 state or a legacy v1 state
    already exists, and BootstrapError for a missing, unreadable or changing source or a failed write."""
    # v2 only: the v1 path never loads a v2 module.
    from .v2_transform import SOURCE_ORDER

    unknown = [source_cfg.name for source_cfg in cfg.sources if source_cfg.name not in SOURCE_ORDER]
    if unknown:
        raise ConfigError(
            f"config 'sources' has name(s) Contract v2 does not recognize: {', '.join(unknown)} "
            f"(recognized: {', '.join(SOURCE_ORDER)})"
        )

    if v2_state_path.exists():
        raise UnsafeStateError(
            "v2_state_exists",
            f"v2 state file already exists: {v2_state_path}\n"
            "Contract v2 has already been initialized on this install. There is no override: re-seeding at\n"
            "end-of-file would overwrite the committed cursor and could skip unprocessed records.",
        )
    if cfg.state_path.exists():
        raise UnsafeStateError(
            "legacy_state_exists",
            f"a legacy v1 state file exists ({cfg.state_path}) and no v2 state file does ({v2_state_path}).\n"
            "v2 bootstrap will not infer whether this is a fresh install or a v1-to-v2 conversion: a conversion\n"
            "may need continuity from the v1 cursor, and seeding at end-of-file would skip every record between\n"
            "that cursor and the end of each file. Neither state file was changed. There is no override.",
        )

    new_state = state.empty_state()
    for source_cfg in cfg.sources:
        result = reader.read_new_lines(source_cfg.path, None)
        if not result.existed:
            raise BootstrapError(
                f"source {source_cfg.name!r} not found or unreadable: {source_cfg.path} -- "
                "refusing to seed a partial bootstrap. Fix the source path and retry."
            )
        # The offset is only meaningful for the file it was read from: one that was replaced (rotated) or shrank
        # (truncated) while it was being read would be seeded with a cursor that belongs to something else.
        try:
            size_now = os.path.getsize(source_cfg.path)
        except OSError:
            size_now = -1
        if reader.identify(source_cfg.path) != result.cursor.identity or size_now < result.cursor.offset:
            raise BootstrapError(
                f"source {source_cfg.name!r} was replaced or truncated while it was being read: {source_cfg.path} -- "
                "refusing to seed. Nothing was written; retry."
            )
        new_state = state.with_source(new_state, source_cfg.name, _source_state_from_cursor(result.cursor))

    try:
        state.save_state(v2_state_path, new_state)
    except OSError as exc:
        raise BootstrapError(
            f"could not write the v2 state file {v2_state_path} ({type(exc).__name__}) -- nothing was seeded."
        ) from None
    return new_state


def _main_v2(cfg: CollectorConfig, config_path: str) -> int:
    """`bootstrap` for a `contract_mode: "v2"` config. 0 seeded, 1 failed, 2 configuration, 3 refused (EXIT_REFUSED)."""
    try:
        from .v2_config import load_v2_config  # v2 only

        v2 = load_v2_config(config_path, require=True)
        if v2 is None:  # unreachable with require=True; keeps the type honest without an assert
            return 2
        new_state = bootstrap_v2_state(cfg, v2.state_path)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except UnsafeStateError as exc:
        print(f"Refusing to bootstrap (reason={exc.reason}): {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except BootstrapError as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        return 1

    print(f"Seeded v2 state at {v2.state_path}:")
    for name, source_state in new_state.sources.items():
        print(f"  {name}: offset={source_state.offset} identity={source_state.identity}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SortView Collector -- production first-run bootstrap. Seeds "
        "state.json at the current, complete-line-safe end of each configured source "
        "file. Never parses, uploads, or modifies source data. Run once, before the "
        "first scheduled/manual collector.run on a fresh production install."
    )
    parser.add_argument("--config", required=True, help="Path to the collector config JSON file")
    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)
        # After load_config, so a v1 config's own errors are reported exactly as before. Absent means "v1".
        from .v2_config import read_contract_mode

        mode = read_contract_mode(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if mode == "v2":
        return _main_v2(cfg, args.config)

    try:
        new_state = bootstrap_state(cfg)
    except ExistingStateError as exc:
        print(f"Refusing to bootstrap: {exc}", file=sys.stderr)
        return 2
    except BootstrapError as exc:
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        return 1

    print(f"Seeded state at {cfg.state_path}:")
    for name, source_state in new_state.sources.items():
        print(f"  {name}: offset={source_state.offset} identity={source_state.identity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
