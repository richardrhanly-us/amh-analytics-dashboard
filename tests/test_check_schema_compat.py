"""scripts/check_schema_compat.py without a database: the revision graph, the five relations, and the fixed, sanitized output.

The PostgreSQL side (a real alembic_version, roles, read-only enforcement, timeouts) is in test_check_schema_compat_postgres.py.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from scripts import check_schema_compat as tool

HISTORICAL_PRODUCTION, M1 = "f7b3d2c81a46", "c1e7a94f2b60"  # the 2026-11-02 incident: production behind code that needed M1
SECRET = "s3cret-Pa55word"
URL = f"postgresql://checker:{SECRET}@db.example.invalid:5432/neondb?sslmode=require"

# a -> b -> c is the main line; a -> d is a second line, so this graph has two heads (c and d).
FORKED = {"a": (), "b": ("a",), "c": ("b",), "d": ("a",)}
LINEAR = {"a": (), "b": ("a",), "c": ("b",)}


def _versions(tmp_path: Path, revisions: dict[str, tuple[str, ...]]) -> Path:
    """A minimal Alembic script directory holding exactly these revisions."""
    (tmp_path / "versions").mkdir()
    for revision, downs in revisions.items():
        down = None if not downs else (downs[0] if len(downs) == 1 else downs)
        (tmp_path / "versions" / f"{revision}_m.py").write_text(
            f"revision = {revision!r}\ndown_revision = {down!r}\nbranch_labels = None\ndepends_on = None\n\n"
            "def upgrade():\n    pass\n\n\ndef downgrade():\n    pass\n"
        )
    return tmp_path


def _reader(revision):
    def read(database_url):
        assert database_url == URL
        if isinstance(revision, Exception):
            raise revision
        return revision
    return read


# --- the relations ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize(("head", "database", "expected"), [
    ("c", "c", "equal"),
    ("c", "b", "ancestor"),
    ("c", "a", "ancestor"),
    ("b", "c", "descendant"),
    ("c", "d", "divergent"),
    ("d", "b", "divergent"),
    ("c", "zzz", "unknown"),
    ("c", "", "unknown"),
])
def test_relation_classifies_every_case(head, database, expected):
    assert tool.relation(FORKED, head, database) == expected


def test_a_prefix_of_a_known_revision_is_unknown_not_matched():
    # Alembic's own lookups accept a unique prefix; a database row must match exactly.
    assert tool.relation({M1: (HISTORICAL_PRODUCTION,), HISTORICAL_PRODUCTION: ()}, M1, M1[:4]) == "unknown"


def test_merge_revisions_count_both_parents_as_ancestors():
    merged = {"a": (), "b": ("a",), "c": ("a",), "m": ("b", "c")}
    assert tool.code_heads(merged) == ["m"]
    assert tool.ancestors(merged, "m") == {"a", "b", "c"}
    assert tool.relation(merged, "m", "c") == "ancestor"


def test_heads():
    assert tool.code_heads(LINEAR) == ["c"]
    assert tool.code_heads(FORKED) == ["c", "d"]
    with pytest.raises(tool.CheckRefused) as refused:
        tool.single_head(FORKED)
    assert refused.value.code == "multiple_code_heads"


# --- the repository's own graph -------------------------------------------------------------------------------------------

def test_the_repository_graph_has_one_head_and_the_incident_state_is_behind_it():
    graph = tool.load_code_graph()
    head = tool.single_head(graph)
    assert HISTORICAL_PRODUCTION in tool.ancestors(graph, head)
    assert tool.relation(graph, head, head) == "equal"
    assert tool.relation(graph, head, HISTORICAL_PRODUCTION) == "ancestor"
    # Named explicitly, so this keeps describing the incident after later migrations move the head.
    assert tool.relation(graph, M1, HISTORICAL_PRODUCTION) == "ancestor"
    assert tool.relation(graph, M1, M1) == "equal"


def test_the_incident_state_is_refused_and_the_head_passes():
    head = tool.single_head(tool.load_code_graph())
    code, line = tool.check({tool.URL_VARIABLE: URL}, reader=_reader(HISTORICAL_PRODUCTION))
    assert code == tool.EXIT_REFUSED
    assert line.startswith("SCHEMA CHECK REFUSED: ancestor:") and f"database {HISTORICAL_PRODUCTION}" in line
    code, line = tool.check({tool.URL_VARIABLE: URL}, reader=_reader(head))
    assert code == tool.EXIT_OK
    assert line == (f"SCHEMA CHECK PASSED: equal: the database is at the code's head | code head {head} | database {head}")


# --- check(), on synthetic script directories ------------------------------------------------------------------------------

@pytest.mark.parametrize(("revisions", "database", "exit_code", "status"), [
    (LINEAR, "c", tool.EXIT_OK, "PASSED: equal"),
    (LINEAR, "b", tool.EXIT_REFUSED, "REFUSED: ancestor"),
    (LINEAR, "x9", tool.EXIT_REFUSED, "REFUSED: unknown"),
])
def test_check_decides_from_the_script_directory(tmp_path, revisions, database, exit_code, status):
    code, line = tool.check({tool.URL_VARIABLE: URL}, script_location=_versions(tmp_path, revisions),
                            reader=_reader(database))
    assert code == exit_code
    assert line.startswith(f"SCHEMA CHECK {status}:")


def test_descendant_passes_and_divergent_is_refused(monkeypatch):
    # Only reachable from a graph with several heads, which check() refuses first; the policy is tested directly.
    monkeypatch.setattr(tool, "single_head", lambda graph: "b")
    monkeypatch.setattr(tool, "load_code_graph", lambda location: FORKED)
    code, line = tool.check({tool.URL_VARIABLE: URL}, reader=_reader("c"))
    assert code == tool.EXIT_OK and line.startswith("SCHEMA CHECK PASSED: descendant:") and "not proof" in line
    code, line = tool.check({tool.URL_VARIABLE: URL}, reader=_reader("d"))
    assert code == tool.EXIT_REFUSED and line.startswith("SCHEMA CHECK REFUSED: divergent:")


def test_several_code_heads_are_refused_before_any_connection(tmp_path):
    def never(database_url):
        raise AssertionError("connected")

    code, line = tool.check({tool.URL_VARIABLE: URL}, script_location=_versions(tmp_path, FORKED), reader=never)
    assert code == tool.EXIT_REFUSED
    assert line == ("SCHEMA CHECK REFUSED: multiple_code_heads: the code's migration graph has more than one head"
                    " | code head -")


def test_an_unreadable_graph_is_refused_before_any_connection(tmp_path):
    def never(database_url):
        raise AssertionError("connected")

    location = _versions(tmp_path, LINEAR)
    (location / "versions" / "broken_m.py").write_text("revision = 'e'\ndown_revision = 'c'\nraise RuntimeError('x')\n")
    code, line = tool.check({tool.URL_VARIABLE: URL}, script_location=location, reader=never)
    assert (code, line.split(":")[1].strip()) == (tool.EXIT_REFUSED, "code_graph_unreadable")
    code, line = tool.check({tool.URL_VARIABLE: URL}, script_location=tmp_path / "missing", reader=never)
    assert (code, line.split(":")[1].strip()) == (tool.EXIT_REFUSED, "code_graph_unreadable")


# --- the URL, and what is printed -----------------------------------------------------------------------------------------

@pytest.mark.parametrize(("environ", "status"), [
    ({}, "database_url_required"),
    ({tool.URL_VARIABLE: ""}, "database_url_required"),
    ({"DATABASE_URL": URL}, "database_url_required"),  # the application's URL is never a substitute
    ({tool.URL_VARIABLE: "::not a url::"}, "database_url_invalid"),
    ({tool.URL_VARIABLE: f"mysql://checker:{SECRET}@db.example.invalid/neondb"}, "postgresql_required"),
    ({tool.URL_VARIABLE: f"postgresql://checker:{SECRET}@db.example.invalid/neondb"}, "tls_required"),
    ({tool.URL_VARIABLE: f"postgresql://checker:{SECRET}@db.example.invalid/neondb?sslmode=prefer"}, "tls_required"),
    ({tool.URL_VARIABLE: f"postgresql://checker:{SECRET}@db.example.invalid/neondb?sslmode=disable"}, "tls_required"),
])
def test_unusable_urls_are_refused_without_connecting(environ, status):
    code, line = tool.check(environ)
    assert code == tool.EXIT_REFUSED
    assert line.startswith(f"SCHEMA CHECK REFUSED: {status}:")
    assert SECRET not in line and "example.invalid" not in line


@pytest.mark.parametrize("raised", [RuntimeError(URL), ValueError(f"password={SECRET}"), KeyError(SECRET)])
def test_an_unexpected_error_prints_its_class_only(raised):
    code, line = tool.check({tool.URL_VARIABLE: URL}, reader=_reader(raised))
    assert code == tool.EXIT_REFUSED
    assert line.endswith(f"| exception {type(raised).__name__}")
    assert SECRET not in line and "example.invalid" not in line


def test_a_refusal_carries_only_its_fixed_detail():
    code, line = tool.check({tool.URL_VARIABLE: URL}, reader=_reader(tool.CheckRefused("metadata_unreadable", sqlstate="42501")))
    assert code == tool.EXIT_REFUSED
    assert line.startswith("SCHEMA CHECK REFUSED: metadata_unreadable: this role cannot read alembic_version")
    assert line.endswith("| sqlstate 42501")


@pytest.mark.parametrize("revision", ["c1e7a94f2b60' OR 1=1", f"postgresql://x:{SECRET}@h/d", "a" * 33, "line\nbreak"])
def test_a_database_revision_that_is_not_shaped_like_one_is_not_printed(revision):
    code, line = tool.check({tool.URL_VARIABLE: URL}, reader=_reader(revision))
    assert code == tool.EXIT_REFUSED
    assert line.endswith("| database <not a revision id>")
    assert SECRET not in line and "\n" not in line


def test_main_prints_one_line_and_returns_the_exit_code(monkeypatch):
    monkeypatch.delenv(tool.URL_VARIABLE, raising=False)
    out = io.StringIO()
    assert tool.main(out) == tool.EXIT_REFUSED
    assert out.getvalue().count("\n") == 1 and out.getvalue().startswith("SCHEMA CHECK REFUSED: database_url_required:")


def test_every_status_has_a_message():
    for status in ("equal", "descendant", "ancestor", "divergent", "unknown"):
        assert status in tool._MESSAGES
    assert tool._PASSING == {"equal", "descendant"}
