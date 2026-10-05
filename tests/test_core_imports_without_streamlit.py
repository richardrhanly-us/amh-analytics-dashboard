"""Import-boundary tests: framework-independent modules must import with
Streamlit unavailable.

Each check runs in a fresh subprocess with sys.modules["streamlit"] = None
(so any `import streamlit`, direct or transitive, raises) and without
DATABASE_URL, so a module that quietly starts depending on Streamlit -- or on
a configured database at import time -- fails here instead of in a future
non-Streamlit caller.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

STREAMLIT_FREE_MODULES = [
    "database",
    "services.session_service",
    "formatting",
    "services.filter_context_service",
    "services.pipeline_context_service",
    "tenant_db",
    "services.auth_service",
    "services.access_service",
    "services.user_admin_service",
    "services.entitlement_service",
    "customer_api.router",
    "services.tenant_resolution_service",
    "services.operational_read_service",
    "customer_api.tenant_scope",
    "customer_api.operational_routes",
    "services.operational_metrics_service",
]


@pytest.mark.parametrize("module", STREAMLIT_FREE_MODULES)
def test_module_imports_without_streamlit(module):
    env = {k: v for k, v in os.environ.items() if k not in {"DATABASE_URL", "PYTHONPATH"}}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    code = textwrap.dedent(
        f"""
        import importlib, sys
        sys.modules["streamlit"] = None
        sys.path.insert(0, {str(SRC)!r})
        importlib.import_module({module!r})
        leaked = sorted(name for name, mod in sys.modules.items() if name.startswith("streamlit") and mod is not None)
        if leaked:
            print("STREAMLIT LOADED:", leaked)
            sys.exit(1)
        """
    )

    result = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, f"{module} failed to import without Streamlit:\n{result.stdout}\n{result.stderr[-2000:]}"
