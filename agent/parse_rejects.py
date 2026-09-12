from pathlib import Path

from .config import load_config
from .logger_config import get_logger
from .parser.rejects import COLUMNS, empty_df, parse_lines, simplify_error_message

logger = get_logger("parse_rejects")

# Parsing logic (COLUMNS, simplify_error_message, the line parser) now lives
# in agent/parser/rejects.py -- this module is a thin, behavior-preserving
# wrapper kept for existing callers (agent/run_pipeline.py -- LEGACY /
# VALIDATION-ONLY, see its docstring -- and this module's own legacy-
# import-path tests). See agent/parser/__init__.py. The canonical
# continuous runtime (agent/runtime/*) imports agent.parser directly and
# never goes through this wrapper.
__all__ = [
    "COLUMNS",
    "load_rejects",
    "load_rejects_incremental",
    "save_rejects_csv",
    "simplify_error_message",
]

_empty_rejects_df = empty_df
_parse_reject_lines = parse_lines


def load_rejects(filepath=None):
    if filepath is None:
        filepath = load_config()["raw_rejects_file"]

    logger.info("Loading rejects from %s", filepath)

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    return _parse_reject_lines(lines)


def load_rejects_incremental(filepath=None, start_offset=0):
    if filepath is None:
        filepath = load_config()["raw_rejects_file"]

    file_path = Path(filepath)

    if not file_path.exists():
        logger.warning("Rejects file not found: %s", filepath)
        return _empty_rejects_df(), 0

    file_size = file_path.stat().st_size

    if start_offset is None:
        start_offset = 0

    if start_offset < 0 or start_offset > file_size:
        logger.warning(
            "Rejects offset invalid or file rotated/truncated. Resetting offset from %s to 0",
            start_offset
        )
        start_offset = 0

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        f.seek(start_offset)
        lines = f.readlines()
        end_offset = f.tell()

    df = _parse_reject_lines(lines)

    logger.info(
        "Loaded rejects incrementally | start_offset=%s end_offset=%s rows=%s",
        start_offset,
        end_offset,
        len(df)
    )

    return df, end_offset


def save_rejects_csv(df, output_path=None):
    if output_path is None:
        output_path = load_config()["processed_rejects_file"]

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        df.to_csv(output_file, index=False)
        logger.info("Saved rejects CSV to %s", output_path)
    except PermissionError:
        logger.exception("Could not save rejects CSV because the file is open: %s", output_path)
        raise


if __name__ == "__main__":
    config = load_config()
    raw_rejects_file = config["raw_rejects_file"]
    processed_rejects_file = config["processed_rejects_file"]

    df = load_rejects(raw_rejects_file)
    save_rejects_csv(df, processed_rejects_file)

    logger.info("Saved cleaned rejects file to: %s", processed_rejects_file)
    logger.info("Row count: %s", len(df))
    logger.info("Bad datetime rows: %s", df["datetime"].isna().sum())
    logger.info("Top reject reasons:\n%s", df["error_simple"].value_counts())
