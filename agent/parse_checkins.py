from pathlib import Path

from .config import load_config
from .logger_config import get_logger
from .parser.checkins import COLUMNS, empty_df, normalize_destination, parse_lines

logger = get_logger("parse_checkins")

# Parsing logic (COLUMNS, normalize_destination, the line parser) now lives
# in agent/parser/checkins.py -- this module is a thin, behavior-preserving
# wrapper kept for existing callers (agent/run_pipeline.py -- LEGACY /
# VALIDATION-ONLY, see its docstring -- and this module's own legacy-
# import-path tests). See agent/parser/__init__.py. The canonical
# continuous runtime (agent/runtime/*) imports agent.parser directly and
# never goes through this wrapper.
# __all__ marks COLUMNS/normalize_destination as a deliberate re-export, not
# dead imports -- they're part of this module's existing public surface.
__all__ = [
    "COLUMNS",
    "load_checkins",
    "load_checkins_incremental",
    "normalize_destination",
    "save_checkins_csv",
]

_empty_checkins_df = empty_df
_parse_checkins_lines = parse_lines


def load_checkins(filepath=None):
    if filepath is None:
        filepath = load_config()["raw_checkins_file"]

    logger.info("Loading checkins from %s", filepath)

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    return _parse_checkins_lines(lines)


def load_checkins_incremental(filepath=None, start_offset=0):
    if filepath is None:
        filepath = load_config()["raw_checkins_file"]

    file_path = Path(filepath)

    if not file_path.exists():
        logger.warning("Checkins file not found: %s", filepath)
        return _empty_checkins_df(), 0

    file_size = file_path.stat().st_size

    if start_offset is None:
        start_offset = 0

    if start_offset < 0 or start_offset > file_size:
        logger.warning(
            "Checkins offset invalid or file rotated/truncated. Resetting offset from %s to 0",
            start_offset
        )
        start_offset = 0

    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        f.seek(start_offset)
        lines = f.readlines()
        end_offset = f.tell()

    df = _parse_checkins_lines(lines)

    logger.info(
        "Loaded checkins incrementally | start_offset=%s end_offset=%s rows=%s",
        start_offset,
        end_offset,
        len(df)
    )

    return df, end_offset


def save_checkins_csv(df, output_path=None):
    if output_path is None:
        output_path = load_config()["processed_checkins_file"]

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        df.to_csv(output_file, index=False)
        logger.info("Saved checkins CSV to %s", output_path)
    except PermissionError:
        logger.exception(
            "Could not save checkins CSV because the file is open: %s "
            "(close it in Excel and run the script again)",
            output_path,
        )
        raise


if __name__ == "__main__":
    config = load_config()
    raw_checkins_file = config["raw_checkins_file"]
    processed_checkins_file = config["processed_checkins_file"]

    df = load_checkins(raw_checkins_file)
    save_checkins_csv(df, processed_checkins_file)

    logger.info("Saved cleaned checkins file to: %s", processed_checkins_file)
    logger.info("Row count: %s", len(df))
    logger.info(
        "Destination breakdown:\n%s",
        df["destination"].value_counts(dropna=False),
    )
    logger.info("Bad datetime rows: %s", df["datetime"].isna().sum())
    logger.info("Problem items: %s", int(df["is_problem"].sum()))
    logger.info("Transit items: %s", int(df["is_transit"].sum()))
