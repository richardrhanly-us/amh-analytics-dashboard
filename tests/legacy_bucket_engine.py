"""The report engine's counting as it was before Reports R9D1 -- a TEST-ONLY reference.

Before R9D1, services.operational_report_service counted with one COUNT(*) FILTER column per bucket:

    SELECT [group,] COUNT(*) FILTER (WHERE event_time >= :boundary_0 AND event_time < :boundary_1), ...

This module keeps that statement, unchanged, and an executor with the shape the service's callers now take (a
list of rows: one row of counts for a plain count, or one row per stored value, that value first). Swapped in for
the service's own _execute_bucket_count, it lets a test ask the same question of the same rows both ways and
require the same answer. Nothing in the application imports it. It goes when the equivalence it proves no longer
needs proving.

The old statement could not count more than PostgreSQL's 1,664 columns; tests/test_report_buckets_postgres.py
shows that limit with it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.sql.elements import TextClause

from services.operational_report_service import _SOURCES, _Source


def legacy_bucket_count_statement(source: _Source, buckets: int) -> TextClause:
    if source not in _SOURCES:
        raise ValueError("a count statement can only be built for one of this module's own sources")
    if buckets < 1:
        raise ValueError("a count statement needs at least one bucket")

    columns = ",\n".join(
        f"        COUNT(*) FILTER (WHERE event_time >= :boundary_{index} AND event_time < :boundary_{index + 1})"
        for index in range(buckets)
    )
    grouped = source.group_column is not None
    sql = (
        "    SELECT\n"  # nosec B608 - the service's constants and a bucket count only
        + (f"        {source.group_column},\n" if grouped else "")
        + columns
        + f"""
    FROM {source.table}
    WHERE customer_id = :customer_id
      AND branch_id = :branch_id
      AND event_time >= :span_start
      AND event_time < :span_end
"""
        + (f"    GROUP BY {source.group_column}\n" if grouped else "")
    )
    time_type = DateTime(timezone=source.aware)
    return text(sql).bindparams(
        bindparam("span_start", type_=time_type),
        bindparam("span_end", type_=time_type),
        *(bindparam(f"boundary_{index}", type_=time_type) for index in range(buckets + 1)),
    )


def legacy_execute_bucket_count(conn, tenant, source: _Source, span: tuple[datetime, datetime], boundaries: Sequence[datetime]) -> list[tuple]:
    result = conn.execute(
        legacy_bucket_count_statement(source, len(boundaries) - 1),
        {
            "customer_id": tenant.operational_customer_id,
            "branch_id": tenant.operational_branch_id,
            "span_start": span[0],
            "span_end": span[1],
            **{f"boundary_{index}": boundary for index, boundary in enumerate(boundaries)},
        },
    )
    return [tuple(row) for row in result]
