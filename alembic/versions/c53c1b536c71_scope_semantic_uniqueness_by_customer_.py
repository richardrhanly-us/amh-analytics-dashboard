"""scope semantic uniqueness by customer and branch

Revision ID: c53c1b536c71
Revises: 45ba2e7befbc
Create Date: 2026-09-10 20:21:58.684566

Continuous Ingestion Phase E, multi-tenant correction. Fixes a real
pre-existing correctness bug found while auditing the existing semantic
deduplication ahead of building the new transport-idempotency layer.

THE BUG: checkins_unique_event, rejects_unique_event, and
acs_events_unique_idx (all three created in the 26397a3947b1 baseline)
are GLOBAL unique indexes -- (barcode, event_time),
(barcode, event_time, error_message), and
(event_time, message_code, barcode_key) respectively -- with NO tenant
column in any of them, despite all three tables carrying customer_id and
branch_id on every row. Two different libraries (different customer_id)
that happen to scan the same barcode at the same timestamp -- or two
branches of the same library doing the same -- are unrelated, legitimate
transactions today, but `ON CONFLICT ... DO NOTHING` against these
global indexes would silently drop the second one as a "duplicate" of
the first. This has been live in production since the 26397a3947b1
baseline; this migration does not change WHEN it started, only fixes it
going forward.

THE FIX: replace each global index with a composite one that adds
(customer_id, branch_id) as leading columns. (customer_id, branch_id) is
not a guess -- it is the SAME tenant/branch pair already used to scope
every read in this codebase (src/data_loader.py's _scoped_query, used by
every checkins/rejects/acs/pipeline_status loader) and the same pair
main.py's authenticate_agent() already binds every write to before any
INSERT runs (a request is rejected with 403 if its rows' customer_id/
branch_id don't match the authenticated token's scope). It is also
already the leading pair of the tenant-scoped PERFORMANCE index added in
0a315b52b59f. Three independent parts of this codebase already agree
(customer_id, branch_id) is the real isolation boundary; the semantic
unique indexes were simply never updated to match when this table
predates the multi-tenant model (see 26397a3947b1's own docstring: the
`customers` table is explicitly called out as a "legacy/operational"
table that predates `organizations`).

New index definitions:
    checkins_unique_event_scoped
        ON checkins (customer_id, branch_id, barcode, event_time)
    rejects_unique_event_scoped
        ON rejects (customer_id, branch_id, barcode, event_time, error_message)
    acs_events_unique_scoped
        ON acs_events (customer_id, branch_id, event_time, message_code, barcode_key)

SAFETY AGAINST EXISTING DATA -- a structural proof, not an empirical
check (no production data was inspected; none was needed): the new key
for each table is the OLD key PLUS additional leading columns -- a
strict superset. The OLD global unique index has been continuously
enforced in production, so no two existing rows can already share the
OLD key's columns. Any two rows that could possibly violate the NEW,
more specific key would necessarily also share the OLD key's columns
(a subset) -- which is already provably impossible. Therefore creating
the new composite index CANNOT fail with a duplicate-key error against
whatever data already exists, for any table where the old global index
was actually enforced -- this holds for all three tables here. This is
why no manual "check for duplicates first" step appears below; the
argument above is that step, done once, in general, rather than an
empirical query against data this environment cannot reach anyway (see
the Phase E PostgreSQL-verification report for why).

KNOWN CAVEAT -- acs_events only: unlike checkins/rejects (customer_id/
branch_id are NOT NULL there, enforced since the 26397a3947b1 baseline),
acs_events.customer_id/branch_id are nullable with no NOT NULL
constraint. Standard multi-column unique-index semantics treat any NULL
in an indexed column as making that row's group of columns "not equal"
to every other row's, including another all-NULL row -- so an
acs_events row with a NULL customer_id or branch_id (if any such row
exists) is now EXCLUDED from this constraint's protection entirely,
whereas it was previously covered by the old global index. main.py's
AcsRow Pydantic model has always required customer_id/branch_id as
non-optional ints, so no NEW row can ever be inserted through /upload
with either NULL -- but this migration does not verify whether any
PRE-EXISTING historical acs_events row already has a NULL value in
either column (this environment has no access to production or any
disposable copy of it -- see the Phase E report's PostgreSQL-verification
section). Treat "confirm zero NULL customer_id/branch_id rows in
acs_events before running this against production" as an explicit
pre-deployment gate, not something this migration itself enforces.

DOWNGRADE CAVEAT: downgrade() recreates the original global indexes.
That recreation will fail with a duplicate-key error if, while the
scoped indexes were live, two DIFFERENT tenants/branches legitimately
produced rows sharing the old global key -- e.g. two libraries checking
in the same barcode at the same instant. That is not a bug in this
migration; it is proof the condition this migration fixes is real, and
downgrading would silently reintroduce the data-loss risk by discarding
one of those legitimate rows. Downgrade is safe as an immediate
pre-cutover rollback (before any such row has been legitimately
inserted under the new scoped constraint); it is not an
always-safe operation once new data has accumulated under the fix.

Uses CREATE INDEX CONCURRENTLY / DROP INDEX CONCURRENTLY (autocommit_block,
same pattern as 0a315b52b59f and 45ba2e7befbc). The new indexes are
created BEFORE the old ones are dropped in upgrade() (and the reverse in
downgrade()), so there is no window where a table has zero relevant
semantic unique index.

checkins_clean / rejects_clean (trigger-synced copies) are unaffected --
verified again here for the same reason as 45ba2e7befbc: both sync
triggers INSERT with an explicit column list into tables whose own only
uniqueness is `UNIQUE (id)`, entirely independent of whatever index
exists on the parent table's (barcode, event_time, ...) columns.

No other migration or application code references the old index names
directly (grepped: `checkins_unique_event`, `rejects_unique_event`,
`acs_events_unique_idx` appear nowhere outside the baseline migration
that created them) -- main.py's /upload already uses a BARE
`ON CONFLICT DO NOTHING` (no named target), so it needs no code change
to pick up the new indexes automatically.

Migration-runtime note, same disclosure as 45ba2e7befbc/9a39e1b9ed07:
authored with no reachable Postgres instance in this environment (no
Docker, no local server, no Neon credentials configured) -- see the
Phase E report for exactly what was and was not verified against a real
database, and what is needed to close that gap.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c53c1b536c71'
down_revision: str | Sequence[str] | None = '45ba2e7befbc'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_INDEXES = [
    # (new_name, old_name, table, new_columns)
    (
        "checkins_unique_event_scoped",
        "checkins_unique_event",
        "checkins",
        "customer_id, branch_id, barcode, event_time",
    ),
    (
        "rejects_unique_event_scoped",
        "rejects_unique_event",
        "rejects",
        "customer_id, branch_id, barcode, event_time, error_message",
    ),
    (
        "acs_events_unique_scoped",
        "acs_events_unique_idx",
        "acs_events",
        "customer_id, branch_id, event_time, message_code, barcode_key",
    ),
]


def _drop_if_invalid(index_name: str) -> None:
    """Defensive retry-safety, found empirically while verifying this
    migration's downgrade against a real disposable Postgres instance: a
    CREATE INDEX CONCURRENTLY that fails partway (e.g. the DOWNGRADE
    CAVEAT scenario -- a duplicate-key violation while rebuilding the
    original global index) leaves an INVALID index object behind under
    that name, rather than cleaning up after itself (this is standard,
    documented PostgreSQL behavior for CONCURRENTLY, not specific to this
    migration). Without this check, a later retry's
    `CREATE ... IF NOT EXISTS <name>` would see the name already taken
    (even though it's unusable) and silently skip rebuilding it --
    leaving the table with NO valid semantic unique index at all once the
    paired DROP of the other index still runs. to_regclass() returns NULL
    (never raises) for a name that doesn't exist yet, so this is safe to
    call unconditionally before every CREATE below, whether or not a
    prior attempt ever actually failed.
    """
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"),
        {"name": index_name},
    ).fetchone()
    if row is not None and row[0] is False:
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}")


def upgrade() -> None:
    """Upgrade schema."""
    with op.get_context().autocommit_block():
        for new_name, _old_name, table, columns in _INDEXES:
            _drop_if_invalid(new_name)
            op.execute(
                f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {new_name} ON {table} ({columns})"
            )
        for _new_name, old_name, _table, _columns in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {old_name}")


def downgrade() -> None:
    """Downgrade schema.

    See the module docstring's DOWNGRADE CAVEAT -- recreating the
    original global index will fail if legitimate cross-tenant data now
    exists under the scoped constraint, and that failure is correct
    behavior, not a bug to work around. _drop_if_invalid makes a RETRY of
    this downgrade (after the offending data has been resolved) safe --
    see its docstring for why that matters specifically here, empirically
    confirmed against a real disposable Postgres instance during Phase E
    verification.
    """
    column_map = {
        "checkins_unique_event": "barcode, event_time",
        "rejects_unique_event": "barcode, event_time, error_message",
        "acs_events_unique_idx": "event_time, message_code, barcode_key",
    }
    with op.get_context().autocommit_block():
        for _new_name, old_name, table, _columns in _INDEXES:
            _drop_if_invalid(old_name)
            op.execute(
                f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {old_name} "
                f"ON {table} ({column_map[old_name]})"
            )
        for new_name, _old_name, _table, _columns in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {new_name}")
