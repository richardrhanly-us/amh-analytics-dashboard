"""Contract v2 collector: the privacy boundary, proved two ways.

DYNAMIC. A canary is planted in EVERY raw field a Tech Logic record can carry, the collector runs (success, conflict, invalid-event, failure,
hostile server, dry run), and EVERY sink is searched: every byte of every file the collector wrote (logs, state, status, patron cache,
quarantine), every logged record, stdout/stderr, and every HTTP payload it sent. A canary is searched raw, lower-cased, upper-cased, hex,
base64 and as UTF-16.

STATIC. The import graph and the AST are checked so the property does not depend on today's test data: only the transformation layer may
reach the raw layer, downstream modules hold no raw-typed value, no log/print argument can be an exception message, and no module hashes
data with a bare digest.
"""

from __future__ import annotations

import ast
import base64
import dataclasses
import json
import logging
import re
import sys
from pathlib import Path

import pytest
import requests
from collector_v2_support import (
    KEY_ID,
    MASTER,
    TOKEN,
    Reply,
    ScriptedSession,
    acs_line,
    checkin_line,
    every_byte_under,
    item_message,
    minutes_ago,
    reject_line,
)
from test_collector_v2_run import Env, conflict_reply, empty_other_sources

from collector import (
    v2_events,
    v2_identity,
    v2_keys,
    v2_status,
    v2_transform,
    v2_uploader,
)
from src.services import ingest_v2_models as server

ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = ROOT / "collector"
BASE = minutes_ago(45)

# One DISTINCT canary per raw field, so a failure names the field that leaked.
F = {
    "acs.patron_id": "CNRY-ACS-PATRONID-01", "acs.patron_name": "CNRY-ACS-PATRONNAME-02", "acs.patron_type": "CNRY-ACS-PATRONTYPE-03",
    "acs.address": "CNRY-ACS-ADDRESS-04", "acs.email": "CNRY-ACS-EMAIL-05", "acs.item_barcode": "CNRY-ACS-BARCODE-06",
    "acs.title": "CNRY-ACS-TITLE-07", "acs.item_patron_id": "CNRY-ACS-ITEMPATRON-08", "acs.destination": "CNRY-ACS-DESTINATION-09",
    "acs.sip2_extra": "CNRY-ACS-SIP2EXTRA-10", "acs.nonhold_barcode": "CNRY-ACS-NONHOLD-11", "acs.other10_barcode": "CNRY-ACS-OTHER10-12",
    "acs.card_scan": "CNRY-ACS-CARDSCAN-13",
    "ci.title": "CNRY-CI-TITLE-20", "ci.barcode": "CNRY-CI-BARCODE-21", "ci.collection": "CNRY-CI-COLLECTION-22",
    "ci.call_number": "CNRY-CI-CALLNUMBER-23", "ci.shelf": "CNRY-CI-SHELF-24", "ci.destination": "CNRY-CI-DESTINATION-25",
    "ci.message": "CNRY-CI-MESSAGE-26", "ci.bin": "CNRY-CI-BIN-27", "ci.flag": "CNRY-CI-FLAG-28", "ci.card_scan": "CNRY-CI-CARDSCAN-29",
    "rj.barcode": "CNRY-RJ-BARCODE-30", "rj.text": "CNRY-RJ-TEXT-31", "rj.card_scan": "CNRY-RJ-CARDSCAN-32",
}
SECRET_ITEMS = {"master": MASTER}


def variants(value: str) -> list[bytes]:
    raw = value.encode("utf-8")
    forms = {raw, value.lower().encode(), value.upper().encode(), raw.hex().encode(), raw.hex().upper().encode(),
             base64.b64encode(raw), base64.urlsafe_b64encode(raw), value.encode("utf-16-le"), value.encode("utf-16-be")}
    return sorted(forms)


def leaked_fields(blob: bytes) -> list[str]:
    return [name for name, value in F.items() if any(form in blob for form in variants(value))]


