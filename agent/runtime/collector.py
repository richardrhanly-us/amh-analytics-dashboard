"""Canonical continuous collector (Continuous Ingestion Phase F).

    source file -> tailer -> parser -> source_event_id -> durable spool
    -> cursor/state update

INVARIANT (unchanged since Phase C/D): THE CURSOR MAY LAG DURABLE
CAPTURE, BUT MUST NEVER LEAD DURABLE CAPTURE. Enforced structurally here:
state.save_state is only ever called with a cursor once everything up to
that cursor is either (a) already durably in the spool
(spool.write_batch has returned successfully), or (b) known to contain no
parseable events at all, so there is nothing that could be lost by
advancing past it.

BATCHING: parsed events are buffered in memory (never persisted) across
poll cycles and flushed to the spool when either batch_max_events have
accumulated or batch_max_seconds have elapsed since the first
still-unflushed event was read -- whichever comes first. A single read
that returns more than batch_max_events at once is flushed as multiple
spool batches (chunked), never one oversized batch and never held back
waiting for more.

Why an unpersisted in-memory buffer is still crash-safe: if the process
dies while events sit in the buffer, nothing is lost. The persisted
cursor is from BEFORE those events were ever read, so a fresh read after
restart simply re-reads the same still-present bytes from the source
file and reproduces IDENTICAL events (same start offsets -> same
deterministic source_event_id -- see agent/event_identity.py). The
backend's transport-idempotency index recognizes the resend as the same
events, not new ones. This is "duplicate reread is acceptable, data loss
is not," restated for the buffering layer specifically.

PERSISTED CURSOR vs. LIVE READ CURSOR (two distinct concepts, kept
deliberately separate): a normal, no-crash sequence of poll cycles must
NEVER re-read or re-add the same physical bytes to the buffer just
because a flush hasn't happened yet -- that would manufacture duplicate
spool records on every single healthy run, not just after a crash, and
backend idempotency existing as a safety net is not a license to produce
avoidable duplicates in the first place.

  - LIVE READ CURSOR (self._read_cursor): this process's own in-memory
    record of "the next byte to read from," advanced to
    tailer.read_new_lines's returned result.cursor after EVERY successful
    read that finds the source, buffered or not, flushed or not. This is
    what actually gets passed to the next read_new_lines call -- so a
    poll cycle with nothing new to append never re-reads bytes already
    sitting in the buffer, regardless of how many cycles pass before a
    flush.
  - PERSISTED CURSOR (whatever agent.state.load_state(state_path)
    actually contains on disk): only ever updated by _persist_state(),
    which only ever runs (a) as part of a successful flush (spool.write_batch
    already returned), or (b) when a read produced zero parseable events,
    so there is nothing pending to lose. It legitimately lags the live
    read cursor for as long as events sit unflushed in the buffer -- this
    is the "cursor may lag" half of the invariant, not a bug.

_pending_cursor holds the value _persist_state() will write once called;
it is set to the same value as _read_cursor at the same moment on every
successful read, so in practice the two are always numerically equal by
the end of a read -- they exist as separate fields because
_read_cursor's own None is ALSO the sentinel REPLAY bootstrap uses to
mean "read from byte 0," which would otherwise be ambiguous with "no
read has ever completed yet."

BOOTSTRAP (decided in Phase 0, implemented here for the first time --
applies ONLY the very first time agent.state.get_source returns None for
a source; every later cycle reads from the persisted cursor exactly as
agent.tailer.read_new_lines already does, independent of bootstrap_mode):

  NORMAL (safe production default) -- seeds the cursor at the source
    file's CURRENT end-of-file. Historical content already in the file
    is never read or spooled. Persisted immediately (there is no data
    associated with this seed position to lose).
  REPLAY -- starts at byte 0, explicitly. Reads and spools everything
    already in the file. Must be requested per source; never implied.
  OFFSET -- starts at an explicit caller-supplied byte offset
    (SourceConfig.bootstrap_offset). A validation/testing tool, not a
    normal production mode.

STATE TRANSITIONS this module defines explicitly (per Phase F's
requirement not to leave any of these implicit):
  - state does not exist for this source -> bootstrap (above).
  - state exists, same identity, offset only grows -> normal read.
  - state exists, identity changed (tailer reports rotated=True) ->
    flush whatever is still buffered under the OLD generation first,
    THEN bump generation (agent.state.advance_generation), then continue
    reading the new stream from byte 0 under the NEW generation.
  - state exists, same identity, file shrank (tailer reports
    truncated=True) -> same handling as rotation, per Phase D's
    correction (truncation resets offsets exactly like rotation does).
  - configured path differs from the persisted path
    (agent.state.path_changed) -> logged explicitly as a warning; no
    separate mechanism needed beyond that, because pointing a source at
    a genuinely different file produces a different identity the next
    time it's stat'd, which is already the rotation path above.

DISCONTINUITY CONFIRMATION (real-AMH-machine correction, added after
2026-09-14 shadow validation): a single tailer.read_new_lines call
reporting rotated=True or truncated=True is NOT, by itself, treated as a
confirmed discontinuity anymore. agent/discovery.py's own module docstring
already flagged (st_dev, st_ino) identity as "proven safe on one dev
machine, real-machine semantics not yet validated" -- shadow validation on
the actual Tech Logic AMH machine exposed exactly that gap: ACS Log.txt's
identity (and/or apparent size) was observed to disagree with the
persisted cursor on a single stat(), while checkins/rejects never did and
a later 60s/250ms sample showed all three identities stable. Whatever the
underlying platform cause (this module does not assume or diagnose it --
see the logging this correction adds), a SINGLE inconsistent read must
never by itself throw away and destructively re-read an entire multi-MB
source file, because tailer.read_new_lines has no memory between calls --
each call re-derives rotated/truncated fresh from whatever cursor it's
given, so a repeated false-positive against an UNCHANGED persisted cursor
would otherwise re-trigger every single poll cycle, forever (exactly what
was observed: ACS climbed from generation 0 to 7, replaying the same ~7MB
~6 times, while the spool grew from 9 files/17KB to 4,228 files/171MB).

The fix: a candidate discontinuity (rotated or truncated) is only ACTED
ON (buffer flushed under the old generation, generation bumped, cursor
advanced past the reset) once the SAME kind of discontinuity is reported
on two CONSECUTIVE poll cycles against the SAME still-untouched persisted
cursor -- see _pending_discontinuity_kind and poll_once below. The first
sighting is withheld: self._read_cursor is deliberately left unchanged
(never advanced to the candidate's post-reset cursor), so the very next
cycle calls tailer.read_new_lines with the IDENTICAL old cursor and
independently re-derives the comparison from scratch:

  - genuine, persistent rotation/truncation -> the same discontinuity is
    reported again next cycle (the old file/identity is genuinely gone,
    or genuinely still smaller) -> CONFIRMED, handled exactly as before,
    just one poll cycle (default ~1s) later. Detection is not weakened.
  - a transient/spurious single-sample inconsistency -> the very next
    stat() most likely sees the true, unchanged file again, matching the
    still-untrusted old cursor -> tailer reports no discontinuity at all
    -> SELF-RESOLVED: the candidate is discarded, nothing is reset, no
    replay happens, and this cycle's (legitimate) read is processed
    normally.

Normal appends (the overwhelming common case) never enter this path at
all -- zero added latency, zero behavior change. Every decision point
(withhold / confirm / self-resolve) is logged with enough detail (source,
previous identity, current identity, prior offset, current file size,
rotated vs. truncated) to diagnose a future real-machine recurrence
directly from agent.log, which this shadow run's own logs could not do --
see the module docstring's note on why that gap itself was a problem.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import discovery, event_identity, spool, state, tailer
from ..parser import acs as acs_parser
from ..parser import checkins as checkins_parser
from ..parser import rejects as rejects_parser
from .config import BootstrapMode, SourceConfig
from .events import build_event
from .logging_setup import get_component_logger

_PARSERS = {
    "checkins": checkins_parser,
    "rejects": rejects_parser,
    "acs": acs_parser,
}


@dataclass(frozen=True)
class _PendingDiscontinuity:
    """An unconfirmed rotation/truncation candidate, held in memory only
    (never persisted -- see the module docstring's DISCONTINUITY
    CONFIRMATION section).

    signature is what must match on the NEXT cycle for confirmation, not
    just `kind` alone -- a bare kind-only check ("rotated" == "rotated")
    would wrongly confirm a genuinely flaky identity read that reports a
    DIFFERENT bogus identity on every single call, since every one of
    those calls is independently labeled "rotated". For kind="rotated",
    signature is the candidate's new SourceIdentity.token -- two
    consecutive reads must agree on the SAME new identity, which a truly
    random/never-repeating flake never will, while a genuine new file
    (whose identity is not going to change again a poll cycle later)
    always will. For kind="truncated" (identity unchanged by
    definition), signature is left None -- confirmation is kind-only,
    re-derived fresh both times against the same still-untouched
    persisted cursor.offset, which is itself already a stable reference
    point (see poll_once).
    """

    kind: str
    signature: tuple[int, ...] | None


@dataclass(frozen=True)
class CollectorCycleReport:
    source: str
    existed: bool
    rotated: bool
    truncated: bool
    events_read: int
    events_flushed: int
    spool_batches_written: int
    state_persisted: bool
    source_missing: bool = False
    # True when this cycle saw a first-sighting (not yet confirmed)
    # rotation/truncation candidate and deliberately took no action --
    # see the module docstring's DISCONTINUITY CONFIRMATION section.
    # rotated/truncated above stay False for such a cycle; the actual
    # generation bump (if any) is reported on whichever LATER cycle
    # confirms it.
    pending_discontinuity: bool = False


class SourceCollector:
    """Owns one source's in-memory read/buffer state across poll cycles.
    Not thread-safe on its own -- exactly one thread is ever expected to
    call poll_once() for a given instance, sequentially."""

    def __init__(
        self,
        source_cfg: SourceConfig,
        *,
        agent_id: str,
        customer_id: int,
        branch_id: int,
        state_path: str | Path,
        spool_root: str | Path,
        batch_max_events: int = 100,
        batch_max_seconds: float = 2.0,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.source_cfg = source_cfg
        self.agent_id = agent_id
        self.customer_id = customer_id
        self.branch_id = branch_id
        self.state_path = Path(state_path)
        self.spool_root = spool_root
        self.batch_max_events = batch_max_events
        self.batch_max_seconds = batch_max_seconds
        self.time_fn = time_fn

        self._read_cursor: tailer.FileCursor | None = None
        self._generation: int = 0
        self._buffer: list[tuple[int, dict]] = []
        self._buffer_generation: int | None = None
        self._buffer_final_end_offset: int | None = None
        self._buffer_started_at: float | None = None
        self._pending_cursor: tailer.FileCursor | None = None
        self._bootstrapped = False
        # First-sighting-not-yet-confirmed rotation/truncation candidate,
        # or None if none is currently pending. See the module
        # docstring's DISCONTINUITY CONFIRMATION section.
        self._pending_discontinuity: _PendingDiscontinuity | None = None
        self.logger = get_component_logger("collector")

    # --- bootstrap -----------------------------------------------------

    def _bootstrap_if_needed(self) -> bool:
        """Returns True once bootstrapped (either just now, or already
        done on a prior call). Returns False if bootstrap could not
        complete this cycle (source not readable yet) -- caller should
        treat this cycle as a no-op and try again next time."""
        if self._bootstrapped:
            return True

        current_state = state.load_state(self.state_path)
        prior = state.get_source(current_state, self.source_cfg.name)

        if prior is not None:
            if state.path_changed(prior, self.source_cfg.path):
                self.logger.warning(
                    "Source %s: configured path changed (%s -> %s) -- "
                    "will be handled as a rotation the next time this path is read",
                    self.source_cfg.name,
                    prior.path,
                    self.source_cfg.path,
                )
            self._read_cursor = prior.cursor
            self._generation = prior.generation
            self._bootstrapped = True
            return True

        mode = self.source_cfg.bootstrap_mode

        if mode is BootstrapMode.REPLAY:
            self._read_cursor = None
            self._generation = 0
            self._bootstrapped = True
            return True

        if mode is BootstrapMode.OFFSET:
            identity = discovery.identify(self.source_cfg.path)
            if identity is None:
                return False
            offset = self.source_cfg.bootstrap_offset or 0
            self._read_cursor = tailer.FileCursor(identity=identity, offset=offset)
            self._generation = 0
            self._bootstrapped = True
            return True

        # NORMAL -- seed at current EOF; ingest nothing historical.
        identity = discovery.identify(self.source_cfg.path)
        if identity is None:
            return False

        eof_offset = os.path.getsize(self.source_cfg.path)
        self._read_cursor = tailer.FileCursor(identity=identity, offset=eof_offset)
        self._generation = 0
        self._bootstrapped = True

        new_state = state.update_source(
            current_state,
            self.source_cfg.name,
            path=self.source_cfg.path,
            cursor=self._read_cursor,
            generation=self._generation,
        )
        state.save_state(self.state_path, new_state)
        return True

    # --- flushing --------------------------------------------------------

    def _persist_state(self) -> bool:
        if self._pending_cursor is None:
            return False
        current_state = state.load_state(self.state_path)
        new_state = state.update_source(
            current_state,
            self.source_cfg.name,
            path=self.source_cfg.path,
            cursor=self._pending_cursor,
            generation=self._generation,
        )
        state.save_state(self.state_path, new_state)
        return True

    def _should_flush(self) -> bool:
        if not self._buffer:
            return False
        if len(self._buffer) >= self.batch_max_events:
            return True
        if self._buffer_started_at is not None:
            return (self.time_fn() - self._buffer_started_at) >= self.batch_max_seconds
        return False

    def _flush(self) -> tuple[int, int, bool]:
        if not self._buffer:
            return 0, 0, False

        generation = self._buffer_generation if self._buffer_generation is not None else self._generation
        chunks = [
            self._buffer[i : i + self.batch_max_events] for i in range(0, len(self._buffer), self.batch_max_events)
        ]

        # Invariant: a non-empty buffer only ever exists after at least
        # one successful read set _buffer_final_end_offset -- see
        # poll_once. Raising (not assert, which -O strips) narrows the
        # type for the final chunk's end_offset below rather than
        # silently tolerating None there.
        if self._buffer_final_end_offset is None:
            raise RuntimeError(
                "unreachable: _flush called with a non-empty buffer but no _buffer_final_end_offset"
            )

        for i, chunk in enumerate(chunks):
            start_offset = chunk[0][0]
            end_offset = chunks[i + 1][0][0] if i + 1 < len(chunks) else self._buffer_final_end_offset
            records = [event for _offset, event in chunk]
            spool.write_batch(
                self.spool_root,
                self.source_cfg.name,
                records,
                source_generation=generation,
                start_offset=start_offset,
                end_offset=end_offset,
            )

        events_flushed = len(self._buffer)
        batches_written = len(chunks)

        self._buffer = []
        self._buffer_generation = None
        self._buffer_started_at = None
        persisted = self._persist_state()

        return events_flushed, batches_written, persisted

    # --- discontinuity confirmation / logging ---------------------------

    def _current_file_size(self) -> int | None:
        """Best-effort fresh byte size for diagnostic logging only --
        never load-bearing for a correctness decision (tailer.py already
        made its rotated/truncated call using its own reads). Returns
        None rather than raising if the file is momentarily unreadable,
        so a logging call can never itself crash a poll cycle."""
        try:
            return os.path.getsize(self.source_cfg.path)
        except OSError:
            return None

    def _log_discontinuity(
        self, *, kind: str, confirmed: bool, result: tailer.TailResult
    ) -> None:
        prior_identity = self._read_cursor.identity if self._read_cursor is not None else None
        prior_offset = self._read_cursor.offset if self._read_cursor is not None else 0
        current_size = self._current_file_size()

        if confirmed:
            self.logger.warning(
                "Source %s: %s CONFIRMED (seen on 2 consecutive poll cycles against the "
                "same persisted cursor) | previous_identity=%s current_identity=%s "
                "prior_offset=%s current_size=%s -- flushing generation %s and advancing "
                "to generation %s",
                self.source_cfg.name,
                kind,
                prior_identity,
                result.cursor.identity,
                prior_offset,
                current_size,
                self._generation,
                self._generation + 1,
            )
        else:
            self.logger.warning(
                "Source %s: %s candidate detected (UNCONFIRMED, 1st sighting) | "
                "previous_identity=%s current_identity=%s prior_offset=%s current_size=%s "
                "-- withholding generation bump for one poll cycle to confirm; persisted "
                "cursor and buffer left unchanged, this cycle's read is discarded",
                self.source_cfg.name,
                kind,
                prior_identity,
                result.cursor.identity,
                prior_offset,
                current_size,
            )

    def _log_discontinuity_self_resolved(self) -> None:
        self.logger.info(
            "Source %s: previously-candidate %s discontinuity did NOT repeat on the next "
            "poll cycle -- treating as a transient/spurious identity or size read, no "
            "generation bump, no data replay, resuming normal read from the unchanged "
            "persisted cursor",
            self.source_cfg.name,
            self._pending_discontinuity.kind if self._pending_discontinuity is not None else "?",
        )

    # --- main entry point --------------------------------------------------

    def poll_once(self) -> CollectorCycleReport:
        if not self._bootstrap_if_needed():
            return CollectorCycleReport(
                self.source_cfg.name, False, False, False, 0, 0, 0, False, source_missing=True
            )

        result = tailer.read_new_lines(self.source_cfg.path, self._read_cursor)

        if not result.existed:
            events_flushed, batches, persisted = (0, 0, False)
            if self._should_flush():
                events_flushed, batches, persisted = self._flush()
            return CollectorCycleReport(
                self.source_cfg.name, False, False, False, 0, events_flushed, batches, persisted,
                source_missing=True,
            )

        candidate_kind = "rotated" if result.rotated else "truncated" if result.truncated else None
        # result.cursor.identity is only ever None when the source doesn't
        # exist (result.existed is False), already handled/returned above
        # -- a rotated=True result always carries a real identity here.
        candidate_signature = (
            result.cursor.identity.token
            if candidate_kind == "rotated" and result.cursor.identity is not None
            else None
        )

        is_confirmation_of_pending = (
            candidate_kind is not None
            and self._pending_discontinuity is not None
            and self._pending_discontinuity.kind == candidate_kind
            and self._pending_discontinuity.signature == candidate_signature
        )

        if candidate_kind is not None and not is_confirmation_of_pending:
            # First sighting of this exact discontinuity (or one that
            # doesn't match whatever was already pending -- e.g. a
            # rotation candidate whose new identity differs from the
            # previous cycle's candidate identity, which is itself a sign
            # of a genuinely flaky read, not a genuine stable rotation)
            # -- withhold. Deliberately do NOT touch
            # self._read_cursor/_pending_cursor or the buffer: leaving
            # the cursor exactly as it was means the NEXT poll_once call
            # re-derives rotated/truncated from scratch against the same
            # still-trusted cursor, which is what lets a transient false
            # positive self-resolve instead of compounding. This cycle's
            # read (already reset to offset 0 by tailer.py) is discarded
            # entirely -- see module docstring's DISCONTINUITY
            # CONFIRMATION section.
            self._log_discontinuity(kind=candidate_kind, confirmed=False, result=result)
            self._pending_discontinuity = _PendingDiscontinuity(
                kind=candidate_kind, signature=candidate_signature
            )

            events_flushed, batches_written, persisted = (0, 0, False)
            if self._should_flush():
                events_flushed, batches_written, persisted = self._flush()

            return CollectorCycleReport(
                self.source_cfg.name,
                True,
                False,
                False,
                0,
                events_flushed,
                batches_written,
                persisted,
                pending_discontinuity=True,
            )

        if candidate_kind is not None and is_confirmation_of_pending:
            # Second consecutive cycle reporting the SAME discontinuity
            # (same kind, and for rotation, the SAME new identity) against
            # the same untouched cursor -- confirmed.
            self._log_discontinuity(kind=candidate_kind, confirmed=True, result=result)
            self._pending_discontinuity = None
            self._flush()
            prior_for_generation = state.SourceState(
                path=self.source_cfg.path,
                cursor=self._read_cursor or tailer.FileCursor(None, 0),
                generation=self._generation,
            )
            self._generation = state.advance_generation(
                prior_for_generation, rotated=result.rotated, truncated=result.truncated
            )
        elif self._pending_discontinuity is not None:
            # This cycle shows a clean read against the old cursor after
            # all -- the previously-pending candidate was transient.
            self._log_discontinuity_self_resolved()
            self._pending_discontinuity = None

        events_read = 0
        if result.lines:
            parser_module = _PARSERS[self.source_cfg.name]
            numbered = list(zip(result.line_offsets, result.lines, strict=True))
            df, kept_offsets = parser_module.parse_lines_with_offsets(numbered)
            records = df.to_dict(orient="records")
            events_read = len(records)

            for offset, record in zip(kept_offsets, records, strict=True):
                event = build_event(
                    self.source_cfg.name,
                    record,
                    customer_id=self.customer_id,
                    branch_id=self.branch_id,
                )
                event["source_event_id"] = event_identity.compute_source_event_id(
                    agent_id=self.agent_id,
                    source=self.source_cfg.name,
                    generation=self._generation,
                    start_offset=offset,
                )
                if self._buffer_generation is None:
                    self._buffer_generation = self._generation
                    self._buffer_started_at = self.time_fn()
                self._buffer.append((offset, event))

        self._buffer_final_end_offset = result.cursor.offset
        self._pending_cursor = result.cursor
        self._read_cursor = result.cursor

        state_persisted = False
        if events_read == 0 and not self._buffer:
            # Nothing durable is at risk -- safe to persist the advanced
            # cursor immediately even though nothing was spooled.
            state_persisted = self._persist_state()

        events_flushed, batches_written = 0, 0
        if self._should_flush():
            events_flushed, batches_written, flush_persisted = self._flush()
            state_persisted = state_persisted or flush_persisted

        return CollectorCycleReport(
            self.source_cfg.name,
            True,
            result.rotated,
            result.truncated,
            events_read,
            events_flushed,
            batches_written,
            state_persisted,
        )

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def current_offset(self) -> int:
        return self._read_cursor.offset if self._read_cursor is not None else 0

    def force_flush(self) -> tuple[int, int, bool]:
        """Flushes whatever is currently buffered regardless of
        threshold -- used for clean shutdown, so nothing sits unflushed
        (and thus unspooled) longer than necessary when the process is
        asked to stop."""
        return self._flush()
