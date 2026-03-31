import pandas as pd

from scripts.parse_checkins import (
    normalize_destination,
    load_checkins,
    save_checkins_csv,
)


def make_sample_checkins_file(tmp_path):
    sample_lines = [
        "Flashlight : /|33472004611481|WLFICNEW|FIC CHOI|000|1|False||0|N|N|N|1/31/2026|8:04:52 AM",
        "Theo of golden :|33472009128671|MLFICNEW|FIC LEVI|000|WESTSIDE (2910 IH35)|False||5|N|N|N|1/31/2026|8:04:55 AM",
        "First lie wins /|33472004162170|MLFIC|FIC ELSTON|000|LIBRARY EXPRESS|TRUE|Overdue.|3|N|Y|N|1/31/2026|8:07:41 AM",
        "Broken row with too few fields|123",
    ]

    file_path = tmp_path / "checkins.txt"
    file_path.write_text("\n".join(sample_lines), encoding="utf-8")
    return file_path


def test_normalize_destination():
    assert normalize_destination("1") == "Main"
    assert normalize_destination("LOCAL") == "Main"
    assert normalize_destination("MAIN") == "Main"
    assert normalize_destination("WESTSIDE (2910 IH35)") == "Westside"
    assert normalize_destination("LIBRARY EXPRESS") == "Library Express"
    assert normalize_destination("No Agency Destination") == "No Agency Destination"
    assert normalize_destination("") == ""
    assert normalize_destination(None) == ""


def test_load_checkins_parses_rows_and_skips_short_lines(tmp_path):
    file_path = make_sample_checkins_file(tmp_path)

    df = load_checkins(filepath=file_path)

    assert len(df) == 3
    assert "datetime" in df.columns
    assert "destination" in df.columns
    assert "date_only" in df.columns
    assert "hour" in df.columns
    assert "day_of_week" in df.columns
    assert "is_transit" in df.columns


def test_load_checkins_normalizes_destination_values(tmp_path):
    file_path = make_sample_checkins_file(tmp_path)

    df = load_checkins(filepath=file_path)

    assert df.loc[0, "destination"] == "Main"
    assert df.loc[1, "destination"] == "Westside"
    assert df.loc[2, "destination"] == "Library Express"


def test_load_checkins_builds_datetime_fields(tmp_path):
    file_path = make_sample_checkins_file(tmp_path)

    df = load_checkins(filepath=file_path)

    assert pd.notna(df.loc[0, "datetime"])
    assert str(df.loc[0, "date_only"]) == "2026-01-31"
    assert int(df.loc[0, "hour"]) == 8
    assert df.loc[0, "day_of_week"] == "Saturday"


def test_load_checkins_sets_transit_flag(tmp_path):
    file_path = make_sample_checkins_file(tmp_path)

    df = load_checkins(filepath=file_path)

    assert bool(df.loc[0, "is_transit"]) is False
    assert bool(df.loc[1, "is_transit"]) is True
    assert bool(df.loc[2, "is_transit"]) is True


def test_load_checkins_converts_is_problem_to_boolean(tmp_path):
    file_path = make_sample_checkins_file(tmp_path)

    df = load_checkins(filepath=file_path)

    assert bool(df.loc[0, "is_problem"]) is False
    assert bool(df.loc[1, "is_problem"]) is False
    assert bool(df.loc[2, "is_problem"]) is True


def test_save_checkins_csv_writes_file(tmp_path):
    file_path = make_sample_checkins_file(tmp_path)
    df = load_checkins(filepath=file_path)

    output_path = tmp_path / "processed" / "checkins_clean.csv"
    save_checkins_csv(df, output_path=output_path)

    assert output_path.exists()

    saved_df = pd.read_csv(output_path)
    assert len(saved_df) == 3
    assert "destination" in saved_df.columns
    assert "datetime" in saved_df.columns