"""Contract v2: the transformation layer -- THE privacy boundary (docs/collector-v2.md).

This is the only module that may hold raw Tech Logic records (with collector/v2_reader.py, v2_normalize.py and v2_classify.py, which only it
imports). Raw lines are read, parsed, classified, keyed and DROPPED here. What leaves is:

    * typed, frozen, self-validating safe events (collector/v2_events.py) -- no raw field exists on them;
    * an integer cursor;
    * a `Counters` object of integers -- aggregate, never a value.

Nothing downstream (uploader, status, quarantine, state, logs) ever receives a raw string. Every raw-touching call runs through
`run_guarded`, so a failure surfaces as a fixed code plus the exception's TYPE and code location -- never its message, which pandas and the
standard library fill with the offending value.

ORDER. ACS is transformed BEFORE checkins and rejects (the run orders its sources): the patron cache it populates is what the patron-card guard
consults for the others. Message-64 patron records only populate the local cache; they never produce an event.

THE GUARD. A barcode that matches a patron identifier this machine has already seen (a keyed lookup -- never a length or pattern test) is dropped and
only counted. This applies to check-ins, rejects and ACS item records alike.

IDENTITY. `item_key` is the keyed HMAC of the barcode; `event_key` is the keyed HMAC of a canonical form of v2-safe fields only
(collector/v2_identity.py). Two genuinely distinct events with identical safe fields (same second, same item, same destination and bin) share an
event_key -- the same collapse the v1 semantic keys had; `identical_identity_events` counts how often it happens so it is measured, not guessed.

TIME. Tech Logic timestamps are naive local times. They are converted with the configured IANA zone; a fall-back (ambiguous) time uses its FIRST
occurrence and only increments a counter; an instant outside 2000-01-01 .. now+1 day is dropped and counted (the server would refuse it).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import v2_classify, v2_identity, v2_normalize, v2_reader
from .v2_events import (
    ACS_ITEM_STATES,
    ERROR_CLASSES,
    KIND_ACS_ITEMS,
    KIND_CHECKINS,
    KIND_REJECTS,
    Cursor,
    SafeEvent,
    format_time,
)
from .v2_identity import SubKeys
from .v2_patrons import HoldRow, PatronCache, PatronInfo
from .v2_rules import Rules
from .v2_safe_errors import TransformError, run_guarded

SOURCE_ORDER = ("acs", "checkins", "rejects")  # ACS first: it populates the cache the guard uses
_EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)
_FUTURE = timedelta(days=1)


@dataclass
class Counters:
    """Aggregate integers about one transform. No field can hold a value from a record."""

    lines_read: int = 0
    records_parsed: int = 0
    dropped_bad_time: int = 0
    dropped_out_of_range_time: int = 0
    dropped_patron_card: int = 0
    dropped_missing_barcode: int = 0
    dst_ambiguous: int = 0
    dst_nonexistent: int = 0
    unknown_destination: int = 0
    unknown_bin: int = 0
    patron_records: int = 0
    acs_corrections: int = 0
    identical_identity_events: int = 0
    events: dict[str, int] = field(default_factory=lambda: {KIND_CHECKINS: 0, KIND_REJECTS: 0, KIND_ACS_ITEMS: 0})
    acs_states: dict[str, int] = field(default_factory=lambda: dict.fromkeys(ACS_ITEM_STATES, 0))
    reject_classes: dict[str, int] = field(default_factory=lambda: dict.fromkeys(ERROR_CLASSES, 0))

    _SCALARS = ("lines_read", "records_parsed", "dropped_bad_time", "dropped_out_of_range_time", "dropped_patron_card",
                "dropped_missing_barcode", "dst_ambiguous", "dst_nonexistent", "unknown_destination", "unknown_bin",
                "patron_records", "acs_corrections", "identical_identity_events")

    def merge(self, other: Counters) -> None:
        for name in self._SCALARS:
            setattr(self, name, getattr(self, name) + getattr(other, name))
        for mine, theirs in ((self.events, other.events), (self.acs_states, other.acs_states), (self.reject_classes, other.reject_classes)):
            for key, value in theirs.items():
                mine[key] = mine.get(key, 0) + value

    def flat(self) -> dict[str, int]:
        """`name -> int`, every key a fixed identifier. This is all a log line, a status file or the dry run ever prints."""
        out = {name: getattr(self, name) for name in self._SCALARS}
        out.update({f"events_{kind}": count for kind, count in self.events.items()})
        out.update({f"acs_state_{state}": count for state, count in self.acs_states.items()})
        out.update({f"reject_class_{name}": count for name, count in self.reject_classes.items()})
        return out


@dataclass
class TransformContext:
    keys: SubKeys
    rules: Rules
    ruleset_id: str
    cache: PatronCache
    zone: ZoneInfo
    now: datetime  # timezone-aware UTC


@dataclass(frozen=True)
class SafeChunk:
    source: str
    events: tuple[SafeEvent, ...]
    cursor: Cursor | None
    counters: Counters
    existed: bool
    rotated: bool
    truncated: bool
    more: bool
    empty: bool  # no complete new line was available


# --- small raw-value helpers (inputs are pandas values: str, None or NaN) -----------------------------------------------------------

def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _naive_time(value: Any) -> datetime | None:
    if value is None or isinstance(value, float) or str(value) == "NaT":  # None, NaN or NaT
        return None
    return value.to_pydatetime() if hasattr(value, "to_pydatetime") else value


def to_utc(naive_local: datetime, zone: ZoneInfo) -> tuple[datetime, bool, bool]:
    """(UTC instant, ambiguous, nonexistent). An ambiguous fall-back time uses its FIRST occurrence (fold=0)."""
    first = naive_local.replace(tzinfo=zone, fold=0)
    second = naive_local.replace(tzinfo=zone, fold=1)
    instant = first.astimezone(UTC)
    nonexistent = instant.astimezone(zone).replace(tzinfo=None) != naive_local  # a spring-forward gap: the wall time never happened
    # In a gap the two folds also have different offsets, but that is not a fall-back overlap: only a REAL time can be ambiguous.
    ambiguous = not nonexistent and first.utcoffset() != second.utcoffset()
    return instant, ambiguous, nonexistent


def _instant(naive_local: datetime, ctx: TransformContext, counters: Counters) -> datetime | None:
    instant, ambiguous, nonexistent = to_utc(naive_local, ctx.zone)
    counters.dst_ambiguous += int(ambiguous)
    counters.dst_nonexistent += int(nonexistent)
    if not _EARLIEST <= instant <= ctx.now + _FUTURE:
        counters.dropped_out_of_range_time += 1
        return None
    return instant


def _guarded_card(ctx: TransformContext, barcode: str) -> bool:
    """True if `barcode` is a patron identifier this machine has seen. A keyed lookup; never a length or pattern test."""
    if not barcode:
        return False
    pk = v2_identity.patron_id_hmac(ctx.keys, barcode)
    if ctx.cache.contains(pk):
        ctx.cache.stage(pk)  # seen again: the sliding TTL moves
        return True
    return False


def _finish(events: list[SafeEvent], counters: Counters) -> None:
    counters.identical_identity_events += len(events) - len({e.event_key for e in events})


# --- ACS ----------------------------------------------------------------------------------------------------------------------------

def _parse_stamp(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _corrections(ctx: TransformContext, patron_pk: bytes, outcome: tuple[bool, bool, bool], counters: Counters) -> list[SafeEvent]:
    """Re-sends the ledger holds of one patron whose flags change under this profile: the same item, the same event_time, the corrected flags.
    A correction carries a higher `revision` (so it is a NEW row even if the flags return to an earlier value); the server's "latest = greatest
    (event_time, id)" rule makes it win, and it carries no patron information."""
    events: list[SafeEvent] = []
    for row in ctx.cache.holds_for_patron(patron_pk):
        flags = v2_classify.combine(row.static, outcome)
        if flags == row.flags:
            continue
        revision = row.revision + 1
        events.append(v2_identity.build_acs_item(
            ctx.keys, event_time=_parse_stamp(row.event_time), item_key=row.item_key, state="hold", destination=row.destination,
            is_ill=flags[0], is_branch_services=flags[1], is_collection_services=flags[2], ruleset_id=ctx.ruleset_id, revision=revision))
        ctx.cache.hold_put(replace(row, flags=flags, revision=revision))
        counters.acs_corrections += 1
        counters.events[KIND_ACS_ITEMS] += 1
    return events


