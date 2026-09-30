"""Tests for collector/config.py -- SortView Collector v1 (Phase 4a)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import collector
from collector import api_token_store
from collector import config as config_module
from collector.config import ConfigError, load_config


def _write_config(tmp_path, **overrides):
    doc = {
        "customer_id": 1,
        "branch_id": 1,
        "api_url": "https://sortview-app-2p336.ondigitalocean.app",
        "sources": [
            {"name": "checkins", "path": "C:\\TLCFinalDlls\\Checkins.txt"},
            {"name": "rejects", "path": "C:\\TLCFinalDlls\\Rejects.txt"},
            {"name": "acs", "path": "C:\\TLCFinalDlls\\ACS Log.txt"},
        ],
        "state_path": str(tmp_path / "data" / "state.json"),
        "status_path": str(tmp_path / "data" / "status.json"),
        "log_path": str(tmp_path / "logs" / "collector.log"),
    }
    doc.update(overrides)
    path = tmp_path / "collector_config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_loads_valid_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path)

    cfg = load_config(path)

    assert cfg.customer_id == 1
    assert cfg.branch_id == 1
    assert cfg.api_url == "https://sortview-app-2p336.ondigitalocean.app"
    assert cfg.api_token == "test-token"
    assert [s.name for s in cfg.sources] == ["checkins", "rejects", "acs"]
    assert cfg.source("acs").path == "C:\\TLCFinalDlls\\ACS Log.txt"


def test_api_url_trailing_slash_is_stripped(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path, api_url="https://example.invalid/")

    cfg = load_config(path)
    assert cfg.api_url == "https://example.invalid"


def test_missing_config_file_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does-not-exist.json")


def test_missing_required_key_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    doc = {"customer_id": 1}
    path = tmp_path / "collector_config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_api_token_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    path = _write_config(tmp_path)

    with pytest.raises(ConfigError):
        load_config(path)


def test_api_token_never_read_from_config_file(tmp_path, monkeypatch):
    # Even if a token-shaped field is present in the file, it must be
    # ignored -- the token comes from the environment only.
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "real-token")
    path = _write_config(tmp_path, api_token="token-from-file-must-be-ignored")

    cfg = load_config(path)
    assert cfg.api_token == "real-token"


def test_empty_sources_list_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path, sources=[])

    with pytest.raises(ConfigError):
        load_config(path)


def test_source_missing_name_or_path_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path, sources=[{"name": "checkins"}])

    with pytest.raises(ConfigError):
        load_config(path)


def test_duplicate_source_names_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path, sources=[
        {"name": "checkins", "path": "a.txt"},
        {"name": "checkins", "path": "b.txt"},
    ])

    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_source_lookup_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    cfg = load_config(_write_config(tmp_path))

    with pytest.raises(ConfigError):
        cfg.source("does-not-exist")


def test_numeric_overrides_from_json(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path, max_records_per_batch=250, http_read_timeout=120.0)

    cfg = load_config(path)
    assert cfg.max_records_per_batch == 250
    assert cfg.http_read_timeout == 120.0


def test_numeric_defaults_when_not_specified(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    cfg = load_config(_write_config(tmp_path))

    assert cfg.max_records_per_batch == 1000
    assert cfg.http_connect_timeout == 10.0
    assert cfg.http_read_timeout == 60.0


# --- installation_id ----------------------------------------------------
#
# Required for newly generated commercial configs (the installers write it,
# finish-install validates it), but OPTIONAL at load time so an already
# deployed 1.0.2 config that predates it keeps loading and running.


def test_installation_id_is_loaded_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")

    cfg = load_config(_write_config(tmp_path, installation_id=41))

    assert cfg.installation_id == 41
    assert isinstance(cfg.installation_id, int)


def test_legacy_config_without_installation_id_still_loads_with_none(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    path = _write_config(tmp_path)
    assert "installation_id" not in json.loads(path.read_text(encoding="utf-8"))

    cfg = load_config(path)

    assert cfg.installation_id is None
    # ...and everything else is exactly as before.
    assert (cfg.customer_id, cfg.branch_id) == (1, 1)
    assert [s.name for s in cfg.sources] == ["checkins", "rejects", "acs"]


def test_explicit_null_installation_id_is_treated_as_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")

    assert load_config(_write_config(tmp_path, installation_id=None)).installation_id is None


@pytest.mark.parametrize("bad", [0, -1, "12", "abc", 1.5, True, [3], {"id": 3}])
def test_invalid_installation_id_fails_loudly_at_load(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")

    with pytest.raises(ConfigError, match="installation_id"):
        load_config(_write_config(tmp_path, installation_id=bad))


def test_the_shipped_example_template_placeholder_is_rejected_not_silently_used(monkeypatch):
    # The template's installation_id is a 0 placeholder: copying it without
    # filling it in must not produce a Collector that heartbeats as id 0.
    monkeypatch.setenv("SORTVIEW_API_TOKEN", "test-token")
    template = Path(__file__).resolve().parent.parent / "collector" / "deploy" / "collector_config.example.json"

    with pytest.raises(ConfigError, match="installation_id"):
        load_config(template)


# =====================================================================================================================
# API token resolution (1.0.11): api_token.dpapi first; the environment only when that file is ABSENT
# =====================================================================================================================

DPAPI_TOKEN = "CANARY-dpapi-token-000000000000000001"
ENV_TOKEN = "CANARY-env-token-00000000000000000001"
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="real DPAPI is Windows-only")


def _stored(monkeypatch, token=DPAPI_TOKEN):
    """api_token.dpapi is present and valid for the config's tenant (the store itself is tested in test_collector_api_token_store)."""
    seen = []

    def load(self, customer_id, branch_id):
        seen.append((self.path, customer_id, branch_id))
        return token

    monkeypatch.setattr(api_token_store.DpapiTokenStore, "load", load)
    return seen


