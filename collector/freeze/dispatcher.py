"""SortView Collector -- frozen single-executable dispatcher.

PACKAGING-ONLY -- never imported by, or a dependency of, any real
Collector runtime module. This is the PyInstaller entry-point script that
produces SortViewCollector.exe (see sortview_collector.spec), letting one
frozen executable cover all four CLI surfaces (production run, preflight,
bootstrap, support-info) instead of four separate executables -- one
shared PyInstaller onedir payload (pandas/numpy/etc. bundled once) rather
than four duplicated ones.

THIN DISPATCH ONLY, per the frozen-runtime proof's explicit constraint:
no Collector runtime logic is duplicated or reimplemented here. Each
subcommand below forwards straight to the SAME main(argv) -> int entry
point collector/run.py, collector/preflight.py, collector/bootstrap_state.py,
and collector/support_info.py already expose via `python -m collector.X`
-- unchanged, not copied. Each target module's own argparse still parses
everything after the subcommand itself, exactly as it does today; this
file only decides WHICH one to call and returns its exit code unchanged.

Static (not dynamic/importlib) imports deliberately -- PyInstaller's
analyzer follows ordinary `from X import Y` statements even inside
if/elif branches, so all four subcommand targets are discovered without
needing an opaque runtime importlib.import_module(name) call. They are
also listed explicitly in sortview_collector.spec's hiddenimports as a
belt-and-suspenders measure -- see that file's own comment.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

_SUBCOMMANDS = ("run", "preflight", "bootstrap", "support-info")


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
    else:  # "support-info"
        from collector.support_info import main as sub_main

    return sub_main(rest)


if __name__ == "__main__":
    raise SystemExit(main())
