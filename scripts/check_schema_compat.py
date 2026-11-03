"""A deployment prerequisite: refuses to let code go live on a database that has not yet applied the code's migrations.

    SORTVIEW_SCHEMA_CHECK_DATABASE_URL=postgresql://... python scripts/check_schema_compat.py

Exit 0: compatible, deploy. Exit 1: refused, do not deploy. It prints one line: a fixed status code and revision ids.

WHY IT EXISTS. On 2026-11-02 the backend was deployed at a commit whose code read a column added by migration c1e7a94f2b60,
while production was still at f7b3d2c81a46. Every Contract v2 request failed with UndefinedColumn until the migration was
applied. Merging to `main` deploys (DigitalOcean autodeploy), and nothing compared the code with the database. This script
is that comparison, meant to run as a DigitalOcean PRE_DEPLOY job (docs/deployment.md, "schema compatibility guard").

WHAT IT COMPARES.
    the code's head      from Alembic's revision graph in alembic/versions. The migration files are loaded, as Alembic
                         always loads them, but nothing connects and env.py is never run. Exactly one head is required.
    the database's       the single row of alembic_version, read in a READ ONLY transaction with short timeouts.
    revision

    equal        the database is at the code's head                            PASS
    descendant   the database is past the head, on a revision this code knows  PASS
    ancestor     the database is behind: the code needs migrations not applied REFUSED
    divergent    the code knows the revision, but it is on another line        REFUSED
    unknown      this code's graph has no such revision                        REFUSED

With exactly one head, every revision the code knows is an ancestor of it, so `descendant` and `divergent` arise only in
a graph with several heads, which is refused before the database is read. In practice a database that is AHEAD of the
code (a schema-first migration from a newer commit, then a deploy or rollback of older code) is `unknown` and refused:
deploying older code over a newer schema is then a manual decision (docs/deployment.md, manual recovery).

WHAT A PASS DOES NOT PROVE. Revision ancestry says which migrations have run, not that the schema and the code work
together. A migration can break the code that was running before it (dropping or renaming a column that code reads), and a
database can differ from what its revision implies (manual changes). Backward compatibility is still a review rule.

HOW IT FAILS CLOSED. Anything other than a clean pass is a refusal: no URL, a non-PostgreSQL URL, a remote host without
TLS required, a failed connection, an unreadable or missing alembic_version, no row or several, a timeout, an unreadable
revision graph, several code heads. It reads only SORTVIEW_SCHEMA_CHECK_DATABASE_URL, never the application's DATABASE_URL,
so it runs as its own role (docs/deployment.md: SELECT on alembic_version and nothing else).

WHAT IT PRINTS. Fixed codes, revision ids from the code's graph, a database revision only if it looks like one, and a
SQLSTATE or an exception class name. Never the URL, a credential, a SQL parameter or exception text.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import TextIO

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, DBAPIError, OperationalError
from sqlalchemy.pool import NullPool

EXIT_OK = 0
EXIT_REFUSED = 1

URL_VARIABLE = "SORTVIEW_SCHEMA_CHECK_DATABASE_URL"
SCRIPT_LOCATION = Path(__file__).resolve().parent.parent / "alembic"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
TLS_MODES = {"require", "verify-ca", "verify-full"}

_PASSING = {"equal", "descendant"}
_REVISION_SHAPE = re.compile(r"[A-Za-z0-9_]{1,32}")

_MESSAGES = {
    "equal": "the database is at the code's head",
    "descendant": "the database is past the code's head on a known revision (ancestry only, not proof of compatibility)",
    "ancestor": "the database is behind the code: apply the reviewed migrations before deploying",
    "divergent": "the database is on a revision outside the code's line of migrations",
    "unknown": "the database is at a revision this code does not know",
    "database_url_required": f"{URL_VARIABLE} is not set",
    "database_url_invalid": f"{URL_VARIABLE} could not be read",
    "postgresql_required": "the check runs on PostgreSQL only",
    "tls_required": "a remote database URL must set sslmode to require, verify-ca or verify-full",
    "connection_failed": "could not connect to the database",
    "read_only_unavailable": "a READ ONLY transaction could not be established",
    "metadata_missing": "the database has no alembic_version table",
    "metadata_unreadable": "this role cannot read alembic_version",
    "no_database_revision": "alembic_version holds no revision",
    "multiple_database_revisions": "alembic_version holds more than one revision",
    "database_timeout": "the database did not answer within the check's timeouts",
    "database_error": "the database refused the check",
    "code_graph_unreadable": "the code's migration graph could not be read",
    "code_head_missing": "the code's migration graph has no head",
    "multiple_code_heads": "the code's migration graph has more than one head",
    "check_failed": "the check could not be completed",
}


class CheckRefused(Exception):
    """A fixed code from _MESSAGES and, optionally, a SQLSTATE or an exception class name. Never a value or exception text."""

    def __init__(self, code: str, **detail: str):
        super().__init__(code)
        self.code = code
        self.detail = detail


# --- the code's revision graph (no connection) ------------------------------------------------------------------------

Graph = dict[str, tuple[str, ...]]  # revision -> its down revisions


def load_code_graph(script_location: Path = SCRIPT_LOCATION) -> Graph:
    """Every revision in the migration scripts and its parents. Exact ids only: Alembic's own lookups also accept a prefix."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    try:
        config = Config()
        config.set_main_option("script_location", str(script_location))
        graph: Graph = {}
        for script in ScriptDirectory.from_config(config).walk_revisions():
            down = script.down_revision
            graph[script.revision] = () if down is None else ((down,) if isinstance(down, str) else tuple(down))
    except Exception:
        raise CheckRefused("code_graph_unreadable") from None
    if not graph:
        raise CheckRefused("code_head_missing")
    return graph