def _stored_but_unusable(monkeypatch, code):
    def load(self, customer_id, branch_id):
        raise api_token_store.ApiTokenStoreError(code)

    monkeypatch.setattr(api_token_store.DpapiTokenStore, "load", load)


def test_a_stored_dpapi_token_is_used_and_labelled(tmp_path, monkeypatch):
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    seen = _stored(monkeypatch)

    cfg = load_config(_write_config(tmp_path))

    assert (cfg.api_token, cfg.api_token_source) == (DPAPI_TOKEN, "dpapi")
    assert seen == [(tmp_path / "secrets" / "api_token.dpapi", 1, 1)]  # <root>\secrets, bound to the config's tenant


def test_dpapi_takes_precedence_over_the_environment_when_both_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", ENV_TOKEN)
    _stored(monkeypatch)

    cfg = load_config(_write_config(tmp_path))

    assert (cfg.api_token, cfg.api_token_source) == (DPAPI_TOKEN, "dpapi")


def test_the_environment_is_the_migration_fallback_only_when_the_token_file_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", ENV_TOKEN)

    cfg = load_config(_write_config(tmp_path))  # no api_token.dpapi anywhere under tmp_path

    assert (cfg.api_token, cfg.api_token_source) == (ENV_TOKEN, "environment")


@pytest.mark.parametrize("code", [
    "token_unreadable", "token_exposed", "token_acl_unverified", "token_tenant_mismatch", "token_invalid", "token_store_unavailable",
])
def test_a_present_but_unusable_token_file_fails_closed_and_never_falls_back(tmp_path, monkeypatch, code):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", ENV_TOKEN)  # a perfectly good fallback is RIGHT THERE -- and must not be used
    _stored_but_unusable(monkeypatch, code)

    with pytest.raises(ConfigError) as caught:
        load_config(_write_config(tmp_path))

    message = str(caught.value)
    assert code in message and "NOT used" in message
    assert ENV_TOKEN not in message and caught.value.__cause__ is None and caught.value.__suppress_context__