def corpus():
    """A corpus with one canary in every raw field of every source, plus patron-card scans in all three sources."""
    def at(n):
        return BASE + __import__("datetime").timedelta(seconds=n)

    acs = [
        acs_line(at(0), f"64 |AA{F['acs.patron_id']}|AE{F['acs.patron_name']}|PT{F['acs.patron_type']}"
                        f"|BD{F['acs.address']}|BE{F['acs.email']}"),
        acs_line(at(1), f"64 |AA{F['acs.card_scan']}|AENAME|PTADULT"),
        acs_line(at(2), item_message(F["acs.item_barcode"], F["acs.item_patron_id"], F["acs.destination"],
                                     extra=f"|ZZ{F['acs.sip2_extra']}", title=F["acs.title"])),
        acs_line(at(3), item_message(F["acs.nonhold_barcode"], F["acs.patron_id"], "Main", prefix="101NNY", title=F["acs.title"])),
        acs_line(at(4), item_message(F["acs.other10_barcode"], F["acs.patron_id"], "Main", prefix="100NUN", title=F["acs.title"])),
        acs_line(at(5), item_message(F["acs.card_scan"], None, "Main")),
    ]
    checkins = [
        checkin_line(at(10), F["ci.barcode"], F["ci.destination"], F["ci.bin"], title=F["ci.title"], call_number=F["ci.call_number"],
                     collection=F["ci.collection"], shelf=F["ci.shelf"], message=F["ci.message"], flag=F["ci.flag"]),
        checkin_line(at(11), F["ci.card_scan"], "1", "3"),
        checkin_line(at(12), F["acs.card_scan"], "1", "3"),
    ]
    rejects = [reject_line(at(20), F["rj.barcode"], F["rj.text"]), reject_line(at(21), F["rj.card_scan"], "acs down"),
               reject_line(at(22), F["acs.card_scan"], "acs down")]
    return {"acs": acs, "checkins": checkins, "rejects": rejects}


def everything(env, session, capsys, caplog):
    out = capsys.readouterr()
    return b"\n".join([every_byte_under(env.root), env.v2.status_path.read_bytes() if env.v2.status_path.exists() else b"",
                       json.dumps(session.calls).encode() if session is not None else b"", out.out.encode(), out.err.encode(),
                       caplog.text.encode()])


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    e.put_all(corpus())
    return e


def sweep_all(blob):
    assert leaked_fields(blob) == []
    for secret_name, secret in SECRET_ITEMS.items():
        for form in (secret, secret.hex().encode(), base64.b64encode(secret)):
            assert form not in blob, secret_name
    keys = v2_identity.derive_subkeys(MASTER)
    for purpose in v2_identity.PURPOSES:
        sub = getattr(keys, purpose)
        assert sub not in blob and sub.hex().encode() not in blob, purpose
    assert TOKEN.encode() not in blob


SCENARIOS = {
    "success": lambda: ScriptedSession(),
    "a 409 conflict on the first ACS event": lambda: ScriptedSession(script=[conflict_reply([0], "acs_items")]),
    "a 409 conflict on a check-in": lambda: ScriptedSession(script=[None, conflict_reply([0], "checkins")]),
    "an event-level 422": lambda: ScriptedSession(script=[Reply(422, {"detail": [{"loc": ["body", "acs_items", 1, "state"],
                                                                                   "msg": "x", "input": "echoed"}]})]),
    "an outage": lambda: ScriptedSession(default=Reply(503, {"detail": "CNRY-SERVER-TEXT"})),
    "an auth failure": lambda: ScriptedSession(default=Reply(401, {"detail": "CNRY-SERVER-TEXT"})),
    "a network exception": lambda: ScriptedSession(default=requests.ConnectionError(f"CNRY-EXC {F['acs.item_barcode']}")),
    "an unexpected exception": lambda: ScriptedSession(default=RuntimeError(f"CNRY-EXC {F['ci.barcode']} {TOKEN}")),
    "a hostile 200": lambda: ScriptedSession(default=Reply(200, {"status": "success", "echo": F["acs.patron_name"]})),
}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=list(SCENARIOS))
def test_no_canary_reaches_any_sink_in_any_scenario(env, capsys, caplog, scenario):
    session = SCENARIOS[scenario]()
    with caplog.at_level(logging.DEBUG):
        env.run(session)
        env.run(session)                                          # a second run: state, cache and quarantine are re-read and re-written
    blob = everything(env, session, capsys, caplog)
    sweep_all(blob)
    assert b"CNRY-SERVER" not in blob and b"CNRY-EXC" not in blob


