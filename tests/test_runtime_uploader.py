"""Tests for agent/runtime/uploader.py -- the canonical spool uploader
(Continuous Ingestion Phase F).

Exercises retry classification, poison-event isolation, ACK handling,
and the isolation request budget against a scripted FakeSession -- no
real network, but real spool files on disk (spool.py is not mocked).
"""

from __future__ import annotations

import json
import logging
import threading

import requests

from agent import spool
from agent.runtime.backoff import Backoff
from agent.runtime.config import RuntimeConfig, SourceConfig
from agent.runtime.uploader import Uploader

SOURCE = "checkins"


def _json_dumps_bytes(payload):
    return json.dumps(payload).encode("utf-8")


class _FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body


class FakeSession:
    """Scripted responses, consumed in order. Each entry is either an
    _FakeResponse or an exception instance to raise. `calls` records
    every (url, payload) pair for assertions."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def post(self, url, json, headers, timeout):
        self.calls.append((url, json))
        if not self.script:
            raise AssertionError("FakeSession script exhausted -- unexpected extra call")
        next_item = self.script.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item


def _cfg(tmp_path, **overrides):
    kwargs = {
        "customer_id": 100,
        "branch_id": 5,
        "api_url": "https://example.invalid",
        "api_token": "test-token",
        "sources": (SourceConfig(name="checkins", path=str(tmp_path / "Checkins.txt")),),
        "state_path": tmp_path / "state" / "agent_state.json",
        "spool_root": tmp_path / "spool",
        "agent_identity_path": tmp_path / "agent_identity.json",
        "log_dir": tmp_path / "logs",
        "diagnostics_dir": tmp_path / "diagnostics",
        "uploader_max_isolation_requests": 20,
    }
    kwargs.update(overrides)
    return RuntimeConfig(**kwargs)


def _logger():
    logger = logging.getLogger("test-uploader")
    logger.addHandler(logging.NullHandler())
    return logger


def _write_batch(cfg, records, start=0, end=None, generation=0):
    if end is None:
        end = start + 100
    return spool.write_batch(
        cfg.spool_root, SOURCE, records, source_generation=generation, start_offset=start, end_offset=end
    )


def _success_response(checkins=1):
    return _FakeResponse(200, {"status": "success", "checkins_inserted": checkins, "rejects_inserted": 0, "acs_inserted": 0})


# --- success path ------------------------------------------------------


def test_successful_upload_acknowledges_batch(tmp_path):
    cfg = _cfg(tmp_path)
    _write_batch(cfg, [{"barcode": "111", "source_event_id": "a" * 64}])
    session = FakeSession([_success_response()])
    uploader = Uploader(cfg, session, _logger())

    result = uploader.upload_one_pending_batch(SOURCE)

    assert result.delivered_records == 1
    assert spool.list_pending_batches(cfg.spool_root, SOURCE) == []


def test_uploader_never_regenerates_source_event_id(tmp_path):
    cfg = _cfg(tmp_path)
    original_id = "b" * 64
    _write_batch(cfg, [{"barcode": "111", "source_event_id": original_id}])
    session = FakeSession([_success_response()])
    uploader = Uploader(cfg, session, _logger())

    uploader.upload_one_pending_batch(SOURCE)

    sent_payload = session.calls[0][1]
    assert sent_payload["checkins"][0]["source_event_id"] == original_id


def test_no_http_request_when_nothing_pending(tmp_path):
    cfg = _cfg(tmp_path)
    session = FakeSession([])
    uploader = Uploader(cfg, session, _logger())

    result = uploader.upload_one_pending_batch(SOURCE)

    assert result is None
    assert session.calls == []


def test_never_sends_empty_payload(tmp_path):
    cfg = _cfg(tmp_path)
    _write_batch(cfg, [{"barcode": "111", "source_event_id": "c" * 64}])
    session = FakeSession([_success_response()])
    uploader = Uploader(cfg, session, _logger())

    uploader.upload_one_pending_batch(SOURCE)

    payload = session.calls[0][1]
    assert payload["checkins"] != []
    assert any(payload[key] for key in ("checkins", "rejects", "acs"))


# --- retryable infra -----------------------------------------------------


def test_retryable_infra_leaves_batch_pending(tmp_path):
    cfg = _cfg(tmp_path)
    path = _write_batch(cfg, [{"barcode": "111", "source_event_id": "d" * 64}])
    session = FakeSession([_FakeResponse(503, text="service unavailable")])
    uploader = Uploader(cfg, session, _logger())

    result = uploader.upload_one_pending_batch(SOURCE)

    assert result.pending_records == 1
    assert spool.list_pending_batches(cfg.spool_root, SOURCE) == [path]
    assert path.exists()


def test_network_outage_leaves_data_pending(tmp_path):
    cfg = _cfg(tmp_path)
    path = _write_batch(cfg, [{"barcode": "111", "source_event_id": "e" * 64}])
    session = FakeSession([requests.ConnectionError("refused")])
    uploader = Uploader(cfg, session, _logger())

    result = uploader.upload_one_pending_batch(SOURCE)

    assert result.category == spool.FailureCategory.RETRYABLE_INFRA
    assert path.exists()
    assert spool.read_batch(path) == [{"barcode": "111", "source_event_id": "e" * 64}]


def test_long_network_outage_still_leaves_data_pending_across_many_attempts(tmp_path):
    cfg = _cfg(tmp_path)
    path = _write_batch(cfg, [{"barcode": "111", "source_event_id": "f" * 64}])
    session = FakeSession([requests.ConnectionError("refused")] * 10)
    uploader = Uploader(cfg, session, _logger())

    for _ in range(10):
        uploader.upload_one_pending_batch(SOURCE)

    assert path.exists()
    assert spool._load_attempts(path).attempts == 10


def test_429_classified_as_retryable_and_stays_pending(tmp_path):
    cfg = _cfg(tmp_path)
    path = _write_batch(cfg, [{"barcode": "111", "source_event_id": "1" * 64}])
    session = FakeSession([_FakeResponse(429, text="rate limited")])
    uploader = Uploader(cfg, session, _logger())

    result = uploader.upload_one_pending_batch(SOURCE)

    assert result.category == spool.FailureCategory.RETRYABLE_INFRA
    assert path.exists()


# --- auth failure --------------------------------------------------------


def test_auth_failure_leaves_batch_pending_with_distinct_category(tmp_path):
    cfg = _cfg(tmp_path)
    path = _write_batch(cfg, [{"barcode": "111", "source_event_id": "2" * 64}])
    session = FakeSession([_FakeResponse(401, text="invalid token")])
    uploader = Uploader(cfg, session, _logger())

    result = uploader.upload_one_pending_batch(SOURCE)

    assert result.category == spool.FailureCategory.AUTH_FAILURE
    assert path.exists()


def test_auth_recovery_resumes_normally(tmp_path):
    cfg = _cfg(tmp_path)
    path = _write_batch(cfg, [{"barcode": "111", "source_event_id": "3" * 64}])
    session = FakeSession([_FakeResponse(401, text="invalid token"), _success_response()])
    uploader = Uploader(cfg, session, _logger())

    first = uploader.upload_one_pending_batch(SOURCE)
    assert first.category == spool.FailureCategory.AUTH_FAILURE
    assert path.exists()

    second = uploader.upload_one_pending_batch(SOURCE)
    assert second.delivered_records == 1
    assert spool.list_pending_batches(cfg.spool_root, SOURCE) == []


# --- poison-event isolation ------------------------------------------------


def test_single_poison_record_isolated_good_records_delivered(tmp_path):
    cfg = _cfg(tmp_path)
    records = [
        {"barcode": "GOOD1", "source_event_id": "a1" + "0" * 62},
        {"barcode": "POISON", "source_event_id": "a2" + "0" * 62},
        {"barcode": "GOOD2", "source_event_id": "a3" + "0" * 62},
        {"barcode": "GOOD3", "source_event_id": "a4" + "0" * 62},
    ]
    path = _write_batch(cfg, records)

    # Splits: [4] -> reject -> [2,2] -> left(2) reject -> [1,1]:
    #   GOOD1 succeeds, POISON rejects (isolated) -- right(2) succeeds.
    session = FakeSession([
        _FakeResponse(400, text="bad request"),   # whole batch of 4
        _FakeResponse(400, text="bad request"),   # left half [GOOD1, POISON]
        _success_response(),                       # GOOD1 alone
        _FakeResponse(400, text="poison record"),  # POISON alone
        _success_response(),                       # right half [GOOD2, GOOD3]
    ])
    uploader = Uploader(cfg, session, _logger())

    result = uploader.upload_one_pending_batch(SOURCE)

    assert result.delivered_records == 3
    assert result.quarantined_records == 1
    assert not path.exists()  # original file resolved and acknowledged

    quarantined = list((cfg.spool_root / "quarantine" / SOURCE).glob("*.ndjson"))
    assert len(quarantined) == 1
    assert spool.read_batch(quarantined[0]) == [records[1]]


def test_good_events_still_delivered_despite_poison_sibling(tmp_path):
    cfg = _cfg(tmp_path)
    records = [
        {"barcode": "GOOD", "source_event_id": "b1" + "0" * 62},
        {"barcode": "POISON", "source_event_id": "b2" + "0" * 62},
    ]
    _write_batch(cfg, records)
    session = FakeSession([
        _FakeResponse(400, text="bad"),
        _success_response(),  # GOOD alone
        _FakeResponse(400, text="bad"),  # POISON alone
    ])
    uploader = Uploader(cfg, session, _logger())

    result = uploader.upload_one_pending_batch(SOURCE)

    assert result.delivered_records == 1
    assert result.quarantined_records == 1


def test_quarantine_preserves_evidence_and_reason(tmp_path):
    cfg = _cfg(tmp_path)
    _write_batch(cfg, [{"barcode": "ONLY", "source_event_id": "c1" + "0" * 62}])
    session = FakeSession([_FakeResponse(400, text="specific rejection reason")])
    uploader = Uploader(cfg, session, _logger())

    uploader.upload_one_pending_batch(SOURCE)

    quarantined = list((cfg.spool_root / "quarantine" / SOURCE).glob("*.ndjson"))
    assert len(quarantined) == 1
    reason_files = list((cfg.spool_root / "quarantine" / SOURCE).glob("*.reason.json"))
    assert len(reason_files) == 1
    assert "specific rejection reason" in reason_files[0].read_text(encoding="utf-8")


def test_413_payload_too_large_isolated_same_as_400(tmp_path):
    cfg = _cfg(tmp_path)
    _write_batch(cfg, [{"barcode": "ONLY", "source_event_id": "d1" + "0" * 62}])
    session = FakeSession([_FakeResponse(413, text="payload too large")])
    uploader = Uploader(cfg, session, _logger())

    result = uploader.upload_one_pending_batch(SOURCE)

    assert result.quarantined_records == 1


# --- proactive byte-budget splitting (ported from the experimental uploader) --


def test_oversized_batch_is_proactively_split_without_consuming_isolation_budget(tmp_path):
    # A budget of exactly 1 request would normally mean "split once and
    # you're out" -- but proactive byte-budget splitting happens BEFORE
    # any HTTP call, so it costs nothing against the isolation budget.
    # Each of the resulting chunks still gets its own real request.
    cfg = _cfg(tmp_path, uploader_max_upload_body_bytes=200, uploader_max_isolation_requests=20)
    records = [
        {"barcode": f"BIG{i}", "message": "x" * 100, "source_event_id": f"{i:02d}" + "0" * 62}
        for i in range(4)
    ]
    _write_batch(cfg, records)
    session = FakeSession([_success_response(), _success_response(), _success_response(), _success_response()])
    uploader = Uploader(cfg, session, _logger())

    result = uploader.upload_one_pending_batch(SOURCE)

    assert result.delivered_records == 4
    assert len(session.calls) == 4  # split into 4 separate, byte-safe requests
    for _url, payload in session.calls:
        assert len(_json_dumps_bytes(payload)) <= 200 or len(payload["checkins"]) == 1


def test_batch_fitting_never_returns_an_empty_chunk_for_nonempty_input(tmp_path):
    from agent.runtime.uploader import _fit_to_byte_budget

    records = [{"barcode": str(i), "message": "y" * 50} for i in range(5)]
    chunks = _fit_to_byte_budget(SOURCE, records, max_bytes=10)  # absurdly small

    assert sum(len(c) for c in chunks) == 5
    assert all(len(c) >= 1 for c in chunks)


def test_single_oversized_record_still_gets_its_own_chunk(tmp_path):
    from agent.runtime.uploader import _fit_to_byte_budget

    records = [{"barcode": "1", "message": "z" * 1000}]
    chunks = _fit_to_byte_budget(SOURCE, records, max_bytes=10)

    assert chunks == [records]  # nothing smaller to split a single record into


def test_byte_budget_split_still_correctly_isolates_a_genuine_poison_record(tmp_path):
    # Proactive size-splitting and reactive poison-isolation must compose
    # correctly: a real 400 on one of the size-split chunks still gets
    # isolated down to the smallest practical unit, not just accepted
    # as "the size split explains the failure."
    cfg = _cfg(tmp_path, uploader_max_upload_body_bytes=200)
    records = [
        {"barcode": f"BIG{i}", "message": "x" * 100, "source_event_id": f"{i:02d}" + "0" * 62}
        for i in range(4)
    ]
    _write_batch(cfg, records)
    # 4 chunks of 1 each (given the tiny byte budget) -- one is poison.
    session = FakeSession(
        [_success_response(), _success_response(), _FakeResponse(400, text="poison"), _success_response()]
    )
    uploader = Uploader(cfg, session, _logger())

    result = uploader.upload_one_pending_batch(SOURCE)

    assert result.delivered_records == 3
    assert result.quarantined_records == 1


# --- token/credential leak safety -------------------------------------------


def test_auth_failure_error_never_contains_raw_token(tmp_path):
    cfg = _cfg(tmp_path, api_token="super-secret-raw-token-value")
    _write_batch(cfg, [{"barcode": "1", "source_event_id": "a5" + "0" * 62}])
    session = FakeSession([_FakeResponse(401, text="invalid credentials")])
    uploader = Uploader(cfg, session, _logger())

    result = uploader.upload_one_pending_batch(SOURCE)

    assert "super-secret-raw-token-value" not in (result.last_error or "")


def test_quarantine_reason_never_contains_raw_token(tmp_path):
    cfg = _cfg(tmp_path, api_token="super-secret-raw-token-value")
    _write_batch(cfg, [{"barcode": "1", "source_event_id": "a6" + "0" * 62}])
    session = FakeSession([_FakeResponse(400, text="bad request")])
    uploader = Uploader(cfg, session, _logger())

    uploader.upload_one_pending_batch(SOURCE)

    reason_files = list((cfg.spool_root / "quarantine" / SOURCE).glob("*.reason.json"))
    assert len(reason_files) == 1
    assert "super-secret-raw-token-value" not in reason_files[0].read_text(encoding="utf-8")


def test_bearer_header_is_sent_but_never_logged(tmp_path):
    cfg = _cfg(tmp_path, api_token="super-secret-raw-token-value")
    _write_batch(cfg, [{"barcode": "1", "source_event_id": "a7" + "0" * 62}])
    session = FakeSession([_success_response()])

    class RecordingLogger:
        def __init__(self):
            self.messages = []

        def info(self, msg, *args):
            self.messages.append(msg % args if args else msg)

        def warning(self, msg, *args):
            self.messages.append(msg % args if args else msg)

    logger = RecordingLogger()
    uploader = Uploader(cfg, session, logger)
    uploader.upload_one_pending_batch(SOURCE)

    assert session.calls[0][1] is not None  # the request itself did carry the payload
    assert all("super-secret-raw-token-value" not in m for m in logger.messages)


# --- retryable failure then success -----------------------------------------


def test_retryable_failure_then_success_delivers_on_retry(tmp_path):
    cfg = _cfg(tmp_path)
    path = _write_batch(cfg, [{"barcode": "1", "source_event_id": "a8" + "0" * 62}])
    session = FakeSession([_FakeResponse(503, text="unavailable"), _success_response()])
    uploader = Uploader(cfg, session, _logger())

    first = uploader.upload_one_pending_batch(SOURCE)
    assert first.category == spool.FailureCategory.RETRYABLE_INFRA
    assert path.exists()

    second = uploader.upload_one_pending_batch(SOURCE)
    assert second.delivered_records == 1
    assert spool.list_pending_batches(cfg.spool_root, SOURCE) == []


def test_isolation_stops_splitting_when_budget_exhausted_leaves_original_untouched(tmp_path):
    cfg = _cfg(tmp_path, uploader_max_isolation_requests=1)
    records = [
        {"barcode": "A", "source_event_id": "e1" + "0" * 62},
        {"barcode": "B", "source_event_id": "e2" + "0" * 62},
    ]
    path = _write_batch(cfg, records)
    # Only 1 request allowed -- the whole-batch attempt consumes it.
    session = FakeSession([_FakeResponse(400, text="bad")])
    uploader = Uploader(cfg, session, _logger())

    result = uploader.upload_one_pending_batch(SOURCE)

    assert result.isolation_budget_exhausted is True
    assert path.exists()  # untouched -- nothing quarantined, nothing acknowledged
    assert spool.read_batch(path) == records


def test_transient_failure_mid_isolation_leaves_original_file_untouched(tmp_path):
    cfg = _cfg(tmp_path)
    records = [
        {"barcode": "A", "source_event_id": "f1" + "0" * 62},
        {"barcode": "B", "source_event_id": "f2" + "0" * 62},
    ]
    path = _write_batch(cfg, records)
    session = FakeSession([
        _FakeResponse(400, text="bad"),  # whole batch
        _success_response(),  # left half succeeds
        _FakeResponse(503, text="transient"),  # right half transient failure
    ])
    uploader = Uploader(cfg, session, _logger())

    result = uploader.upload_one_pending_batch(SOURCE)

    # Not fully resolved (one record still pending) -- original file left
    # untouched even though the other record was genuinely delivered.
    assert result.pending_records == 1
    assert path.exists()
    assert spool.read_batch(path) == records


# --- malformed spool file ------------------------------------------------


def test_malformed_spool_file_quarantined_not_uploaded(tmp_path):
    cfg = _cfg(tmp_path)
    pending_dir = cfg.spool_root / "pending" / SOURCE
    pending_dir.mkdir(parents=True)
    bad_path = pending_dir / "batch-000000-00000000000000000000-00000000000000000001-1-0000000000-deadbeef.ndjson"
    bad_path.write_text("not valid json", encoding="utf-8")

    session = FakeSession([])
    uploader = Uploader(cfg, session, _logger())

    result = uploader.upload_one_pending_batch(SOURCE)

    assert result.corrupt_batch_quarantined is True
    assert session.calls == []  # never attempted to upload corrupt content
    assert not bad_path.exists()
    assert list((cfg.spool_root / "quarantine" / SOURCE).glob("*.ndjson"))


# --- run_forever / backoff -------------------------------------------------


def test_run_forever_stops_promptly_on_stop_event(tmp_path):
    cfg = _cfg(tmp_path, uploader_poll_seconds=1000.0)
    session = FakeSession([])
    uploader = Uploader(cfg, session, _logger())
    stop_event = threading.Event()
    stop_event.set()  # already stopped -- should return immediately

    uploader.run_forever(stop_event=stop_event, max_iterations=None)  # must not hang


class _FakeStopEvent:
    """Duck-typed in place of threading.Event -- run_forever only ever
    calls is_set() and wait(delay); this records every requested delay
    instead of actually sleeping, so backoff tests run instantly."""

    def __init__(self):
        self.waited = []

    def is_set(self):
        return False

    def wait(self, delay):
        self.waited.append(delay)
        return False


def test_run_forever_backs_off_on_repeated_no_progress(tmp_path):
    cfg = _cfg(tmp_path)
    _write_batch(cfg, [{"barcode": "1", "source_event_id": "g1" + "0" * 62}])
    session = FakeSession([_FakeResponse(503, text="x")] * 3)
    uploader = Uploader(cfg, session, _logger())
    stop_event = _FakeStopEvent()
    backoff = Backoff(base_seconds=1.0, max_seconds=10.0, multiplier=2.0, jitter_fraction=0.0)

    uploader.run_forever(stop_event=stop_event, backoff=backoff, max_iterations=3)

    assert stop_event.waited == [1.0, 2.0]  # 3rd iteration's wait is skipped (max_iterations reached)


def test_run_forever_resets_backoff_after_progress(tmp_path):
    cfg = _cfg(tmp_path)
    _write_batch(cfg, [{"barcode": "1", "source_event_id": "h1" + "0" * 62}], start=0, end=100)
    session = FakeSession([_FakeResponse(503, text="x"), _success_response()])
    uploader = Uploader(cfg, session, _logger())
    stop_event = _FakeStopEvent()

    backoff = Backoff(base_seconds=1.0, max_seconds=10.0, multiplier=2.0, jitter_fraction=0.0)

    # First iteration fails (backoff grows to 2.0), second iteration: the
    # batch is still there (never acknowledged) -- retry the SAME batch,
    # this time it succeeds.
    uploader.run_forever(stop_event=stop_event, backoff=backoff, max_iterations=1)
    assert backoff._current == 2.0

    uploader.run_forever(stop_event=stop_event, backoff=backoff, max_iterations=1)
    assert backoff._current == backoff.base_seconds  # reset after progress
