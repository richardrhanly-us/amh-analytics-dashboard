"""Support/version info (Phase 4c).

A single, fast, read-only command for support/IT to answer "what is
actually installed here and what did it last do" without a GUI -- exactly
what a library IT admin or a support engineer needs before troubleshooting
anything else. Never modifies state, config, or logs; never makes a
network call (deliberately -- see run_preflight for connectivity checks,
kept separate so support-info is always instant and side-effect-free).
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__
from .config import ConfigError, load_config
from .state import load_status
from .task_settings import TASK_NAME


@dataclass(frozen=True)
class SupportInfo:
    collector_version: str
    install_root: str
    config_path: str
    task_name: str
    python_executable: str
    python_version: str
    config_loaded: bool
    config_error: str | None
    last_status: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "collector_version": self.collector_version,
            "install_root": self.install_root,
            "config_path": self.config_path,
            "task_name": self.task_name,
            "python_executable": self.python_executable,
            "python_version": self.python_version,
            "config_loaded": self.config_loaded,
            "config_error": self.config_error,
            "last_status": self.last_status,
        }


def gather_support_info(config_path: str) -> SupportInfo:
    install_root = str(Path(__file__).resolve().parent.parent)

    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        return SupportInfo(
            collector_version=__version__,
            install_root=install_root,
            config_path=str(config_path),
            task_name=TASK_NAME,
            python_executable=sys.executable,
            python_version=sys.version.split()[0],
            config_loaded=False,
            config_error=str(exc),
            last_status=None,
        )

    last_status = load_status(cfg.status_path) or None

    return SupportInfo(
        collector_version=__version__,
        install_root=install_root,
        config_path=str(config_path),
        task_name=TASK_NAME,
        python_executable=sys.executable,
        python_version=sys.version.split()[0],
        config_loaded=True,
        config_error=None,
        last_status=last_status,
    )


def _print_support_info(info: SupportInfo) -> None:
    print("SortView Collector -- support info")
    print("=" * 78)
    print(f"Collector version:   {info.collector_version}")
    print(f"Install root:        {info.install_root}")
    print(f"Config path:         {info.config_path}")
    print(f"Scheduled Task name: {info.task_name}")
    print(f"Python executable:   {info.python_executable}")
    print(f"Python version:      {info.python_version}")
    print("-" * 78)
    if not info.config_loaded:
        print(f"Config did NOT load: {info.config_error}")
        return

    if info.last_status is None:
        print("No status.json found yet -- the collector has not completed a run.")
        return

    status = info.last_status
    print(f"Last attempt:        {status.get('last_attempt', '(none)')}")
    print(f"Last successful run: {status.get('last_run', '(none)')}")
    print(f"Last status:         {status.get('status', '(none)')}")
    if status.get("last_error"):
        print(f"Last error:          {status.get('last_error')}")
    if status.get("sources_missing"):
        print(f"Sources missing:     {', '.join(status['sources_missing'])}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SortView Collector -- support/version info")
    parser.add_argument("--config", required=True, help="Path to the collector config JSON file")
    parser.add_argument("--output", help="Optional path to also write the info as JSON")
    args = parser.parse_args(argv)

    info = gather_support_info(args.config)
    _print_support_info(info)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps(info.to_dict(), indent=2), encoding="utf-8")

    return 0 if info.config_loaded else 2


if __name__ == "__main__":
    raise SystemExit(main())
