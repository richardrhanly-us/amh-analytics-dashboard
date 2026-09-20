"""Tests for collector/uploader.py -- SortView Collector v1 (Phase 4a)."""

from __future__ import annotations

import json as json_mod

import requests

import collector
from collector.config import CollectorConfig, SourceConfig
from collector.uploader import (
    FailureCategory,
    build_batches,
    post_status,
    upload_records,
)


class _FakeResponse:
    def __init__(self, status_code, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {"status": "success"}
        self.text = text or json_mod.dumps(self._json_body)

    def json(self):
        return self._json_body


class FakeSession:
    """Replays a scripted sequence of responses per call; records every
    call for assertion."""

    def __init__(self, script=None):
        self.script = list(script) if script is not None else None
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json))
        if self.script is not None and self.script:
            next_item = self.script.pop(0)
            if isinstance(next_item, Exception):
                raise next_item
            return next_item
        return _FakeResponse(200, {"status": "success"})


def _cfg(**overrides):
    kwargs = {
        "customer_id": 1,
        "branch_id": 1,
        "api_url": "https://example.invalid",
        "api_token": "test-token",
        "sources": (SourceConfig(name="checkins", path="Checkins.txt"),),
        "state_path": "state.json",
        "status_path": "status.json",
        "log_path": "collector.log",
    }
    kwargs.update(overrides)
    return CollectorConfig(**kwargs)


# --- build_batches -----------------------------------------------------


def test_build_batches_empty_input_returns_no_batches():
    assert build_batches([], [], [], max_records_per_batch=100) == []


def test_build_batches_single_batch_when_under_limit():
    checkins = [{"barcode": "1"}, {"barcode": "2"}]
    batches = build_batches(checkins, [], [], max_records_per_batch=100)

    assert len(batches) == 1
    assert batches[0]["checkins"] == checkins
    assert batches[0]["rejects"] == []
    assert batches[0]["acs"] == []


def test_build_batches_splits_when_over_limit():
    checkins = [{"barcode": str(i)} for i in range(25)]
    batches = build_batches(checkins, [], [], max_records_per_batch=10)

    assert len(batches) == 3
    assert [len(b["checkins"]) for b in batches] == [10, 10, 5]


def test_build_batches_interleaves_all_three_sources():
    checkins = [{"barcode": "c"}]
    rejects = [{"barcode": "r"}]
    acs = [{"barcode": "a"}]
    batches = build_batches(checkins, rejects, acs, max_records_per_batch=100)

    assert len(batches) == 1
    assert batches[0] == {"checkins": checkins, "rejects": rejects, "acs": acs}


# --- upload_records: success paths --------------------------------------


def test_upload_records_nothing_to_upload_makes_no_http_call():
    session = FakeSession()
    result = upload_records(session, _cfg(), [], [], [])

    assert result.success is True
    assert result.batches_attempted == 0
    assert session.calls == []


def test_upload_records_single_batch_success():
    session = FakeSession()
    checkins = [{"barcode": "1"}]
    result = upload_records(session, _cfg(), checkins, [], [])

    assert result.success is True
    assert result.batches_attempted == 1
    assert result.batches_delivered == 1
    assert len(session.calls) == 1
    assert session.calls[0][0] == "https://example.invalid/upload"
    assert session.calls[0][1]["checkins"] == checkins


def test_upload_records_multiple_batches_all_succeed():
    session = FakeSession()
    checkins = [{"barcode": str(i)} for i in range(25)]
    result = upload_records(session, _cfg(max_records_per_batch=10), checkins, [], [])

    assert result.success is True
    assert result.batches_attempted == 3
    assert result.batches_delivered == 3
    assert len(session.calls) == 3

