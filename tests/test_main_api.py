import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


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
