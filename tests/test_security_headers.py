"""The API's security headers (B2): exact values, on success and error responses alike, with CORS and the Origin guard
unchanged.

The React apps' headers (CSP among them) are set by CloudFront, not here: docs/react-hosting-runbook.md, section N.
"""

import pytest
from fastapi.testclient import TestClient

import main

EXPECTED = {
    "strict-transport-security": "max-age=31536000",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}
APP_ORIGIN = "https://app.example.test"

client = TestClient(main.app)


def _security_headers(response) -> dict[str, str]:
    return {name: response.headers.get(name) for name in EXPECTED}


@pytest.mark.parametrize("method,path,status", [
    ("GET", "/", 200),                                   # an ordinary response
    ("GET", "/api/auth/session", 401),                  # signed out: the customer API's own error
    ("GET", "/no-such-route", 404),                     # the framework's not-found
    ("POST", "/upload", 422),                           # a request the validation handler refuses
])
def test_every_response_carries_exactly_the_api_security_headers(method, path, status):
    response = client.request(method, path)

    assert response.status_code == status
    assert _security_headers(response) == EXPECTED


def test_an_oversized_request_refused_by_the_body_limit_still_carries_them():
    response = client.post("/upload", content=b"x" * (main.MAX_REQUEST_BODY_BYTES + 1),
                           headers={"Content-Type": "application/json"})

    assert response.status_code == 413
    assert _security_headers(response) == EXPECTED


def test_hsts_is_this_host_only_and_never_preloaded():
    value = client.get("/").headers["strict-transport-security"]

    assert "includesubdomains" not in value.lower() and "preload" not in value.lower()


def test_the_api_sends_no_content_security_policy():
    # Deliberate: API responses are JSON. The React apps' CSP is CloudFront's, on their default behavior only.
    response = client.get("/")

    assert "content-security-policy" not in response.headers


def test_the_origin_guard_is_unchanged(monkeypatch):
    monkeypatch.setenv("SORTVIEW_CUSTOMER_ALLOWED_ORIGINS", APP_ORIGIN)

    refused = client.post("/api/auth/logout", headers={"Origin": "https://evil.example.test"})
    allowed = client.post("/api/auth/logout", headers={"Origin": APP_ORIGIN})

    assert refused.status_code == 403 and refused.json()["code"] == "origin_not_allowed"
    assert allowed.status_code == 204
    assert _security_headers(refused) == EXPECTED and _security_headers(allowed) == EXPECTED


def test_a_cors_preflight_is_unchanged_and_carries_them():
    origin = main.ALLOWED_ORIGINS[0]

    response = client.options("/", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert _security_headers(response) == EXPECTED