def test_upload_records_aggregates_inserted_counts_across_batches():
    session = FakeSession(
        script=[
            _FakeResponse(
                200,
                {
                    "status": "success",
                    "checkins_inserted": 10,
                    "rejects_inserted": 2,
                    "acs_inserted": 4,
                },
            ),
            _FakeResponse(
                200,
                {
                    "status": "success",
                    "checkins_inserted": 5,
                    "rejects_inserted": 1,
                    "acs_inserted": 3,
                },
            ),
        ]
    )

    checkins = [{"barcode": str(i)} for i in range(15)]
    rejects = [{"barcode": str(i)} for i in range(3)]
    acs = [{"barcode": str(i)} for i in range(7)]

    result = upload_records(
        session,
        _cfg(max_records_per_batch=10),
        checkins,
        rejects,
        acs,
    )

    assert result.success is True
    assert result.checkins_inserted == 15
    assert result.rejects_inserted == 3
    assert result.acs_inserted == 7

# --- upload_records: multi-batch partial-failure semantics ---------------


def test_upload_records_stops_at_first_failed_batch(tmp_path):
    session = FakeSession(script=[
        _FakeResponse(200, {"status": "success"}),  # batch 1: succeeds
        _FakeResponse(500, text="server error"),      # batch 2: fails
    ])
    checkins = [{"barcode": str(i)} for i in range(30)]  # 3 batches of 10

    result = upload_records(session, _cfg(max_records_per_batch=10), checkins, [], [])

    assert result.success is False
    assert result.batches_attempted == 2   # batch 3 never attempted
    assert result.batches_delivered == 1   # only batch 1 actually delivered
    assert result.failure is not None
    assert result.failure.category == FailureCategory.RETRYABLE_INFRA
    assert len(session.calls) == 2  # confirms batch 3's request was never sent


def test_upload_records_first_batch_fails_delivers_nothing():
    session = FakeSession(script=[_FakeResponse(500, text="down")])
    checkins = [{"barcode": str(i)} for i in range(5)]

    result = upload_records(session, _cfg(), checkins, [], [])

    assert result.success is False
    assert result.batches_delivered == 0


# --- failure classification -----------------------------------------------


def test_401_classified_as_auth_failure_with_preserved_message():
    session = FakeSession(script=[_FakeResponse(401, text="Invalid agent token")])
    result = upload_records(session, _cfg(), [{"barcode": "1"}], [], [])

    assert result.failure.category == FailureCategory.AUTH_FAILURE
    assert "Invalid agent token" in result.failure.error


def test_403_scope_mismatch_message_is_preserved_not_genericized():
    # The exact backend message from main.py::authenticate_agent for a
    # customer_id/branch_id mismatch -- must be visible verbatim in the
    # error, not collapsed into a generic "auth failed" string.
    session = FakeSession(script=[
        _FakeResponse(403, text="Token scope does not match customer_id / branch_id")
    ])
    result = upload_records(session, _cfg(), [{"barcode": "1"}], [], [])

    assert result.failure.category == FailureCategory.AUTH_FAILURE
    assert "Token scope does not match customer_id / branch_id" in result.failure.error


def test_403_inactive_token_message_is_preserved():
    session = FakeSession(script=[_FakeResponse(403, text="Agent token is inactive")])
    result = upload_records(session, _cfg(), [{"barcode": "1"}], [], [])

    assert result.failure.category == FailureCategory.AUTH_FAILURE
    assert "Agent token is inactive" in result.failure.error


def test_429_classified_as_retryable_infra():
    session = FakeSession(script=[_FakeResponse(429, text="rate limited")])
    result = upload_records(session, _cfg(), [{"barcode": "1"}], [], [])
    assert result.failure.category == FailureCategory.RETRYABLE_INFRA


def test_400_classified_as_permanent_rejection():
    session = FakeSession(script=[_FakeResponse(400, text="bad payload")])
    result = upload_records(session, _cfg(), [{"barcode": "1"}], [], [])
    assert result.failure.category == FailureCategory.PERMANENT_REJECTION