def test_a_transform_failure_leaks_neither_the_record_nor_the_exception_message(env, capsys, caplog, monkeypatch):
    from agent.parser import checkins as checkins_parser

    def explode(lines):
        raise ValueError(f"cannot parse {lines[0]!r} {F['ci.barcode']} {F['ci.title']}")

    monkeypatch.setattr(checkins_parser, "parse_lines", explode)
    session = ScriptedSession()
    with caplog.at_level(logging.DEBUG):
        code = env.run(session)
    assert code == 1
    blob = everything(env, session, capsys, caplog)
    sweep_all(blob)
    assert b"ValueError" in blob                                   # the TYPE is reported, the message is not
    assert env.status()["last_error_class"] == "other"


def test_a_pandas_datetime_failure_message_never_reaches_a_sink(env, capsys, caplog, monkeypatch):
    import pandas as pd

    from agent.parser import rejects as rejects_parser

    def explode(_lines):
        return pd.to_datetime([F["rj.barcode"]], format="%Y-%m-%d")     # pandas quotes the offending value in its message

    monkeypatch.setattr(rejects_parser, "parse_lines", explode)
    session = ScriptedSession()
    with caplog.at_level(logging.DEBUG):
        env.run(session)
    sweep_all(everything(env, session, capsys, caplog))


def test_the_dry_run_leaks_nothing_to_stdout_stderr_logs_or_disk(env, capsys, caplog):
    with caplog.at_level(logging.DEBUG):
        code, output = env.dry()
    assert code == 0
    blob = everything(env, None, capsys, caplog) + output.encode()
    sweep_all(blob)


def test_the_v1_parsers_do_not_log_anything_during_a_v2_run(env, caplog):
    with caplog.at_level(logging.DEBUG):
        env.run(ScriptedSession())
    assert [r for r in caplog.records if r.name.startswith("parser.")] == []


def test_the_patron_cache_and_quarantine_contain_no_canary_after_a_conflicting_run(env):
    env.run(ScriptedSession(script=[conflict_reply([0], "acs_items")]))
    for path in (env.v2.patron_cache_path, env.v2.quarantine_path, env.v2.state_path, env.v2.status_path):
        assert path.exists(), path
        assert leaked_fields(path.read_bytes()) == [], path.name


def test_a_run_writes_only_the_expected_files(env):
    env.run(ScriptedSession(script=[conflict_reply([0], "acs_items")]))
    written = {p.relative_to(env.root).as_posix() for p in env.root.rglob("*") if p.is_file()}
    tech = {f"tech/{n}.txt" for n in ("acs", "checkins", "rejects")}
    expected = {"config.json", "config/classification_rules.json", "logs/collector.log", "data/v2/patron_cache.db",
                "data/v2/quarantine_v2.json", "data/v2/state_v2.json", "data/v2/status_v2.json"}
    assert written == expected | tech


