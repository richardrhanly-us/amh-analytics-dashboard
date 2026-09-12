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

_PARSERS = {
    "checkins": checkins_parser,
    "rejects": rejects_parser,
    "acs": acs_parser,
}


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
                from ..logger_config import get_logger

                get_logger("runtime.collector").warning(
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

        if result.rotated or result.truncated:
            self._flush()
            prior_for_generation = state.SourceState(
                path=self.source_cfg.path,
                cursor=self._read_cursor or tailer.FileCursor(None, 0),
                generation=self._generation,
            )
            self._generation = state.advance_generation(
                prior_for_generation, rotated=result.rotated, truncated=result.truncated
            )

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