def test_413_classified_as_permanent_rejection():
    session = FakeSession(script=[_FakeResponse(413, text="too large")])
    result = upload_records(session, _cfg(), [{"barcode": "1"}], [], [])
    assert result.failure.category == FailureCategory.PERMANENT_REJECTION


def test_connection_error_classified_as_retryable_infra():
    session = FakeSession(script=[requests.ConnectionError("no route to host")])
    result = upload_records(session, _cfg(), [{"barcode": "1"}], [], [])
    assert result.failure.category == FailureCategory.RETRYABLE_INFRA
    assert "connection error" in result.failure.error


def test_timeout_classified_as_retryable_infra():
    session = FakeSession(script=[requests.Timeout("read timed out")])
    result = upload_records(session, _cfg(), [{"barcode": "1"}], [], [])
    assert result.failure.category == FailureCategory.RETRYABLE_INFRA
    assert "timeout" in result.failure.error


def test_non_json_200_response_classified_as_retryable_infra():
    class _BadJsonResponse(_FakeResponse):
        def json(self):
            raise ValueError("not json")

    session = FakeSession(script=[_BadJsonResponse(200, text="<html>oops</html>")])
    result = upload_records(session, _cfg(), [{"barcode": "1"}], [], [])
    assert result.failure.category == FailureCategory.RETRYABLE_INFRA


# --- status posting (best-effort) -----------------------------------------


def test_post_status_success():
    session = FakeSession()
    outcome = post_status(session, _cfg(), {"status": "completed"})
    assert outcome.success is True
    assert session.calls[0][0] == "https://example.invalid/upload-pipeline-status"
    assert session.calls[0][1]["customer_id"] == 1
    assert session.calls[0][1]["branch_id"] == 1


def test_post_status_failure_does_not_raise():
    session = FakeSession(script=[_FakeResponse(500, text="down")])
    outcome = post_status(session, _cfg(), {"status": "completed"})
    assert outcome.success is False
    assert outcome.category == FailureCategory.RETRYABLE_INFRA


# --- installation lifecycle linkage in the status heartbeat ---------------


def test_post_status_sends_installation_id_and_the_running_collector_version():
    session = FakeSession()

    outcome = post_status(session, _cfg(installation_id=41), {"status": "completed"})

    assert outcome.success is True
    payload = session.calls[0][1]
    assert payload["installation_id"] == 41
    assert payload["collector_version"] == collector.__version__
    # Existing heartbeat fields are unchanged.
    assert payload["status"] == "completed"
    assert (payload["customer_id"], payload["branch_id"]) == (1, 1)


def test_post_status_without_installation_id_is_exactly_the_legacy_payload():
    session = FakeSession()

    post_status(session, _cfg(), {"status": "completed"})

    assert session.calls[0][1] == {"status": "completed", "customer_id": 1, "branch_id": 1}


def test_post_status_never_sends_a_hostname_identity():
    session = FakeSession()

    post_status(session, _cfg(installation_id=41), {"status": "completed"})

    assert "hostname" not in session.calls[0][1]


def test_a_status_dict_cannot_override_the_configured_installation_identity():
    session = FakeSession()

    post_status(
        session, _cfg(installation_id=41),
        {"status": "completed", "installation_id": 999, "collector_version": "spoofed", "customer_id": 7},
    )

    payload = session.calls[0][1]
    assert payload["installation_id"] == 41
    assert payload["collector_version"] == collector.__version__
    assert payload["customer_id"] == 1


def test_upload_records_payload_never_carries_installation_fields():
    session = FakeSession()

    upload_records(session, _cfg(installation_id=41), [{"barcode": "1"}], [], [])

    payload = session.calls[0][1]
    assert "installation_id" not in payload and "collector_version" not in payload


def test_collector_reports_current_release_version():
    # Deliberately a literal, updated by hand with every release bump: it is what the running Collector reports
    # on each heartbeat, so an accidental change (or a forgotten bump) must fail here.
    assert collector.__version__ == "1.0.4"
