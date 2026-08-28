"""Tests for agent/watcher.py -- polling, offset tracking, rotation and
truncation detection, and outbox insertion. No network access anywhere in
this module or the one under test.

Requires agent/ on sys.path for watcher.py's own top-level-style imports
(config/parse_checkins/parse_rejects/parse_acs/logger_config) -- provided
by tests/conftest.py, matching CI's PYTHONPATH.
"""

import json
import os
import socket

import pytest

from agent import outbox, watcher

CUSTOMER_ID = 1
BRANCH_ID = 1


def make_checkin_line(barcode, title="Some Book Title", time_str="02:15:00 PM", date_str="08/27/2026"):
    fields = [
        title, barcode, "FIC", "F SMITH", "A1", "1", "FALSE", "OK", "5", "", "", "",
        date_str, time_str,
    ]
    return "|".join(fields) + "\n"


def make_reject_line(barcode, message="Item not found", time_str="02:16:00 PM", date_str="08/27/2026"):
    return f"{barcode}|{message}|{date_str}|{time_str}\n"


def make_acs_line(barcode, patron="P1001", dest="Main", time_str="02:17:00 PM", date_str="08/27/2026"):
    message = f"AB{barcode}|AJSome Title|AA{patron}|CT{dest}"
    return f"{date_str}\x02{time_str}\x02{message}\x01\n"


@pytest.fixture
def conn(tmp_path):
    connection = outbox.connect(tmp_path / "agent.db")
    yield connection
    connection.close()


@pytest.fixture
def watched_paths(tmp_path):
    return {
        "checkins_path": str(tmp_path / "Checkins.txt"),
        "rejects_path": str(tmp_path / "Rejects.txt"),
        "acs_path": str(tmp_path / "ACS Log.txt"),
    }


def _poll(conn, paths):
    return watcher.poll_once(
        conn,
        checkins_path=paths["checkins_path"],
        rejects_path=paths["rejects_path"],
        acs_path=paths["acs_path"],
        customer_id=CUSTOMER_ID,
        branch_id=BRANCH_ID,
    )


def test_missing_files_are_skipped_without_error(conn, watched_paths):
    result = _poll(conn, watched_paths)

    assert result.checkins.existed is False
    assert result.rejects.existed is False
    assert result.acs.existed is False
    assert outbox.count_pending_events(conn) == 0


