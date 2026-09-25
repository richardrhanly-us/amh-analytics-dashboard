"""Unit tests for cookie_service's pure logic: staging, name selection, and
component mount decisions.

Whether document.cookie is actually written/read/cleared in a real browser is
not testable here -- see scratch/cookie_probe.py and its manual browser
validation instructions for that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.services import cookie_service

NOW = datetime(
    2026,
    9,
    25,
    12,
    0,
    0,
    tzinfo=UTC,
)  # freshness: allow FRESH004 -- passed explicitly as now= to every time-sensitive call


@pytest.fixture(autouse=True)
def _isolated_session_state(monkeypatch):
    monkeypatch.setattr(cookie_service.st, "session_state", {})


# --- cookie name selection -------------------------------------------------

def test_cookie_name_uses_host_prefix_when_secure(monkeypatch):
    monkeypatch.setenv("SORTVIEW_COOKIE_SECURE", "true")

    assert cookie_service._cookie_name() == "__Host-sortview_session"


def test_cookie_name_falls_back_to_plain_name_when_not_secure(monkeypatch):
    monkeypatch.setenv("SORTVIEW_COOKIE_SECURE", "false")

    assert cookie_service._cookie_name() == "sortview_session"


# --- get_session_cookie ----------------------------------------------------

def test_get_session_cookie_mounts_reader_and_returns_value(monkeypatch):
    monkeypatch.setenv("SORTVIEW_COOKIE_SECURE", "false")
    calls = []

    def fake_reader(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(value="abc123")

    monkeypatch.setattr(
        cookie_service,
        "_COOKIE_READER",
        fake_reader,
    )

    assert cookie_service.get_session_cookie() == "abc123"

    assert len(calls) == 1
    assert calls[0]["key"] == "sortview_cookie_reader"
    assert calls[0]["data"] == {"name": "sortview_session"}
    assert calls[0]["default"] == {"value": None}
    assert callable(calls[0]["on_value_change"])


def test_get_session_cookie_returns_none_when_reader_reports_no_cookie(monkeypatch):
    monkeypatch.setattr(
        cookie_service,
        "_COOKIE_READER",
        lambda **kwargs: SimpleNamespace(value=None),
    )

    assert cookie_service.get_session_cookie() is None


def test_get_session_cookie_returns_none_on_component_error(monkeypatch):
    def raising_reader(**kwargs):
        raise RuntimeError("component unavailable")

    monkeypatch.setattr(
        cookie_service,
        "_COOKIE_READER",
        raising_reader,
    )

    assert cookie_service.get_session_cookie() is None


# --- set_session_cookie / clear_session_cookie -----------------------------

def test_set_session_cookie_stages_the_expected_pending_op(monkeypatch):
    monkeypatch.setenv("SORTVIEW_COOKIE_SECURE", "true")

    cookie_service.set_session_cookie(
        "raw-token-value",
        NOW + timedelta(days=14),
        now=NOW,
    )

    pending = cookie_service.st.session_state[cookie_service._PENDING_KEY]

    assert pending["action"] == "set"
    assert pending["name"] == "__Host-sortview_session"
    assert pending["value"] == "raw-token-value"
    assert pending["max_age_seconds"] == 14 * 24 * 3600
    assert pending["secure"] is True


def test_set_session_cookie_floors_max_age_at_zero_for_past_expiry(monkeypatch):
    cookie_service.set_session_cookie(
        "raw-token-value",
        NOW - timedelta(days=1),
        now=NOW,
    )

    pending = cookie_service.st.session_state[cookie_service._PENDING_KEY]

    assert pending["max_age_seconds"] == 0


def test_clear_session_cookie_stages_a_clear_op(monkeypatch):
    monkeypatch.setenv("SORTVIEW_COOKIE_SECURE", "false")

    cookie_service.clear_session_cookie()

    pending = cookie_service.st.session_state[cookie_service._PENDING_KEY]

    assert pending["action"] == "clear"
    assert pending["name"] == "sortview_session"
    assert "value" not in pending
    assert "max_age_seconds" not in pending


# --- render_cookie_writer --------------------------------------------------

def test_render_cookie_writer_mounts_once_with_the_staged_data(monkeypatch):
    calls = []

    monkeypatch.setattr(
        cookie_service,
        "_COOKIE_WRITER",
        lambda **kwargs: calls.append(kwargs),
    )

    cookie_service.set_session_cookie(
        "raw-token-value",
        NOW + timedelta(days=14),
        now=NOW,
    )
    cookie_service.render_cookie_writer()

    assert len(calls) == 1
    assert calls[0]["key"] == "sortview_cookie_writer"
    assert calls[0]["data"]["action"] == "set"
    assert calls[0]["data"]["value"] == "raw-token-value"


def test_render_cookie_writer_is_a_no_op_when_nothing_pending(monkeypatch):
    calls = []

    monkeypatch.setattr(
        cookie_service,
        "_COOKIE_WRITER",
        lambda **kwargs: calls.append(kwargs),
    )

    cookie_service.render_cookie_writer()

    assert calls == []


def test_render_cookie_writer_pops_the_pending_op_so_it_mounts_only_once(
    monkeypatch,
):
    calls = []

    monkeypatch.setattr(
        cookie_service,
        "_COOKIE_WRITER",
        lambda **kwargs: calls.append(kwargs),
    )

    cookie_service.set_session_cookie(
        "raw-token-value",
        NOW + timedelta(days=14),
        now=NOW,
    )

    cookie_service.render_cookie_writer()
    cookie_service.render_cookie_writer()

    assert len(calls) == 1
    assert cookie_service._PENDING_KEY not in cookie_service.st.session_state