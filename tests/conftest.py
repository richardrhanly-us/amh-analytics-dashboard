import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, "src")

# src/services/*.py import their sibling modules top-level style
# (e.g. `from database import get_engine`), matching how Streamlit
# runs the app with src/ as the working directory. Tests need src/
# on sys.path too so those imports resolve the same way.
for path in (ROOT_DIR, SRC_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

        # main.py reads DATABASE_URL at import time. Tests never hit a real
        # database (main.engine is monkeypatched), so a placeholder is enough.
        os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

        # Never initialize Sentry during tests, even if the developer's shell
        # currently has a real SENTRY_DSN configured.
        os.environ["SENTRY_DSN"] = ""
        os.environ["SENTRY_ENVIRONMENT"] = "test"