def _transform_acs(lines: list[str], ctx: TransformContext) -> tuple[list[SafeEvent], Counters]:
    from agent.parser import (
        acs as acs_parser,  # imported here, not at module level: importing the parsers opens their log files
    )

    counters = Counters(lines_read=len(lines))
    frame = acs_parser.parse_lines(lines)
    records = []
    for row in frame.to_dict(orient="records"):
        when = _naive_time(row.get("datetime"))
        if when is None:
            counters.dropped_bad_time += 1  # the dashboard never saw a record without a usable time either
            continue
        records.append((when, _text(row.get("message_code")).strip(), _text(row.get("barcode")).strip(),
                        _text(row.get("patron_id")).strip(), row.get("destination"), _text(row.get("raw_message"))))
    counters.records_parsed = len(records)
    keys, cache, rules = ctx.keys, ctx.cache, ctx.rules
    events: list[SafeEvent] = []

    # pass 1 -- patron profiles. The LATEST message-64 record of each patron (by time) is the profile, as in the dashboard. Where that
    # changes what a patron's holds should say, the holds already sent (kept in the ledger) are corrected FIRST, so a same-second later record
    # of the same item still gets the higher id and wins.
    profiles: dict[bytes, PatronInfo] = {}
    for _when, _code, _barcode, patron_id, _destination, raw in sorted((r for r in records if r[1] == "64" and r[3]), key=lambda r: r[0]):
        name, patron_type = v2_classify.patron_profile(raw)
        profiles[v2_identity.patron_id_hmac(keys, patron_id)] = PatronInfo(
            v2_identity.name_hmac(keys, name) if name else None, v2_classify.type_is_ill(patron_type), v2_classify.name_is_ill_like(name))
        counters.patron_records += 1
    for pk, info in profiles.items():
        after = v2_classify.profile_outcome(info, rules)
        if v2_classify.profile_outcome(cache.lookup(pk), rules) != after:  # only a change in the classification outcome can matter
            events.extend(_corrections(ctx, pk, after, counters))
        cache.stage(pk, name_k=info.name_k, type_ill=info.type_ill, name_ill=info.name_ill, profile=True)
    for _when, code, _barcode, patron_id, _destination, _raw in records:
        if code != "64" and patron_id:
            cache.stage(v2_identity.patron_id_hmac(keys, patron_id))  # a sighting: the sliding TTL moves; a profile is never replaced

    # pass 2 -- item records (code 10) become events; message-64 records produce NOTHING for the cloud.
    for when, code, barcode, patron_id, destination, raw in records:
        if code != "10":
            continue
        if not barcode:
            counters.dropped_missing_barcode += 1  # the contract requires an item_key on every ACS item event
            continue
        if _guarded_card(ctx, barcode):
            counters.dropped_patron_card += 1
            continue
        instant = _instant(when, ctx, counters)
        if instant is None:
            continue
        stamp = format_time(instant)
        state = v2_classify.item_state(raw)
        item_key = v2_identity.item_key(keys, barcode)
        existing = cache.hold_get(item_key)
        if state == "hold":
            patron_pk = v2_identity.patron_id_hmac(keys, patron_id) if patron_id else None
            static = v2_classify.static_flags(raw_message=raw, destination_raw=destination, rules=rules)
            flags = v2_classify.combine(static, v2_classify.profile_outcome(cache.lookup(patron_pk) if patron_pk else None, rules))
            slug = v2_normalize.normalize_destination(destination, rules)
            counters.unknown_destination += int(slug == v2_normalize.UNKNOWN)
            revision = 0
            if existing is not None and existing.event_time == stamp:  # the same hold read again: keep its identity, or move it on if its flags did
                revision = existing.revision if existing.flags == flags else existing.revision + 1
            events.append(v2_identity.build_acs_item(keys, event_time=instant, item_key=item_key, state="hold", destination=slug,
                                                     is_ill=flags[0], is_branch_services=flags[1], is_collection_services=flags[2],
                                                     ruleset_id=ctx.ruleset_id, revision=revision))
            newest = existing is None or stamp >= existing.event_time
            if patron_pk is not None and newest:
                cache.hold_put(HoldRow(item_key, stamp, patron_pk, slug, static, flags, revision))
            elif patron_pk is None and existing is not None and newest:
                cache.hold_delete(item_key)  # the item's latest hold has no patron: nothing left that a profile could correct
        else:
            events.append(v2_identity.build_acs_item(keys, event_time=instant, item_key=item_key, state=state))
            if existing is not None and ((state == "non_hold_101" and stamp >= existing.event_time)
                                         or (state == "other_code10" and stamp == existing.event_time)):
                # A later 101 record retracts the hold. An other-code-10 record only does so when it shares the hold's second (then a
                # correction, inserted later, could wrongly outrank it); a strictly later one leaves the Overview hold standing.
                cache.hold_delete(item_key)
        counters.acs_states[state] += 1
        counters.events[KIND_ACS_ITEMS] += 1
    _finish(events, counters)
    return events, counters


