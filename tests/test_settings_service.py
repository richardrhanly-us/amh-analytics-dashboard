import pytest

from src.services import settings_service


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    # load_runtime_settings is st.cache_data-wrapped (dashboard
    # performance pass); its cache is process-global, so clear it
    # before/after every test to keep these tests isolated from each
    # other and from other test modules, matching the existing
    # convention in tests/test_data_loader_refresh.py.
    settings_service.load_runtime_settings.clear()
    yield
    settings_service.load_runtime_settings.clear()


def _fake_effective_settings(org_slug, branch_slug=None):
    return {
        "organization": {"id": 1, "name": f"Org {org_slug}", "slug": org_slug},
        "branch": {"id": 1, "name": branch_slug or "Main", "slug": branch_slug or "main", "is_primary": True},
        "subscription": None,
        "settings": {},
        "entitlements": {},
    }


def test_cache_hits_on_repeated_call_same_args(monkeypatch, tmp_path):
    calls = []

    def counting(org_slug, branch_slug=None):
        calls.append((org_slug, branch_slug))
        return _fake_effective_settings(org_slug, branch_slug)

    monkeypatch.setattr(settings_service, "get_effective_settings", counting)
    settings_file = tmp_path / "branch_settings.json"

    for _ in range(5):
        settings_service.load_runtime_settings(settings_file, org_slug="acme", branch_slug="main")

    assert len(calls) == 1


def test_cache_is_scoped_per_org_and_branch(monkeypatch, tmp_path):
    # A different (org_slug, branch_slug) pair must never share a cache
    # entry -- one tenant's settings can never leak into another
    # tenant's or another branch's result.
    calls = []

    def counting(org_slug, branch_slug=None):
        calls.append((org_slug, branch_slug))
        return _fake_effective_settings(org_slug, branch_slug)

    monkeypatch.setattr(settings_service, "get_effective_settings", counting)
    settings_file = tmp_path / "branch_settings.json"

    acme_settings = settings_service.load_runtime_settings(settings_file, org_slug="acme", branch_slug="main")
    other_settings = settings_service.load_runtime_settings(settings_file, org_slug="other-org", branch_slug="main")

    assert len(calls) == 2
    assert acme_settings["LIBRARY_NAME"] != other_settings["LIBRARY_NAME"]


def test_falls_back_to_file_when_database_lookup_raises(monkeypatch, tmp_path):
    def raising(org_slug, branch_slug=None):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(settings_service, "get_effective_settings", raising)

    settings_file = tmp_path / "branch_settings.json"
    settings_file.write_text(
        '{"library": {"library_name": "Fallback Library"}}',
        encoding="utf-8",
    )

    result = settings_service.load_runtime_settings(settings_file, org_slug="acme", branch_slug="main")

    assert result["source"] == "file_fallback"
    assert result["LIBRARY_NAME"] == "Fallback Library"
    assert "settings_error" in result
