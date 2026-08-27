"""Shared fakes for unit-testing service functions that call get_engine().

Service functions follow one of two shapes:
    with engine.connect() as conn: ...        (reads)
    with engine.begin() as conn: ...           (writes)
and call either `.execute(sql, params).first()` or
`.execute(sql, params).mappings().first()/.all()`.

FakeConn returns pre-programmed FakeQueryResult objects in call order so a
test can assert exactly what each query received without touching a real
database.
"""

from __future__ import annotations

from typing import Any


class FakeQueryResult:
    def __init__(self, first: Any = None, all_rows: list[Any] | None = None):
        self._first = first
        self._all = all_rows if all_rows is not None else ([first] if first is not None else [])

    def mappings(self):
        return self

    def first(self):
        return self._first

    def all(self):
        return self._all


class FakeConn:
    def __init__(self, results: list[FakeQueryResult], calls: list[dict] | None = None):
        # NOTE: shares the list object with the caller (no copy) so that
        # multiple connect()/begin() calls against the same FakeEngine keep
        # draining one continuous queue, matching services that open several
        # short-lived connections in sequence (e.g. build_entitlement_context).
        self._results = results
        self._calls = calls if calls is not None else []

    def execute(self, sql, params=None):
        self._calls.append({"sql": str(sql), "params": params})
        if not self._results:
            raise AssertionError("FakeConn received more execute() calls than results were queued")
        return self._results.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeEngine:
    """Queues query results for successive execute() calls, across either
    connect() or begin() context managers, and records every call made."""

    def __init__(self, results: list[FakeQueryResult]):
        self.results = list(results)
        self.calls: list[dict] = []

    def connect(self):
        return FakeConn(self.results, self.calls)

    def begin(self):
        return FakeConn(self.results, self.calls)
