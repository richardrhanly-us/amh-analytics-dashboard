"""Contract v2: orchestration (docs/collector-v2.md).

    secret + rules + cache + quarantine -> for each source (ACS first): bounded chunk -> transform -> deliver -> commit that chunk's cursor
    -> heartbeat (/v2/status) -> exit

THE PRIVACY BOUNDARY IS THE IMPORT GRAPH. This module never imports the raw layer (reader, normalizer, classifier, parsers, pandas). It calls
`v2_transform.read_next_chunk`, which returns typed safe events and integers; a raw string has no way in. Every log line and status value here is
a fixed code, an integer or an enum. Exceptions are described by TYPE and code LOCATION only (`describe`), never by message.

PROGRESSIVE COMMIT. A chunk's cursor (and the patron-cache changes it produced) is committed only after EVERY event of the chunk was either
delivered or safely quarantined. If delivery fails, nothing about that chunk is committed and the run stops (the next scheduled run re-reads it;
identical resends are idempotent on the server).

409 / 422 HANDLING. The server rejects a request atomically. For a 409 `event_conflict` (or a 422 that pins an event), the reported positions are
removed, quarantined as safe metadata (event_key, kind, normalized time, fixed reason, date), and the REST of the batch is re-sent. The server
reports at most 50 conflicts per list per response, so this repeats round by round until no conflict remains (bounded). A quarantined event is
skipped on every later run.

NO OUTBOX. The Tech Logic files are the durable queue and the cursor is the only progress marker, so `pending_outbox_count` is 0.

CONTRACT MODE. Nothing here runs unless `contract_mode` is "v2" (or `--v2-dry-run` is given): existing configs default to v1.
"""

from __future__ import annotations

import logging
import secrets
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from . import state, uploader
from .config import CollectorConfig, ConfigError
from .v2_config import DryRunSettings, V2Config, load_dry_run_settings, load_v2_config
from .v2_events import KINDS, Cursor, SafeEvent, format_time
from .v2_identity import derive_subkeys
from .v2_keys import DpapiSecretStore, SecretStore, SecretStoreError, require_protected
from .v2_patrons import PatronCache
from .v2_quarantine import Quarantine, QuarantineEntry
from .v2_rules import RulesError, load_rules
from .v2_safe_errors import CollectorV2Error, TransformError, describe
from .v2_status import (
    AUTH,
    CONFIG,
    OTHER,
    PERMANENT,
    RETRYABLE,
    StatusSnapshot,
    decide,
    local_status_document,
    parse_prior,
)
from .v2_transform import (
    SOURCE_ORDER,
    Counters,
    TransformContext,
    read_next_chunk,
    read_tail_chunk,
)
from .v2_uploader import CONFLICT, FAILED, INVALID, OK, Batch, post_batch, post_status

_MAX_ROUNDS = 60  # a batch of up to 1000 events at 50 reported conflicts per response needs at most ~21


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Runtime:
    ctx: TransformContext
    quarantine: Quarantine


def build_runtime(v2: V2Config, store: SecretStore, now: datetime) -> Runtime:
    """A normal run's setup. It FAILS CLOSED, with a fixed code and no detail, on every problem with the persistent secret: missing,
    unreadable, bound to another key_id, or in a folder whose ACL is known-insecure (`secret_exposed`) OR cannot be verified
    (`secret_acl_unverified`). Only a verified-protected ACL proceeds. (The dry run never comes through here: it uses a throwaway key.)"""
    if not store.exists():
        raise SecretStoreError("secret_missing")
    require_protected(store.acl_state())
    master = store.load(v2.key_id)
    keys = derive_subkeys(master)
    del master
    rules = load_rules(v2.rules_path, keys)
    cache = PatronCache(v2.patron_cache_path, ttl_days=v2.patron_ttl_days, max_rows=v2.patron_cache_max_rows,
                        hold_days=v2.hold_ledger_days, hold_max_rows=v2.hold_ledger_max_rows, today=now.date())
    quarantine = Quarantine(v2.quarantine_path, max_entries=v2.quarantine_max_entries, today=now.date())
    ctx = TransformContext(keys=keys, rules=rules, ruleset_id=cache.ruleset_id_for(rules.fingerprint), cache=cache,
                           zone=v2.zone(), now=now)
    return Runtime(ctx, quarantine)


