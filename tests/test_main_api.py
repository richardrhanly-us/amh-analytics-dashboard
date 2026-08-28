import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

import main

client = TestClient(main.app)


def make_request(headers=None, client_host="203.0.113.10"):
    """Minimal Starlette Request for unit-testing get_agent_rate_limit_key
    directly, without going through a real ASGI call."""
    headers = headers or {}
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "headers": raw_headers,
        "client": (client_host, 12345),
        "method": "POST",
        "path": "/upload",
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    # The limiter's in-memory storage persists across requests within a
    # process; reset it before each test so one test's request count can't
    # push another test over the limit.
    main.limiter.reset()

VALID_TOKEN_ROW = {
    "id": 1,
    "customer_id": 100,
    "branch_id": 5,
    "is_active": True,
    "description": "test agent",
}


class FakeResult:
    def __init__(self, rowcount=1, mapping=None):
        self.rowcount = rowcount
        self._mapping = mapping

    def mappings(self):
        return self

    def first(self):
        return self._mapping


class FakeConnection:
    def __init__(self, token_row):
        self.token_row = token_row
        self.executed = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append((sql, params))

        if "FROM agent_tokens" in sql:
            return FakeResult(mapping=self.token_row)

        return FakeResult(rowcount=1)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeEngine:
    def __init__(self, token_row=None):
        self.token_row = token_row
        self.connections = []

    def begin(self):
        conn = FakeConnection(self.token_row)
        self.connections.append(conn)
        return conn


def use_fake_engine(monkeypatch, token_row=VALID_TOKEN_ROW):
    fake_engine = FakeEngine(token_row=token_row)
    monkeypatch.setattr(main, "engine", fake_engine)
    return fake_engine


def auth_headers(token="good-token"):
    return {"Authorization": f"Bearer {token}"}


def base_checkin_row(**overrides):
    row = {
        "customer_id": VALID_TOKEN_ROW["customer_id"],
        "branch_id": VALID_TOKEN_ROW["branch_id"],
        "event_time": "2026-07-27T09:00:00",
        "barcode": "12345",
    }
    row.update(overrides)
    return row


# --- request validation (no DB involved) ----------------------------------

def test_upload_rejects_row_missing_customer_id():
    payload = {"checkins": [{"branch_id": 5, "event_time": "2026-07-27T09:00:00"}]}

    response = client.post("/upload", json=payload, headers=auth_headers())

    assert response.status_code == 422


def test_upload_rejects_non_integer_branch_id():
    payload = {"checkins": [{"customer_id": 100, "branch_id": "not-a-number"}]}

    response = client.post("/upload", json=payload, headers=auth_headers())

    assert response.status_code == 422


def test_upload_empty_payload_returns_400():
    response = client.post(
        "/upload",
        json={"checkins": [], "rejects": [], "acs": []},
        headers=auth_headers(),
    )

    assert response.status_code == 400
    assert "No upload rows" in response.json()["detail"]


def test_upload_mismatched_customer_ids_returns_400():
    payload = {
        "checkins": [
            base_checkin_row(customer_id=100),
            base_checkin_row(customer_id=999),
        ]
    }

    response = client.post("/upload", json=payload, headers=auth_headers())

    assert response.status_code == 400
    assert "same customer_id and branch_id" in response.json()["detail"]


# --- authentication ---------------------------------------------------------

def test_upload_missing_auth_header_returns_401(monkeypatch):
    use_fake_engine(monkeypatch)
    payload = {"checkins": [base_checkin_row()]}

    response = client.post("/upload", json=payload)

    assert response.status_code == 401


def test_upload_unknown_token_returns_401(monkeypatch):
    use_fake_engine(monkeypatch, token_row=None)
    payload = {"checkins": [base_checkin_row()]}

    response = client.post("/upload", json=payload, headers=auth_headers("bad-token"))

    assert response.status_code == 401


def test_upload_token_scoped_to_different_branch_returns_403(monkeypatch):
    mismatched_token = dict(VALID_TOKEN_ROW, branch_id=999)
    use_fake_engine(monkeypatch, token_row=mismatched_token)
    payload = {"checkins": [base_checkin_row()]}

    response = client.post("/upload", json=payload, headers=auth_headers())

    assert response.status_code == 403


# --- success path -----------------------------------------------------------

def test_upload_success_returns_insert_counts(monkeypatch):
    use_fake_engine(monkeypatch)
    payload = {"checkins": [base_checkin_row(), base_checkin_row(barcode="67890")]}

    response = client.post("/upload", json=payload, headers=auth_headers())

    assert response.status_code == 200
    body = response.json()
    assert body["checkins_received"] == 2
    assert body["checkins_inserted"] == 2