def test_a_token_file_that_cannot_even_be_checked_fails_closed(tmp_path, monkeypatch):
    # e.g. a non-elevated process that may not look inside the Administrators+SYSTEM-only secrets folder
    monkeypatch.setenv("SORTVIEW_API_TOKEN", ENV_TOKEN)
    real_stat = api_token_store.os.stat

    def denied(path, *args, **kwargs):
        if str(path).endswith("api_token.dpapi"):
            raise PermissionError(13, "Access is denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(api_token_store.os, "stat", denied)
    with pytest.raises(ConfigError, match="token_unreadable"):
        load_config(_write_config(tmp_path))


@windows_only
def test_a_really_corrupt_token_file_with_a_valid_environment_token_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", ENV_TOKEN)
    monkeypatch.setattr(api_token_store, "acl_state_of", lambda *_a, **_k: "protected")
    path = tmp_path / "secrets" / "api_token.dpapi"
    path.parent.mkdir()
    path.write_bytes(b"\x00 not a dpapi blob \x00")

    with pytest.raises(ConfigError, match="token_unreadable"):
        load_config(_write_config(tmp_path))


@windows_only
def test_a_really_stored_token_in_an_exposed_folder_fails_closed(tmp_path, monkeypatch):
    # No ACL stubbing on load: an ordinary temp folder is readable by the current user, so the real check calls it exposed.
    monkeypatch.setenv("SORTVIEW_API_TOKEN", ENV_TOKEN)
    with monkeypatch.context() as trusted:
        trusted.setattr(api_token_store, "acl_state_of", lambda *_a, **_k: "protected")
        api_token_store.DpapiTokenStore(tmp_path / "secrets" / "api_token.dpapi").save(1, 1, DPAPI_TOKEN)

    with pytest.raises(ConfigError, match="token_exposed"):
        load_config(_write_config(tmp_path))


@windows_only
def test_a_really_stored_token_for_another_tenant_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", ENV_TOKEN)
    monkeypatch.setattr(api_token_store, "acl_state_of", lambda *_a, **_k: "protected")
    api_token_store.DpapiTokenStore(tmp_path / "secrets" / "api_token.dpapi").save(1, 2, DPAPI_TOKEN)  # branch 2, config says 1

    with pytest.raises(ConfigError, match="token_tenant_mismatch"):
        load_config(_write_config(tmp_path))


@windows_only
def test_a_really_stored_token_end_to_end(tmp_path, monkeypatch):
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    monkeypatch.setattr(api_token_store, "acl_state_of", lambda *_a, **_k: "protected")
    api_token_store.DpapiTokenStore(tmp_path / "secrets" / "api_token.dpapi").save(1, 1, DPAPI_TOKEN)

    cfg = load_config(_write_config(tmp_path))

    assert (cfg.api_token, cfg.api_token_source) == (DPAPI_TOKEN, "dpapi")


def test_removing_the_token_file_brings_back_the_environment_fallback_during_the_migration(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", ENV_TOKEN)
    path = tmp_path / "secrets" / "api_token.dpapi"
    path.parent.mkdir()
    path.write_bytes(b"damaged")
    with pytest.raises(ConfigError):
        load_config(_write_config(tmp_path))

    path.unlink()  # the documented rollback, only while the old token has not yet been revoked

    assert load_config(_write_config(tmp_path)).api_token_source == "environment"


def test_neither_source_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="Missing API token"):
        load_config(_write_config(tmp_path))


@pytest.mark.parametrize("value", ["", " ", "\t", "\r\n", "   \n  "])
def test_a_blank_or_whitespace_environment_token_is_not_a_token(tmp_path, monkeypatch, value):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", value)
    with pytest.raises(ConfigError, match="Missing API token"):
        load_config(_write_config(tmp_path))


def test_the_token_is_never_in_the_config_repr(tmp_path, monkeypatch):
    monkeypatch.setenv("SORTVIEW_API_TOKEN", ENV_TOKEN)
    cfg = load_config(_write_config(tmp_path))
    assert ENV_TOKEN not in repr(cfg) and ENV_TOKEN not in str(cfg)
    assert "api_token_source='environment'" in repr(cfg)


def test_load_token_settings_needs_no_token_and_derives_the_v2_secret_folder(tmp_path, monkeypatch):
    # Native paths on whatever host runs the tests: <root>\data\state.json -> <root>\secrets\api_token.dpapi, a concrete Path
    # (the store does real file I/O with it).
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    settings = config_module.load_token_settings(_write_config(tmp_path))
    assert (settings.customer_id, settings.branch_id) == (1, 1)
    assert settings.token_path == tmp_path / "secrets" / "api_token.dpapi"
    assert isinstance(settings.token_path, Path)


PRODUCTION_STATE_PATH = r"C:\ProgramData\SortViewCollector\data\state.json"
PRODUCTION_TOKEN_PATH = r"C:\ProgramData\SortViewCollector\secrets\api_token.dpapi"


def test_the_production_windows_state_path_derives_the_production_token_path_on_any_host(monkeypatch):
    # The helper parses with the HOST's path flavor (as collector/v2_config.py does for the v2 secret), and production is
    # Windows. To check the real derivation code under Windows parsing rules even on a Linux CI runner, the module's `Path`
    # is swapped for PureWindowsPath -- the helper itself is unchanged.
    from pathlib import PureWindowsPath

    monkeypatch.setattr(api_token_store, "Path", PureWindowsPath)

    assert api_token_store.default_path(PRODUCTION_STATE_PATH) == PureWindowsPath(PRODUCTION_TOKEN_PATH)


@pytest.mark.skipif(sys.platform != "win32", reason="a concrete Windows Path exists only on Windows; the derivation itself "
                    "is checked on every host by the PureWindowsPath test above")
def test_on_windows_the_unmodified_helper_derives_the_production_token_path_as_a_concrete_path():
    # Where production actually runs, the real (native) Path must give the same answer -- and stay a concrete, I/O-capable Path.
    derived = api_token_store.default_path(PRODUCTION_STATE_PATH)
    assert derived == Path(PRODUCTION_TOKEN_PATH) and isinstance(derived, Path)


def test_an_api_token_path_override_replaces_the_derived_location(tmp_path, monkeypatch):
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    override = tmp_path / "elsewhere" / "token.dpapi"

    settings = config_module.load_token_settings(_write_config(tmp_path, api_token_path=str(override)))

    assert settings.token_path == override and isinstance(settings.token_path, Path)


# --- the migration fallback has a fixed sunset: the very next release ---------------------------------------------------------

def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("-")[0].split("+")[0].split("."))


