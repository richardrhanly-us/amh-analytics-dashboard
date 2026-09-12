"""Tests for data_loader.validate_tenant_schema (dashboard performance
pass): it previously ran 4 uncached information_schema.columns queries
on every single Streamlit rerun to check something that cannot change
without a database migration/deploy. Now cached process-wide, since it
takes no tenant-scoped arguments.
"""

import pytest

import data_loader as dl


@pytest.fixture(autouse=True)
def _clear_schema_cache():
    dl.validate_tenant_schema.clear()
    yield
    dl.validate_tenant_schema.clear()


def test_no_errors_when_all_required_columns_present(monkeypatch):
    monkeypatch.setattr(dl, "_table_has_columns", lambda table_name, required_columns: (True, []))

    assert dl.validate_tenant_schema() == []


def test_reports_missing_columns_per_table(monkeypatch):
    def fake_table_has_columns(table_name, required_columns):
        if table_name == "checkins":
            return False, ["customer_id"]
        return True, []

    monkeypatch.setattr(dl, "_table_has_columns", fake_table_has_columns)

    errors = dl.validate_tenant_schema()

    assert errors == [{"table": "checkins", "missing": ["customer_id"]}]


def test_cache_hits_on_repeated_call(monkeypatch):
    calls = []

    def counting(table_name, required_columns):
        calls.append(table_name)
        return True, []

    monkeypatch.setattr(dl, "_table_has_columns", counting)

    for _ in range(5):
        dl.validate_tenant_schema()

    # 4 tables checked once each on the first call; zero on every
    # subsequent call within the TTL.
    assert len(calls) == 4
