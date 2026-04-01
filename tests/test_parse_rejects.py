import pandas as pd

from scripts.parse_rejects import (
    simplify_error_message,
    load_rejects,
    save_rejects_csv,
)


def make_sample_rejects_file(tmp_path):
    sample_lines = [
        "33472004611481|Item not found in database|1/31/2026|8:04:52 AM",
        "33472009128671|ACS connection failure|1/31/2026|8:05:55 AM",
        "33472004162170|Multiple RFID tags detected|1/31/2026|8:07:41 AM",
        "badrow|onlytwofields",
    ]

    file_path = tmp_path / "rejects.txt"
    file_path.write_text("\n".join(sample_lines), encoding="utf-8")
    return file_path


def test_simplify_error_message():
    assert simplify_error_message("Item not found in database") == "Item Not Found"
    assert simplify_error_message("ACS connection failure") == "ILS / ACS Failure"
    assert simplify_error_message("Multiple RFID tags detected") == "RFID Collision"
    assert simplify_error_message("Collection code mismatch") == "Call Number / Config Error"
    assert simplify_error_message("Library not found") == "Routing Error"
    assert simplify_error_message("Something else entirely") == "Other"
    assert simplify_error_message(None) == "Unknown"


def test_load_rejects_parses_rows_and_skips_short_lines(tmp_path):
    file_path = make_sample_rejects_file(tmp_path)

    df = load_rejects(filepath=file_path)

    assert len(df) == 3
    assert "datetime" in df.columns
    assert "date_only" in df.columns
    assert "hour" in df.columns
    assert "day_of_week" in df.columns
    assert "error_simple" in df.columns


def test_load_rejects_builds_datetime_fields(tmp_path):
    file_path = make_sample_rejects_file(tmp_path)

    df = load_rejects(filepath=file_path)

    assert pd.notna(df.loc[0, "datetime"])
    assert str(df.loc[0, "date_only"]) == "2026-01-31"
    assert int(df.loc[0, "hour"]) == 8
    assert df.loc[0, "day_of_week"] == "Saturday"


def test_load_rejects_simplifies_errors(tmp_path):
    file_path = make_sample_rejects_file(tmp_path)

    df = load_rejects(filepath=file_path)

    assert df.loc[0, "error_simple"] == "Item Not Found"
    assert df.loc[1, "error_simple"] == "ILS / ACS Failure"
    assert df.loc[2, "error_simple"] == "RFID Collision"


def test_save_rejects_csv_writes_file(tmp_path):
    file_path = make_sample_rejects_file(tmp_path)
    df = load_rejects(filepath=file_path)

    output_path = tmp_path / "processed" / "rejects_clean.csv"
    save_rejects_csv(df, output_path=output_path)

    assert output_path.exists()

    saved_df = pd.read_csv(output_path)
    assert len(saved_df) == 3
    assert "datetime" in saved_df.columns
    assert "error_simple" in saved_df.columns
