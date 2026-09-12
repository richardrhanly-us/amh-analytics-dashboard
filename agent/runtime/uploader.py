"""Canonical spool uploader (Continuous Ingestion Phase F).

    pending spool batch (oldest logical-source order, per source)
    -> upload -> positive ACK -> spool.acknowledge (delete)

NEVER regenerates source_event_id -- every record already carries the ID
the collector attached before durable spool publication (see
agent/runtime/collector.py); this module only ever reads it back.

FAILURE CLASSIFICATION (preserves the categories corrected in Phase D,
reused directly from agent.spool.FailureCategory -- not reinvented):

  RETRYABLE_INFRA (connection errors, DNS, timeout, 429, 5xx):
    the whole batch file stays pending, untouched, indefinitely.
    Exponential backoff with jitter between cycles (agent.runtime.backoff).
    Never quarantined on attempt count alone. Attempt metadata preserved
    via agent.spool.record_attempt_failure.
  AUTH_FAILURE (401/403):
    same disk behavior as RETRYABLE_INFRA -- valid data stays pending,
    never quarantined -- but tracked as its OWN category so heartbeat can
    report "credentials are wrong" distinctly from "backend is down."
    Backoff naturally slows futile retries the same way.
  PERMANENT_REJECTION (400/413-shaped, deterministic):
    isolated -- see below. Never blamed on the whole batch blindly.
  LOCAL_SPOOL_CORRUPTION (unreadable/malformed NDJSON):
    handled by agent.spool.read_batch_or_quarantine before this module
    ever sees the batch as records -- quarantines the file, never
    silently discards it.

POISON-EVENT ISOLATION -- adapted from the proven concept in the
experimental agent/outbox_uploader.py (recursive halving over an
in-memory candidate set, one shared request budget, "resolve everything
before touching disk" ordering), reimplemented here over spool FILES
instead of SQLite rows, since there's no row-level UPDATE to fall back on:

  1. Upload the whole batch file's records as one request.
  2. Success -> spool.acknowledge(path). Done.
  3. RETRYABLE_INFRA / AUTH_FAILURE -> record_attempt_failure on the
     WHOLE file; leave it pending, untouched. Done -- never split; the
     payload hasn't been proven bad (infra), or the failure isn't
     row-level at all (auth).
  4. PERMANENT_REJECTION, > 1 record -> split the in-memory record list
     in half, recurse on each half as an independent sub-request. No new
     spool files are written for intermediate splits -- only the FINAL,
     fully-resolved outcome touches disk.
  5. PERMANENT_REJECTION, exactly 1 record -> that record independently
     reproduced a deterministic failure on its own -- the smallest
     practical unit.
  6. Once recursion FULLY resolves every record in the original file
     (each is either delivered or singly quarantined, nothing left
     pending) -- any quarantined record(s) are written via
     spool.quarantine_records (a new evidence file, with reason), THEN
     the ORIGINAL file is acknowledged (deleted) -- its content is now
     durably accounted for elsewhere, in exactly two places: delivered
     (at the backend) or quarantined (on local disk).
  7. If recursion does NOT fully resolve everything this cycle (a
     transient RETRYABLE_INFRA/AUTH_FAILURE on a sub-branch, or the
     isolation budget ran out) -- the ORIGINAL file is left COMPLETELY
     UNTOUCHED. Any sub-batches that already succeeded before the
     interruption were genuinely delivered; resending them on the next
     full retry of this file is safe and idempotent (source_event_id /
     semantic dedup both apply), so nothing needs to be tracked on disk
     for this case -- simplicity over avoiding a harmless resend.

ISOLATION REQUEST BUDGET: bounded exactly like the experimental
uploader's _IsolationBudget -- one counter shared across every recursive
call for a single batch's isolation attempt. Worst case for an
all-poison batch of N records is ~2N-1 logical POSTs; N is already capped
at RuntimeConfig.batch_max_events (spool batches are never larger, see
agent/runtime/collector.py), and RuntimeConfig.uploader_max_isolation_requests
(default 20) caps the total regardless. Exhaustion is a safety stop, not
a verdict -- see case 7.

PROACTIVE BYTE-BUDGET SPLITTING (ported from the experimental uploader's
_fit_batch_to_byte_budget -- a real requirement, not implementation
detail): title/message/raw_message/etc. are unbounded-length text with
no schema-level size cap, so a fixed 100-event count cap alone is not a
provable guarantee the serialized JSON stays under the backend's
SORTVIEW_MAX_REQUEST_BODY_BYTES. Before a file's records ever enter
isolation, _fit_to_byte_budget splits them (in memory, no HTTP calls, so
this costs nothing against the isolation budget) into chunks that each
serialize under RuntimeConfig.uploader_max_upload_body_bytes, halving
repeatedly and never returning an empty chunk. Each resulting chunk is
then run through _attempt_and_isolate independently, but ALL chunks for
one file share the SAME _IsolationBudget and are aggregated back into one
outcome -- so "is this file fully resolved, can it be acknowledged" still
means everything from every chunk. This is what actually keeps a 413
caused purely by aggregate size from ever reaching the reactive
PERMANENT_REJECTION path (and burning isolation-budget requests) in the
first place; the reactive path remains as the fallback for when this
budget assumption turns out to be wrong for the server's real limit.

ORDERING: pending batches within one source already sort oldest-first by
(generation, offset) purely from their filenames -- see agent/spool.py's
ORDERING section -- so "oldest logical source order" is just
spool.list_pending_batches(...)[0], no separate bookkeeping needed here.
This uploader drains ONE batch per source per cycle (round-robin across
sources), letting the caller's poll loop naturally pace repeated draining
of a deep backlog rather than looping internally until empty.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any

import requests

from .. import spool
from ..spool import FailureCategory
from .backoff import Backoff
from .config import RuntimeConfig
from .http_client import post_json

_PAYLOAD_KEY = {"checkins": "checkins", "rejects": "rejects", "acs": "acs"}


def _build_payload(source_name: str, records: list[dict]) -> dict[str, list]:
    payload: dict[str, list] = {"checkins": [], "rejects": [], "acs": []}
    payload[_PAYLOAD_KEY[source_name]] = records
    return payload


def _payload_byte_size(source_name: str, records: list[dict]) -> int:
    return len(json.dumps(_build_payload(source_name, records)).encode("utf-8"))


def _fit_to_byte_budget(source_name: str, records: list[dict], max_bytes: int) -> list[list[dict]]:
    """Splits `records` into chunks that each serialize under `max_bytes`
    -- purely in-memory, no HTTP calls, so this never touches the
    isolation request budget. Halves repeatedly; a single record that is
    itself over budget is still returned as its own (oversized) chunk of
    one -- there is nothing smaller to split it into, and the reactive
    PERMANENT_REJECTION path is the fallback if the server actually
    rejects it. Never returns an empty chunk for a non-empty input."""
    if not records:
        return []
    if len(records) == 1 or _payload_byte_size(source_name, records) <= max_bytes:
        return [records]

    mid = len(records) // 2
    return _fit_to_byte_budget(source_name, records[:mid], max_bytes) + _fit_to_byte_budget(
        source_name, records[mid:], max_bytes
    )


@dataclass(frozen=True)
class UploadCycleResult:
    source: str
    attempted: bool
    delivered_records: int = 0
    quarantined_records: int = 0
    pending_records: int = 0
    category: FailureCategory | None = None
    isolation_budget_exhausted: bool = False
    corrupt_batch_quarantined: bool = False
    last_error: str | None = None

    @property
    def made_progress(self) -> bool:
        return bool(self.delivered_records or self.quarantined_records or self.corrupt_batch_quarantined)


class _IsolationBudget:
    def __init__(self, max_requests: int) -> None:
        self.max_requests = max_requests
        self.used = 0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.max_requests

    def consume(self) -> None:
        self.used += 1


@dataclass
class _IsolationOutcome:
    delivered: list[dict] = field(default_factory=list)
    quarantined: list[dict] = field(default_factory=list)
    pending: list[dict] = field(default_factory=list)
    last_category: FailureCategory | None = None
    last_error: str | None = None
    budget_exhausted: bool = False

    @property
    def fully_resolved(self) -> bool:
        return not self.pending and not self.budget_exhausted


class Uploader:
    """One instance per running agent -- self.session is shared across
    every call for connection reuse, per Phase F's "one reusable Session"
    requirement."""

    def __init__(self, cfg: RuntimeConfig, session: requests.Session, logger: Any) -> None:
        self.cfg = cfg
        self.session = session
        self.logger = logger

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.api_token}", "Content-Type": "application/json"}

    def _post(self, source_name: str, records: list[dict]):
        url = f"{self.cfg.api_url}/upload"
        return post_json(
            self.session,
            url,
            _build_payload(source_name, records),
            headers=self._auth_headers(),
            timeout=(self.cfg.http_connect_timeout, self.cfg.http_upload_read_timeout),
        )

    def _attempt_and_isolate(
        self, source_name: str, records: list[dict], budget: _IsolationBudget
    ) -> _IsolationOutcome:
        if budget.exhausted:
            self.logger.warning(
                "Isolation budget exhausted | source=%s used=%s max=%s records_left=%s",
                source_name, budget.used, budget.max_requests, len(records),
            )
            return _IsolationOutcome(pending=records, budget_exhausted=True)

        budget.consume()
        outcome = self._post(source_name, records)

        if outcome.success:
            return _IsolationOutcome(delivered=records)

        if outcome.category in (FailureCategory.RETRYABLE_INFRA, FailureCategory.AUTH_FAILURE):
            return _IsolationOutcome(pending=records, last_category=outcome.category, last_error=outcome.error)

        # PERMANENT_REJECTION -- deterministic, isolate.
        if len(records) == 1:
            self.logger.warning(
                "Record quarantined | source=%s reason=%s", source_name, outcome.error
            )
            return _IsolationOutcome(
                quarantined=records, last_category=outcome.category, last_error=outcome.error
            )

        self.logger.info(
            "Splitting failing batch for isolation | source=%s records=%s category=%s",
            source_name, len(records), outcome.category,
        )
        mid = len(records) // 2
        left = self._attempt_and_isolate(source_name, records[:mid], budget)
        right = self._attempt_and_isolate(source_name, records[mid:], budget)
        return _IsolationOutcome(
            delivered=left.delivered + right.delivered,
            quarantined=left.quarantined + right.quarantined,
            pending=left.pending + right.pending,
            last_category=right.last_category or left.last_category,
            last_error=right.last_error or left.last_error,
            budget_exhausted=left.budget_exhausted or right.budget_exhausted,
        )

    def upload_one_pending_batch(self, source_name: str) -> UploadCycleResult | None:
        """Uploads the OLDEST pending batch for `source_name`, if any.
        Returns None (sends no HTTP request) if nothing is pending."""
        batches = spool.list_pending_batches(self.cfg.spool_root, source_name)
        if not batches:
            return None

        path = batches[0]
        records = spool.read_batch_or_quarantine(self.cfg.spool_root, source_name, path)
        if records is None:
            self.logger.warning("Corrupt spool batch quarantined | source=%s path=%s", source_name, path)
            return UploadCycleResult(source_name, attempted=False, corrupt_batch_quarantined=True)

        budget = _IsolationBudget(self.cfg.uploader_max_isolation_requests)
        chunks = _fit_to_byte_budget(source_name, records, self.cfg.uploader_max_upload_body_bytes)

        delivered: list[dict] = []
        quarantined: list[dict] = []
        pending: list[dict] = []
        last_category: FailureCategory | None = None
        last_error: str | None = None
        budget_exhausted = False

        for chunk in chunks:
            chunk_outcome = self._attempt_and_isolate(source_name, chunk, budget)
            delivered += chunk_outcome.delivered
            quarantined += chunk_outcome.quarantined
            pending += chunk_outcome.pending
            last_category = chunk_outcome.last_category or last_category
            last_error = chunk_outcome.last_error or last_error
            budget_exhausted = budget_exhausted or chunk_outcome.budget_exhausted

        fully_resolved = not pending and not budget_exhausted

        if fully_resolved:
            if quarantined:
                spool.quarantine_records(
                    self.cfg.spool_root, source_name, quarantined,
                    reason=last_error or "permanent rejection, isolated",
                )
            spool.acknowledge(path)
            if delivered:
                self.logger.info(
                    "Batch delivered | source=%s records=%s", source_name, len(delivered)
                )
        else:
            spool.record_attempt_failure(
                path,
                error=last_error or "unknown error",
                category=last_category or FailureCategory.RETRYABLE_INFRA,
            )
            self.logger.warning(
                "Batch upload failed | source=%s category=%s error=%s",
                source_name, last_category, last_error,
            )

        return UploadCycleResult(
            source_name,
            attempted=True,
            delivered_records=len(delivered),
            quarantined_records=len(quarantined),
            pending_records=len(pending),
            category=last_category,
            isolation_budget_exhausted=budget_exhausted,
            last_error=last_error,
        )

    def run_cycle(self) -> list[UploadCycleResult]:
        """One round-robin pass: at most one batch per configured source.

        SHADOW MODE (cfg.upload_enabled=False): returns [] immediately,
        without listing pending batches or sending any HTTP request --
        spool content is left completely untouched (nothing acknowledged,
        nothing attempted, nothing recorded as a failure). This is a
        single early return, not a broken URL or invalid token, so no
        RETRYABLE_INFRA/AUTH_FAILURE bookkeeping or backoff growth is
        ever triggered by validation mode being active -- an empty result
        list is treated by run_forever exactly like "nothing pending,"
        the normal steady poll cadence, no noise.
        """
        if not self.cfg.upload_enabled:
            return []

        results = []
        for source_cfg in self.cfg.sources:
            result = self.upload_one_pending_batch(source_cfg.name)
            if result is not None:
                results.append(result)
        return results

    def run_forever(
        self,
        *,
        stop_event: threading.Event,
        backoff: Backoff | None = None,
        max_iterations: int | None = None,
    ) -> None:
        """Drains the spool in a loop until stop_event is set.

        Steady poll_interval cadence while there's pending data and
        progress is being made; backoff grows on a cycle that attempted
        something but made no progress (pure RETRYABLE_INFRA/AUTH_FAILURE),
        and always applies when any source's isolation budget was
        exhausted this cycle, even alongside partial progress elsewhere --
        a chronically pathological backlog can't re-exhaust its budget
        every poll_interval_seconds in a tight loop.
        """
        if not self.cfg.upload_enabled:
            self.logger.info(
                "Uploader starting in SHADOW MODE (upload_enabled=false) -- "
                "spool will accumulate, no /upload requests will be sent"
            )

        active_backoff = backoff or Backoff(
            base_seconds=self.cfg.uploader_backoff_base_seconds,
            max_seconds=self.cfg.uploader_backoff_max_seconds,
            multiplier=self.cfg.uploader_backoff_multiplier,
        )
        iterations = 0

        while max_iterations is None or iterations < max_iterations:
            if stop_event.is_set():
                return

            results = self.run_cycle()
            iterations += 1

            if not results:
                delay = self.cfg.uploader_poll_seconds
            elif any(r.isolation_budget_exhausted for r in results):
                delay = active_backoff.next_delay()
            elif any(r.made_progress for r in results):
                active_backoff.reset()
                delay = self.cfg.uploader_poll_seconds
            else:
                delay = active_backoff.next_delay()

            if (max_iterations is None or iterations < max_iterations) and stop_event.wait(delay):
                return