def _failure_category(exc: BaseException) -> str:
    if isinstance(exc, (SecretStoreError, RulesError, ConfigError)):
        return CONFIG
    if isinstance(exc, CollectorV2Error) and exc.code in ("state_corrupt", "unknown_source"):
        return CONFIG
    return OTHER


class _Totals:
    def __init__(self) -> None:
        self.chunks = 0
        self.batches = 0
        self.inserted = 0
        self.duplicates = 0
        self.quarantined_conflict = 0
        self.quarantined_invalid = 0
        self.skipped_quarantined = 0
        self.sources_missing = 0
        self.sources_rotated = 0
        self.sources_truncated = 0

    def flat(self) -> dict[str, int]:
        return {"chunks_committed": self.chunks, "batches_sent": self.batches, "events_inserted": self.inserted,
                "events_server_duplicates": self.duplicates, "events_quarantined_conflict": self.quarantined_conflict,
                "events_quarantined_invalid": self.quarantined_invalid, "events_skipped_quarantined": self.skipped_quarantined,
                "sources_missing": self.sources_missing,
                "sources_rotated": self.sources_rotated, "sources_truncated": self.sources_truncated}


def _deliver(events: list[SafeEvent], *, cfg: CollectorConfig, v2: V2Config, session: Any, quarantine: Quarantine, today: str,
             totals: _Totals, sleep: Callable[[float], None]) -> str | None:
    """Delivers a chunk's events. Returns None when every event was delivered or safely quarantined, else a failure category."""
    for start in range(0, len(events), v2.max_events_per_request):
        batch = Batch.of(v2.key_id, events[start:start + v2.max_events_per_request])
        rounds = 0
        while len(batch):
            rounds += 1
            if rounds > _MAX_ROUNDS:
                return PERMANENT
            outcome = post_batch(session, cfg, v2, batch)
            if outcome.result != FAILED and v2.request_interval_seconds > 0:
                sleep(v2.request_interval_seconds)  # paced after EVERY request, 409 retry rounds included, to stay under the rate limit
            if outcome.result == OK:
                totals.batches += 1
                totals.inserted += outcome.inserted
                totals.duplicates += outcome.duplicates
                break
            if outcome.result in (CONFLICT, INVALID):
                batch, removed = batch.without(outcome.positions)
                if not removed:
                    return PERMANENT  # no progress possible: never loop
                reason = "event_conflict" if outcome.result == CONFLICT else "invalid_event"
                quarantine.add([QuarantineEntry(e.event_key, e.kind, format_time(e.event_time), reason, today) for e in removed])
                if outcome.result == CONFLICT:
                    totals.quarantined_conflict += len(removed)
                else:
                    totals.quarantined_invalid += len(removed)
                continue
            return outcome.failure or OTHER
    return None


