"""Startup smoke tests for the Streamlit dashboard (src/app.py).

These guard the DETERMINISTIC part of "does the app start": every src/ module
must import cleanly from a fresh interpreter, the way Streamlit exposes src/
(script folder on sys.path, flat imports), with no DATABASE_URL -- and the real
src/app.py must render the login page without raising. That catches a broken
sibling import, a circular import, or syntax the interpreter rejects, before it
reaches the hosted app.

Each check runs in its own subprocess so it sees a genuinely fresh sys.modules,
unaffected by this test process's conftest sys.path/env setup.

What these tests deliberately do NOT cover: a hot-redeploy race in Streamlit's
file watcher, where one session evicts a module from sys.modules while another
is mid-import and importlib raises KeyError: '<module>'. That is a Streamlit
runtime behaviour, independent of import style and Python version, and cannot
be pinned by an import test.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def _run(code: str, timeout: int = 180) -> subprocess.CompletedProcess:
    # No DATABASE_URL: the dashboard must be able to start (and show its login
    # page) without touching the database. PYTHONPATH is dropped so only the
    # explicit src/ path below decides what is importable.
    env = {k: v for k, v in os.environ.items() if k not in {"DATABASE_URL", "PYTHONPATH"}}
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    return subprocess.run(
        [sys.executable, "-B", "-c", textwrap.dedent(code)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _discover_modules() -> list[str]:
    top = sorted(p.stem for p in SRC.glob("*.py") if p.stem not in {"app", "__init__"})
    services = sorted(f"services.{p.stem}" for p in (SRC / "services").glob("*.py") if p.stem != "__init__")
    views = sorted(f"views.{p.stem}" for p in (SRC / "views").glob("*.py"))
    return top + services + views


def test_every_src_module_imports_fresh_without_database_url():
    modules = _discover_modules()
    assert "metrics" in modules and "dashboard_context" in modules  # discovery sanity check

    result = _run(
        f"""
        import importlib, sys
        sys.path.insert(0, {str(SRC)!r})
        failures = []
        for name in {modules!r}:
            try:
                importlib.import_module(name)
            except Exception as exc:
                failures.append(f"{{name}}: {{type(exc).__name__}}: {{exc}}")
        print("\\n".join(failures))
        sys.exit(1 if failures else 0)
        """
    )

    assert result.returncode == 0, f"src module(s) failed to import:\n{result.stdout}\n{result.stderr[-2000:]}"


def test_login_page_renders_from_a_cold_start_without_database_url():
    result = _run(
        f"""
        import sys
        sys.path.insert(0, {str(SRC)!r})
        from streamlit.testing.v1 import AppTest
        at = AppTest.from_file({str(SRC / "app.py")!r}, default_timeout=120)
        at.run()
        problems = [e.value for e in at.exception]
        if problems:
            print("EXCEPTIONS:", problems)
            sys.exit(1)
        if [t.value for t in at.title] != ["SortView Login"]:
            print("UNEXPECTED PAGE:", [t.value for t in at.title])
            sys.exit(2)
        """
    )

    assert result.returncode == 0, f"login page did not render:\n{result.stdout}\n{result.stderr[-2000:]}"
