"""SortView Collector -- frozen single-executable dispatcher.

PACKAGING-ONLY -- never imported by, or a dependency of, any real
Collector runtime module. This is the PyInstaller entry-point script that
produces SortViewCollector.exe (see sortview_collector.spec), letting one
frozen executable cover all eight CLI surfaces (production run, preflight,
bootstrap, support-info, task-xml -- deployment/Task-Scheduler-XML
generation, added for the frozen release-bundle integration phase; see
collector/deploy/register-collector-task.ps1 --, identity-collision-diag --
the onsite identical_identity_events diagnostic, see
collector/identity_collision_diag.py; needs no Python or loose source files
on the target machine now that it forwards through this same frozen exe --,
v2-key -- local DPAPI secret provisioning (`init`/`check`), see
collector/v2_keys.py; makes the AMH's Python-free frozen install able to run
the SAME provisioning CLI docs/collector-v2.md already documents as
`python -m collector.v2_keys` -- and version) instead of eight separate
executables -- one shared PyInstaller onedir payload (pandas/numpy/etc.
bundled once) rather than eight duplicated ones.

`version` is the one subcommand that is NOT a forward to a module main():
it prints collector.__version__ (the single authoritative Collector
version -- never a copy of it) and exits 0. It needs no config, token,
network or file access, so release tooling can ask ANY built executable
"what version are you actually?" -- collector/freeze/build_frozen.ps1 does
right after building, and collector/build_release.py does again before
packaging a frozen bundle. stdout is exactly the version and a newline.

THIN DISPATCH ONLY, per the frozen-runtime proof's explicit constraint:
no Collector runtime logic is duplicated or reimplemented here. Each
subcommand below forwards straight to the SAME main(argv) -> int entry
point collector/run.py, collector/preflight.py, collector/bootstrap_state.py,
collector/support_info.py, collector/task_settings.py,
collector/identity_collision_diag.py, and collector/v2_keys.py already
expose via `python -m collector.X` -- unchanged, not copied (v2_keys.py's
key-generation and DPAPI/ACL logic in particular is untouched by this file;
`v2-key` is pure argv forwarding, same as every other subcommand). Each
target module's own argparse still parses everything after the subcommand
itself, exactly as it does today; this file only decides WHICH one to call
and returns its exit code unchanged.

Static (not dynamic/importlib) imports deliberately -- PyInstaller's
analyzer follows ordinary `from X import Y` statements even inside
if/elif branches, so all seven subcommand targets are discovered without
needing an opaque runtime importlib.import_module(name) call. They are
also listed explicitly in sortview_collector.spec's hiddenimports as a
belt-and-suspenders measure -- see that file's own comment (collector.v2_keys
was already listed there, as one of the Contract v2 modules; this file is
what makes it actually REACHABLE as a subcommand).
"""

from __future__ import annotations

import sys
from collections.abc import Callable

_SUBCOMMANDS = ("run", "preflight", "bootstrap", "support-info", "task-xml", "identity-collision-diag", "v2-key", "version")


def _usage() -> str:
    return (
        "Usage: SortViewCollector.exe <subcommand> [args...]\n"
        f"  subcommands: {', '.join(_SUBCOMMANDS)}"
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if not argv or argv[0] not in _SUBCOMMANDS:
        print(_usage(), file=sys.stderr)
        return 2

    subcommand, rest = argv[0], argv[1:]

    if subcommand == "version":
        # Config-free by design: takes no arguments, so a stray --config (or
        # anything else) is a usage error rather than being silently ignored.
        if rest:
            print(_usage(), file=sys.stderr)
            return 2
        from collector import __version__

        print(__version__)
        return 0

    # Explicit annotation, not inferred from the first branch: collector/run.py's
    # main() takes Sequence[str] | None while the other three take
    # list[str] | None (a genuine, pre-existing minor inconsistency across
    # those four modules, out of scope to change here -- see this
    # module's own docstring on not duplicating/modifying Collector logic).
    # `rest` below is always a concrete list[str], which satisfies either.
    sub_main: Callable[[list[str] | None], int]
    if subcommand == "run":
        from collector.run import main as sub_main
    elif subcommand == "preflight":
        from collector.preflight import main as sub_main
    elif subcommand == "bootstrap":
        from collector.bootstrap_state import main as sub_main
    elif subcommand == "support-info":
        from collector.support_info import main as sub_main
    elif subcommand == "task-xml":
        # Deployment tooling, not Collector ingestion; see
        # register-collector-task.ps1, which invokes this subcommand for a
        # frozen install instead of shelling out to a Python venv (which
        # does not exist in a frozen install).
        from collector.task_settings import main as sub_main
    elif subcommand == "identity-collision-diag":
        # Onsite identical_identity_events diagnostic, not Collector
        # ingestion; see collector/identity_collision_diag.py's own
        # docstring. Reuses the SAME v2 dry-run config path and transform
        # logic, throwaway in-memory key, no persistent secret, no network
        # call, no state/cursor write -- identical safety contract to
        # `run --v2-dry-run`.
        from collector.identity_collision_diag import main as sub_main
    else:  # "v2-key" -- local DPAPI secret provisioning (init/check), not
        # Collector ingestion; see collector/v2_keys.py's own docstring.
        # `sub_main` here is v2_keys.main itself, which parses its own
        # `init`/`check` positional command and `--config` -- this file
        # never touches key generation, DPAPI or ACL logic.
        from collector.v2_keys import main as sub_main

    return sub_main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
