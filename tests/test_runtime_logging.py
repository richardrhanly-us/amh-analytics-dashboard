"""Tests for agent/runtime/logging_setup.py (Continuous Ingestion Phase F)."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from agent.runtime.logging_setup import configure_logging, get_component_logger


def test_configure_logging_creates_rotating_file_handler(tmp_path):
    logger = configure_logging(tmp_path, console=False)

    file_handlers = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
    assert len(file_handlers) == 1
    assert file_handlers[0].maxBytes > 0
    assert file_handlers[0].backupCount > 0


def test_configure_logging_respects_bounds(tmp_path):
    logger = configure_logging(tmp_path, max_bytes=1024, backup_count=2, console=False)

    handler = next(h for h in logger.handlers if isinstance(h, RotatingFileHandler))
    assert handler.maxBytes == 1024
    assert handler.backupCount == 2


def test_configure_logging_is_idempotent_no_duplicate_handlers(tmp_path):
    configure_logging(tmp_path, console=False)
    logger = configure_logging(tmp_path, console=False)

    assert len(logger.handlers) == 1


def test_log_file_actually_written(tmp_path):
    configure_logging(tmp_path, console=False)
    component_logger = get_component_logger("test-component")
    component_logger.info("hello from test")

    for handler in logging.getLogger("sortview.runtime").handlers:
        handler.flush()

    log_file = tmp_path / "agent.log"
    assert log_file.exists()
    assert "hello from test" in log_file.read_text(encoding="utf-8")


def test_rotation_actually_occurs_past_max_bytes(tmp_path):
    logger = configure_logging(tmp_path, max_bytes=200, backup_count=3, console=False)

    for i in range(200):
        logger.info("padding line number %s to exceed the tiny maxBytes limit quickly", i)

    log_dir_files = list(tmp_path.iterdir())
    rotated = [p for p in log_dir_files if p.name.startswith("agent.log.")]
    assert len(rotated) >= 1
    assert len(rotated) <= 3  # never exceeds backup_count -- bounded, not unbounded growth


def test_component_logger_is_child_of_runtime_logger():
    logger = get_component_logger("uploader")
    assert logger.name == "sortview.runtime.uploader"
