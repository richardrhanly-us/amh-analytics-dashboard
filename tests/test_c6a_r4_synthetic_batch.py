"""The synthetic Tech Logic batch writer used to qualify machine replacement on VMs (scripts/c6a_r4)."""

from __future__ import annotations

import hashlib
import json

import pytest

from scripts.c6a_r4 import make_synthetic_batch as batch


def test_a_batch_has_every_acs_separator_line_breaks_and_matching_hashes(tmp_path):
    folder = batch.write_batch(tmp_path, "A1", "America/Chicago", 2.0)

    manifest = json.loads((folder / "batch.json").read_text(encoding="utf-8"))
    assert manifest["label"] == "A1" and manifest["expected_events"] == batch.EXPECTED_EVENTS
    assert {name: entry["target"] for name, entry in manifest["files"].items()} == batch.TARGETS
    for entry in manifest["files"].values():
        data = (folder / entry["part"]).read_bytes()
        assert data.endswith(b"\n")
        assert (len(data), data.count(b"\n"), hashlib.sha256(data).hexdigest()) == (
            entry["bytes"], entry["lines"], entry["sha256"])
    acs = (folder / "acs.part").read_bytes().splitlines()
    assert len(acs) == 13 and all(line.count(b"\x02") == 2 for line in acs)


def test_a_line_without_its_separators_is_refused():
    _, parts = batch.build_batch(2.0, "America/Chicago")
    broken = dict(parts, acs=parts["acs"].replace(b"\x02", b" ", 2))

    assert batch.check_parts(parts) == []
    assert batch.check_parts(broken) == ["acs line 1: 0 separators (0x02), expected 2"]


def test_two_batches_have_different_records(tmp_path):
    first = batch.write_batch(tmp_path, "A1", "America/Chicago", 10.0)
    second = batch.write_batch(tmp_path, "A2", "America/Chicago", 2.0)

    assert (first / "acs.part").read_bytes() != (second / "acs.part").read_bytes()


def test_an_existing_batch_or_a_bad_label_is_never_overwritten(tmp_path):
    folder = batch.write_batch(tmp_path, "A1", "America/Chicago", 2.0)
    before = (folder / "acs.part").read_bytes()

    with pytest.raises(FileExistsError):
        batch.write_batch(tmp_path, "A1", "America/Chicago", 2.0)
    with pytest.raises(ValueError, match="label"):
        batch.write_batch(tmp_path, "../A1", "America/Chicago", 2.0)
    assert (folder / "acs.part").read_bytes() == before
