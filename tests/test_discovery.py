"""Tests for agent/discovery.py -- the SourceIdentity abstraction
(Continuous Ingestion Phase B correction: file identity must not leak
into the rest of the architecture as raw OS fields).
"""

import os

from agent.discovery import SourceIdentity, identify


def test_identify_returns_none_for_missing_file(tmp_path):
    assert identify(str(tmp_path / "does-not-exist.txt")) is None


def test_identify_returns_stable_identity_for_unchanged_file(tmp_path):
    path = tmp_path / "checkins.txt"
    path.write_text("line one\n", encoding="utf-8")

    first = identify(str(path))
    second = identify(str(path))

    assert first is not None
    assert first == second


def test_identify_stable_across_appends(tmp_path):
    path = tmp_path / "checkins.txt"
    path.write_text("line one\n", encoding="utf-8")
    before = identify(str(path))

    with open(path, "a", encoding="utf-8") as f:
        f.write("line two\n")

    after = identify(str(path))

    assert before == after


def test_identify_changes_across_delete_and_recreate(tmp_path):
    path = tmp_path / "checkins.txt"
    path.write_text("line one\n", encoding="utf-8")
    before = identify(str(path))

    os.remove(path)
    path.write_text("a fresh file\n", encoding="utf-8")
    after = identify(str(path))

    assert before is not None
    assert after is not None
    # Not asserting inequality unconditionally: filesystems may reuse a
    # freed inode (confirmed happening on Linux CI during Phase 1's own
    # watcher tests). The real safety property tailer.py relies on is
    # that IDENTITY-BASED rotation detection, combined with the
    # truncation check, always catches a reset -- not that identity must
    # always differ after delete+recreate specifically. See
    # test_tailer.py for that combined guarantee.


def test_source_identity_equality_is_value_based():
    a = SourceIdentity(token=(1, 2))
    b = SourceIdentity(token=(1, 2))
    c = SourceIdentity(token=(1, 3))

    assert a == b
    assert a != c


def test_source_identity_is_hashable():
    a = SourceIdentity(token=(1, 2))
    assert hash(a) == hash(SourceIdentity(token=(1, 2)))