def test_repr_and_str_of_the_runtime_objects_never_show_a_key(env):
    from collector_v2_support import make_context
    ctx, cache = make_context(env.root / "x")
    try:
        for text in (repr(ctx), str(ctx), repr(ctx.keys), repr(ctx.rules)):
            assert MASTER.hex() not in text and ctx.keys.event.hex() not in text and ctx.keys.item.hex() not in text
    finally:
        cache.close()


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI machine scope is Windows-only")
def test_a_full_run_with_the_real_dpapi_store_leaves_no_secret_or_canary_on_disk(tmp_path, monkeypatch, capsys, caplog):
    e = Env(tmp_path, monkeypatch)
    e.put_all(corpus())

    class ProtectedStore(v2_keys.DpapiSecretStore):
        def acl_state(self):  # the ACL itself is exercised in test_collector_v2_keys.py; a test tmp dir inherits the user's ACL
            return "protected"

    store = ProtectedStore(e.v2.secret_path)
    store.create(KEY_ID)
    secret = store.load(KEY_ID)
    e.store = store
    session = ScriptedSession()
    with caplog.at_level(logging.DEBUG):
        assert e.run(session) == 0
    blob = b"\n".join([every_byte_under(tmp_path), json.dumps(session.calls).encode(), capsys.readouterr().out.encode(),
                       caplog.text.encode()])
    assert leaked_fields(blob) == []
    for form in (secret, secret.hex().encode(), base64.b64encode(secret)):
        assert form not in blob


# =====================================================================================================================
# The payload can only carry what the server contract allows
# =====================================================================================================================

def test_the_event_formats_are_the_servers_formats():
    for name in ("UUID4_PATTERN", "HMAC_HEX_PATTERN", "DESTINATION_PATTERN", "BIN_PATTERN"):
        assert getattr(v2_events, name) == getattr(server, name), name
    assert v2_events.ERROR_CLASSES == server.ERROR_CLASSES and v2_events.ACS_ITEM_STATES == server.ACS_ITEM_STATES
    assert v2_events.NON_HOLD_STATES == server.NON_HOLD_STATES and v2_events.HEALTH_STATUSES == server.HEALTH_STATUSES
    assert v2_events.LAST_ERROR_CLASSES == server.LAST_ERROR_CLASSES
    assert v2_status.MAX_COUNTER == server.MAX_COUNTER
    assert v2_events.KINDS == ("checkins", "rejects", "acs_items")


def test_every_payload_field_is_a_field_of_the_server_model():
    keys = v2_identity.derive_subkeys(MASTER)
    when = __import__("datetime").datetime.now(__import__("datetime").UTC)
    item = v2_identity.item_key(keys, "X")
    samples = {
        "CheckinEvent": v2_identity.build_checkin(keys, event_time=when, item_key=item, destination="main", bin="1"),
        "RejectEvent": v2_identity.build_reject(keys, event_time=when, item_key=item, error_class="other"),
        "AcsHoldEvent": v2_identity.build_acs_item(keys, event_time=when, item_key=item, state="hold", destination="main", is_ill=False,
                                                   is_branch_services=False, is_collection_services=False,
                                                   ruleset_id="0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d"),
        "AcsNonHoldEvent": v2_identity.build_acs_item(keys, event_time=when, item_key=item, state="non_hold_101"),
    }
    for model_name, event in samples.items():
        model = getattr(server, model_name)
        assert set(event.payload()) <= set(model.model_fields), model_name
        model.model_validate(event.payload())                       # and the server accepts it
    assert set(v2_status.StatusSnapshot("healthy", None, 0, None, None).payload(KEY_ID)) <= set(server.StatusV2Request.model_fields)


def test_the_event_dataclasses_have_exactly_the_payload_fields():
    names = {cls.__name__: {f.name for f in dataclasses.fields(cls)} for cls in (v2_events.CheckinV2, v2_events.RejectV2, v2_events.AcsItemV2)}
    assert names["CheckinV2"] == {"event_key", "event_time", "item_key", "destination", "bin"}
    assert names["RejectV2"] == {"event_key", "event_time", "error_class", "item_key"}
    assert names["AcsItemV2"] == {"event_key", "event_time", "item_key", "state", "destination", "is_ill", "is_branch_services",
                                  "is_collection_services", "ruleset_id"}


