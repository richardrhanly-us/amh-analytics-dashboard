# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec for SortView Collector v1's frozen-runtime proof
(ONEDIR mode) -- PACKAGING-ONLY, never installed or run as part of the
production Collector itself.

Produces one dispatcher executable (SortViewCollector.exe, see
dispatcher.py) covering all four CLI surfaces via subcommand:

    SortViewCollector.exe run --config <path>
    SortViewCollector.exe preflight --config <path>
    SortViewCollector.exe bootstrap --config <path>
    SortViewCollector.exe support-info --config <path>

Chosen over four separate executables specifically because it requires
the SAME zero-refactoring dispatch (dispatcher.py imports and forwards to
each existing collector/*.py main(), never duplicating logic) while
producing only ONE shared onedir payload -- pandas/numpy/requests/certifi
etc. bundled once, not once per executable.

MUST be built from the ISOLATED .pyinstaller-venv (collector/deploy/requirements.txt
+ PyInstaller as a build-only dependency), never the normal project .venv
-- see collector/freeze/build_frozen.ps1, which enforces this.

EXCLUDES are defensive/documentary, not strictly load-bearing: PyInstaller
only bundles what the dependency graph starting at dispatcher.py actually
imports (collector.* + the narrow agent.parser.*/agent.logger_config.py
slice, transitively) -- agent/runtime/*, agent/main.py, and SortViewAgent/
are never reachable from that graph in the first place. Listed explicitly
anyway as a DEFENSIVE exclusion: PyInstaller excludes do not guarantee a
build-time failure if a future accidental import (e.g. someone adding an
agent.runtime import to a collector module) slips in -- an excluded
module is simply prevented from being bundled, which can instead surface
as a runtime ImportError the next time that code path actually runs.
Catching an accidental dependency before it ships is the responsibility
of this project's tests and runtime validation (see
tests/test_collector_freeze.py and the frozen-runtime proof's own scratch
validation), not something this exclude list enforces by itself.
"""

from pathlib import Path

# This file lives at <repo>/collector/freeze/sortview_collector.spec.
# SPECPATH (injected by PyInstaller) is this file's DIRECTORY, not its
# full path -- collector/freeze -> collector -> repo root is two levels
# up, not three (verified empirically: three levels up pointed one
# directory above the actual repo root).
REPO_ROOT = Path(SPECPATH).resolve().parent.parent  # noqa: F821
DISPATCHER = str(REPO_ROOT / "collector" / "freeze" / "dispatcher.py")

a = Analysis(  # noqa: F821 (Analysis/PYZ/EXE/COLLECT are injected by PyInstaller's exec environment)
    [DISPATCHER],
    pathex=[str(REPO_ROOT)],
    binaries=[],
    datas=[],
    # Belt-and-suspenders for the five subcommand targets -- see
    # dispatcher.py's own docstring for why these are expected to be
    # auto-discovered anyway (static from-imports, even inside if/elif).
    hiddenimports=[
        "collector.run",
        "collector.preflight",
        "collector.bootstrap_state",
        "collector.support_info",
        "collector.task_settings",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "agent.runtime",
        "agent.runtime.config",
        "agent.runtime.supervisor",
        "agent.runtime.collector",
        "agent.runtime.uploader",
        "agent.runtime.heartbeat",
        "agent.runtime.housekeeping",
        "agent.main",
        "agent.tailer",
        "agent.state",
        "agent.spool",
        "agent.discovery",
        "agent.identity",
        "agent.event_identity",
        "agent.uploader",
        "agent.run_pipeline",
        "streamlit",
        "fastapi",
        "sqlalchemy",
        "psycopg2",
        "pytest",
        "tkinter",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SortViewCollector",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SortViewCollector",
)