def _process_sources(*, cfg: CollectorConfig, v2: V2Config, runtime: Runtime, session: Any, logger: logging.Logger,
                     counters: Counters, totals: _Totals, sleep: Callable[[float], None], now: datetime) -> str | None:
    corrupt = False
    try:
        current = state.load_state(v2.state_path)
    except state.CorruptStateError:
        corrupt = True
    if corrupt:
        raise CollectorV2Error("state_corrupt")

    configured = {source.name: source for source in cfg.sources}
    if any(source_name not in SOURCE_ORDER for source_name in configured):
        raise CollectorV2Error("unknown_source")

    today = now.date().isoformat()
    budget = v2.max_chunks_per_run
    for source_name in SOURCE_ORDER:  # ACS first: it fills the patron cache the guard uses for the others
        source = configured.get(source_name)
        if source is None:
            continue
        saved = state.get_source(current, source_name)
        cursor = Cursor(saved.identity, saved.offset) if saved is not None else None
        while budget > 0:
            chunk = read_next_chunk(source_name, source.path, cursor, runtime.ctx, v2.chunk_lines)
            if not chunk.existed:
                totals.sources_missing += 1
                logger.warning("v2 source missing | source=%s", source_name)
                break
            totals.sources_rotated += int(chunk.rotated)
            totals.sources_truncated += int(chunk.truncated)
            if chunk.empty:
                if chunk.cursor is not None and chunk.cursor != cursor:  # e.g. a rotated file with no complete line yet
                    current = state.with_source(current, source_name, state.SourceState(chunk.cursor.identity, chunk.cursor.offset))
                    state.save_state(v2.state_path, current)
                    cursor = chunk.cursor
                break

            skipped = {kind: runtime.quarantine.keys_for(kind) for kind in KINDS}  # built once per chunk, not once per event
            events = [e for e in chunk.events if e.event_key not in skipped[e.kind]]
            totals.skipped_quarantined += len(chunk.events) - len(events)
            failure = _deliver(events, cfg=cfg, v2=v2, session=session, quarantine=runtime.quarantine, today=today,
                               totals=totals, sleep=sleep)
            if failure is not None:
                runtime.ctx.cache.discard()  # nothing about this chunk is committed
                logger.error("v2 delivery failed | source=%s category=%s", source_name, failure)
                return failure

            if chunk.cursor is None:  # a non-empty chunk always has a cursor; never commit a guess
                runtime.ctx.cache.discard()
                raise CollectorV2Error("internal_error")
            current = state.with_source(current, source_name, state.SourceState(chunk.cursor.identity, chunk.cursor.offset))
            # Cache first, cursor second: a crash between the two re-reads this chunk (idempotent), never skips a patron profile.
            runtime.ctx.cache.commit()
            state.save_state(v2.state_path, current)
            cursor = chunk.cursor
            counters.merge(chunk.counters)
            totals.chunks += 1
            budget -= 1
            logger.info("v2 chunk committed | source=%s lines=%s events=%s dropped_patron_card=%s", source_name,
                        chunk.counters.lines_read, len(events), chunk.counters.dropped_patron_card)
            if not chunk.more:
                break
    return None


def run_once_v2(cfg: CollectorConfig, v2: V2Config, *, session: Any, logger: logging.Logger, store: SecretStore | None = None,
                sleep: Callable[[float], None] = time.sleep, clock: Callable[[], datetime] = _utc_now) -> int:
    """One v2 run. Returns the process exit code: 0 completed, 1 failed, 2 a configuration problem."""
    now = clock()
    store = store or DpapiSecretStore(v2.secret_path)
    prior = state.load_status(v2.status_path)
    prior_failures, prior_success = parse_prior(prior)
    counters, totals = Counters(), _Totals()
    failure: str | None = None
    runtime: Runtime | None = None
    quarantine_before = prior.get("quarantined_count", 0) if isinstance(prior.get("quarantined_count", 0), int) else 0

    try:
        runtime = build_runtime(v2, store, now)
        expired, evicted = runtime.ctx.cache.purge()
        holds_purged = runtime.ctx.cache.purge_holds()
        runtime.quarantine.prune_and_save()
        quarantine_before = runtime.quarantine.count
        logger.info("v2 run started | patron_rows_expired=%s patron_rows_evicted=%s holds_purged=%s", expired, evicted, holds_purged)
        failure = _process_sources(cfg=cfg, v2=v2, runtime=runtime, session=session, logger=logger, counters=counters,
                                   totals=totals, sleep=sleep, now=now)
    except Exception as exc:  # described by type and location only, never by message
        failure = _failure_category(exc)
        logger.error("v2 run failed | category=%s detail=%s", failure, describe(exc))
    finally:
        quarantined_now = runtime.quarantine.count if runtime is not None else quarantine_before
        if runtime is not None:
            runtime.ctx.cache.close()

    new_quarantined = totals.quarantined_conflict + totals.quarantined_invalid
    consecutive = 0 if failure is None else prior_failures + 1
    health, error_class = decide(failure=failure, new_quarantined=new_quarantined, sources_missing=totals.sources_missing,
                                 consecutive_failures=consecutive, error_after=v2.error_after_consecutive_failures)
    last_success = now if failure is None else prior_success
    snapshot = StatusSnapshot(status=health, last_error_class=error_class, quarantined_count=quarantined_now,
                              last_success_at=last_success, watcher_last_active_at=now)
    flat = {**counters.flat(), **totals.flat()}
    try:
        state.write_status(v2.status_path, local_status_document(
            now=now, ok=failure is None, health=health, last_error_class=error_class, consecutive_failures=consecutive,
            last_success_at=last_success, counters=flat, quarantined_count=quarantined_now))
    except OSError as exc:
        logger.warning("v2 status file not written | detail=%s", describe(exc))

    heartbeat = post_status(session, cfg, v2, snapshot)
    if not heartbeat.ok:
        logger.warning("v2 heartbeat failed | category=%s status_code=%s", heartbeat.failure, heartbeat.status_code)
    logger.info("v2 run finished | health=%s last_error_class=%s %s", health, error_class,
                " ".join(f"{k}={v}" for k, v in sorted(totals.flat().items())))
    if failure is None:
        return 0
    return 2 if failure == CONFIG else 1