def test_first_read_from_new_file(conn, watched_paths):
    with open(watched_paths["checkins_path"], "w", encoding="utf-8") as f:
        f.write(make_checkin_line("111"))
        f.write(make_checkin_line("222"))

    result = _poll(conn, watched_paths)

    assert result.checkins.events_inserted == 2
    assert result.checkins.rotated is False
    assert result.checkins.truncated is False

    state = outbox.get_file_state(conn, watched_paths["checkins_path"])
    assert state is not None
    assert state["last_byte_offset"] == result.checkins.end_offset

    rows = conn.execute(
        "SELECT * FROM local_events WHERE event_type = 'checkin' ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    payload = json.loads(rows[0]["payload_json"])
    assert payload["barcode"] == "111"
    assert payload["customer_id"] == CUSTOMER_ID
    assert payload["branch_id"] == BRANCH_ID
    assert payload["source_file"] == "Checkins.txt"


def test_append_only_read_resumes_from_stored_offset(conn, watched_paths):
    path = watched_paths["checkins_path"]

    with open(path, "w", encoding="utf-8") as f:
        f.write(make_checkin_line("111"))

    first = _poll(conn, watched_paths)
    assert first.checkins.events_inserted == 1

    with open(path, "a", encoding="utf-8") as f:
        f.write(make_checkin_line("222"))

    second = _poll(conn, watched_paths)
    assert second.checkins.events_inserted == 1
    assert second.checkins.start_offset == first.checkins.end_offset

    assert outbox.count_pending_events(conn) == 2


def test_restart_resumes_from_stored_offset(tmp_path, watched_paths):
    db_path = tmp_path / "agent.db"
    path = watched_paths["checkins_path"]

    with open(path, "w", encoding="utf-8") as f:
        f.write(make_checkin_line("111"))

    conn_a = outbox.connect(db_path)
    _poll(conn_a, watched_paths)
    conn_a.close()  # simulate the process exiting

    with open(path, "a", encoding="utf-8") as f:
        f.write(make_checkin_line("222"))

    # A brand new connection to the same db file stands in for a restart.
    conn_b = outbox.connect(db_path)
    try:
        result = _poll(conn_b, watched_paths)
        assert result.checkins.events_inserted == 1  # only the appended line
        assert outbox.count_pending_events(conn_b) == 2
    finally:
        conn_b.close()


def test_delete_and_recreate_detected_via_dev_ino(conn, watched_paths):
    path = watched_paths["checkins_path"]

    with open(path, "w", encoding="utf-8") as f:
        f.write(make_checkin_line("111"))
        f.write(make_checkin_line("222"))

    first = _poll(conn, watched_paths)
    assert first.checkins.events_inserted == 2

    state_before = outbox.get_file_state(conn, path)

    os.remove(path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(make_checkin_line("999"))

    second = _poll(conn, watched_paths)

    assert second.checkins.rotated is True
    assert second.checkins.start_offset == 0
    # The new file's one line must be captured, not skipped as if it
    # were a continuation of the old file at some nonzero offset.
    assert second.checkins.events_inserted == 1

    state_after = outbox.get_file_state(conn, path)
    assert state_after["file_ino"] != state_before["file_ino"]

    rows = conn.execute(
        "SELECT payload_json FROM local_events WHERE event_type = 'checkin' ORDER BY id"
    ).fetchall()
    barcodes = [json.loads(r["payload_json"])["barcode"] for r in rows]
    assert barcodes == ["111", "222", "999"]


def test_in_place_truncation_resets_offset(conn, watched_paths):
    path = watched_paths["checkins_path"]

    with open(path, "w", encoding="utf-8") as f:
        f.write(make_checkin_line("111"))
        f.write(make_checkin_line("222"))

    first = _poll(conn, watched_paths)
    assert first.checkins.events_inserted == 2

    state_before = outbox.get_file_state(conn, path)

    # Reopening an existing file in "w" mode truncates it in place on
    # Windows (CREATE_ALWAYS on an existing file) -- same file identity,
    # smaller size. Deliberately not os.remove()'d first, unlike the
    # rotation test above.
    with open(path, "w", encoding="utf-8") as f:
        f.write(make_checkin_line("333"))

    second = _poll(conn, watched_paths)

    assert second.checkins.truncated is True
    assert second.checkins.rotated is False
    assert second.checkins.start_offset == 0
    assert second.checkins.events_inserted == 1

    state_after = outbox.get_file_state(conn, path)
    assert state_after["file_ino"] == state_before["file_ino"]

    rows = conn.execute(
        "SELECT payload_json FROM local_events WHERE event_type = 'checkin' ORDER BY id"
    ).fetchall()
    barcodes = [json.loads(r["payload_json"])["barcode"] for r in rows]
    assert barcodes == ["111", "222", "333"]


def test_forced_reread_produces_zero_duplicate_rows(conn, watched_paths):
    path = watched_paths["checkins_path"]

    with open(path, "w", encoding="utf-8") as f:
        f.write(make_checkin_line("111"))
        f.write(make_checkin_line("222"))

    first = _poll(conn, watched_paths)
    assert first.checkins.events_inserted == 2

    # Simulate an offset-resumption glitch: force the stored offset back
    # to 0 without the file itself changing identity or content at all.
    state = outbox.get_file_state(conn, path)
    outbox.save_file_state(
        conn,
        file_path=path,
        file_dev=state["file_dev"],
        file_ino=state["file_ino"],
        last_byte_offset=0,
        last_modified=state["last_modified"],
    )

    second = _poll(conn, watched_paths)

    assert second.checkins.lines_read == 2  # both lines really were re-read
    assert second.checkins.events_inserted == 0  # but produced no new rows
    assert second.checkins.events_deduped == 2
    assert outbox.count_pending_events(conn) == 2


def test_partial_trailing_line_not_consumed(conn, watched_paths):
    path = watched_paths["checkins_path"]
    complete = make_checkin_line("111") + make_checkin_line("222")

    # Derive the partial/completion split from a correctly-built line
    # rather than hand-typing a truncated one, so this test can't itself
    # introduce a field-count/field-order mistake independent of the
    # thing it's meant to verify. Cuts off the trailing "PM\n" of the
    # third line's time field -- partial has no trailing newline yet
    # (simulating a write in progress); remainder completes it.
    full_third_line = make_checkin_line("333")
    assert full_third_line.endswith("PM\n")
    partial = full_third_line[: -len("PM\n")]
    remainder = full_third_line[-len("PM\n") :]

    with open(path, "w", encoding="utf-8") as f:
        f.write(complete)

    # Real on-disk size after just the two complete lines -- not
    # len(complete), since Windows text-mode writes translate "\n" to
    # "\r\n", so a naive Python string length wouldn't match the actual
    # byte offset the watcher's own f.tell()-based tracking produces.
    size_after_complete_lines = os.path.getsize(path)

    with open(path, "a", encoding="utf-8") as f:
        f.write(partial)

    result = _poll(conn, watched_paths)

    # Only the two complete lines are captured; the partial one is left
    # unconsumed rather than parsed as a (wrong) short/garbage row.
    assert result.checkins.events_inserted == 2
    assert result.checkins.end_offset == size_after_complete_lines

    state = outbox.get_file_state(conn, path)
    assert state["last_byte_offset"] == size_after_complete_lines

    # The writer finishes the line a moment later.
    with open(path, "a", encoding="utf-8") as f:
        f.write(remainder)

    second = _poll(conn, watched_paths)
    assert second.checkins.events_inserted == 1

    rows = conn.execute(
        "SELECT payload_json FROM local_events WHERE event_type = 'checkin' ORDER BY id"
    ).fetchall()
    barcodes = [json.loads(r["payload_json"])["barcode"] for r in rows]
    assert barcodes == ["111", "222", "333"]


def test_no_new_data_is_a_no_op(conn, watched_paths):
    path = watched_paths["checkins_path"]

    with open(path, "w", encoding="utf-8") as f:
        f.write(make_checkin_line("111"))

    first = _poll(conn, watched_paths)
    second = _poll(conn, watched_paths)

    assert second.checkins.lines_read == 0
    assert second.checkins.events_inserted == 0
    assert second.checkins.start_offset == first.checkins.end_offset
    assert second.checkins.end_offset == first.checkins.end_offset


def test_all_three_event_types_enter_the_outbox(conn, watched_paths):
    with open(watched_paths["checkins_path"], "w", encoding="utf-8") as f:
        f.write(make_checkin_line("111"))

    with open(watched_paths["rejects_path"], "w", encoding="utf-8") as f:
        f.write(make_reject_line("222"))

    with open(watched_paths["acs_path"], "w", encoding="utf-8") as f:
        f.write(make_acs_line("333"))

    result = _poll(conn, watched_paths)

    assert result.checkins.events_inserted == 1
    assert result.rejects.events_inserted == 1
    assert result.acs.events_inserted == 1

    counts = dict(
        conn.execute(
            "SELECT event_type, COUNT(*) as n FROM local_events GROUP BY event_type"
        ).fetchall()
    )
    assert counts == {"checkin": 1, "reject": 1, "acs": 1}

    reject_payload = json.loads(
        conn.execute(
            "SELECT payload_json FROM local_events WHERE event_type = 'reject'"
        ).fetchone()["payload_json"]
    )
    assert reject_payload["barcode"] == "222"
    assert reject_payload["message"] == "Item not found"

    acs_payload = json.loads(
        conn.execute(
            "SELECT payload_json FROM local_events WHERE event_type = 'acs'"
        ).fetchone()["payload_json"]
    )
    assert acs_payload["barcode"] == "333"
    assert acs_payload["patron_id"] == "P1001"
    assert acs_payload["destination"] == "Main"


def test_watcher_makes_no_network_calls(conn, watched_paths, monkeypatch):
    def _blocked(*args, **kwargs):
        raise AssertionError("watcher attempted to open a network socket")

    monkeypatch.setattr(socket, "socket", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)

    with open(watched_paths["checkins_path"], "w", encoding="utf-8") as f:
        f.write(make_checkin_line("111"))
    with open(watched_paths["rejects_path"], "w", encoding="utf-8") as f:
        f.write(make_reject_line("222"))
    with open(watched_paths["acs_path"], "w", encoding="utf-8") as f:
        f.write(make_acs_line("333"))

    # If poll_once ever touches the network, the monkeypatched socket
    # functions above raise before this line finishes.
    result = _poll(conn, watched_paths)

    assert result.checkins.events_inserted == 1
    assert result.rejects.events_inserted == 1
    assert result.acs.events_inserted == 1


def test_run_forever_respects_max_iterations(conn, watched_paths):
    with open(watched_paths["checkins_path"], "w", encoding="utf-8") as f:
        f.write(make_checkin_line("111"))

    watcher.run_forever(
        conn,
        checkins_path=watched_paths["checkins_path"],
        rejects_path=watched_paths["rejects_path"],
        acs_path=watched_paths["acs_path"],
        customer_id=CUSTOMER_ID,
        branch_id=BRANCH_ID,
        poll_interval_seconds=0,
        max_iterations=2,
    )

    # Two iterations over an unchanging file must still only capture the
    # one real event once.
    assert outbox.count_pending_events(conn) == 1
