"""enable RLS on operational event tables (phase 1)

Revision ID: 0acba192bf69
Revises: 67d06f4ccd24
Create Date: 2026-09-22 00:00:00.000000

Now revises 67d06f4ccd24 (secure the checkins_clean/rejects_clean sync
triggers) rather than e5a2c7b93d14 directly -- that migration was inserted
below this one after it was discovered, while writing this migration's own
tests, that sortview_app's already-deployed grants would make v1 POST
/upload fail outright once it became the runtime role, independent of RLS.
Both migrations are logically independent; the chain ordering here is
sequencing, not a dependency between their DDL.

Government-readiness hardening, PRE-GA. First Row Level Security tranche:
defense-in-depth against an application query that accidentally omits its
own tenant WHERE clause. This is explicitly NOT a boundary against a
compromised sortview_app credential -- sortview_app is the one shared
runtime role for all ordinary traffic, and any SQL running as that role
can set the same session context these policies check. See the RLS design
assessment for the full threat-model writeup; this migration implements
Category A (accidental-unscoped-query defense) only.

SCOPE -- exactly seven tables, the operational (customer_id, branch_id)
domain:
    checkins, rejects, acs_events
    checkin_events, reject_events, acs_item_events
    ingest_key_ids
pipeline_status is deliberately OUT OF SCOPE for this migration: its
platform-admin cross-tenant read (list_libraries_with_status) needs a
separate, still-undesigned bypass mechanism, and folding it in here would
either break that feature or require rushing that design. It gets its own
follow-up migration.

TENANT CONTEXT. Application code (src/data_loader.py::_read_table,
main.py::_authenticate_agent) sets two transaction-local session
variables before any scoped query, via
    SELECT set_config('app.operational_customer_id', :v, true)
    SELECT set_config('app.operational_branch_id', :v, true)
(bound parameters, is_local=true -- cleared automatically at COMMIT/
ROLLBACK, so nothing can leak across a pooled connection's reuse). Every
policy below reads those same two names.

NULLIF(..., '')::int, not a bare ::int cast. current_setting(name, true)
(missing_ok) already returns NULL for a GUC that was never set in this
session. But a custom GUC that WAS set earlier and is later reset/cleared
on a reused connection can observe as an empty string rather than NULL on
some PostgreSQL versions/paths, and ''::int raises a cast error rather
than evaluating to NULL -- which would make the policy raise instead of
silently denying. NULLIF(current_setting(...), '') collapses both the
"never set" and "reset to empty" cases to NULL before the cast, so a
missing OR emptied context always fails closed (the equality comparison
against NULL is never true), never errors.

WHY THE UPDATE ASYMMETRY. Confirmed directly against the application code
(grepped every INSERT/UPDATE statement in main.py and src/services/*.py):
checkins, rejects, acs_events, checkin_events, reject_events and
acs_item_events have ZERO UPDATE statements anywhere in the deployed
application -- every insert uses ON CONFLICT DO NOTHING (v1) or the v2
conflict-as-409 pattern, never DO UPDATE. Those six tables are genuinely
immutable from the application's perspective and get SELECT+INSERT
policies only. Under RLS, a command type with no defined policy is denied
by default once RLS is enabled on a table -- so leaving UPDATE (and
DELETE) undefined on these six is a real fail-closed backstop, not an
oversight: if UPDATE is ever mistakenly granted to sortview_app on one of
these tables later, RLS still blocks it with no policy change needed.
ingest_key_ids is the only one of the seven with real UPDATE statements
(src/services/ingest_v2_service.py: retire_ingest_key, and the v2
heartbeat write) and gets an UPDATE policy accordingly.

DELETE: no policy anywhere in this migration, on any table. sortview_app
has no DELETE grant on any of these seven tables (verified during the
runtime-role privilege audit), so RLS is never even reached for DELETE --
and the absence of a policy denies it a second, independent way once RLS
is enabled.

FORCE ROW LEVEL SECURITY is deliberately NOT used. sortview_app is a
non-owning role (owns no public objects), so plain ENABLE ROW LEVEL
SECURITY already binds it fully; FORCE only matters for the table OWNER
(neondb_owner), which must keep bypassing for migrations and ops.

DEPLOY ORDER (see the phase 1 implementation plan): the application code
that sets these two GUCs must already be deployed and live on BOTH the
Streamlit dashboard and the FastAPI/collector backend before this
migration is applied to production, or every scoped query on these seven
tables goes silently empty the moment this runs. As of this migration's
authorship, DigitalOcean's and Streamlit Cloud's DATABASE_URL have NOT
yet been cut over to sortview_app at all -- this migration must not be
applied to production until that cutover is confirmed complete and the
application-code deploy is confirmed live.
"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0acba192bf69'
down_revision: str | Sequence[str] | None = '67d06f4ccd24'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_TENANT_MATCH_SQL = (
    "customer_id = NULLIF(current_setting('app.operational_customer_id', true), '')::int "
    "AND branch_id = NULLIF(current_setting('app.operational_branch_id', true), '')::int"
)

# All seven tables get SELECT + INSERT. Only ingest_key_ids additionally
# gets UPDATE -- see the WHY THE UPDATE ASYMMETRY note above.
_ALL_TABLES = (
    "checkins", "rejects", "acs_events",
    "checkin_events", "reject_events", "acs_item_events",
    "ingest_key_ids",
)
_UPDATE_TABLES = ("ingest_key_ids",)


def upgrade() -> None:
    """Upgrade schema."""
    for table in _ALL_TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY")

        op.execute(f"""
            CREATE POLICY tenant_isolation_select ON public.{table}
                FOR SELECT
                USING ({_TENANT_MATCH_SQL})
        """)
        op.execute(f"""
            CREATE POLICY tenant_isolation_insert ON public.{table}
                FOR INSERT
                WITH CHECK ({_TENANT_MATCH_SQL})
        """)

    for table in _UPDATE_TABLES:
        op.execute(f"""
            CREATE POLICY tenant_isolation_update ON public.{table}
                FOR UPDATE
                USING ({_TENANT_MATCH_SQL})
                WITH CHECK ({_TENANT_MATCH_SQL})
        """)


def downgrade() -> None:
    """Downgrade schema. Drops every policy this migration created, then
    disables RLS on all seven tables. Reverses cleanly regardless of data
    present -- no data is read, moved or destroyed by either direction."""
    for table in _UPDATE_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_update ON public.{table}")

    for table in _ALL_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_insert ON public.{table}")
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation_select ON public.{table}")
        op.execute(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY")
