import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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

# main.py reads DATABASE_URL at import time. Tests never hit a real
# database (main.engine is monkeypatched), so a placeholder is enough.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

# agent/uploader.py reads SORTVIEW_API_TOKEN eagerly at import time (it's
# a real uploader -- genuinely needs a token, unlike Phase 1's watcher,
# which deliberately doesn't import uploader.py at all). Anything that
# imports agent.outbox_uploader transitively imports agent.uploader, so
# this placeholder keeps that import from failing. Tests that assert the
# real token value is never logged/stored read it back via
# agent.uploader.API_TOKEN rather than hardcoding a second copy of this
# string, so the two can't drift apart.
os.environ.setdefault("SORTVIEW_API_TOKEN", "test-agent-token-placeholder")

# Never initialize Sentry during tests, even if the developer's shell
# currently has a real SENTRY_DSN configured.
os.environ["SENTRY_DSN"] = ""
os.environ["SENTRY_ENVIRONMENT"] = "test"