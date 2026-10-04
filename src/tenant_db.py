#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         tenant_db.py
#
#  Description: Framework-independent tenant-context helpers for the
#               operational tables under row level security (migration
#               0acba192bf69). Sets the two transaction-local settings
#               the RLS policies read -- app.operational_customer_id and
#               app.operational_branch_id -- on the same connection the
#               caller then queries. No Streamlit, no pandas, no engine
#               creation: the caller supplies the engine or connection.
#
#               Strict by design: both ids are required, there is no
#               dialect guard (a database without set_config raises
#               rather than silently running unscoped), and database
#               errors propagate to the caller.
#
#***************************************************************

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

_CUSTOMER_CONTEXT_SQL = text("SELECT set_config('app.operational_customer_id', :v, true)")
_BRANCH_CONTEXT_SQL = text("SELECT set_config('app.operational_branch_id', :v, true)")


#***************************************************************
#
#  Function:     apply_tenant_context
#
#  Description: Sets the RLS tenant context on an open connection:
#               customer first, then branch, as bound string values
#               with is_local = true, so the settings last only until
#               the connection's current transaction ends and never
#               leak to the next user of a pooled connection.
#
#  Parameters:  conn - Open SQLAlchemy connection the caller will query.
#               customer_id - Operational customer id (0 is valid).
#               branch_id - Operational branch id (0 is valid).
#
#  Returns:     None
#
#  Raises:      ValueError - If either id is None.
#
#***************************************************************

def apply_tenant_context(conn: Connection, customer_id: int | str | None, branch_id: int | str | None) -> None:
    if customer_id is None or branch_id is None:
        raise ValueError("Tenant context requires both customer_id and branch_id.")

    conn.execute(_CUSTOMER_CONTEXT_SQL, {"v": str(customer_id)})
    conn.execute(_BRANCH_CONTEXT_SQL, {"v": str(branch_id)})


#***************************************************************
#
#  Function:     tenant_connection
#
#  Description: Opens a connection with the RLS tenant context already
#               applied. The ids are validated before any connection is
#               opened. Uses engine.connect(), not engine.begin(): the
#               first set_config autobegins the transaction the caller's
#               queries then run in, and leaving the block closes the
#               connection, which rolls that transaction back -- the
#               same lifecycle data_loader._read_table uses. A caller
#               that writes must commit explicitly inside the block.
#
#  Parameters:  engine - SQLAlchemy engine to connect with.
#               customer_id - Operational customer id (0 is valid).
#               branch_id - Operational branch id (0 is valid).
#
#  Yields:      Connection - The connection, tenant context applied.
#
#  Raises:      ValueError - If either id is None.
#
#***************************************************************

@contextmanager
def tenant_connection(
    engine: Engine, customer_id: int | str | None, branch_id: int | str | None
) -> Iterator[Connection]:
    if customer_id is None or branch_id is None:
        raise ValueError("Tenant context requires both customer_id and branch_id.")

    with engine.connect() as conn:
        apply_tenant_context(conn, customer_id, branch_id)
        yield conn