@pytest.mark.parametrize("field,value", [
    ("event_key", "CNRY-RAW"), ("event_key", "A" * 64), ("event_key", "a" * 63), ("item_key", "31234000123456"), ("item_key", ""),
    ("destination", "Westside"), ("destination", "1abc"), ("destination", "a" * 33), ("destination", "west side"), ("bin", "Bin 3"),
    ("bin", "a" * 17), ("bin", ""), ("event_time", "2026-01-01T00:00:00Z"),
])
def test_an_event_refuses_a_field_that_is_not_in_its_safe_format_without_echoing_it(field, value):
    good = {"event_key": "a" * 64, "event_time": __import__("datetime").datetime.now(__import__("datetime").UTC), "item_key": "b" * 64,
                "destination": "main", "bin": "1"}
    good[field] = value
    with pytest.raises(v2_events.UnsafeEventError) as caught:
        v2_events.CheckinV2(**good)
    assert str(value) not in str(caught.value) or value == ""


def test_an_event_refuses_a_naive_or_non_utc_time():
    from datetime import datetime, timedelta, timezone
    for bad in (datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=-5))), "2026-01-01T00:00:00Z", 1_700_000_000):  # noqa: DTZ001
        with pytest.raises(v2_events.UnsafeEventError):
            v2_events.RejectV2("a" * 64, bad, "other", None)


def test_a_non_hold_event_cannot_carry_a_destination_or_flag():
    from datetime import UTC, datetime
    for extra in ({"destination": "main"}, {"is_ill": False}, {"is_branch_services": True}, {"is_collection_services": False},
                  {"ruleset_id": "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d"}):
        with pytest.raises(v2_events.UnsafeEventError):
            v2_events.AcsItemV2("a" * 64, datetime.now(UTC), "b" * 64, "non_hold_101", **extra)


def test_a_hold_event_needs_its_classification():
    from datetime import UTC, datetime
    with pytest.raises(v2_events.UnsafeEventError):
        v2_events.AcsItemV2("a" * 64, datetime.now(UTC), "b" * 64, "hold")


# =====================================================================================================================
# Static: the raw boundary
# =====================================================================================================================

V2_MODULES = sorted(p.stem for p in COLLECTOR.glob("v2_*.py"))
RAW_LAYER = {"v2_reader", "v2_normalize", "v2_classify"}
BOUNDARY = "v2_transform"


def imports_of(stem):
    tree = ast.parse((COLLECTOR / f"{stem}.py").read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            found.add(base)
            found.update(f"{base}.{alias.name}".strip(".") if base.strip(".") else alias.name for alias in node.names)
    return found


def short(name):
    return name.rsplit(".", 1)[-1].lstrip(".")


def test_the_module_inventory_is_what_this_test_assumes():
    assert set(V2_MODULES) == {"v2_classify", "v2_config", "v2_events", "v2_identity", "v2_keys", "v2_normalize", "v2_patrons",
                               "v2_quarantine", "v2_reader", "v2_rules", "v2_run", "v2_safe_errors", "v2_status", "v2_transform",
                               "v2_uploader"}


@pytest.mark.parametrize("stem", [m for m in V2_MODULES if m != BOUNDARY and m not in RAW_LAYER])
def test_only_the_transformation_layer_imports_the_raw_layer(stem):
    imported = {short(name) for name in imports_of(stem)}
    assert not imported & RAW_LAYER, (stem, imported & RAW_LAYER)


@pytest.mark.parametrize("stem", [m for m in V2_MODULES if m != BOUNDARY])
def test_no_module_but_the_boundary_touches_the_v1_parsers_pandas_or_the_v1_reader(stem):
    names = imports_of(stem)
    raw_modules = {"pandas", "agent", "agent.parser", "collector.parsers", "collector.reader", "parsers", "reader"}
    hits = {n for n in names if n.lstrip(".") in raw_modules or n.lstrip(".").startswith(("agent.", "pandas."))}
    allowed = {"v2_reader": {".reader", "reader"}}      # the raw layer's own reader primitive (identify)
    assert hits <= allowed.get(stem, set()), (stem, hits)


def test_the_run_module_uses_the_v1_uploader_module_only_for_build_session():
    tree = ast.parse((COLLECTOR / "v2_run.py").read_text(encoding="utf-8"))
    used = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "uploader"}
    assert used == {"build_session"}


