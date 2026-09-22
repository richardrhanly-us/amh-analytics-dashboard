"""Contract v2: the upload client (docs/collector-v2.md).

It receives ONLY typed, privacy-safe events (collector/v2_events.py) and builds the request field by field from their `payload()` methods.
There is no path from a raw record to here, and nothing it does can add one.

RESPONSES ARE CLASSIFIED BY STATUS CODE ALONE, and never kept. This client never stores, logs or forwards a response body, `str(exc)` or a header:

    200                              delivered (integer counts are read)
    409 event_conflict               the reported request POSITIONS (integers) are permanent conflicts
    422                              positions that name one event are that event's validation failure; anything else is a configuration problem
    401, 403                         auth
    404, 405                         configuration (v2 ingest off, or the wrong URL)
    400, 413                         permanent
    429, 5xx, timeouts, connection   retryable
    anything else                    other

For a 409 and a 422 the only things read from the body are integer positions and the fixed names of the three lists; every other byte of it is
ignored and dropped with the response object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests

from .config import CollectorConfig
from .v2_config import V2Config
from .v2_events import KINDS, SafeEvent
from .v2_status import AUTH, CONFIG, OTHER, PERMANENT, RETRYABLE, StatusSnapshot

OK, CONFLICT, INVALID, FAILED = "ok", "conflict", "invalid", "failed"


@dataclass(frozen=True)
class Batch:
    """One request's events, in the order they were placed in the payload lists, so a reported position maps back to its event."""

    key_id: str
    order: dict[str, tuple[SafeEvent, ...]]

    @classmethod
    def of(cls, key_id: str, events: list[SafeEvent] | tuple[SafeEvent, ...]) -> Batch:
        order: dict[str, list[SafeEvent]] = {kind: [] for kind in KINDS}
        for event in events:
            order[event.kind].append(event)
        return cls(key_id, {kind: tuple(items) for kind, items in order.items()})

    def __len__(self) -> int:
        return sum(len(items) for items in self.order.values())

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"contract_version": 2, "key_id": self.key_id}
        for kind in KINDS:
            if self.order[kind]:
                body[kind] = [event.payload() for event in self.order[kind]]
        return body

    def without(self, positions: dict[str, tuple[int, ...]]) -> tuple[Batch, list[SafeEvent]]:
        """(the batch minus the events at these positions, the removed events)."""
        removed: list[SafeEvent] = []
        kept: dict[str, tuple[SafeEvent, ...]] = {}
        for kind in KINDS:
            drop = set(positions.get(kind, ()))
            kept[kind] = tuple(event for i, event in enumerate(self.order[kind]) if i not in drop)
            removed.extend(event for i, event in enumerate(self.order[kind]) if i in drop)
        return Batch(self.key_id, kept), removed


@dataclass(frozen=True)
class BatchOutcome:
    result: str                                   # ok | conflict | invalid | failed
    failure: str | None = None                    # a FAILURE_CATEGORY when result == failed
    status_code: int | None = None
    positions: dict[str, tuple[int, ...]] = field(default_factory=dict)   # conflict / invalid positions per list
    inserted: int = 0
    duplicates: int = 0


@dataclass(frozen=True)
class StatusOutcome:
    ok: bool
    failure: str | None = None
    status_code: int | None = None


def _headers(cfg: CollectorConfig) -> dict[str, str]:
    return {"Authorization": f"Bearer {cfg.api_token}", "Content-Type": "application/json"}


def _timeout(cfg: CollectorConfig) -> tuple[float, float]:
    return (cfg.http_connect_timeout, cfg.http_read_timeout)


def _failure_for(status_code: int) -> str:
    if status_code in (401, 403):
        return AUTH
    if status_code in (404, 405):
        return CONFIG
    if status_code in (400, 413):
        return PERMANENT
    if status_code == 429 or 500 <= status_code <= 599:
        return RETRYABLE
    return OTHER


def _json(response: Any) -> dict[str, Any] | None:
    try:
        body = response.json()
    except Exception:  # an unreadable body is simply "no body"; its content is never inspected further
        return None
    return body if isinstance(body, dict) else None


