"""Bounded rotating logs for the canonical runtime (Continuous Ingestion
Phase F).

Replaces agent/logger_config.py's plain, unbounded FileHandler for the
NEW runtime path only -- that module is untouched and still used by the
legacy scheduled pipeline and the experimental SQLite modules. An
unbounded log file is a real operational risk for a long-running,
always-on process in a way it never was for a 15-minute scheduled
script; RotatingFileHandler caps that.

Never logs bearer tokens, credentials, or request bodies -- every caller
in this package logs status codes and response-body PREVIEWS (the
server's own text), never what was sent (see agent/runtime/http_client.py).
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

RUNTIME_LOGGER_NAME = "sortview.runtime"

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUP_COUNT = 5


def configure_logging(
    log_dir: str | Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    level: int = logging.INFO,
    console: bool = True,
) -> logging.Logger:
    """Idempotent: safe to call more than once (e.g. across tests, or a
    supervisor restart within the same process) without duplicating
    handlers -- existing handlers on the runtime logger are replaced, not
    stacked."""
    log_dir_path = Path(log_dir)
    log_dir_path.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(RUNTIME_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    file_handler = RotatingFileHandler(
        log_dir_path / "agent.log", maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    return logger


def get_component_logger(name: str) -> logging.Logger:
    """A child of the runtime logger -- inherits its handlers/level,
    named e.g. sortview.runtime.collector for log-line filtering."""
    return logging.getLogger(f"{RUNTIME_LOGGER_NAME}.{name}")
