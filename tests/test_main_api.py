import re

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

# The token lookup also resolves the tenant behind the token through the
# operational bridge (see main._AGENT_TOKEN_LOOKUP_SQL); a row must carry a
# usable organization_status/branch_status or authenticate_agent rejects it
# with 403. Tenant-state rejection itself is covered in
# test_agent_tenant_authorization.py.
VALID_TOKEN_ROW = {
    "id": 1,
    "customer_id": 100,
    "branch_id": 5,
    "is_active": True,
    "description": "test agent",
    "organization_status": "active",
    "branch_status": "active",
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


# --- uniqueness-simulating fake DB (Phase E double-protection tests) -------
#
# FakeConnection above always returns rowcount=1 -- it never actually
# models a unique constraint, so it cannot exercise "does a duplicate
# upload actually get skipped." UniquenessSimulatingConnection below
# reimplements, in plain Python, the specific unique constraints
# checkins/rejects/acs_events declare (see alembic/versions/
# 26397a3947b1's baseline schema and 45ba2e7befbc's new partial index)
# and simulates a bare `ON CONFLICT DO NOTHING` against them.
#
# IMPORTANT: this proves main.py's OWN call sequencing under Postgres's
# documented, standard ON CONFLICT / partial-unique-index semantics --
# it is NOT a substitute for verifying against a real Postgres instance,
# and must never be read as "Postgres uniqueness behavior was tested
# here." No Docker/Postgres was available in the environment this was
# written in -- see the Phase E report's Remaining Risks section. This
# exists specifically so the "legacy upload X, then new-agent upload of
# the same logical X" family of scenarios can be expressed as real
# assertions instead of being skipped entirely.
#
# Semantic keys below include (customer_id, branch_id) as leading
# columns -- see alembic revision c53c1b536c71's docstring for why the
# original global (barcode, event_time)-shaped indexes were a real
# multi-tenant bug and why (customer_id, branch_id) is the correct scope
# (the same pair src/data_loader.py's _scoped_query and main.py's
# authenticate_agent already use). source_event_id uniqueness is
# likewise scoped by (customer_id, branch_id) -- see 45ba2e7befbc's
# revised docstring for why global source_event_id uniqueness was itself
# a latent cross-tenant risk (an operator copying agent_identity.json
# between installations).

_SEMANTIC_KEYS = {
    "checkins": ("customer_id", "branch_id", "barcode", "event_time"),
    "rejects": ("customer_id", "branch_id", "barcode", "event_time", "error_message"),
    "acs_events": ("customer_id", "branch_id", "event_time", "message_code", "barcode_key"),
}

_SOURCE_EVENT_ID_SCOPE = ("customer_id", "branch_id", "source_event_id")


class UniquenessSimulatingConnection:
    """tokens_by_bearer maps a raw bearer token string -> its token_row,
    so a single shared engine/connection (one simulated database) can
    authenticate MULTIPLE distinct agents/tenants within the same test --
    required for the cross-tenant non-collision tests below, where two
    different (customer_id, branch_id) scopes must write into the same
    simulated `tables` dict through two separately-authenticated
    requests. Unlike FakeConnection (which always returns one fixed
    token_row regardless of which token was actually sent), this reads
    the real bound :token param authenticate_agent() sends."""

    def __init__(self, tokens_by_bearer):
        self.tokens_by_bearer = tokens_by_bearer
        self.tables = {name: [] for name in _SEMANTIC_KEYS}
        self.executed = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.executed.append((sql, params))

        if "FROM agent_tokens" in sql:
            bearer_token = (params or {}).get("token")
            return FakeResult(mapping=self.tokens_by_bearer.get(bearer_token))
        if "UPDATE agent_tokens" in sql:
            return FakeResult(rowcount=1)

        match = re.search(r"INSERT INTO (\w+)", sql)
        if not match:
            return FakeResult(rowcount=1)

        table = match.group(1)
        row = dict(params or {})

        if self._conflicts(table, row):
            return FakeResult(rowcount=0)

        self.tables[table].append(row)
        return FakeResult(rowcount=1)

    def _conflicts(self, table, row):
        semantic_keys = _SEMANTIC_KEYS[table]
        source_event_id = row.get("source_event_id")

        for existing in self.tables[table]:
            if all(existing.get(k) == row.get(k) for k in semantic_keys):
                return True
            if source_event_id is not None and all(
                existing.get(k) == row.get(k) for k in _SOURCE_EVENT_ID_SCOPE
            ):
                return True

        return False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class UniquenessSimulatingEngine:
    def __init__(self, tokens_by_bearer):
        self.tokens_by_bearer = tokens_by_bearer
        self.conn = UniquenessSimulatingConnection(tokens_by_bearer)

    def begin(self):
        # Same underlying `conn.tables` across every `with engine.begin()`
        # block, exactly like separate real requests sharing one durable
        # database -- unlike FakeEngine, which hands back state that
        # doesn't need to persist across calls.
        return self.conn


def use_uniqueness_engine(monkeypatch, tokens_by_bearer=None):
    fake_engine = UniquenessSimulatingEngine(
        tokens_by_bearer=tokens_by_bearer or {"good-token": VALID_TOKEN_ROW}
    )
    monkeypatch.setattr(main, "engine", fake_engine)
    return fake_engine


SECOND_BRANCH_TOKEN_ROW = {
    "id": 2,
    "customer_id": 200,
    "branch_id": 9,
    "is_active": True,
    "description": "second branch test agent",
    "organization_status": "active",
    "branch_status": "active",
}


SOURCE_EVENT_ID_A = "a" * 64
SOURCE_EVENT_ID_B = "b" * 64


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


def second_branch_checkin_row(**overrides):
    row = {
        "customer_id": SECOND_BRANCH_TOKEN_ROW["customer_id"],
        "branch_id": SECOND_BRANCH_TOKEN_ROW["branch_id"],
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


# --- source_event_id validation (Phase E) -----------------------------------


def test_upload_accepts_row_with_valid_source_event_id(monkeypatch):
    use_fake_engine(monkeypatch)
    payload = {"checkins": [base_checkin_row(source_event_id=SOURCE_EVENT_ID_A)]}

    response = client.post("/upload", json=payload, headers=auth_headers())

    assert response.status_code == 200
    assert response.json()["checkins_inserted"] == 1


def test_upload_rejects_malformed_source_event_id():
    payload = {"checkins": [base_checkin_row(source_event_id="not-a-valid-hash")]}

    response = client.post("/upload", json=payload, headers=auth_headers())

    assert response.status_code == 422


def test_upload_rejects_short_source_event_id():
    payload = {"checkins": [base_checkin_row(source_event_id="a" * 63)]}

    response = client.post("/upload", json=payload, headers=auth_headers())

    assert response.status_code == 422


def test_upload_rejects_uppercase_source_event_id():
    # The canonical format is lowercase hex (hashlib.hexdigest()'s own
    # output casing) -- uppercase is deliberately not normalized/accepted
    # silently, to keep exactly one canonical representation.
    payload = {"checkins": [base_checkin_row(source_event_id="A" * 64)]}

    response = client.post("/upload", json=payload, headers=auth_headers())

    assert response.status_code == 422


def test_legacy_upload_without_source_event_id_still_works(monkeypatch):
    # The currently deployed legacy scheduled agent never sends this
    # field at all -- Pydantic must default it to None with no error.
    use_fake_engine(monkeypatch)
    payload = {"checkins": [base_checkin_row()]}

    response = client.post("/upload", json=payload, headers=auth_headers())

    assert response.status_code == 200
    assert response.json()["checkins_inserted"] == 1


# --- double-protection / transport + semantic dedup coexistence (Phase E) --
#
# See the UniquenessSimulatingConnection docstring above: these exercise
# main.py's call sequencing under Postgres's DOCUMENTED unique-index/
# ON CONFLICT semantics, simulated in Python -- not a live Postgres
# instance. Kept as a distinct, clearly-labeled section so this
# limitation is never lost track of.


def test_same_source_event_id_uploaded_twice_stores_once(monkeypatch):
    use_uniqueness_engine(monkeypatch)
    payload = {"checkins": [base_checkin_row(barcode="111", source_event_id=SOURCE_EVENT_ID_A)]}

    first = client.post("/upload", json=payload, headers=auth_headers())
    second = client.post("/upload", json=payload, headers=auth_headers())

    assert first.json()["checkins_inserted"] == 1
    assert second.json()["checkins_inserted"] == 0


def test_ack_lost_style_resend_stores_once(monkeypatch):
    # Simulates the agent believing the upload failed (ACK never arrived)
    # and resending the exact same spooled batch -- same source_event_id,
    # same row content, a plain retry rather than a deliberate re-upload.
    use_uniqueness_engine(monkeypatch)
    payload = {"checkins": [base_checkin_row(barcode="222", source_event_id=SOURCE_EVENT_ID_B)]}

    responses = [client.post("/upload", json=payload, headers=auth_headers()) for _ in range(3)]

    assert [r.json()["checkins_inserted"] for r in responses] == [1, 0, 0]


def test_semantic_duplicate_with_different_source_event_id_still_stores_once(monkeypatch):
    # Same logical transaction (barcode + event_time), but two DIFFERENT
    # source_event_id values -- e.g. reparsed under a different
    # generation. The existing semantic unique index must still catch
    # this even though the new transport identity does not.
    use_uniqueness_engine(monkeypatch)
    first_payload = {"checkins": [base_checkin_row(barcode="333", source_event_id=SOURCE_EVENT_ID_A)]}
    second_payload = {"checkins": [base_checkin_row(barcode="333", source_event_id=SOURCE_EVENT_ID_B)]}

    first = client.post("/upload", json=first_payload, headers=auth_headers())
    second = client.post("/upload", json=second_payload, headers=auth_headers())

    assert first.json()["checkins_inserted"] == 1
    assert second.json()["checkins_inserted"] == 0


def test_semantic_duplicate_with_no_source_event_id_still_stores_once_under_existing_rules(monkeypatch):
    # Two purely legacy uploads (no source_event_id at all) of the same
    # logical transaction -- the pre-existing semantic dedup, completely
    # untouched by this phase, must still be authoritative.
    use_uniqueness_engine(monkeypatch)
    payload = {"checkins": [base_checkin_row(barcode="444")]}

    first = client.post("/upload", json=payload, headers=auth_headers())
    second = client.post("/upload", json=payload, headers=auth_headers())

    assert first.json()["checkins_inserted"] == 1
    assert second.json()["checkins_inserted"] == 0


def test_legacy_first_then_new_agent_same_logical_event_does_not_duplicate(monkeypatch):
    # The hard parallel-validation requirement: the legacy scheduled
    # pipeline stays authoritative while the new agent is validated, so a
    # new-agent upload of something the legacy agent already delivered
    # must not create a second row, even though only the new upload
    # carries a source_event_id at all.
    use_uniqueness_engine(monkeypatch)
    legacy_payload = {"checkins": [base_checkin_row(barcode="555")]}
    new_agent_payload = {"checkins": [base_checkin_row(barcode="555", source_event_id=SOURCE_EVENT_ID_A)]}

    legacy = client.post("/upload", json=legacy_payload, headers=auth_headers())
    new_agent = client.post("/upload", json=new_agent_payload, headers=auth_headers())

    assert legacy.json()["checkins_inserted"] == 1
    assert new_agent.json()["checkins_inserted"] == 0


def test_new_agent_first_then_legacy_same_logical_event_does_not_duplicate(monkeypatch):
    # The inverse ordering -- e.g. the new agent races ahead and delivers
    # first during coexistence, then the legacy pipeline's own scheduled
    # run uploads the same logical transaction with no source_event_id.
    use_uniqueness_engine(monkeypatch)
    new_agent_payload = {"checkins": [base_checkin_row(barcode="666", source_event_id=SOURCE_EVENT_ID_A)]}
    legacy_payload = {"checkins": [base_checkin_row(barcode="666")]}

    new_agent = client.post("/upload", json=new_agent_payload, headers=auth_headers())
    legacy = client.post("/upload", json=legacy_payload, headers=auth_headers())

    assert new_agent.json()["checkins_inserted"] == 1
    assert legacy.json()["checkins_inserted"] == 0


def test_two_different_legacy_rows_with_null_source_event_id_both_insert(monkeypatch):
    # NULL must never collide with another NULL under the partial unique
    # index -- two DIFFERENT logical legacy transactions (different
    # barcodes) must both insert normally, proving the transport-identity
    # layer never interferes with ordinary legacy traffic.
    use_uniqueness_engine(monkeypatch)
    payload = {
        "checkins": [
            base_checkin_row(barcode="777"),
            base_checkin_row(barcode="888"),
        ]
    }

    response = client.post("/upload", json=payload, headers=auth_headers())

    assert response.json()["checkins_inserted"] == 2


def test_rejects_double_protection_same_source_event_id(monkeypatch):
    use_uniqueness_engine(monkeypatch)
    payload = {
        "rejects": [
            {
                "customer_id": VALID_TOKEN_ROW["customer_id"],
                "branch_id": VALID_TOKEN_ROW["branch_id"],
                "event_time": "2026-07-27T09:00:00",
                "barcode": "999",
                "message": "Item Not Found",
                "source_event_id": SOURCE_EVENT_ID_A,
            }
        ]
    }

    first = client.post("/upload", json=payload, headers=auth_headers())
    second = client.post("/upload", json=payload, headers=auth_headers())

    assert first.json()["rejects_inserted"] == 1
    assert second.json()["rejects_inserted"] == 0


def test_acs_double_protection_same_source_event_id(monkeypatch):
    use_uniqueness_engine(monkeypatch)
    payload = {
        "acs": [
            {
                "customer_id": VALID_TOKEN_ROW["customer_id"],
                "branch_id": VALID_TOKEN_ROW["branch_id"],
                "event_time": "2026-07-27T09:00:00",
                "message_code": "CK",
                "barcode": "12345",
                "source_event_id": SOURCE_EVENT_ID_A,
            }
        ]
    }

    first = client.post("/upload", json=payload, headers=auth_headers())
    second = client.post("/upload", json=payload, headers=auth_headers())

    assert first.json()["acs_inserted"] == 1
    assert second.json()["acs_inserted"] == 0


# --- cross-tenant non-collision (multi-tenant scoping correction) --------
#
# Mandatory per the multi-tenant audit: the original global semantic
# indexes ((barcode, event_time) etc., no tenant column) would have
# silently merged these into one row. After alembic revision
# c53c1b536c71 scopes them by (customer_id, branch_id), they must not.


def test_same_barcode_event_time_different_customer_creates_two_rows(monkeypatch):
    use_uniqueness_engine(
        monkeypatch, tokens_by_bearer={"good-token": VALID_TOKEN_ROW, "other-token": SECOND_BRANCH_TOKEN_ROW}
    )
    library_a_payload = {"checkins": [base_checkin_row()]}
    library_b_payload = {"checkins": [second_branch_checkin_row()]}

    library_a = client.post("/upload", json=library_a_payload, headers=auth_headers("good-token"))
    library_b = client.post("/upload", json=library_b_payload, headers=auth_headers("other-token"))

    assert library_a.json()["checkins_inserted"] == 1
    assert library_b.json()["checkins_inserted"] == 1  # NOT treated as a duplicate of library A's row


def test_same_barcode_event_time_different_branch_same_customer_creates_two_rows(monkeypatch):
    # Two branches of the SAME customer_id -- branch_id alone must also
    # be part of the scope, not just customer_id.
    branch_two_token = dict(SECOND_BRANCH_TOKEN_ROW, customer_id=VALID_TOKEN_ROW["customer_id"], branch_id=77)
    use_uniqueness_engine(
        monkeypatch, tokens_by_bearer={"good-token": VALID_TOKEN_ROW, "branch-two-token": branch_two_token}
    )
    branch_one_payload = {"checkins": [base_checkin_row()]}
    branch_two_payload = {
        "checkins": [base_checkin_row(branch_id=branch_two_token["branch_id"])]
    }

    branch_one = client.post("/upload", json=branch_one_payload, headers=auth_headers("good-token"))
    branch_two = client.post("/upload", json=branch_two_payload, headers=auth_headers("branch-two-token"))

    assert branch_one.json()["checkins_inserted"] == 1
    assert branch_two.json()["checkins_inserted"] == 1


def test_copied_agent_identity_across_tenants_does_not_collide(monkeypatch):
    # Simulates the exact risk that drove scoping source_event_id by
    # (customer_id, branch_id) in 45ba2e7befbc: an operator accidentally
    # reuses the same agent_id (e.g. a copied agent_identity.json) across
    # two DIFFERENT installations. The resulting source_event_id can
    # legitimately collide as a raw string, but the two uploads belong to
    # two different tenants and must both be stored.
    use_uniqueness_engine(
        monkeypatch, tokens_by_bearer={"good-token": VALID_TOKEN_ROW, "other-token": SECOND_BRANCH_TOKEN_ROW}
    )
    library_a_payload = {"checkins": [base_checkin_row(source_event_id=SOURCE_EVENT_ID_A)]}
    library_b_payload = {
        "checkins": [second_branch_checkin_row(barcode="99999", source_event_id=SOURCE_EVENT_ID_A)]
    }

    library_a = client.post("/upload", json=library_a_payload, headers=auth_headers("good-token"))
    library_b = client.post("/upload", json=library_b_payload, headers=auth_headers("other-token"))

    assert library_a.json()["checkins_inserted"] == 1
    assert library_b.json()["checkins_inserted"] == 1


def test_unrelated_events_across_branches_never_accidentally_collide(monkeypatch):
    # General coexistence sanity check: a batch of otherwise-unrelated
    # legitimate events from two different branches, mixing legacy
    # (no source_event_id) and new-agent (source_event_id present) rows,
    # must all land as independent rows.
    use_uniqueness_engine(
        monkeypatch, tokens_by_bearer={"good-token": VALID_TOKEN_ROW, "other-token": SECOND_BRANCH_TOKEN_ROW}
    )
    library_a_payload = {
        "checkins": [
            base_checkin_row(barcode="AAA", source_event_id=SOURCE_EVENT_ID_A),
            base_checkin_row(barcode="BBB"),
        ]
    }
    library_b_payload = {
        "checkins": [
            second_branch_checkin_row(barcode="AAA", source_event_id=SOURCE_EVENT_ID_B),
            second_branch_checkin_row(barcode="BBB"),
        ]
    }

    library_a = client.post("/upload", json=library_a_payload, headers=auth_headers("good-token"))
    library_b = client.post("/upload", json=library_b_payload, headers=auth_headers("other-token"))

    assert library_a.json()["checkins_inserted"] == 2
    assert library_b.json()["checkins_inserted"] == 2


def test_upload_sql_uses_bare_on_conflict_do_nothing_for_all_three_tables(monkeypatch):
    # Guards the specific mechanism the double-protection design depends
    # on: a bare `ON CONFLICT DO NOTHING` (no target), which is what lets
    # one INSERT absorb either the semantic OR the source_event_id
    # unique index without the caller needing to know which one applies.
    fake_engine = use_fake_engine(monkeypatch)
    payload = {
        "checkins": [base_checkin_row(source_event_id=SOURCE_EVENT_ID_A)],
        "rejects": [
            {
                "customer_id": VALID_TOKEN_ROW["customer_id"],
                "branch_id": VALID_TOKEN_ROW["branch_id"],
                "barcode": "1",
                "message": "Item Not Found",
            }
        ],
        "acs": [
            {
                "customer_id": VALID_TOKEN_ROW["customer_id"],
                "branch_id": VALID_TOKEN_ROW["branch_id"],
                "barcode": "1",
            }
        ],
    }

    client.post("/upload", json=payload, headers=auth_headers())

    conn = fake_engine.connections[-1]
    insert_statements = [sql for sql, _params in conn.executed if "INSERT INTO" in sql]
    assert len(insert_statements) == 3
    for sql in insert_statements:
        assert "ON CONFLICT DO NOTHING" in sql
        assert "ON CONFLICT (" not in sql
        assert "source_event_id" in sql