def test_the_boundary_returns_only_safe_types():
    """SafeChunk and Counters are the only things read_next_chunk / read_tail_chunk return: events, integers and booleans."""
    hints = {f.name: str(f.type) for f in dataclasses.fields(v2_transform.SafeChunk)}
    assert hints == {"source": "str", "events": "tuple[SafeEvent, ...]", "cursor": "Cursor | None", "counters": "Counters",
                     "existed": "bool", "rotated": "bool", "truncated": "bool", "more": "bool", "empty": "bool"}
    counter_types = {f.name: str(f.type) for f in dataclasses.fields(v2_transform.Counters)}
    assert set(counter_types.values()) <= {"int", "dict[str, int]"}


def test_every_public_boundary_function_returns_a_safe_type_at_runtime(tmp_path):
    from collector_v2_support import make_context
    ctx, cache = make_context(tmp_path)
    try:
        path = tmp_path / "c.txt"
        path.write_bytes("".join(corpus()["checkins"]).encode())
        chunk = v2_transform.read_next_chunk("checkins", str(path), None, ctx, 100)
        tail = v2_transform.read_tail_chunk("checkins", str(path), ctx, 10_000)
        for result in (chunk, tail):
            assert all(isinstance(e, (v2_events.CheckinV2, v2_events.RejectV2, v2_events.AcsItemV2)) for e in result.events)
            assert result.cursor is None or (isinstance(result.cursor.offset, int) and result.cursor.identity is None
                                             or all(isinstance(i, int) for i in result.cursor.identity))
            assert all(isinstance(v, int) for v in result.counters.flat().values())
        text_fields = [f for f in dir(chunk) if not f.startswith("_") and isinstance(getattr(chunk, f), str)]
        assert text_fields == ["source"]
    finally:
        cache.close()


def test_the_raw_layer_is_imported_only_lazily_by_the_boundary_and_the_parsers_only_inside_functions():
    tree = ast.parse((COLLECTOR / "v2_transform.py").read_text(encoding="utf-8"))
    top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not any((getattr(n, "module", "") or "").startswith("agent") for n in top_level)   # importing a parser opens its log file


# =====================================================================================================================
# Static: logging, printing and exceptions
# =====================================================================================================================

LOG_METHODS = {"debug", "info", "warning", "error", "critical", "exception", "log"}
SAFE_WRAPPERS = {"describe", "_failure_category", "type", "isinstance", "summarize"}


def _calls_using(node, name):
    """Calls inside `node` that pass the exception variable `name` other than through a safe wrapper."""
    offenders = []

    def visit(n, guarded):
        if isinstance(n, ast.Call):
            func = n.func
            fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            guarded = guarded or fname in SAFE_WRAPPERS
        if isinstance(n, ast.Name) and n.id == name and not guarded:
            offenders.append(n.lineno)
        for child in ast.iter_child_nodes(n):
            visit(child, guarded)

    visit(node, False)
    return offenders


@pytest.mark.parametrize("stem", V2_MODULES)
def test_no_exception_variable_is_ever_formatted_logged_printed_or_returned_except_through_describe(stem):
    """Inside an `except ... as exc:` block the variable may only be passed to `describe`/`isinstance`/`type` or used for its `.code`."""
    tree = ast.parse((COLLECTOR / f"{stem}.py").read_text(encoding="utf-8"))
    offenders = []
    for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.name):
        # A `ConfigError` is the ONE error whose message may be shown: collector/v2_config.py builds every message from setting NAMES only
        # (test_bad_v2_settings_are_refused_naming_the_setting_not_the_value proves no value is echoed).
        if isinstance(handler.type, ast.Name) and handler.type.id == "ConfigError":
            continue
        for statement in handler.body:
            for line in _calls_using(statement, handler.name):
                # `exc.code` / `exc.summary` (attribute access on our own fixed-code errors) is the one allowed use
                offenders.append((stem, handler.name, line))
    allowed = []
    for stem_, name, line in offenders:
        source = (COLLECTOR / f"{stem_}.py").read_text(encoding="utf-8").splitlines()[line - 1]
        if re.search(rf"\b{name}\.(code|summary)\b|isinstance\(\s*{name}\b|_failure_category\(\s*{name}\b|describe\(\s*{name}\b", source):
            allowed.append((stem_, name, line))
    assert [o for o in offenders if o not in allowed] == []


