"""Contract v2 diagnostic: identity-collision report AND bounded onsite acceptance gate (docs/collector-v2.md). Deliberately named
without the `v2_` prefix so it is excluded from every `collector/v2_*.py` glob the release tooling and its tests use (see
build_release.py, test_v2_release_manifest.py) -- this is an onsite troubleshooting tool for `identical_identity_events`, not part
of Collector ingestion and never scheduled to run.

Reachable two ways, both forwarding to this module's own `main(argv)`, never a copy of its logic (same convention as every other
collector/*.py CLI entry point -- see collector/freeze/dispatcher.py's own docstring):

    python -m collector.identity_collision_diag --config <path>      (source checkout)
    SortViewCollector.exe identity-collision-diag --config <path>    (frozen install -- no Python needed on the target machine;
                                                                       collector/freeze/dispatcher.py forwards to this module's
                                                                       main(), and collector/build_release.py bundles this file
                                                                       into both the source and frozen release manifests)

WHAT THIS EXPLAINS. `run --v2-dry-run` prints one aggregate `identical_identity_events` count. This script re-runs the exact same
transform -- `v2_config.load_dry_run_settings` + `v2_transform.read_tail_chunk`, a throwaway in-memory key, an in-memory patron
cache, zero network calls, zero use of the persistent secret -- against the SAME source files and tail window, but instead of
discarding the built events after counting them, it groups them by `event_key` and reports AGGREGATE facts about the collision
groups: how many groups, how big, how many distinct items and timestamps they touch, and which normalized category (destination,
bin, error_class, ACS state/flags) their members carry.

THE BOUNDED ACCEPTANCE GATE (below the per-source detail, as `=== gate ===`). A bare `identical_identity_events == 0` requirement
is wrong: the collector's own documented, tested design intentionally collapses two DIFFERENT cases onto one `event_key` --
(a) an ACS hold read twice, unchanged, in the same second (v2_transform.py's "the same hold read again: keep its identity"), and
(b) two barcode-less reject records (no `item_key`) in the same second with the same normalized `error_class` -- the same
safe-field collapse v1's semantic keys already had (see v2_transform.py's own module docstring). Neither is a hash collision or a
design flaw; both were found, on the real NBPL AMH logs, to be exactly what explains a non-zero `identical_identity_events`. This
gate is source/category-aware instead of a flat zero check, so it can tell "expected collapse" from "something to investigate"
without a human reading the per-source detail on every normal run -- but every input it uses is a count, a null-check or a ratio
of counts already computed above; nothing here is a new PII surface, and nothing here changes what counts as a collision.

    GATE RULES (each independently fails the gate closed; see MAX_GROUP_SIZE / MAX_KEYLESS_REJECT_COLLISION_RATE /
    MIN_COLLISION_TIME_SPREAD / PERMITTED_KEYLESS_REJECT_ERROR_CLASSES below for the exact constants):
      1. No collision group may exceed MAX_GROUP_SIZE (2) members, in ANY source.
      2. An item-keyed collision group (a real barcode on both sides) is permitted ONLY for the documented ACS-hold-duplicate
         case: source=acs, state=hold, group size exactly 2 (already implied by rule 1) -- its destination/flags are identical
         BY CONSTRUCTION of a collision (they are part of the hold's canonical form). Any other item-keyed collision -- a
         checkin, a reject with a barcode, or a non-hold ACS record -- fails the gate.
      3. A keyless collision group (no barcode on either side) is permitted ONLY in the rejects source, and only when its
         `error_class` is one of PERMITTED_KEYLESS_REJECT_ERROR_CLASSES. A keyless collision anywhere else (acs, checkins) or
         under any other error_class fails the gate. Permitted reject collisions are additionally bounded:
           a. keyless_reject_collision_rate (= rejects' identical_identity_events / rejects' events_total) must stay
              <= MAX_KEYLESS_REJECT_COLLISION_RATE.
           b. collision_time_spread (= rejects' distinct_event_times_in_collisions / rejects' collision_groups) must stay
              >= MIN_COLLISION_TIME_SPREAD -- collisions must land on mostly-distinct seconds, not stack onto a handful of
              timestamps (a stacked pattern would instead suggest a frozen/rounded clock, not sporadic barcode-less duplicates).

    This module never invents a network call, a secret read, or a state/cursor write to compute any of the above -- the gate is
    pure arithmetic over the SAME safe aggregates the per-source block already prints, evaluated once the whole tail window (all
    three sources) has been read. `identical_identity_events` is always printed regardless of the gate's verdict -- it is never
    hidden, zeroed or suppressed.

WHY THIS IS SAFE. Every field it inspects is already a `SafeEvent` -- the same privacy-safe, self-validating type
(collector/v2_events.py) the collector would upload to the cloud: a keyed HMAC-SHA256 item_key/event_key (not reversible without
the already-discarded throwaway key), a UTC timestamp, and closed-enum categories (destination slug, bin code, error_class, ACS
state, three boolean flags). v2_transform never returns a raw line, a barcode, a patron identifier or name, a title or a call
number to begin with (see its module docstring) -- there is no PII in scope for this script to leak. It never prints a raw value,
only counts, set sizes, ratios and a tally of category labels -- never an `event_key` or `item_key` value itself.

USAGE.   python -m collector.identity_collision_diag --config <path to the collector config the pilot's --v2-dry-run reads>

Reads only the Tech Logic source files named in that config's dry-run `sources`/`v2` sections. Writes nothing, opens no secret,
state, status, quarantine or log file, makes no network call.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TextIO

from .config import ConfigError
from .v2_config import load_dry_run_settings
from .v2_events import AcsItemV2, CheckinV2, RejectV2, SafeEvent
from .v2_identity import derive_subkeys
from .v2_patrons import PatronCache
from .v2_rules import load_rules
from .v2_safe_errors import CollectorV2Error, describe
from .v2_transform import SOURCE_ORDER, TransformContext, read_tail_chunk

# --- the bounded acceptance gate's constants (see the module docstring for the full rationale) -------------------------------

MAX_GROUP_SIZE = 2
MAX_KEYLESS_REJECT_COLLISION_RATE = 0.12
MIN_COLLISION_TIME_SPREAD = 0.90
PERMITTED_KEYLESS_REJECT_ERROR_CLASSES = frozenset({"ils_acs_failure", "rfid_collision"})


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _category(event: SafeEvent) -> str:
    """The normalized, non-PII category label a colliding event carries -- never a value derived from raw or personal data."""
    if isinstance(event, CheckinV2):
        return f"destination={event.destination} bin={event.bin}"
    if isinstance(event, RejectV2):
        return f"error_class={event.error_class}"
    if isinstance(event, AcsItemV2):
        if event.state == "hold":
            return (f"state=hold destination={event.destination} is_ill={event.is_ill} "
                    f"is_branch_services={event.is_branch_services} is_collection_services={event.is_collection_services}")
        return f"state={event.state}"
    return "unknown"  # pragma: no cover - SafeEvent is a closed set of the three types above


@dataclass(frozen=True)
class SourceCollisionStats:
    """One source's tail-window transform, reduced to the aggregates both the per-source report and the gate need. `collisions`
    holds only `event_key -> events` groups with more than one member -- never a single, non-colliding event."""

    source: str
    events_total: int
    identical_identity_events: int
    collisions: dict[str, list[SafeEvent]] = field(default_factory=dict)

    @property
    def collision_groups(self) -> int:
        return len(self.collisions)

    @property
    def distinct_event_times(self) -> set:
        return {e.event_time for events in self.collisions.values() for e in events}


def _collect_source(source_name: str, path: str, ctx: TransformContext, tail_bytes: int) -> SourceCollisionStats:
    chunk = read_tail_chunk(source_name, path, ctx, tail_bytes)
    groups: dict[str, list[SafeEvent]] = defaultdict(list)
    for event in chunk.events:
        groups[event.event_key].append(event)
    collisions = {key: events for key, events in groups.items() if len(events) > 1}
    return SourceCollisionStats(source_name, len(chunk.events), chunk.counters.identical_identity_events, collisions)


def _print_source(stats: SourceCollisionStats, out: TextIO) -> None:
    print(f"=== {stats.source} ===", file=out)
    print(f"events_total={stats.events_total}", file=out)
    print(f"identical_identity_events={stats.identical_identity_events}", file=out)
    print(f"collision_groups={stats.collision_groups}", file=out)
    if not stats.collisions:
        return

    sizes = sorted(len(events) for events in stats.collisions.values())
    print(f"group_size_histogram={dict(sorted(Counter(sizes).items()))}", file=out)
    distinct_items = {e.item_key for events in stats.collisions.values() for e in events if e.item_key}
    print(f"distinct_item_keys_in_collisions={len(distinct_items)}", file=out)
    print(f"distinct_event_times_in_collisions={len(stats.distinct_event_times)}", file=out)
    print("category_tally:", file=out)
    tally = Counter(_category(e) for events in stats.collisions.values() for e in events)
    for label, count in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {count}\t{label}", file=out)


# --- the bounded acceptance gate ------------------------------------------------------------------------------------------------

def _classify_group(source: str, events: list[SafeEvent]) -> str:
    """One of "oversized", "ok_acs_hold_pair", "ok_keyless_reject", "unexpected_item_keyed", "unexpected_keyless" -- see the
    module docstring's GATE RULES for what each means. `events` is one collision group (all sharing one event_key, so all the
    same source, the same item_key-or-absent, and for an ACS group the same state)."""
    if len(events) > MAX_GROUP_SIZE:
        return "oversized"
    sample = events[0]
    if sample.item_key is not None:
        if source == "acs" and isinstance(sample, AcsItemV2) and sample.state == "hold":
            return "ok_acs_hold_pair"
        return "unexpected_item_keyed"
    if (source == "rejects" and isinstance(sample, RejectV2)
            and sample.error_class in PERMITTED_KEYLESS_REJECT_ERROR_CLASSES):
        return "ok_keyless_reject"
    return "unexpected_keyless"


@dataclass(frozen=True)
class GateResult:
    passed: bool
    max_collision_group_size: int
    unexpected_item_keyed_collision_groups: int
    unexpected_keyless_collision_groups: int
    keyless_reject_collision_rate: float
    collision_time_spread: float


def _evaluate_gate(all_stats: list[SourceCollisionStats]) -> GateResult:
    max_group_size = 0
    unexpected_item_keyed = 0
    unexpected_keyless = 0
    for stats in all_stats:
        for events in stats.collisions.values():
            max_group_size = max(max_group_size, len(events))
            kind = _classify_group(stats.source, events)
            if kind == "unexpected_item_keyed":
                unexpected_item_keyed += 1
            elif kind == "unexpected_keyless":
                unexpected_keyless += 1
            # "oversized" is its own failure reason (max_group_size, below) -- not double-counted here.
            # "ok_acs_hold_pair" and "ok_keyless_reject" need no counter; they are what PASSING looks like.

    rejects = next((s for s in all_stats if s.source == "rejects"), None)
    if rejects is not None and rejects.events_total > 0:
        keyless_reject_collision_rate = rejects.identical_identity_events / rejects.events_total
    else:
        keyless_reject_collision_rate = 0.0
    if rejects is not None and rejects.collision_groups > 0:
        collision_time_spread = len(rejects.distinct_event_times) / rejects.collision_groups
    else:
        collision_time_spread = 1.0  # nothing to spread out -- vacuously fine, never a reason to fail

    passed = (
        max_group_size <= MAX_GROUP_SIZE
        and unexpected_item_keyed == 0
        and unexpected_keyless == 0
        and keyless_reject_collision_rate <= MAX_KEYLESS_REJECT_COLLISION_RATE
        and collision_time_spread >= MIN_COLLISION_TIME_SPREAD
    )
    return GateResult(passed, max_group_size, unexpected_item_keyed, unexpected_keyless,
                      keyless_reject_collision_rate, collision_time_spread)


def _print_gate(gate: GateResult, out: TextIO) -> None:
    print("=== gate ===", file=out)
    print(f"identity_collision_gate={'pass' if gate.passed else 'fail'}", file=out)
    print(f"keyless_reject_collision_rate={gate.keyless_reject_collision_rate:.4f}", file=out)
    print(f"max_collision_group_size={gate.max_collision_group_size}", file=out)
    print(f"collision_time_spread={gate.collision_time_spread:.4f}", file=out)
    print(f"unexpected_item_keyed_collision_groups={gate.unexpected_item_keyed_collision_groups}", file=out)
    print(f"unexpected_keyless_collision_groups={gate.unexpected_keyless_collision_groups}", file=out)


def report(config_path: str, *, out: TextIO, clock=_utc_now) -> int:
    settings = load_dry_run_settings(config_path)
    now = clock()
    keys = derive_subkeys(secrets.token_bytes(32))
    rules = load_rules(settings.rules_path, keys)
    cache = PatronCache.in_memory(today=now.date())
    configured = dict(settings.sources)
    ctx = TransformContext(keys=keys, rules=rules, ruleset_id=cache.ruleset_id_for(rules.fingerprint), cache=cache,
                           zone=settings.zone(), now=now)
    all_stats: list[SourceCollisionStats] = []
    try:
        for source_name in SOURCE_ORDER:
            path = configured.get(source_name)
            if path is None:
                continue
            stats = _collect_source(source_name, path, ctx, settings.dry_run_tail_bytes)
            all_stats.append(stats)
            _print_source(stats, out)
    finally:
        cache.discard()
        cache.close()
    _print_gate(_evaluate_gate(all_stats), out)
    return 0


def main(argv: list[str] | None = None) -> int:
    """The frozen dispatcher's `identity-collision-diag` subcommand (collector/freeze/dispatcher.py) forwards straight here, argv
    unchanged -- same `list[str] | None -> int` shape as every other subcommand target. Same fail-closed error handling as
    `v2_run.main_v2_dry_run`: a bad or missing config is a fixed message plus exit 2, never a raw traceback. The exit code
    reflects only whether the diagnostic itself RAN successfully -- the bounded gate's own pass/fail verdict is conveyed by the
    printed `identity_collision_gate=pass|fail` line, read by the caller (collector/deploy/prepare_v2_pilot.ps1's
    Test-IdentityCollisionGatePassed), exactly as `run --v2-dry-run`'s printed counters are read by Test-DryRunAcceptance."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, help="path to the collector config JSON (the same one --v2-dry-run reads)")
    args = parser.parse_args(argv)
    try:
        return report(args.config, out=sys.stdout)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except CollectorV2Error as exc:
        print(f"Configuration error: {exc.code}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Diagnostic failed: {describe(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
