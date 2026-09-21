"""Dashboard-side privacy containment (security/privacy Step 2).

Under the v1 Collector contract the cloud classifier still reads `patron_id` and `raw_message`, so those still
reach the dashboard's memory (documented in src/data_loader.py). What this step guarantees:

  * the patron-level "Internal Workflow Debug" view is gone -- nothing renders a patron field, a raw message, a
    title or a barcode -- while the aggregate KPIs are unchanged;
  * ACS is loaded with an explicit column list (no SELECT *), limited to what the classifier still needs;
  * the classifier's supporting frames no longer carry patron columns out of the metrics layer.

Every value is a SYNTHETIC canary.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from streamlit.testing.v1 import AppTest

import data_loader as dl
import metrics

ROOT = Path(__file__).resolve().parent.parent
VIEWS = ROOT / "src" / "views"

P_ADULT, P_ILL, P_PROG, P_COLL = "CANARY-PATRON-A", "CANARY-PATRON-B", "CANARY-PATRON-C", "CANARY-PATRON-D"
NAME_PROG, NAME_COLL = "CANARY PROGRAMMING ACCOUNT", "CANARY CATALOGING ACCOUNT"
PATTERN_COLL = "DACANARY DEPARTMENT PATTERN"
CANARIES = ("CANARY-PATRON", "CANARY-NAME", "CANARY-ADDR", "CANARY-MAIL", "CANARY-TITLE", "CANARY-BARCODE",
            NAME_PROG, NAME_COLL, PATTERN_COLL, "CANARY-RAW")


def _patron(pid, name, patron_type):
    return {"datetime": "2026-01-05 09:00:00", "message_code": "64", "barcode": None, "patron_id": pid, "destination": None,
            "raw_message": f"64 |AA{pid}|AE{name}|PT{patron_type}|BDCANARY-ADDR-1 MAIN ST|BECANARY-MAIL@example.invalid"}


def _item(barcode, pid, destination, prefix="101YNY", extra=""):
    return {"datetime": "2026-01-05 10:00:00", "message_code": "10", "barcode": barcode, "patron_id": pid,
            "destination": destination,
            "raw_message": f"{prefix}|AB{barcode}|AJCANARY-TITLE {barcode}|AA{pid or ''}|CT{destination or ''}{extra}"}


def synthetic_acs_rows() -> list[dict]:
    """Five hold items -- one public, one ILL (patron type), one programming (service-account name), one collection
    services (service-account name), one collection services (delimited pattern) -- plus a non-hold check-in."""
    return [
        _patron(P_ADULT, "CANARY-NAME ONE", "ADULT"),
        _patron(P_ILL, "CANARY-NAME TWO", "ILL"),
        _patron(P_PROG, NAME_PROG, "STAFF"),
        _patron(P_COLL, NAME_COLL, "STAFF"),
        _item("CANARY-BARCODE-A", P_ADULT, "Main"),
        _item("CANARY-BARCODE-B", P_ILL, "Westside"),
        _item("CANARY-BARCODE-C", P_PROG, "Main"),
        _item("CANARY-BARCODE-D", P_COLL, "Main"),
        _item("CANARY-BARCODE-E", None, "Main", extra=f"|{PATTERN_COLL}|"),
        _item("CANARY-BARCODE-F", P_ADULT, "Main", prefix="101YNN"),  # not a hold: never counted
    ]


CLASSIFIER_CONFIG = {
    "transit_labels": ["Westside", "Library Express"],
    "branch_services_names": [NAME_PROG],
    "collection_services_names": [NAME_COLL],
    "branch_services_da_patterns": [],
    "collection_services_da_patterns": [PATTERN_COLL],
}
EXPECTED_COUNTS = {"holds_total": 1, "ill_total": 1, "programming_total": 1, "collection_services_total": 2}


def _frame(rows):
    frame = pd.DataFrame(rows)
    frame["datetime"] = pd.to_datetime(frame["datetime"])
    return frame


# --- 1. the patron-level debug view is gone; the aggregate KPIs are not --------------------------------------------

def _overview_script():
    from datetime import date

    import pandas as pd
    import streamlit as st

    import views.overview_view as overview

    kpis = []
    original = overview.render_kpi_card

    def spy(*args, **kwargs):
        kpis.append((args[0], args[1]))
        return original(*args, **kwargs)

    overview.render_kpi_card = spy
    try:
        acs = pd.DataFrame(st.session_state["acs_rows"])
        acs["datetime"] = pd.to_datetime(acs["datetime"])
        overview.render_overview(
            pd.DataFrame({"datetime": pd.to_datetime([])}),
            pd.DataFrame({"datetime": pd.to_datetime([]), "error_simple": []}),
            acs, date(2026, 1, 1), date(2026, 1, 31), "Jan 2026", ["Westside", "Library Express"], "Main",
            [st.session_state["programming_name"]], [st.session_state["collection_name"]], [],
            [st.session_state["collection_pattern"]], "Attention", "text", "#6b7280", {}, {},
            can_view_internal_workflow=st.session_state["internal_workflow"],
        )
    finally:
        overview.render_kpi_card = original
    st.session_state["kpis"] = kpis


def _run_overview(internal_workflow: bool = True) -> AppTest:
    at = AppTest.from_function(_overview_script, default_timeout=60)
    at.session_state["acs_rows"] = synthetic_acs_rows()
    at.session_state["programming_name"] = NAME_PROG
    at.session_state["collection_name"] = NAME_COLL
    at.session_state["collection_pattern"] = PATTERN_COLL
    at.session_state["internal_workflow"] = internal_workflow
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _everything_rendered(at: AppTest) -> str:
    """All text the page shows: element values, labels and bodies, dataframe contents, expander labels."""
    parts: list[str] = []

    def walk(block):
        for element in block:
            for attribute in ("value", "label", "body", "code"):
                value = getattr(element, attribute, None)
                if value is not None and not callable(value):
                    parts.append(value.to_csv() if isinstance(value, pd.DataFrame) else str(value))
            children = getattr(element, "children", None)
            if children:
                walk(children.values() if isinstance(children, dict) else children)

    walk(at.main)
    return "\n".join(parts)


def test_the_overview_renders_no_patron_field_raw_message_title_or_barcode():
    at = _run_overview()

    rendered = _everything_rendered(at)
    assert [c for c in CANARIES if c in rendered] == []
    assert len(rendered) > 1000  # the page really did render (this is not an empty-string pass)


def test_the_overview_has_no_debug_expander_and_no_dataframe_of_workflow_items():
    at = _run_overview()

    assert [e.label for e in at.expander if "debug" in e.label.lower()] == []
    for frame in at.dataframe:
        assert not {"patron_id", "patron_name", "patron_type", "raw_message", "title", "barcode"} & set(frame.value.columns)
    assert not [h for h in at.markdown if "Debug" in h.value]
    assert not [s for s in at.subheader if "Debug" in s.value]


def test_the_overview_still_shows_the_aggregate_internal_workflow_kpis():
    at = _run_overview()

    kpis = dict(at.session_state["kpis"])

    assert {name: kpis[name] for name in ("Holds", "ILL", "Branch Services", "Collection Services")} == {
        "Holds": "1", "ILL": "1", "Branch Services": "1", "Collection Services": "2"}
    assert len(kpis) > 4  # ...and every other overview card is still rendered too


def test_a_viewer_without_the_internal_workflow_entitlement_now_gets_the_page_not_a_crash():
    # Before: the debug expander sat outside the entitlement `if` and raised NameError for these users.
    at = _run_overview(internal_workflow=False)

    assert dict(at.session_state["kpis"]).get("Holds") is None  # the workflow cards are not shown to them
    assert [c for c in CANARIES if c in _everything_rendered(at)] == []


def _view_identifiers() -> dict[str, set[str]]:
    """Every string constant, name and attribute in the views package (comments are not code)."""
    found: dict[str, set[str]] = {}
    for path in sorted(VIEWS.glob("*.py")):
        names: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.add(node.value)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
        found[path.name] = names
    return found


@pytest.mark.parametrize("token", ["patron_id", "patron_name", "patron_type", "raw_message"])
def test_no_view_can_display_a_patron_field_or_a_raw_message(token):
    offenders = {name: sorted(n for n in names if token in n) for name, names in _view_identifiers().items()}

    assert {name: hits for name, hits in offenders.items() if hits} == {}


def test_no_view_has_a_debug_expander():
    offenders = []
    for path in sorted(VIEWS.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "expander"
                and node.args and isinstance(node.args[0], ast.Constant) and "debug" in str(node.args[0].value).lower()
            ):
                offenders.append(f"{path.name}:{node.lineno}")

    assert offenders == []


# --- 2. the classifier: same aggregates, no patron columns leaving the metrics layer -----------------------------------------

def test_the_classifier_still_produces_exactly_the_same_aggregate_counts():
    summary = metrics.build_acs_item_summary(_frame(synthetic_acs_rows()), **CLASSIFIER_CONFIG)

    assert {k: summary[k] for k in EXPECTED_COUNTS} == EXPECTED_COUNTS
    assert summary["ill_by_branch"] == {"Westside": 1, "Library Express": 0} and summary["ill_main"] == 0


def test_the_classifiers_supporting_frames_carry_no_patron_columns_or_raw_text():
    summary = metrics.build_acs_item_summary(_frame(synthetic_acs_rows()), **CLASSIFIER_CONFIG)

    for key in ("items_df", "holds_df", "ill_df", "programming_df", "collection_services_df"):
        frame = summary[key]
        assert set(frame.columns) <= set(metrics._SUMMARY_FRAME_COLUMNS), key
        assert not {"patron_id", "patron_name", "patron_type", "raw_message", "raw_upper", "title"} & set(frame.columns), key
    # the counts and the flags are still there
    assert len(summary["holds_df"]) == 1 and len(summary["ill_df"]) == 1
    assert {"is_hold", "is_ill", "is_programming", "is_collection_services"} <= set(summary["items_df"].columns)


def test_the_supporting_frames_do_not_contain_any_patron_or_message_canary():
    summary = metrics.build_acs_item_summary(_frame(synthetic_acs_rows()), **CLASSIFIER_CONFIG)
    text_of_frames = "\n".join(summary[k].to_csv() for k in
                               ("items_df", "holds_df", "ill_df", "programming_df", "collection_services_df"))

    for canary in ("CANARY-PATRON", "CANARY-NAME", "CANARY-ADDR", "CANARY-MAIL", "CANARY-TITLE", "CANARY-RAW", NAME_PROG, NAME_COLL):
        assert canary not in text_of_frames


def test_an_empty_acs_frame_still_summarises_to_zero():
    for empty in (None, pd.DataFrame()):
        summary = metrics.build_acs_item_summary(empty, **CLASSIFIER_CONFIG)

        assert {k: summary[k] for k in EXPECTED_COUNTS} == dict.fromkeys(EXPECTED_COUNTS, 0)


# --- 3. ACS is loaded with an explicit column list ---------------------------------------------------------------------------

def test_the_acs_column_list_is_explicit_minimal_and_documents_the_temporary_patron_dependency():
    assert dl.ACS_LOAD_COLUMNS == ("event_time", "message_code", "barcode", "destination", "patron_id", "raw_message")
    # The only patron-bearing columns still loaded, solely because the current cloud classifier reads them:
    assert dl.ACS_PATRON_COLUMNS_STILL_LOADED == ("patron_id", "raw_message")
    assert set(dl.ACS_PATRON_COLUMNS_STILL_LOADED) <= set(dl.ACS_LOAD_COLUMNS)
    # never loaded again: nothing reads them
    assert not {"id", "customer_id", "branch_id", "title", "barcode_key", "source_file", "source_event_id", "created_at"} & set(dl.ACS_LOAD_COLUMNS)
    # ...and the comment block that explains the dependency sits next to the definition
    source = (ROOT / "src" / "data_loader.py").read_text(encoding="utf-8")
    assert "TEMPORARY, under the v1 Collector contract" in source and "moves to the collector" in source


def test_every_column_the_classifier_reads_is_still_loaded():
    needed = {"event_time",      # -> datetime
              "message_code",    # "64" patron records
              "raw_message",     # 101/101YNY prefixes, |AE, |PT, ILL keywords, |DA patterns
              "patron_id",       # item -> patron join key
              "barcode",         # distinct items
              "destination"}     # ILL by branch
    assert needed <= set(dl.ACS_LOAD_COLUMNS)


@pytest.fixture
def captured_queries(monkeypatch):
    calls = []
    monkeypatch.setattr(dl, "_read_table", lambda query, params=None: calls.append((" ".join(query.split()), params)) or pd.DataFrame())
    return calls


def test_both_acs_loaders_select_explicit_columns_never_star(captured_queries):
    dl._load_acs_history_from_db(10, 1)
    dl._load_acs_live_from_db(10, 1)

    history, live = captured_queries
    columns = ", ".join(dl.ACS_LOAD_COLUMNS)
    assert history[0] == (
        f"SELECT {columns} FROM acs_events WHERE customer_id = :org_slug AND branch_id = :branch_slug ORDER BY event_time")
    assert live[0].startswith(f"SELECT {columns} FROM acs_events WHERE customer_id = :org_slug AND branch_id = :branch_slug AND event_time::date =")
    for query, params in captured_queries:
        assert "*" not in query
        assert params == {"org_slug": 10, "branch_slug": 1}  # the tenant scoping is untouched


def test_every_acs_events_query_in_the_data_loader_passes_an_explicit_column_list():
    tree = ast.parse((ROOT / "src" / "data_loader.py").read_text(encoding="utf-8"))
    acs_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_scoped_query"
        and any(k.arg == "table_name" and isinstance(k.value, ast.Constant) and k.value.value == "acs_events" for k in node.keywords)
    ]

    assert len(acs_calls) == 2
    for call in acs_calls:
        assert any(k.arg == "columns" and getattr(k.value, "id", "") == "ACS_LOAD_COLUMNS" for k in call.keywords)


def test_the_scoped_query_builder_keeps_select_star_by_default_and_validates_every_column():
    assert " ".join(dl._scoped_query("checkins", "customer_id", "branch_id").split()).startswith("SELECT * FROM checkins")
    for bad in (["event_time; DROP TABLE acs_events"], ["patron_id, raw_message"], [""], ["a b"]):
        with pytest.raises(ValueError):
            dl._scoped_query("acs_events", "customer_id", "branch_id", columns=bad)


def test_the_explicit_query_is_valid_sql_and_returns_only_those_columns_from_a_full_width_table():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE acs_events (id INTEGER PRIMARY KEY, customer_id INTEGER, branch_id INTEGER, event_time TEXT,"
            " message_code TEXT, barcode TEXT, barcode_key TEXT, title TEXT, patron_id TEXT, destination TEXT,"
            " raw_message TEXT, source_file TEXT, source_event_id TEXT, created_at TEXT)"))
        conn.execute(text(
            "INSERT INTO acs_events (customer_id, branch_id, event_time, message_code, barcode, barcode_key, title, patron_id,"
            " destination, raw_message, source_file) VALUES (10, 1, '2026-01-05 10:00:00', '10', 'CANARY-BARCODE-Z',"
            " 'CANARY-BARCODE-Z', 'CANARY-TITLE-Z', 'CANARY-PATRON-Z', 'Main', '101YNY|ABCANARY-BARCODE-Z', 'ACS Log.txt')"))

    query = dl._scoped_query("acs_events", "customer_id", "branch_id", live_only=False, columns=dl.ACS_LOAD_COLUMNS)
    frame = pd.read_sql(text(query), engine, params={"org_slug": 10, "branch_slug": 1})

    assert list(frame.columns) == list(dl.ACS_LOAD_COLUMNS)
    assert "CANARY-TITLE-Z" not in frame.to_csv() and "CANARY-BARCODE" in frame.to_csv()  # barcode kept, title not loaded


def test_rows_loaded_with_only_the_explicit_columns_classify_exactly_as_before(monkeypatch):
    """The whole loader -> normaliser -> classifier path on the reduced column set: the aggregates do not change."""
    full = pd.DataFrame(synthetic_acs_rows()).rename(columns={"datetime": "event_time"})
    monkeypatch.setattr(dl, "_read_table", lambda query, params=None: full[list(dl.ACS_LOAD_COLUMNS)].copy())

    loaded = dl._load_acs_history_from_db(10, 1)

    assert {"datetime", "title", "source_file"} <= set(loaded.columns)  # the normaliser still supplies what other code expects
    summary = metrics.build_acs_item_summary(loaded, **CLASSIFIER_CONFIG)
    assert {k: summary[k] for k in EXPECTED_COUNTS} == EXPECTED_COUNTS


# --- 4. a failed data load shows a generic message, never the database's own error text ------------------------------------

DB_CANARIES = {
    "table": "canary_secret_table_7001",
    "column": "canary_schema_column_7002",
    "value": "CANARY-BOUND-VALUE-7003",
    "host": "canary-db-host-7004.example.invalid",
    "password": "CANARY-DB-PASSWORD-7005",
    "row": "CANARY-FAILING-ROW-7006",
}
# Fragments of a raw database error that must never be on a customer's screen.
DB_TEXT_FRAGMENTS = ("SELECT", "[SQL", "no such table", "parameters", "postgresql://", "DATABASE_URL", "Neon", "psycopg",
                     "Traceback", "OperationalError", "DataError", "RuntimeError", "sqlstate")


def _data_load_failure_script():
    import pandas as pd
    import streamlit as st
    from sqlalchemy import create_engine
    from sqlalchemy.exc import DataError

    import data_loader as dl

    c = st.session_state["canaries"]
    case = st.session_state["case"]
    original_engine, original_read_sql = dl.get_engine, pd.read_sql
    try:
        if case == "engine":
            def broken_engine():
                raise RuntimeError(f"could not connect to postgresql://svc_user:{c['password']}@{c['host']}:5432/{c['table']}")

            dl.get_engine = broken_engine
        elif case == "query":
            engine = create_engine("sqlite://", hide_parameters=True)  # the production engine setting
            dl.get_engine = lambda: engine
        else:  # "driver": a database driver error whose own message quotes the row and the offending value
            class PgError(Exception):
                pgcode = "22007"

            def failing_read_sql(*_args, **_kwargs):
                orig = PgError(f'invalid input syntax for type timestamp: "{c["value"]}" DETAIL: Failing row contains '
                               f"({c['row']}) on host {c['host']}")
                raise DataError(f"INSERT INTO {c['table']} ({c['column']}) VALUES (%(v)s)", {"v": c["value"]}, orig)

            pd.read_sql = failing_read_sql
            dl.get_engine = lambda: object()
        frame = dl._read_table(f"SELECT {c['column']} FROM {c['table']} WHERE tenant = :tenant", {"tenant": c["value"]})
        st.session_state["frame_is_empty"] = bool(frame.empty)
    finally:
        dl.get_engine = original_engine
        pd.read_sql = original_read_sql


def _run_failed_load(case: str) -> AppTest:
    at = AppTest.from_function(_data_load_failure_script, default_timeout=60)
    at.session_state["canaries"] = DB_CANARIES
    at.session_state["case"] = case
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


@pytest.mark.parametrize("case", ["engine", "query", "driver"])
def test_a_failed_data_load_shows_only_a_generic_message_and_no_database_text(case):
    at = _run_failed_load(case)

    rendered = _everything_rendered(at)
    assert [name for name, canary in DB_CANARIES.items() if canary in rendered] == []
    assert [fragment for fragment in DB_TEXT_FRAGMENTS if fragment in rendered] == []
    # the failure itself is not suppressed: the user is told, and nothing was loaded
    assert [e.value for e in at.error] == [dl.DATA_LOAD_FAILED_MESSAGE]
    assert at.session_state["frame_is_empty"] is True


def test_control_the_same_failures_do_carry_the_canaries_in_their_own_text():
    # Proves the assertions above can fail: this is the text the dashboard used to put on screen.
    from sqlalchemy import create_engine, text

    with pytest.raises(Exception, match=DB_CANARIES["table"]) as excinfo, create_engine("sqlite://", hide_parameters=True).connect() as conn:
        conn.execute(text(f"SELECT {DB_CANARIES['column']} FROM {DB_CANARIES['table']}"))

    assert DB_CANARIES["column"] in str(excinfo.value) and "[SQL" in str(excinfo.value)


@pytest.mark.parametrize("case", ["engine", "query", "driver"])
def test_a_failed_data_load_is_logged_as_a_safe_summary_without_values_or_driver_text(case, monkeypatch, caplog):
    import pandas as pd
    from sqlalchemy.exc import DataError

    class PgError(Exception):
        pgcode = "22007"

    shown = []
    monkeypatch.setattr(dl, "_show_db_error_once", lambda key, message: shown.append((key, message)))
    if case == "engine":
        def broken_engine():
            raise RuntimeError(f"cannot reach postgresql://svc:{DB_CANARIES['password']}@{DB_CANARIES['host']}/db")

        monkeypatch.setattr(dl, "get_engine", broken_engine)
    else:
        monkeypatch.setattr(dl, "get_engine", lambda: object())

        def failing_read_sql(*_a, **_k):
            orig = PgError(f'invalid input syntax: "{DB_CANARIES["value"]}" Failing row contains ({DB_CANARIES["row"]}) '
                           f"host {DB_CANARIES['host']} password {DB_CANARIES['password']}")
            raise DataError("INSERT ...", {"v": DB_CANARIES["value"]}, orig)

        monkeypatch.setattr(pd, "read_sql", failing_read_sql)

    with caplog.at_level("DEBUG", logger="sortview.data_loader"):
        frame = dl._read_table("SELECT event_time FROM acs_events WHERE customer_id = :org_slug", {"org_slug": DB_CANARIES["value"]})

    assert frame.empty
    assert shown == [("engine_creation" if case == "engine" else "query_failed", dl.DATA_LOAD_FAILED_MESSAGE)]
    assert [name for name, canary in DB_CANARIES.items() if canary in caplog.text] == []   # no values, no driver/connection text
    assert "Traceback" not in caplog.text and all(record.exc_info is None for record in caplog.records)
    if case == "engine":
        assert "Database engine creation failed | error_type=builtins.RuntimeError" in caplog.text
    else:
        # still useful to support: which loader failed, which parameters (names only), the error class and its SQLSTATE
        assert "Database query failed | params=['org_slug'] | query=SELECT event_time FROM acs_events" in caplog.text
        assert "sqlalchemy.exc.DataError" in caplog.text and "sqlstate=22007" in caplog.text


def test_read_table_can_only_show_the_fixed_message():
    tree = ast.parse((ROOT / "src" / "data_loader.py").read_text(encoding="utf-8"))
    read_table = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_read_table")

    shown = [n for n in ast.walk(read_table) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_show_db_error_once"]
    assert len(shown) == 2
    for call in shown:
        assert isinstance(call.args[1], ast.Name) and call.args[1].id == "DATA_LOAD_FAILED_MESSAGE"  # no f-string, no exception
    assert not [n for n in ast.walk(read_table) if isinstance(n, ast.Attribute) and n.attr == "exception"]  # no logger.exception