# --- check-ins and rejects ---------------------------------------------------------------------------------------------------

def _transform_checkins(lines: list[str], ctx: TransformContext) -> tuple[list[SafeEvent], Counters]:
    from agent.parser import checkins as checkins_parser

    counters = Counters(lines_read=len(lines))
    frame = checkins_parser.parse_lines(lines)
    events: list[SafeEvent] = []
    for row in frame.to_dict(orient="records"):
        when = _naive_time(row.get("datetime"))
        if when is None:
            counters.dropped_bad_time += 1
            continue
        counters.records_parsed += 1
        barcode = _text(row.get("barcode")).strip()
        if _guarded_card(ctx, barcode):
            counters.dropped_patron_card += 1
            continue
        instant = _instant(when, ctx, counters)
        if instant is None:
            continue
        destination = v2_normalize.normalize_destination(row.get("destination"), ctx.rules)
        bin_code = v2_normalize.normalize_bin(row.get("bin"))
        counters.unknown_destination += int(destination == v2_normalize.UNKNOWN)
        counters.unknown_bin += int(bin_code == v2_normalize.UNKNOWN)
        item_key = v2_identity.item_key(ctx.keys, barcode) if barcode else None
        events.append(v2_identity.build_checkin(ctx.keys, event_time=instant, item_key=item_key, destination=destination, bin=bin_code))
        counters.events[KIND_CHECKINS] += 1
    _finish(events, counters)
    return events, counters


