"""Characterization tests for src/database.py::get_database_url.

Pins the current resolution order exactly: the DATABASE_URL environment
variable first, then Streamlit secrets, otherwise RuntimeError("DATABASE_URL
is not set."). A later change that makes the Streamlit import lazy (Phase 0,
Block 1b) must leave every case below unchanged.

streamlit.secrets is replaced on the streamlit module itself -- never via
database.st -- so these tests stay valid whether database.py imports
streamlit at module level or inside the function. Every URL here is a
made-up placeholder; no real environment value or secret is read or printed.
"""

import pytest
import streamlit

import database

ENV_URL = "postgresql://env-user@env-host.invalid/env_db"
SECRETS_URL = "postgresql://secrets-user@secrets-host.invalid/secrets_db"


class RecordingSecrets:
    """Stands in for st.secrets: records every lookup and returns a fixed value or raises."""

    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.lookups = []

    def get(self, key, default=None):
        self.lookups.append(key)
        if self.error is not None:
            raise self.error
        return self.value if self.value is not None else default


@pytest.fixture
def no_env_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)


def test_the_environment_variable_wins_and_secrets_are_not_consulted(monkeypatch):
    secrets = RecordingSecrets(value=SECRETS_URL)
    monkeypatch.setenv("DATABASE_URL", ENV_URL)
    monkeypatch.setattr(streamlit, "secrets", secrets)

    assert database.get_database_url() == ENV_URL
    assert secrets.lookups == []


def test_secrets_are_the_fallback_when_the_environment_variable_is_absent(monkeypatch, no_env_url):
    secrets = RecordingSecrets(value=SECRETS_URL)
    monkeypatch.setattr(streamlit, "secrets", secrets)

    assert database.get_database_url() == SECRETS_URL
    assert secrets.lookups == ["DATABASE_URL"]


def test_an_empty_environment_variable_falls_back_to_secrets(monkeypatch):
    secrets = RecordingSecrets(value=SECRETS_URL)
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setattr(streamlit, "secrets", secrets)

    assert database.get_database_url() == SECRETS_URL


def test_neither_source_raises_runtime_error(monkeypatch, no_env_url):
    monkeypatch.setattr(streamlit, "secrets", RecordingSecrets(value=None))

    with pytest.raises(RuntimeError) as excinfo:
        database.get_database_url()
    assert str(excinfo.value) == "DATABASE_URL is not set."


def test_an_empty_secret_raises_runtime_error(monkeypatch, no_env_url):
    monkeypatch.setattr(streamlit, "secrets", RecordingSecrets(value=""))

    with pytest.raises(RuntimeError) as excinfo:
        database.get_database_url()
    assert str(excinfo.value) == "DATABASE_URL is not set."


@pytest.mark.parametrize("error", [FileNotFoundError("no secrets.toml"), KeyError("DATABASE_URL"), RuntimeError("boom")])
def test_a_failing_secrets_lookup_raises_the_same_runtime_error(monkeypatch, no_env_url, error):
    monkeypatch.setattr(streamlit, "secrets", RecordingSecrets(error=error))

    with pytest.raises(RuntimeError) as excinfo:
        database.get_database_url()
    assert str(excinfo.value) == "DATABASE_URL is not set."