# --- the dry run ---------------------------------------------------------------------------------------------------------------------

def run_dry(settings: DryRunSettings, *, out: TextIO, clock: Callable[[], datetime] = _utc_now) -> int:
    """`run --v2-dry-run`: transforms a bounded TAIL of each source and prints only aggregate safe counts.

    It is INDEPENDENT of every credential: it needs no API token, no server url or key registration, and it never opens the persistent secret
    (or checks its ACL). Its identifiers come from a THROWAWAY key generated in memory and discarded on exit, so they mean nothing outside the
    process and cannot be compared with anything a real run sent. It has no network client, commits no cursor, keeps its patron cache and hold
    ledger in memory only, opens no state, status, quarantine or log file, and prints nothing but `name=integer` lines."""
    now = clock()
    keys = derive_subkeys(secrets.token_bytes(32))
    rules = load_rules(settings.rules_path, keys)
    cache = PatronCache.in_memory(today=now.date())
    configured = dict(settings.sources)
    if any(name not in SOURCE_ORDER for name in configured):
        cache.close()
        raise CollectorV2Error("unknown_source")
    ctx = TransformContext(keys=keys, rules=rules, ruleset_id=cache.ruleset_id_for(rules.fingerprint), cache=cache,
                           zone=settings.zone(), now=now)
    totals = Counters()
    try:
        for source_name in SOURCE_ORDER:
            path = configured.get(source_name)
            if path is None:
                continue
            chunk = read_tail_chunk(source_name, path, ctx, settings.dry_run_tail_bytes)
            totals.merge(chunk.counters)
            print(f"source_{source_name}_present={int(Path(path).exists())}", file=out)
    finally:
        cache.discard()
        cache.close()
    print("throwaway_key=1", file=out)
    print("persistent_secret_used=0", file=out)
    for counter, value in sorted(totals.flat().items()):
        print(f"{counter}={value}", file=out)
    print("network_calls=0", file=out)
    print("dry_run_complete=1", file=out)
    return 0


# --- entry points --------------------------------------------------------------------------------------------------------------------

def main_v2_dry_run(config_path: str, *, out: TextIO | None = None) -> int:
    """Called by collector/run.py for `--v2-dry-run`, BEFORE the v1 configuration or any credential is read."""
    out = out or sys.stdout
    try:
        return run_dry(load_dry_run_settings(config_path), out=out)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except CollectorV2Error as exc:
        print(f"Configuration error: {exc.code}", file=sys.stderr)
        return 2 if _failure_category(exc) == CONFIG else 1
    except Exception as exc:
        print(f"Dry run failed: {describe(exc)}", file=sys.stderr)
        return 1


def main_v2(cfg: CollectorConfig, config_path: str, *, logger: logging.Logger) -> int:
    """Called by collector/run.py for `contract_mode: v2`."""
    try:
        v2 = load_v2_config(config_path, require=True)
        if v2 is None:  # unreachable with require=True; keeps the type honest without an assert
            return 2
        session = uploader.build_session()
        return run_once_v2(cfg, v2, session=session, logger=logger)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except CollectorV2Error as exc:
        print(f"Configuration error: {exc.code}", file=sys.stderr)
        return 2 if _failure_category(exc) == CONFIG else 1
    except Exception as exc:
        logger.error("v2 run crashed | detail=%s", describe(exc))
        return 1


__all__ = ["AUTH", "RETRYABLE", "TransformError", "build_runtime", "main_v2", "main_v2_dry_run", "run_dry", "run_once_v2"]