def _positions(raw: Any, batch: Batch) -> dict[str, tuple[int, ...]] | None:
    """Integer positions per list from a 409 `conflicts` object; None if anything is out of range or malformed."""
    if not isinstance(raw, dict) or not raw:
        return None
    found: dict[str, tuple[int, ...]] = {}
    for kind, values in raw.items():
        if kind not in KINDS or not isinstance(values, list) or not values:
            return None
        if any(isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < len(batch.order[kind]) for v in values):
            return None
        found[kind] = tuple(sorted(set(values)))
    return found


def _invalid_positions(body: dict[str, Any] | None, batch: Batch) -> dict[str, tuple[int, ...]] | None:
    """Positions a 422 pins to individual events (`loc = body, <list>, <index>, ...`); None if ANY error is not event-level."""
    detail = body.get("detail") if body else None
    if not isinstance(detail, list) or not detail:
        return None
    found: dict[str, set[int]] = {}
    for error in detail:
        loc = error.get("loc") if isinstance(error, dict) else None
        if not (isinstance(loc, list) and len(loc) >= 3 and loc[0] == "body" and loc[1] in KINDS
                and isinstance(loc[2], int) and not isinstance(loc[2], bool) and 0 <= loc[2] < len(batch.order[loc[1]])):
            return None  # an envelope-level problem (key_id, version, a cap): not something to quarantine event by event
        found.setdefault(loc[1], set()).add(loc[2])
    return {kind: tuple(sorted(values)) for kind, values in found.items()}


def _counts(body: dict[str, Any] | None) -> tuple[int, int]:
    def total(suffix: str) -> int:
        return sum(v for kind in KINDS if isinstance(v := (body or {}).get(f"{kind}_{suffix}"), int) and not isinstance(v, bool))

    return total("inserted"), total("duplicates")


def post_batch(session: Any, cfg: CollectorConfig, v2: V2Config, batch: Batch) -> BatchOutcome:
    """POST /v2/upload for one batch. Returns a classification only; no body, no exception text."""
    url = f"{cfg.api_url}/v2/upload"
    try:
        response = session.post(url, json=batch.payload(), headers=_headers(cfg), timeout=_timeout(cfg))
    except requests.RequestException:
        return BatchOutcome(FAILED, RETRYABLE)
    except Exception:
        return BatchOutcome(FAILED, OTHER)

    code = int(response.status_code)
    if code == 200:
        body = _json(response)
        if body is None or body.get("status") != "success":
            return BatchOutcome(FAILED, RETRYABLE, code)
        inserted, duplicates = _counts(body)
        return BatchOutcome(OK, None, code, {}, inserted, duplicates)

    if code == 409:
        body = _json(response)
        positions = _positions(body.get("conflicts") if body and body.get("code") == "event_conflict" else None, batch)
        if positions is None:
            return BatchOutcome(FAILED, PERMANENT, code)
        return BatchOutcome(CONFLICT, None, code, positions)

    if code == 422:
        positions = _invalid_positions(_json(response), batch)
        if positions is None:
            return BatchOutcome(FAILED, CONFIG, code)
        return BatchOutcome(INVALID, None, code, positions)

    return BatchOutcome(FAILED, _failure_for(code), code)


def post_status(session: Any, cfg: CollectorConfig, v2: V2Config, snapshot: StatusSnapshot) -> StatusOutcome:
    """POST /v2/status. Best effort: the caller never fails a run because this did."""
    try:
        response = session.post(f"{cfg.api_url}/v2/status", json=snapshot.payload(v2.key_id), headers=_headers(cfg),
                                timeout=_timeout(cfg))
    except requests.RequestException:
        return StatusOutcome(False, RETRYABLE)
    except Exception:
        return StatusOutcome(False, OTHER)
    code = int(response.status_code)
    if code == 200:
        body = _json(response)
        if body is not None and body.get("status") == "success":
            return StatusOutcome(True, None, code)
        return StatusOutcome(False, RETRYABLE, code)
    return StatusOutcome(False, _failure_for(code), code)
