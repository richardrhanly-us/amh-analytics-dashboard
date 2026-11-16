"""UI2 v6: reading sorter sites' bin layouts (services.bin_layout_service), and the operator script's paths that write
nothing (scripts/set_sorter_bin_layout.py: review, dry runs and refusals). Both run their REAL SQL against SQLite;
the script's writes are PostgreSQL's own (jsonb_set) and are tested in tests/test_bin_layout_postgres.py.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from sqlalchemy import create_engine, text

from scripts import set_sorter_bin_layout as script
from services import bin_layout_service
from services.bin_layout import BinLayout

SEVEN_BINS = {"end": "0", "columns": [["6", "5"], ["4", "3"], ["2", "1"]]}
EFFICIENCY = {"labor_rate": "21.50"}

_SCHEMA = (
    "CREATE TABLE organizations (id INTEGER PRIMARY KEY, slug TEXT, name TEXT, status TEXT)",
    (
        "CREATE TABLE branches (id INTEGER PRIMARY KEY, organization_id INTEGER, slug TEXT, name TEXT, "
        "is_primary BOOLEAN, status TEXT)"
    ),
    (
        "CREATE TABLE collector_installations (id INTEGER PRIMARY KEY, organization_id INTEGER, branch_id INTEGER, "
        "name TEXT, status TEXT)"
    ),
    "CREATE TABLE branch_settings (id INTEGER PRIMARY KEY, branch_id INTEGER UNIQUE, settings_json TEXT)",
    "INSERT INTO organizations VALUES (1, 'acme', 'Acme Library', 'active'), (2, 'beta', 'Beta Library', 'active')",
    (
        "INSERT INTO branches VALUES (11, 1, 'main', 'Main Library', 1, 'active'), "
        "(12, 1, 'east', 'East Branch', 0, 'active'), (13, 1, 'closed', 'Closed', 0, 'inactive'), "
        "(21, 2, 'main', 'Beta Main', 1, 'active')"
    ),
    (
        "INSERT INTO collector_installations VALUES (101, 1, 11, 'Main AMH', 'active'), "
        "(102, 1, 12, 'East AMH', 'active'), (103, 1, 13, 'Closed AMH', 'active'), "
        "(201, 2, 21, 'Beta AMH', 'active'), (202, 2, 21, 'Old Beta PC', 'retired')"
    ),
)


def _settings(engine, branch_id, document) -> None:
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO branch_settings (branch_id, settings_json) VALUES (:b, :s)"),
                     {"b": branch_id, "s": document if isinstance(document, str) else json.dumps(document)})


@pytest.fixture
def db(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'sortview.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        for statement in _SCHEMA:
            conn.execute(text(statement))
    monkeypatch.setattr(bin_layout_service, "get_engine", lambda: engine)
    engine.url_text = url
    yield engine
    engine.dispose()


# =====================================================================================================================
# read_bin_layouts
# =====================================================================================================================

def test_a_sorter_with_a_stored_layout_has_it_beside_its_other_settings(db):
    _settings(db, 11, {"efficiency": EFFICIENCY, "bin_layout": SEVEN_BINS})

    assert bin_layout_service.read_bin_layouts("acme") == {
        "main": BinLayout(end="0", columns=(("6", "5"), ("4", "3"), ("2", "1"))),
    }


def test_a_sorter_with_no_layout_or_no_settings_has_none(db):
    _settings(db, 11, {"efficiency": EFFICIENCY})

    assert bin_layout_service.read_bin_layouts("acme") == {}


def test_a_layout_is_never_taken_from_another_organization_or_an_inactive_branch(db):
    _settings(db, 21, {"bin_layout": SEVEN_BINS})   # Beta's main branch: the same slug as Acme's
    _settings(db, 13, {"bin_layout": SEVEN_BINS})   # Acme's inactive branch

    assert bin_layout_service.read_bin_layouts("acme") == {}
    assert list(bin_layout_service.read_bin_layouts("beta")) == ["main"]


@pytest.mark.parametrize("stored", [
    {"end": "0", "columns": [["6", "6"]]},
    {"end": "0", "columns": []},
    {"end": "0"},
    {"end": "04", "columns": [["1", "2"]]},
    "not a layout",
    [["6", "5"]],
])
def test_an_invalid_layout_is_left_out_and_logged_without_its_value(db, caplog, stored):
    _settings(db, 11, {"bin_layout": stored, "efficiency": EFFICIENCY})
    _settings(db, 12, {"bin_layout": SEVEN_BINS})

    with caplog.at_level(logging.WARNING, logger="sortview.bin_layouts"):
        layouts = bin_layout_service.read_bin_layouts("acme")

    # The other sorter's layout is unaffected: one bad document fails nothing else.
    assert list(layouts) == ["east"]
    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert "org=acme sorter=main reason=" in record.getMessage()
    assert json.dumps(stored) not in record.getMessage()
    assert "not a layout" not in record.getMessage()
    assert '"6"' not in record.getMessage()


def test_the_read_selects_only_the_layout_key_of_the_branchs_own_document():
    sql = str(bin_layout_service._LAYOUTS_SQL)

    assert "settings_json -> 'bin_layout'" in sql
    assert "organization_settings" not in sql
    assert sql.count("settings_json") == 1


# =====================================================================================================================
# scripts/set_sorter_bin_layout.py: the paths that write nothing
# =====================================================================================================================

def _run(*argv) -> tuple[int, str]:
    out = io.StringIO()
    return script.main(list(argv), out=out), out.getvalue()


def _documents(db) -> list[tuple]:
    with db.connect() as conn:
        return conn.execute(text("SELECT branch_id, settings_json FROM branch_settings ORDER BY branch_id")).all()


def test_review_lists_each_sorter_site_with_its_installations_and_its_layout_and_changes_nothing(db):
    _settings(db, 11, {"efficiency": EFFICIENCY, "bin_layout": SEVEN_BINS})
    _settings(db, 12, {"bin_layout": {"end": "0", "columns": [["1", "1"]]}})
    before = _documents(db)

    code, output = _run("review", "--database-url", db.url_text)

    assert code == script.EXIT_OK
    assert "organization  slug='acme'  name='Acme Library'  status='active'" in output
    assert "sorter site  slug='main'  branch name='Main Library'" in output
    assert f"bin layout: {json.dumps(SEVEN_BINS)}" in output
    assert "installation: 'Main AMH' (active)" in output
    assert "bin layout: INVALID (duplicate_bin) -- ignored by the API" in output
    # A retired installation and an inactive branch are not sites; another organization's are listed under it.
    assert "Old Beta PC" not in output and "Closed" not in output
    assert "sorter site  slug='main'  branch name='Beta Main'" in output
    assert "bin layout: none" in output
    assert _documents(db) == before


def test_review_can_be_narrowed_to_one_organization(db):
    code, output = _run("review", "--org-slug", "beta", "--database-url", db.url_text)

    assert code == script.EXIT_OK
    assert "slug='beta'" in output and "slug='acme'" not in output
    assert _run("review", "--org-slug", "nobody", "--database-url", db.url_text)[1].strip() == (
        "No sorter sites found for org_slug='nobody'."
    )


def test_review_needs_a_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert _run("review")[0] == script.EXIT_REFUSED


def test_set_without_execute_validates_and_writes_nothing(db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db.url_text)
    _settings(db, 11, {"efficiency": EFFICIENCY})
    before = _documents(db)

    code, output = _run("set", "--org-slug", "acme", "--sorter-slug", "main", "--confirm-branch-name", "Main Library",
                        "--layout", json.dumps(SEVEN_BINS))

    assert code == script.EXIT_OK
    assert output.startswith(f"DRY RUN: would store this bin layout for org_slug='acme' sorter_slug='main': "
                             f"{json.dumps(SEVEN_BINS)}. Nothing was written.")
    assert _documents(db) == before


def test_clear_without_execute_writes_nothing(db, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db.url_text)
    _settings(db, 11, {"bin_layout": SEVEN_BINS})
    before = _documents(db)

    code, output = _run("clear", "--org-slug", "acme", "--sorter-slug", "main", "--confirm-branch-name", "Main Library")

    assert code == script.EXIT_OK
    assert output.startswith("DRY RUN: would remove the bin layout of org_slug='acme' sorter_slug='main'.")
    assert _documents(db) == before


@pytest.mark.parametrize(("layout", "reason"), [
    ('{"end": "0", "columns": [["6", "6"]]}', "duplicate_bin"),
    ('{"end": "0", "columns": [["6", "5"]], "extra": 1}', "wrong_fields"),
    ('{"end": 0, "columns": [["6", "5"]]}', "end_invalid"),
    ("not json", "not JSON"),
])
def test_an_invalid_layout_is_refused_even_with_execute_before_any_connection(layout, reason):
    code, output = _run("set", "--org-slug", "acme", "--sorter-slug", "main", "--confirm-branch-name", "Main Library",
                        "--layout", layout, "--execute", "postgresql://unused.invalid/never")

    assert code == script.EXIT_REFUSED
    assert output.strip() == f"REFUSED: --layout is not a valid bin layout ({reason}). Nothing was written."


def test_the_branch_name_must_be_confirmed_to_change_anything():
    with pytest.raises(SystemExit):
        script.main(["set", "--org-slug", "acme", "--sorter-slug", "main", "--layout", json.dumps(SEVEN_BINS)],
                    out=io.StringIO())


def test_the_script_changes_only_the_bin_layout_key_in_place():
    assert "jsonb_set(branch_settings.settings_json, '{bin_layout}'" in str(script._SET_SQL)
    assert "settings_json - 'bin_layout'" in str(script._CLEAR_SQL)
    assert "efficiency" not in str(script._SET_SQL) + str(script._CLEAR_SQL)


def test_a_layout_can_be_given_as_a_file_and_an_unreadable_file_is_refused(tmp_path):
    # Windows PowerShell 5.1 strips the double quotes inside a native program's argument: a file is the reliable way.
    given = tmp_path / "layout.json"
    given.write_text(json.dumps(SEVEN_BINS), encoding="utf-8-sig")
    common = ("set", "--org-slug", "acme", "--sorter-slug", "main", "--confirm-branch-name", "Main Library")

    code, output = _run(*common, "--layout-file", str(given))
    assert code == script.EXIT_OK
    assert json.dumps(SEVEN_BINS) in output

    code, output = _run(*common, "--layout-file", str(tmp_path / "missing.json"))
    assert (code, output.strip()) == (script.EXIT_REFUSED, "REFUSED: --layout-file could not be read. Nothing was written.")
    with pytest.raises(SystemExit):
        script.main([*common, "--layout", "{}", "--layout-file", str(given)], out=io.StringIO())
