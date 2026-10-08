"""PostgreSQL's width_bucket(value, thresholds), for the report tests that run on SQLite.

The report engine (services.operational_report_service) counts rows by bucket with PostgreSQL's
`width_bucket(event_time, :boundaries)`, the boundaries bound as one array. SQLite has neither the function nor
arrays, so for the tests that run the engine on an in-memory SQLite database this module supplies both -- and
nothing in the service knows SQLite exists:

    an array    a list bound to a SQLite statement is passed as JSON, each time written exactly as SQLAlchemy writes
                a single bound DateTime for SQLite, so it compares with a stored time as that one would
    the function    width_bucket(value, thresholds) is the number of thresholds at or before `value`
                    (bisect_right), compared as SQLite compares the two texts -- PostgreSQL's definition

Installed for the whole test run by tests/conftest.py. The real function is tested against a real server in
tests/test_report_buckets_postgres.py, which is the authority on what the SQL does.
"""

from __future__ import annotations

import json
import sqlite3
from bisect import bisect_right
from datetime import datetime
from typing import Any

from sqlalchemy import event
from sqlalchemy.dialects import sqlite
from sqlalchemy.engine import Engine

_write_time = sqlite.DATETIME().bind_processor(sqlite.dialect())


def _as_json_array(values: list[Any]) -> str:
    return json.dumps([_write_time(value) if isinstance(value, datetime) else value for value in values])  # type: ignore[misc]


def width_bucket(value: Any, thresholds: str | None) -> int | None:
    if value is None or thresholds is None:
        return None
    return bisect_right(json.loads(thresholds), value)


def _register(dbapi_connection: Any, _record: Any) -> None:
    if isinstance(dbapi_connection, sqlite3.Connection):
        dbapi_connection.create_function("width_bucket", 2, width_bucket, deterministic=True)


def install() -> None:
    sqlite3.register_adapter(list, _as_json_array)
    if not event.contains(Engine, "connect", _register):
        event.listen(Engine, "connect", _register)
