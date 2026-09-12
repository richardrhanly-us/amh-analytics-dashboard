from pathlib import Path

import pandas as pd

from .config import load_config
from .logger_config import get_logger
from .parser.acs import EMPTY_COLUMNS, extract_fields, parse_lines, parse_timestamp

logger = get_logger("parse_acs")

# Parsing logic (extract_fields, parse_timestamp, the line parser) now
# lives in agent/parser/acs.py -- this module is a thin, behavior-preserving
# wrapper kept for existing callers (agent/run_pipeline.py -- LEGACY /
# VALIDATION-ONLY, see its docstring -- and this module's own legacy-
# import-path tests). See agent/parser/__init__.py. The canonical
# continuous runtime (agent/runtime/*) imports agent.parser directly and
# never goes through this wrapper.
__all__ = [
    "extract_fields",
    "load_acs",
    "load_acs_incremental",
    "parse_timestamp",
    "save_acs_csv",
]

_parse_lines = parse_lines


def load_acs(filepath=None):
    if filepath is None:
        filepath = load_config()["raw_acs_file"]

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    return _parse_lines(lines)


def load_acs_incremental(filepath=None, start_offset=0):
    if filepath is None:
        filepath = load_config()["raw_acs_file"]

    file_path = Path(filepath)

    if not file_path.exists():
        logger.warning("ACS file not found: %s", filepath)
        return pd.DataFrame(columns=EMPTY_COLUMNS), 0

    file_size = file_path.stat().st_size

    if start_offset is None:
        start_offset = 0

    if start_offset < 0 or start_offset > file_size:
        logger.warning(
            "ACS offset invalid or file rotated/truncated. Resetting offset from %s to 0",
            start_offset
        )
        start_offset = 0

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        f.seek(start_offset)
        lines = f.readlines()
        end_offset = f.tell()

    df = _parse_lines(lines)

    logger.info(
        "Loaded ACS incrementally | start_offset=%s end_offset=%s rows=%s",
        start_offset,
        end_offset,
        len(df)
    )

    return df, end_offset


def save_acs_csv(df, output_path=None):
    if output_path is None:
        output_path = load_config()["processed_acs_file"]

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_file, index=False)
    logger.info("Saved ACS CSV")


if __name__ == "__main__":
    config = load_config()
    processed_acs_file = config["processed_acs_file"]

    df = load_acs()
    save_acs_csv(df, processed_acs_file)
    logger.info("Saved cleaned ACS file to: %s", processed_acs_file)
    logger.info("Row count: %s", len(df))
    logger.info("Bad datetime rows: %s", df["datetime"].isna().sum())