def test_the_environment_fallback_sunsets_at_the_release_right_after_the_current_one():
    # The fallback exists for exactly ONE release: its sunset is the next patch release after the current version. (When
    # that sunset release is reached, the constant's value becomes a copy of collector.__version__ and the release-readiness
    # gate, READY002, refuses to build until it -- and the fallback -- is deleted.)
    major, minor, patch = _version_tuple(collector.__version__)
    assert _version_tuple(config_module.ENV_TOKEN_FALLBACK_REMOVED_IN) == (major, minor, patch + 1)


@pytest.mark.parametrize("newer", ["sunset", "minor", "major"])
def test_the_environment_fallback_is_gone_from_the_sunset_release_on(tmp_path, monkeypatch, newer):
    # Behavioral, not textual: the SAME config and environment that work today fail closed at the sunset release or later.
    major, minor, patch = _version_tuple(config_module.ENV_TOKEN_FALLBACK_REMOVED_IN)
    version = {"sunset": f"{major}.{minor}.{patch}", "minor": f"{major}.{minor + 1}.0", "major": f"{major + 1}.0.0"}[newer]
    monkeypatch.setenv("SORTVIEW_API_TOKEN", ENV_TOKEN)
    monkeypatch.setattr(config_module, "__version__", version)
    with pytest.raises(ConfigError, match="Missing API token"):
        load_config(_write_config(tmp_path))

    _stored(monkeypatch)  # while a stored token keeps working at every version
    assert load_config(_write_config(tmp_path)).api_token_source == "dpapi"


def test_the_real_release_version_decides_the_fallback_and_the_sunset_release_must_remove_it(tmp_path, monkeypatch):
    # Uses the REAL collector.__version__. Before the sunset the environment still works. From the sunset on, this fails
    # until the fallback code itself (ENV_TOKEN_FALLBACK_REMOVED_IN and its branch) has been deleted -- no comment or string
    # edit can satisfy it.
    monkeypatch.setenv("SORTVIEW_API_TOKEN", ENV_TOKEN)
    removed_in = getattr(config_module, "ENV_TOKEN_FALLBACK_REMOVED_IN", None)
    if removed_in is not None and _version_tuple(collector.__version__) < _version_tuple(removed_in):
        assert load_config(_write_config(tmp_path)).api_token_source == "environment"
    else:
        assert removed_in is None, "the sunset release must remove the environment fallback, not just switch it off"
        with pytest.raises(ConfigError):
            load_config(_write_config(tmp_path))
