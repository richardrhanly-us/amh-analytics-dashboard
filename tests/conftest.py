import logging
import os
import sys

import pytest

ROOT_DIR =os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, "src")

# src/services/*.py import their sibling modules top-level style (e.g.
# `from database import get_engine`), matching how Streamlit runs with
# src/ as the working directory. Tests need src/ on sys.path too so those
# imports resolve the same way. Every agent/*.py module uses
# package-relative imports (`from .config import ...`) and is only ever
# imported as part of the `agent` package -- that already works via
# ROOT_DIR on sys.path plus agent/__init__.py, no separate sys.path entry
# needed for agent/ itself.
for path in (ROOT_DIR, SRC_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

# Imported at module level (collection time), not lazily inside a fixture
# -- see _collector_run_audit_fallback_path below for why that distinction
# matters here specifically.
from collector import run_audit

# main.py reads DATABASE_URL at import time. Tests never hit a real
# database (main.engine is monkeypatched), so a placeholder is enough.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

# agent/uploader.py reads SORTVIEW_API_TOKEN eagerly at import time (it's
# a real uploader -- genuinely needs a token). agent/run_pipeline.py (kept
# as a LEGACY/VALIDATION-ONLY scheduled path, see agent/README.md) and its
# own tests import it transitively, so this placeholder keeps that import
# from failing. The canonical continuous runtime (agent/runtime/*) reads
# this same env var too, but lazily inside load_runtime_config() rather
# than at import time, so it has no such import-time dependency itself.
# Tests that assert the real token value is never logged/stored read it
# back via agent.uploader.API_TOKEN rather than hardcoding a second copy
# of this string, so the two can't drift apart.
os.environ.setdefault("SORTVIEW_API_TOKEN", "test-agent-token-placeholder")

# Never initialize Sentry during tests, even if the developer's shell
# currently has a real SENTRY_DSN configured.
os.environ["SENTRY_DSN"] = ""
os.environ["SENTRY_ENVIRONMENT"] = "test"


@pytest.fixture(autouse=True)
def _restore_log_record_factory():
    # Every Streamlit entry script calls services.privacy_hardening.install_streamlit_log_scrubber(), which replaces the
    # PROCESS-WIDE log-record factory. Running a page under AppTest would otherwise leave it installed for every later
    # test in the run, making tests that inspect raw Streamlit log records depend on test order.
    original = logging.getLogRecordFactory()
    yield
    logging.setLogRecordFactory(original)


@pytest.fixture(autouse=True)
def _restore_streamlit_error_details():
    # install_streamlit_log_scrubber() also pins Streamlit's PROCESS-WIDE `client.showErrorDetails` to "none" and remembers the
    # value it replaced (services.privacy_hardening.enforce_streamlit_error_details). Put both back after each test, for the
    # same reason as the log-record factory above: no test may depend on what an earlier one left behind.
    from streamlit import config

    from services import privacy_hardening

    option = "client.showErrorDetails"
    value, where = config.get_option(option), config.get_where_defined(option)
    startup = privacy_hardening._error_details_startup
    yield
    config.set_option(option, value, where)
    privacy_hardening._error_details_startup = startup


@pytest.fixture(autouse=True)
def _collector_run_audit_fallback_path(tmp_path):
    # collector/run_audit.py's DEFAULT_FALLBACK_AUDIT_PATH is a real,
    # hardcoded Windows system path (C:\ProgramData\SortViewCollector\
    # logs\runs.jsonl) -- the known-in-advance location collector/run.py's
    # main() writes to when a config failed to load at all (see
    # run_audit.py's PRE-CONFIG FALLBACK section). Every test run redirects
    # it into this test's own tmp_path, so no test -- including one run on
    # a real Windows machine with real write access to C:\ProgramData --
    # ever creates or touches a file under that real system directory.
    #
    # Deliberately uses pytest.MonkeyPatch() directly here rather than
    # requesting the `monkeypatch` FIXTURE as a parameter -- empirically,
    # an autouse fixture that depends on the `monkeypatch` fixture was
    # found to strip Streamlit's st.cache_data decoration from an
    # unrelated module (src/services/entitlement_service.py, used by a
    # completely different test file) for the rest of the process. Root
    # cause not fully understood (some fixture-teardown-ordering
    # interaction, reproduced even with an otherwise-empty fixture body);
    # using the standalone MonkeyPatch class achieves the identical
    # attribute-patch-and-restore effect without adding a new consumer of
    # the `monkeypatch` fixture, and was verified to avoid the interaction
    # entirely.
    mp = pytest.MonkeyPatch()
    mp.setattr(run_audit, "DEFAULT_FALLBACK_AUDIT_PATH", tmp_path / "_unused_fallback_runs.jsonl")
    yield
    mp.undo()