def code_heads(graph: Graph) -> list[str]:
    parents = {parent for downs in graph.values() for parent in downs}
    return sorted(revision for revision in graph if revision not in parents)


def ancestors(graph: Graph, revision: str) -> set[str]:
    """Every revision strictly below `revision`."""
    found: set[str] = set()
    pending = list(graph.get(revision, ()))
    while pending:
        parent = pending.pop()
        if parent not in found:
            found.add(parent)
            pending.extend(graph.get(parent, ()))
    return found


def relation(graph: Graph, head: str, database_revision: str) -> str:
    """Where the database's revision stands relative to the code's head: equal, ancestor, descendant, divergent, unknown."""
    if database_revision not in graph:
        return "unknown"
    if database_revision == head:
        return "equal"
    if database_revision in ancestors(graph, head):
        return "ancestor"
    if head in ancestors(graph, database_revision):
        return "descendant"
    return "divergent"


def single_head(graph: Graph) -> str:
    heads = code_heads(graph)
    if not heads:
        raise CheckRefused("code_head_missing")
    if len(heads) > 1:
        raise CheckRefused("multiple_code_heads")
    return heads[0]


# --- the database's revision (read only) ------------------------------------------------------------------------------

_READ_ONLY_SETTINGS = (
    "SET TRANSACTION READ ONLY",  # must come first
    "SET LOCAL statement_timeout = '5s'",
    "SET LOCAL lock_timeout = '2s'",  # never queue behind a migration holding alembic_version
    "SET LOCAL search_path = pg_catalog, public",
)
_REVISION_SQL = "SELECT version_num FROM public.alembic_version"


def open_read_only(conn) -> None:
    """Makes the connection's transaction read-only and confirms it, before anything is read."""
    for statement in _READ_ONLY_SETTINGS:
        conn.execute(text(statement))
    if conn.execute(text("SHOW transaction_read_only")).scalar() != "on":
        raise CheckRefused("read_only_unavailable")


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    state = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
    return state if isinstance(state, str) and re.fullmatch("[0-9A-Z]{5}", state) else None


_SQLSTATE_CODES = {"42501": "metadata_unreadable", "42P01": "metadata_missing", "57014": "database_timeout",
                   "55P03": "database_timeout"}


def read_database_revision(database_url: str) -> str:
    try:
        url = make_url(database_url)
    except ArgumentError:
        raise CheckRefused("database_url_invalid") from None
    if url.get_backend_name() != "postgresql":
        raise CheckRefused("postgresql_required")
    if (url.host or "") not in LOCAL_HOSTS and url.query.get("sslmode") not in TLS_MODES:
        raise CheckRefused("tls_required")

    engine = create_engine(url, hide_parameters=True, poolclass=NullPool,
                           connect_args={"connect_timeout": 10, "application_name": "sortview_schema_check"})
    try:
        try:
            conn = engine.connect()
        except OperationalError:
            raise CheckRefused("connection_failed") from None
        try:
            open_read_only(conn)
            rows = [row[0] for row in conn.execute(text(_REVISION_SQL)).all()]
        except DBAPIError as exc:
            state = _sqlstate(exc)
            raise CheckRefused(_SQLSTATE_CODES.get(state or "", "database_error"), sqlstate=state or "unknown") from None
        finally:
            conn.rollback()
            conn.close()
    finally:
        engine.dispose()
    if not rows:
        raise CheckRefused("no_database_revision")
    if len(rows) > 1:
        raise CheckRefused("multiple_database_revisions")
    return rows[0]


# --- the command ------------------------------------------------------------------------------------------------------

def _printable(revision: str) -> str:
    return revision if isinstance(revision, str) and _REVISION_SHAPE.fullmatch(revision) else "<not a revision id>"


def check(environ: dict[str, str] | os._Environ[str], *, script_location: Path = SCRIPT_LOCATION,
          reader=read_database_revision) -> tuple[int, str]:
    """(exit code, the one line to print). The graph is read before any connection, so a broken tree never connects."""
    head = None
    try:
        graph = load_code_graph(script_location)
        head = single_head(graph)
        database_url = environ.get(URL_VARIABLE)
        if not database_url:
            raise CheckRefused("database_url_required")
        database_revision = reader(database_url)
    except CheckRefused as refusal:
        detail = "".join(f" | {name} {value}" for name, value in sorted(refusal.detail.items()))
        return EXIT_REFUSED, (f"SCHEMA CHECK REFUSED: {refusal.code}: {_MESSAGES[refusal.code]}"
                              f" | code head {head or '-'}{detail}")
    except Exception as exc:  # fails closed, and exception text can hold a URL or a value: its class name only
        return EXIT_REFUSED, (f"SCHEMA CHECK REFUSED: check_failed: {_MESSAGES['check_failed']}"
                              f" | code head {head or '-'} | exception {type(exc).__name__}")

    found = relation(graph, head, database_revision)
    verdict = "PASSED" if found in _PASSING else "REFUSED"
    line = (f"SCHEMA CHECK {verdict}: {found}: {_MESSAGES[found]}"
            f" | code head {head} | database {_printable(database_revision)}")
    return (EXIT_OK if found in _PASSING else EXIT_REFUSED), line


def main(out: TextIO | None = None) -> int:
    code, line = check(os.environ)
    print(line, file=out or sys.stdout)
    return code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
