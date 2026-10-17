"""Characterization tests for src/database.py::get_database_url.

The DATABASE_URL environment variable is the only source: there is no other fallback, and an absent or empty
value raises RuntimeError("DATABASE_URL is not set."). Every URL here is a made-up placeholder; no real
environment value or secret is read or printed.
"""

import pytest

import database

ENV_URL = "postgresql://env-user@env-host.invalid/env_db"


def test_the_environment_variable_is_the_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", ENV_URL)

    assert database.get_database_url() == ENV_URL


def test_an_absent_environment_variable_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        database.get_database_url()
    assert str(excinfo.value) == "DATABASE_URL is not set."


def test_an_empty_environment_variable_raises_runtime_error(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")

    with pytest.raises(RuntimeError) as excinfo:
        database.get_database_url()
    assert str(excinfo.value) == "DATABASE_URL is not set."