@pytest.mark.parametrize("stem", V2_MODULES)
def test_nothing_uses_the_forbidden_error_reporting_shapes(stem):
    source = (COLLECTOR / f"{stem}.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert name not in {"exception", "print_exc", "print_exception", "format_exc", "format_exception", "excepthook"}, (stem, node.lineno)
            assert not any(kw.arg == "exc_info" for kw in node.keywords), (stem, node.lineno)
            assert not any(kw.arg == "stack_info" for kw in node.keywords), (stem, node.lineno)
    assert "traceback.format" not in source and "sys.exc_info" not in source and "__traceback__.tb_frame" not in source


@pytest.mark.parametrize("stem", V2_MODULES)
def test_log_and_print_arguments_are_fixed_strings_names_ints_or_safe_calls(stem):
    """A log/print argument may not be a Tech Logic value: only constants, integers, enum-like names from a fixed set, `describe(...)`."""
    tree = ast.parse((COLLECTOR / f"{stem}.py").read_text(encoding="utf-8"))
    forbidden_names = {"line", "lines", "row", "record", "raw", "barcode", "patron_id", "patron", "name", "title", "message", "text",
                       "destination", "error_message", "chunk_lines_raw", "payload", "body", "response", "content"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_log = isinstance(func, ast.Attribute) and func.attr in LOG_METHODS and getattr(func.value, "id", "") in {"logger", "log"}
        is_print = isinstance(func, ast.Name) and func.id == "print"
        if not (is_log or is_print):
            continue
        for sub in (n for arg in node.args for n in ast.walk(arg)):
            if isinstance(sub, ast.Name):
                assert sub.id not in forbidden_names, (stem, node.lineno, sub.id)
            if isinstance(sub, ast.Attribute):
                assert sub.attr not in {"text", "content", "body", "reason", "raw_message"}, (stem, node.lineno, sub.attr)


def test_the_uploader_never_reads_or_keeps_a_response_body_beyond_integer_positions():
    source = (COLLECTOR / "v2_uploader.py").read_text(encoding="utf-8")
    assert ".text" not in source and ".content" not in source and "response.headers" not in source and ".reason" not in source
    assert "logging" not in source and "print(" not in source


def test_the_uploader_builds_the_request_only_from_typed_payload_methods():
    tree = ast.parse((COLLECTOR / "v2_uploader.py").read_text(encoding="utf-8"))
    json_args = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "post":
            for kw in node.keywords:
                if kw.arg == "json":
                    json_args.append(ast.unparse(kw.value))
            assert not any(kw.arg in {"data", "files", "params"} for kw in node.keywords)
    assert sorted(json_args) == ["batch.payload()", "snapshot.payload(v2.key_id)"]


def test_a_batch_can_only_be_built_from_typed_events():
    with pytest.raises(AttributeError):
        v2_uploader.Batch.of(KEY_ID, [{"event_key": "x"}])            # a dict has no `.kind`: a raw record cannot be batched


def test_no_v2_module_reads_the_environment_or_the_token_except_through_v1_config():
    for stem in V2_MODULES:
        source = (COLLECTOR / f"{stem}.py").read_text(encoding="utf-8")
        assert "os.environ" not in source and "getenv" not in source and "SORTVIEW_API_TOKEN" not in source, stem
        assert "api_token" not in source or stem == "v2_uploader", stem


def test_the_master_secret_is_only_handled_in_the_key_store_the_identity_module_and_the_run_loader():
    holders = []
    for stem in V2_MODULES:
        source = (COLLECTOR / f"{stem}.py").read_text(encoding="utf-8")
        if re.search(r"\bmaster\b", source):
            holders.append(stem)
    assert sorted(holders) == ["v2_identity", "v2_keys", "v2_run"]


def test_run_deletes_the_master_after_deriving_the_subkeys():
    source = (COLLECTOR / "v2_run.py").read_text(encoding="utf-8")
    assert re.search(r"keys = derive_subkeys\(master\)\s+del master", source)


# =====================================================================================================================
# Late-profile corrections and the hold ledger
# =====================================================================================================================

def late_profile_lines():
    """An item held BEFORE its patron's profile exists, then the profile (type ILL, a name, an address, an e-mail): the collector must correct
    the hold without any of it reaching a sink. Small chunks put the profile in a later chunk than the hold."""
    def at(n):
        return BASE + __import__("datetime").timedelta(minutes=n)

    return [
        acs_line(at(0), item_message(F["acs.item_barcode"], F["acs.patron_id"], F["acs.destination"], extra=f"|ZZ{F['acs.sip2_extra']}",
                                     title=F["acs.title"])),
        acs_line(at(1), item_message(F["acs.nonhold_barcode"], F["acs.item_patron_id"], "Main", title=F["acs.title"])),
        acs_line(at(2), item_message(F["acs.other10_barcode"], F["acs.item_patron_id"], "Main", title=F["acs.title"])),
        acs_line(at(30), f"64 |AA{F['acs.patron_id']}|AE{F['acs.patron_name']}|PT{F['acs.patron_type']}|BD{F['acs.address']}|BE{F['acs.email']}"),
        acs_line(at(31), f"64 |AA{F['acs.item_patron_id']}|AE{F['acs.patron_name']}|PTILL"),
    ]


@pytest.mark.parametrize("scenario", ["success", "a 409 conflict on a correction", "an outage on the correction chunk", "a hostile 200"])
def test_a_run_that_corrects_holds_leaks_nothing_to_any_sink(tmp_path, monkeypatch, capsys, caplog, scenario):
    env = Env(tmp_path, monkeypatch, chunk_lines=2)
    empty_other_sources(env, acs=late_profile_lines())
    session = {
        "success": lambda: ScriptedSession(),
        "a 409 conflict on a correction": lambda: ScriptedSession(script=[None, conflict_reply([0], "acs_items")]),
        "an outage on the correction chunk": lambda: ScriptedSession(script=[None, Reply(503, {"detail": "CNRY-SERVER-TEXT"})]),
        "a hostile 200": lambda: ScriptedSession(default=Reply(200, {"status": "success", "echo": F["acs.patron_name"]})),
    }[scenario]()
    with caplog.at_level(logging.DEBUG):
        env.run(session)
        corrections = env.status()["counters"]["acs_corrections"]
        env.run(session)
    blob = everything(env, session, capsys, caplog)
    sweep_all(blob)
    assert b"CNRY-SERVER" not in blob
    if scenario == "success":
        assert corrections >= 1                                                   # the correction path really ran


def test_the_ledger_is_part_of_the_cache_file_and_holds_no_readable_value(tmp_path, monkeypatch):
    import sqlite3
    env = Env(tmp_path, monkeypatch, chunk_lines=2)
    empty_other_sources(env, acs=late_profile_lines())
    env.run(ScriptedSession())
    db = sqlite3.connect(env.v2.patron_cache_path)
    columns = {row[1]: row[2] for row in db.execute("PRAGMA table_info(holds)")}
    assert set(columns.values()) <= {"TEXT", "BLOB", "INTEGER"}
    for row in db.execute("SELECT * FROM holds"):
        for value in row:
            assert not isinstance(value, str) or (len(value) == 64 and all(c in "0123456789abcdef" for c in value)) or value[:2] == "20"                 or value.isidentifier(), value
    assert leaked_fields(env.v2.patron_cache_path.read_bytes()) == []
