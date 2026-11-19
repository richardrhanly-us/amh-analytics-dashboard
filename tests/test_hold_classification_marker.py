"""Hold-count accuracy, decision D5: the branch's `hold_classification.corrected_from` marker.

* how a stored value is read (services.hold_classification): anything but an exact, valid marker is NO marker;
* that no application code can write it: only scripts/set_hold_corrected_from.py (audited) does, and every statement in
  src/ that writes a branch's settings document changes its own named key in place, so it leaves the marker untouched.
  The script's writes and audit rows run for real in tests/test_hold_corrected_from_postgres.py.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from services.hold_classification import HOLD_CLASSIFICATION_KEY, marker_block, parse_corrected_from

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def test_a_valid_marker_is_read_and_written_in_one_form():
    assert parse_corrected_from({"corrected_from": "2026-11-19"}) == date(2026, 11, 19)
    assert marker_block(date(2026, 11, 19)) == {"corrected_from": "2026-11-19"}
    assert parse_corrected_from(marker_block(date(2026, 1, 2))) == date(2026, 1, 2)


@pytest.mark.parametrize("stored", [
    None, {}, [], "2026-11-19", {"corrected_from": None}, {"corrected_from": ""}, {"corrected_from": "2026-11-31"},
    {"corrected_from": "2026-11-19T00:00:00"}, {"corrected_from": "20261119"}, {"corrected_from": 20261119},
    {"corrected_from": "2026-11-19", "by": "x"}, {"CORRECTED_FROM": "2026-11-19"}, {"corrected_from": " 2026-11-19"},
])
def test_anything_else_is_no_marker(stored):
    assert parse_corrected_from(stored) is None


def _sql_statements(path: Path) -> list[str]:
    return re.findall(r'text\(\s*"""(.*?)"""', path.read_text(encoding="utf-8"), flags=re.S)


def test_no_application_code_names_the_marker_except_its_reader():
    naming = sorted(str(p.relative_to(ROOT)).replace("\\", "/") for p in SRC.rglob("*.py")
                    if HOLD_CLASSIFICATION_KEY in p.read_text(encoding="utf-8"))

    assert naming == ["src/services/hold_classification.py", "src/services/hold_report_service.py"]


def test_every_write_to_a_branch_settings_document_in_src_changes_only_its_own_named_key():
    writes = []
    for path in SRC.rglob("*.py"):
        for sql in _sql_statements(path):
            if re.search(r"\b(UPDATE|INSERT INTO)\s+branch_settings\b", sql):
                writes.append((path.name, " ".join(sql.split())))

    assert writes, "the scan found no branch settings write at all: it is not looking in the right place"
    for name, sql in writes:
        assert HOLD_CLASSIFICATION_KEY not in sql, name
        if "ON CONFLICT" in sql or sql.startswith("UPDATE"):
            # An existing document is changed only by jsonb_set on one named key, or by removing one named key.
            assigned = re.findall(r"SET settings_json = (.*?), updated_at", sql)
            assert assigned, (name, sql)
            for expression in assigned:
                assert re.fullmatch(r"jsonb_set\(branch_settings\.settings_json, '\{(\w+)\}', CAST\(:\w+ AS JSONB\), true\)"
                                    r"|settings_json - '(\w+)'", expression), (name, expression)
        else:
            # A plain INSERT creates a new branch's document; without ON CONFLICT it can never overwrite one.
            assert sql.startswith("INSERT INTO branch_settings"), (name, sql)
