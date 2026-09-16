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
"""

from __future__ import annotations

import argparse
import sys

from . import reader, state
from .config import CollectorConfig, ConfigError, load_config


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
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

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
