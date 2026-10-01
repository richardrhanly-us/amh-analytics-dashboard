"""Tests for collector/config.py -- SortView Collector v1 (Phase 4a)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from collector_token_support import break_api_token, store_api_token

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
    store_api_token(monkeypatch, "test-token")
    path = _write_config(tmp_path)

    cfg = load_config(path)

    assert cfg.customer_id == 1
    assert cfg.branch_id == 1
    assert cfg.api_url == "https://sortview-app-2p336.ondigitalocean.app"
    assert cfg.api_token == "test-token"
    assert [s.name for s in cfg.sources] == ["checkins", "rejects", "acs"]
    assert cfg.source("acs").path == "C:\\TLCFinalDlls\\ACS Log.txt"


def test_api_url_trailing_slash_is_stripped(tmp_path, monkeypatch):
    store_api_token(monkeypatch, "test-token")
    path = _write_config(tmp_path, api_url="https://example.invalid/")

    cfg = load_config(path)
    assert cfg.api_url == "https://example.invalid"


def test_missing_config_file_raises(tmp_path, monkeypatch):
    store_api_token(monkeypatch, "test-token")
    with pytest.raises(ConfigError):
        load_config(tmp_path / "does-not-exist.json")


def test_missing_required_key_raises(tmp_path, monkeypatch):
    store_api_token(monkeypatch, "test-token")
    doc = {"customer_id": 1}
    path = tmp_path / "collector_config.json"
    path.write_text(json.dumps(doc), encoding="utf-8")

    with pytest.raises(ConfigError):
        load_config(path)


def test_missing_api_token_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    path = _write_config(tmp_path)  # and no api_token.dpapi anywhere under tmp_path

    with pytest.raises(ConfigError):
        load_config(path)


def test_api_token_never_read_from_config_file(tmp_path, monkeypatch):
    # Even if a token-shaped field is present in the file, it must be
    # ignored -- the token comes from api_token.dpapi only.
    store_api_token(monkeypatch, "real-token")
    path = _write_config(tmp_path, api_token="token-from-file-must-be-ignored")

    cfg = load_config(path)
    assert cfg.api_token == "real-token"


def test_empty_sources_list_raises(tmp_path, monkeypatch):
    store_api_token(monkeypatch, "test-token")
    path = _write_config(tmp_path, sources=[])

    with pytest.raises(ConfigError):
        load_config(path)


def test_source_missing_name_or_path_raises(tmp_path, monkeypatch):
    store_api_token(monkeypatch, "test-token")
    path = _write_config(tmp_path, sources=[{"name": "checkins"}])

    with pytest.raises(ConfigError):
        load_config(path)


def test_duplicate_source_names_raise(tmp_path, monkeypatch):
    store_api_token(monkeypatch, "test-token")
    path = _write_config(tmp_path, sources=[
        {"name": "checkins", "path": "a.txt"},
        {"name": "checkins", "path": "b.txt"},
    ])

    with pytest.raises(ConfigError):
        load_config(path)


def test_unknown_source_lookup_raises(tmp_path, monkeypatch):
    store_api_token(monkeypatch, "test-token")
    cfg = load_config(_write_config(tmp_path))

    with pytest.raises(ConfigError):
        cfg.source("does-not-exist")


def test_numeric_overrides_from_json(tmp_path, monkeypatch):
    store_api_token(monkeypatch, "test-token")
    path = _write_config(tmp_path, max_records_per_batch=250, http_read_timeout=120.0)

    cfg = load_config(path)
    assert cfg.max_records_per_batch == 250
    assert cfg.http_read_timeout == 120.0


def test_numeric_defaults_when_not_specified(tmp_path, monkeypatch):
    store_api_token(monkeypatch, "test-token")
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
    store_api_token(monkeypatch, "test-token")

    cfg = load_config(_write_config(tmp_path, installation_id=41))

    assert cfg.installation_id == 41
    assert isinstance(cfg.installation_id, int)


def test_legacy_config_without_installation_id_still_loads_with_none(tmp_path, monkeypatch):
    store_api_token(monkeypatch, "test-token")
    path = _write_config(tmp_path)
    assert "installation_id" not in json.loads(path.read_text(encoding="utf-8"))

    cfg = load_config(path)

    assert cfg.installation_id is None
    # ...and everything else is exactly as before.
    assert (cfg.customer_id, cfg.branch_id) == (1, 1)
    assert [s.name for s in cfg.sources] == ["checkins", "rejects", "acs"]


def test_explicit_null_installation_id_is_treated_as_absent(tmp_path, monkeypatch):
    store_api_token(monkeypatch, "test-token")

    assert load_config(_write_config(tmp_path, installation_id=None)).installation_id is None


@pytest.mark.parametrize("bad", [0, -1, "12", "abc", 1.5, True, [3], {"id": 3}])
def test_invalid_installation_id_fails_loudly_at_load(tmp_path, monkeypatch, bad):
    store_api_token(monkeypatch, "test-token")

    with pytest.raises(ConfigError, match="installation_id"):
        load_config(_write_config(tmp_path, installation_id=bad))


def test_the_shipped_example_template_placeholder_is_rejected_not_silently_used(monkeypatch):
    # The template's installation_id is a 0 placeholder: copying it without
    # filling it in must not produce a Collector that heartbeats as id 0.
    store_api_token(monkeypatch, "test-token")
    template = Path(__file__).resolve().parent.parent / "collector" / "deploy" / "collector_config.example.json"

    with pytest.raises(ConfigError, match="installation_id"):
        load_config(template)


# =====================================================================================================================
# API token resolution: api_token.dpapi is the ONLY source. SORTVIEW_API_TOKEN is never read -- it can neither stand in
# for a missing file nor rescue an unusable one.
# =====================================================================================================================

DPAPI_TOKEN = "CANARY-dpapi-token-000000000000000001"
ENV_TOKEN = "CANARY-env-token-00000000000000000001"
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="real DPAPI is Windows-only")

# Every fixed code the store can raise on load, token_missing included.
UNUSABLE_CODES = [
    "token_unreadable", "token_exposed", "token_acl_unverified", "token_tenant_mismatch", "token_invalid", "token_store_unavailable",
]


@pytest.fixture
def valid_environment_token(monkeypatch):
    """A perfectly good token sitting in SORTVIEW_API_TOKEN -- which must change nothing, anywhere."""
    monkeypatch.setenv("SORTVIEW_API_TOKEN", ENV_TOKEN)


def test_a_stored_dpapi_token_is_used_and_labelled(tmp_path, monkeypatch):
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    seen = store_api_token(monkeypatch, DPAPI_TOKEN)

    cfg = load_config(_write_config(tmp_path))

    assert (cfg.api_token, cfg.api_token_source) == (DPAPI_TOKEN, "dpapi")
    assert seen == [(tmp_path / "secrets" / "api_token.dpapi", 1, 1)]  # <root>\secrets, bound to the config's tenant


def test_a_stored_dpapi_token_wins_and_the_environment_is_irrelevant(tmp_path, monkeypatch, valid_environment_token):
    store_api_token(monkeypatch, DPAPI_TOKEN)

    cfg = load_config(_write_config(tmp_path))

    assert (cfg.api_token, cfg.api_token_source) == (DPAPI_TOKEN, "dpapi")
    assert ENV_TOKEN not in repr(cfg)


@pytest.mark.parametrize("value", [ENV_TOKEN, "", " ", "\t", "   \n  "])
def test_an_environment_token_with_no_token_file_does_not_rescue_startup(tmp_path, monkeypatch, value):
    # The contract change: this exact setup -- a valid SORTVIEW_API_TOKEN and no api_token.dpapi -- used to start the
    # Collector. It must not any more, whatever the variable holds.
    monkeypatch.setenv("SORTVIEW_API_TOKEN", value)

    with pytest.raises(ConfigError, match="Missing API token") as caught:
        load_config(_write_config(tmp_path))  # no api_token.dpapi anywhere under tmp_path

    message = str(caught.value)
    assert str(tmp_path / "secrets" / "api_token.dpapi") in message and "api-token set" in message
    assert ENV_TOKEN not in message and "SORTVIEW_API_TOKEN" not in message  # the dead variable is not even suggested
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_no_token_file_and_no_environment_token_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    with pytest.raises(ConfigError, match="Missing API token"):
        load_config(_write_config(tmp_path))


@pytest.mark.parametrize("environment", [None, ENV_TOKEN])
@pytest.mark.parametrize("code", UNUSABLE_CODES)
def test_an_unusable_token_file_fails_closed_whatever_the_environment_holds(tmp_path, monkeypatch, code, environment):
    if environment is None:
        monkeypatch.delenv("SORTVIEW_API_TOKEN", raising=False)
    else:
        monkeypatch.setenv("SORTVIEW_API_TOKEN", environment)  # a perfectly good token is RIGHT THERE -- and must not be used
    break_api_token(monkeypatch, code)

    with pytest.raises(ConfigError) as caught:
        load_config(_write_config(tmp_path))

    message = str(caught.value)
    assert code in message and "no other token source" in message
    assert ENV_TOKEN not in message and caught.value.__cause__ is None and caught.value.__suppress_context__


@pytest.mark.parametrize("code", ["token_missing", *UNUSABLE_CODES, "token_not_writable", "a_code_added_later"])
def test_every_store_error_becomes_a_config_error_never_a_leaked_store_exception(tmp_path, monkeypatch, valid_environment_token, code):
    # run / preflight / bootstrap / support-info all catch ConfigError (exit 2); a store exception must never escape.
    break_api_token(monkeypatch, code)

    with pytest.raises(ConfigError) as caught:
        config_module.resolve_api_token(config_module.load_token_settings(_write_config(tmp_path)))

    assert not isinstance(caught.value, api_token_store.ApiTokenStoreError)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_resolving_the_token_never_touches_the_environment_variable(tmp_path, monkeypatch):
    # Behavioral, not textual: ANY read of SORTVIEW_API_TOKEN from os.environ during resolution fails the test -- with a
    # stored token, and (the case that used to fall back) without one.
    class Trap(dict):
        def _guard(self, key):
            if key == "SORTVIEW_API_TOKEN":
                raise AssertionError("token resolution read SORTVIEW_API_TOKEN")

        def get(self, key, default=None):
            self._guard(key)
            return super().get(key, default)

        def __getitem__(self, key):
            self._guard(key)
            return super().__getitem__(key)

        def __contains__(self, key):
            self._guard(key)
            return super().__contains__(key)

    path = _write_config(tmp_path)
    environment = Trap(os.environ)
    environment.update({"SORTVIEW_API_TOKEN": ENV_TOKEN})
    monkeypatch.setattr(os, "environ", environment)

    with pytest.raises(ConfigError, match="Missing API token"):
        load_config(path)

    store_api_token(monkeypatch, DPAPI_TOKEN)
    assert load_config(path).api_token == DPAPI_TOKEN


def test_the_migration_fallback_is_deleted_not_just_switched_off():
    # The one-release fallback and everything that existed only to serve it are gone from the module.
    for name in ("ENV_TOKEN_FALLBACK_REMOVED_IN", "TOKEN_SOURCE_ENVIRONMENT", "_release_tuple", "os", "__version__"):
        assert not hasattr(config_module, name), name
    assert config_module.TOKEN_SOURCE_DPAPI == "dpapi"
    source = Path(config_module.__file__).read_text(encoding="utf-8")
    for gone in ("SORTVIEW_API_TOKEN", "os.environ", "getenv", "migration fallback"):
        assert gone not in source, gone


def test_a_token_file_that_cannot_even_be_checked_fails_closed(tmp_path, monkeypatch, valid_environment_token):
    # e.g. a non-elevated process that may not look inside the Administrators+SYSTEM-only secrets folder
    real_stat = api_token_store.os.stat

    def denied(path, *args, **kwargs):
        if str(path).endswith("api_token.dpapi"):
            raise PermissionError(13, "Access is denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(api_token_store.os, "stat", denied)
    with pytest.raises(ConfigError, match="token_unreadable"):
        load_config(_write_config(tmp_path))


@windows_only
def test_a_really_corrupt_token_file_with_a_valid_environment_token_fails_closed(tmp_path, monkeypatch, valid_environment_token):
    monkeypatch.setattr(api_token_store, "acl_state_of", lambda *_a, **_k: "protected")
    path = tmp_path / "secrets" / "api_token.dpapi"
    path.parent.mkdir()
    path.write_bytes(b"\x00 not a dpapi blob \x00")

    with pytest.raises(ConfigError, match="token_unreadable"):
        load_config(_write_config(tmp_path))


@windows_only
def test_a_really_stored_token_in_an_exposed_folder_fails_closed(tmp_path, monkeypatch, valid_environment_token):
    # No ACL stubbing on load: an ordinary temp folder is readable by the current user, so the real check calls it exposed.
    with monkeypatch.context() as trusted:
        trusted.setattr(api_token_store, "acl_state_of", lambda *_a, **_k: "protected")
        api_token_store.DpapiTokenStore(tmp_path / "secrets" / "api_token.dpapi").save(1, 1, DPAPI_TOKEN)

    with pytest.raises(ConfigError, match="token_exposed"):
        load_config(_write_config(tmp_path))


@windows_only
def test_a_really_stored_token_for_another_tenant_fails_closed(tmp_path, monkeypatch, valid_environment_token):
    monkeypatch.setattr(api_token_store, "acl_state_of", lambda *_a, **_k: "protected")
    api_token_store.DpapiTokenStore(tmp_path / "secrets" / "api_token.dpapi").save(1, 2, DPAPI_TOKEN)  # branch 2, config says 1

    with pytest.raises(ConfigError, match="token_tenant_mismatch"):
        load_config(_write_config(tmp_path))


@windows_only
def test_a_really_stored_token_end_to_end(tmp_path, monkeypatch, valid_environment_token):
    monkeypatch.setattr(api_token_store, "acl_state_of", lambda *_a, **_k: "protected")
    api_token_store.DpapiTokenStore(tmp_path / "secrets" / "api_token.dpapi").save(1, 1, DPAPI_TOKEN)

    cfg = load_config(_write_config(tmp_path))

    assert (cfg.api_token, cfg.api_token_source) == (DPAPI_TOKEN, "dpapi")  # the stored token, not the environment's


def test_removing_a_damaged_token_file_does_not_bring_the_environment_back(tmp_path, monkeypatch, valid_environment_token):
    # In the migration release, deleting a damaged api_token.dpapi was a rollback to the environment variable. It no longer
    # is: with the file gone the Collector simply has no token until one is stored again.
    path = tmp_path / "secrets" / "api_token.dpapi"
    path.parent.mkdir()
    path.write_bytes(b"damaged")
    with pytest.raises(ConfigError, match="could not be used"):
        load_config(_write_config(tmp_path))

    path.unlink()

    with pytest.raises(ConfigError, match="Missing API token"):
        load_config(_write_config(tmp_path))


def test_the_token_is_never_in_the_config_repr(tmp_path, monkeypatch):
    store_api_token(monkeypatch, DPAPI_TOKEN)
    cfg = load_config(_write_config(tmp_path))
    assert DPAPI_TOKEN not in repr(cfg) and DPAPI_TOKEN not in str(cfg)
    assert "api_token_source='dpapi'" in repr(cfg)


def test_load_token_settings_needs_no_token_and_derives_the_v2_secret_folder(tmp_path):
    # Native paths on whatever host runs the tests: <root>\data\state.json -> <root>\secrets\api_token.dpapi, a concrete Path
    # (the store does real file I/O with it).
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


def test_an_api_token_path_override_replaces_the_derived_location(tmp_path):
    override = tmp_path / "elsewhere" / "token.dpapi"

    settings = config_module.load_token_settings(_write_config(tmp_path, api_token_path=str(override)))

    assert settings.token_path == override and isinstance(settings.token_path, Path)