def _transform_rejects(lines: list[str], ctx: TransformContext) -> tuple[list[SafeEvent], Counters]:
    from agent.parser import rejects as rejects_parser

    counters = Counters(lines_read=len(lines))
    frame = rejects_parser.parse_lines(lines)
    events: list[SafeEvent] = []
    for row in frame.to_dict(orient="records"):
        when = _naive_time(row.get("datetime"))
        if when is None:
            counters.dropped_bad_time += 1
            continue
        counters.records_parsed += 1
        barcode = _text(row.get("barcode")).strip()
        if _guarded_card(ctx, barcode):
            counters.dropped_patron_card += 1
            continue
        instant = _instant(when, ctx, counters)
        if instant is None:
            continue
        error_class = v2_normalize.classify_reject(row.get("error_message"))
        item_key = v2_identity.item_key(ctx.keys, barcode) if barcode else None
        events.append(v2_identity.build_reject(ctx.keys, event_time=instant, item_key=item_key, error_class=error_class))
        counters.reject_classes[error_class] += 1
        counters.events[KIND_REJECTS] += 1
    _finish(events, counters)
    return events, counters


_PARSER_LOGGERS = ("parser.acs", "parser.checkins", "parser.rejects")


@contextmanager
def _parser_logs_off() -> Iterator[None]:
    """The v1 parsers log parse summaries (including a breakdown of raw destination labels) to `logs/parser.*.log` and to stderr. v2 parses
    quietly: nothing SortView-owned may carry raw routing text, so those loggers are disabled for exactly the duration of a parse and their
    previous state is restored."""
    loggers = [logging.getLogger(name) for name in _PARSER_LOGGERS]
    previous = [log.disabled for log in loggers]
    for log in loggers:
        log.disabled = True
    try:
        yield
    finally:
        for log, was in zip(loggers, previous, strict=True):
            log.disabled = was


_TRANSFORMS = {"acs": _transform_acs, "checkins": _transform_checkins, "rejects": _transform_rejects}


def transform_lines(source: str, lines: list[str], ctx: TransformContext) -> tuple[list[SafeEvent], Counters]:
    """Raw lines of one source -> safe events and counters. Any failure is a TransformError (fixed code, type and location only)."""
    transform = _TRANSFORMS.get(source)
    if transform is None:
        raise TransformError("unknown_source")
    with _parser_logs_off():
        return run_guarded(f"transform_{source}_failed", transform, lines, ctx)


def read_next_chunk(source: str, path: str, cursor: Cursor | None, ctx: TransformContext, max_lines: int) -> SafeChunk:
    """Reads the next bounded chunk of one source and transforms it. The raw lines exist only inside this call."""
    chunk = run_guarded(f"read_{source}_failed", v2_reader.read_chunk, path, cursor, max_lines)
    if not chunk.existed:
        return SafeChunk(source, (), chunk.cursor, Counters(), False, False, False, False, True)
    events, counters = transform_lines(source, chunk.lines, ctx) if chunk.lines else ([], Counters())
    return SafeChunk(source, tuple(events), chunk.cursor, counters, True, chunk.rotated, chunk.truncated, chunk.more, not chunk.lines)


def read_tail_chunk(source: str, path: str, ctx: TransformContext, tail_bytes: int) -> SafeChunk:
    """Dry run: transforms the complete lines of the last `tail_bytes` of a source. Reads nothing else, commits nothing."""
    lines = run_guarded(f"read_{source}_failed", v2_reader.read_tail, path, tail_bytes)
    events, counters = transform_lines(source, lines, ctx) if lines else ([], Counters())
    return SafeChunk(source, tuple(events), None, counters, True, False, False, False, not lines)