def test_upload_pipeline_status_missing_required_field_returns_422():
    response = client.post(
        "/upload-pipeline-status",
        json={"branch_id": 5},
        headers=auth_headers(),
    )

    assert response.status_code == 422


def test_upload_pipeline_status_success(monkeypatch):
    use_fake_engine(monkeypatch)
    payload = {
        "customer_id": VALID_TOKEN_ROW["customer_id"],
        "branch_id": VALID_TOKEN_ROW["branch_id"],
        "status": "completed",
        "destination_breakdown": {"Main": 5},
    }

    response = client.post(
        "/upload-pipeline-status", json=payload, headers=auth_headers()
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"


def test_upload_pipeline_status_heartbeat_success(monkeypatch):
    use_fake_engine(monkeypatch)
    payload = {
        "customer_id": VALID_TOKEN_ROW["customer_id"],
        "branch_id": VALID_TOKEN_ROW["branch_id"],
        "health_status": "degraded",
        "pending_outbox_count": 3,
        "quarantined_count": 0,
        "oldest_pending_event_at": None,
        "last_success_at": "2026-08-28T10:00:00.000000Z",
        "last_failure_category": "retryable_infra",
        "last_error": "connection refused",
        "watcher_last_active_at": "2026-08-28T10:05:00.000000Z",
    }

    response = client.post(
        "/upload-pipeline-status", json=payload, headers=auth_headers()
    )

    assert response.status_code == 200
    assert response.json()["status"] == "success"


def test_upload_pipeline_status_rejects_invalid_health_status():
    payload = {
        "customer_id": VALID_TOKEN_ROW["customer_id"],
        "branch_id": VALID_TOKEN_ROW["branch_id"],
        "health_status": "not-a-real-status",
    }

    response = client.post(
        "/upload-pipeline-status", json=payload, headers=auth_headers()
    )

    assert response.status_code == 422


# --- pipeline_status partial-update mechanism (_build_pipeline_status_upsert) --
#
# main._build_pipeline_status_upsert is the function responsible for the
# omitted-vs-explicit-null partial update semantics that two independent
# writers (the legacy scheduled uploader and the new heartbeat component)
# depend on to coexist safely against the same (customer_id, branch_id)
# row. These are unit tests against that function directly -- there is no
# Postgres available in this repo's CI (see the Phase 3 report), so this
# is what actually exercises the column-selection logic; it does not
# prove the resulting SQL executes correctly against a real database.


def test_upsert_legacy_only_request_updates_only_legacy_fields():
    data = main.PipelineStatusRequest(
        customer_id=1, branch_id=1, status="completed", checkins_rows=5,
    )
    sql, _params = main._build_pipeline_status_upsert(data)

    for field in ["status", "checkins_rows"]:
        assert f"{field} = :{field}" in sql
    for field in main._PIPELINE_STATUS_HEARTBEAT_FIELDS:
        assert f"{field} = :{field}" not in sql


def test_upsert_heartbeat_only_request_updates_only_heartbeat_fields():
    data = main.PipelineStatusRequest(
        customer_id=1, branch_id=1, health_status="healthy", pending_outbox_count=0,
    )
    sql, _params = main._build_pipeline_status_upsert(data)

    for field in ["health_status", "pending_outbox_count"]:
        assert f"{field} = :{field}" in sql
    for field in main._PIPELINE_STATUS_LEGACY_FIELDS:
        assert f"{field} = :{field}" not in sql


def test_upsert_omitted_field_is_absent_from_update_set():
    data = main.PipelineStatusRequest(customer_id=1, branch_id=1, health_status="healthy")
    sql, _params = main._build_pipeline_status_upsert(data)

    # pending_outbox_count was never supplied -- must not appear in the
    # UPDATE SET clause at all (an omitted field must never overwrite a
    # previously-stored value).
    assert "pending_outbox_count = :pending_outbox_count" not in sql


def test_upsert_explicit_null_field_is_present_in_update_set():
    data = main.PipelineStatusRequest(
        customer_id=1, branch_id=1, health_status="healthy", last_error=None,
    )
    # last_error explicitly passed as None -- this differs from never
    # mentioning it at all.
    assert "last_error" in data.model_fields_set

    sql, params = main._build_pipeline_status_upsert(data)

    assert "last_error = :last_error" in sql
    assert params["last_error"] is None


def test_upsert_always_touches_updated_at():
    data = main.PipelineStatusRequest(customer_id=1, branch_id=1)
    sql, _params = main._build_pipeline_status_upsert(data)

    assert "updated_at = CURRENT_TIMESTAMP" in sql


def test_upsert_destination_breakdown_explicit_empty_dict_is_provided():
    # The legacy client always sends destination_breakdown (defaulting to
    # {} when it has nothing to report) -- {} is a provided value, not an
    # omission, and must still appear in the UPDATE SET clause.
    data = main.PipelineStatusRequest(customer_id=1, branch_id=1, destination_breakdown={})
    sql, params = main._build_pipeline_status_upsert(data)

    assert "destination_breakdown = CAST(:destination_breakdown AS JSONB)" in sql
    assert params["destination_breakdown"] == "{}"


def test_upsert_destination_breakdown_omitted_binds_none():
    data = main.PipelineStatusRequest(customer_id=1, branch_id=1, health_status="healthy")
    sql, params = main._build_pipeline_status_upsert(data)

    assert "destination_breakdown = CAST(:destination_breakdown AS JSONB)" not in sql
    # Still bound in params (used by the INSERT branch), but never
    # referenced by the UPDATE SET clause above.
    assert params["destination_breakdown"] is None


def test_upsert_only_uses_fixed_allowlist_column_names():
    # Guards against ever building SQL from caller-controlled field names:
    # every column reference in the UPDATE SET clause must come from the
    # fixed Python allowlists, not from arbitrary request data.
    data = main.PipelineStatusRequest(
        customer_id=1, branch_id=1, status="completed", health_status="healthy",
    )
    sql, _params = main._build_pipeline_status_upsert(data)

    referenced = {
        part.split(" = ")[0].strip()
        for part in sql.split("SET", 1)[1].split("WHERE", 1)[0].split(",")
    }
    referenced.discard("updated_at")
    assert referenced <= set(main._PIPELINE_STATUS_UPDATABLE_FIELDS)


# --- rate limiting and request hardening ------------------------------------

def test_upload_rate_limited_after_too_many_requests():
    limit = int(main.UPLOAD_RATE_LIMIT.split("/")[0])

    responses = [
        client.post("/upload", json={"checkins": [], "rejects": [], "acs": []})
        for _ in range(limit + 1)
    ]

    assert responses[-1].status_code == 429


def test_upload_request_body_too_large_returns_413(monkeypatch):
    monkeypatch.setattr(main, "MAX_REQUEST_BODY_BYTES", 10)
    payload = {"checkins": [base_checkin_row()]}

    response = client.post("/upload", json=payload, headers=auth_headers())

    assert response.status_code == 413


# --- rate-limit key: per-agent identity, not per-IP -------------------------


def test_rate_limit_key_is_hash_based_not_the_raw_token():
    token = "super-secret-token-value"
    request = make_request(headers={"Authorization": f"Bearer {token}"})

    key = main.get_agent_rate_limit_key(request)

    assert key.startswith("agent:")
    assert token not in key


def test_rate_limit_key_same_token_produces_same_key():
    request_a = make_request(headers={"Authorization": "Bearer same-token"})
    request_b = make_request(headers={"Authorization": "Bearer same-token"}, client_host="10.0.0.9")

    assert main.get_agent_rate_limit_key(request_a) == main.get_agent_rate_limit_key(request_b)


def test_rate_limit_key_different_tokens_produce_different_keys():
    request_a = make_request(headers={"Authorization": "Bearer token-a"})
    request_b = make_request(headers={"Authorization": "Bearer token-b"})

    assert main.get_agent_rate_limit_key(request_a) != main.get_agent_rate_limit_key(request_b)


def test_rate_limit_key_falls_back_to_ip_when_auth_header_missing():
    request = make_request(headers={}, client_host="198.51.100.5")

    key = main.get_agent_rate_limit_key(request)

    assert key == "ip:198.51.100.5"


def test_rate_limit_key_falls_back_to_ip_when_auth_header_malformed():
    request = make_request(headers={"Authorization": "NotBearer whatever"}, client_host="198.51.100.5")

    key = main.get_agent_rate_limit_key(request)

    assert key == "ip:198.51.100.5"


def test_rate_limit_key_falls_back_to_ip_when_bearer_token_empty():
    request = make_request(headers={"Authorization": "Bearer  "}, client_host="198.51.100.5")

    key = main.get_agent_rate_limit_key(request)

    assert key == "ip:198.51.100.5"


def test_rate_limit_isolated_per_token_not_shared_across_agents():
    limit = int(main.UPLOAD_RATE_LIMIT.split("/")[0])
    empty_payload = {"checkins": [], "rejects": [], "acs": []}

    # Exhaust the limit for one agent token.
    responses_a = [
        client.post("/upload", json=empty_payload, headers=auth_headers(token="agent-a-token"))
        for _ in range(limit + 1)
    ]
    assert responses_a[-1].status_code == 429

    # A different agent token must not be affected by agent A's usage --
    # this is the whole point of keying by token instead of by IP (the
    # TestClient always presents the same client IP for both).
    response_b = client.post(
        "/upload", json=empty_payload, headers=auth_headers(token="agent-b-token")
    )
    assert response_b.status_code != 429
