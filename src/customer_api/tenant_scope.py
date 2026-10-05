"""The tenant scope of a customer request that reads operational data.

Two pieces, used together by a route:

    tenant = require_resolved_tenant(...)            # a FastAPI dependency
    with open_customer_tenant_connection(tenant) as conn:
        ...                                          # one or more reads

require_resolved_tenant turns the authenticated user plus the organization
and branch slugs in the request path into the operational tenant that user
may read, or refuses. The operational ids come only from that resolution
(services/tenant_resolution_service.py): nothing in a request can supply
them.

open_customer_tenant_connection opens a connection carrying that tenant's row
level security context (tenant_db.tenant_connection) and then READS THE
CONTEXT BACK on the same connection before handing it over. Under RLS a
connection with no tenant context does not fail -- every protected table
simply returns no rows, which is indistinguishable from "this tenant has no
data". The read-back turns that silent failure into a loud one: a context
that is missing or does not match the resolved tenant is a server error,
never an empty answer.

The connection is a plain context manager, not a FastAPI `yield` dependency,
on purpose: its lifetime is then the `with` block in the route, and it is
closed -- the transaction rolled back and the transaction-local context gone
with it -- before the route builds its response.

This module reads nothing itself and holds no state. The engine is the flat
database.get_engine(), the same one the tenant resolver uses; this package
never touches root main.py's engine.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy import text
from sqlalchemy.engine import Connection

from customer_api.auth_dependencies import require_current_user
from customer_api.errors import CustomerApiError
from database import get_engine
from services.tenant_resolution_service import (
    ResolvedOperationalTenant,
    resolve_operational_tenant,
)
from tenant_db import tenant_connection

# The two transaction-local settings tenant_db sets and the RLS policies read.
# current_setting(name, true) returns NULL for a setting that was never set
# in the session and '' for one whose transaction-local value has ended.
_READ_TENANT_CONTEXT_SQL = text("""
    SELECT
        current_setting('app.operational_customer_id', true) AS customer_id,
        current_setting('app.operational_branch_id', true) AS branch_id
""")


class TenantContextError(RuntimeError):
    """The RLS context on a connection is missing or is not the resolved
    tenant's. An internal fault, never a client error: it is answered as the
    customer API's generic 500. Its message is fixed and carries no id."""

    def __init__(self) -> None:
        super().__init__("The tenant context on the connection does not match the resolved tenant.")


def _tenant_not_found() -> CustomerApiError:
    """The one answer for every organization/branch pair that does not
    resolve: an organization that does not exist, that the user is not a
    member of or that is cancelled; a branch that does not exist, belongs to
    another organization or is inactive; or either one not being mapped to an
    operational id. Identical in every case, so slugs cannot be probed."""
    return CustomerApiError(404, "tenant_not_found", "Organization or branch not found.")


def require_resolved_tenant(
    org_slug: str,
    branch_slug: str,
    user: Annotated[dict[str, Any], Depends(require_current_user)],
) -> ResolvedOperationalTenant:
    """The operational tenant behind the path's organization and branch, for
    the authenticated user. Reading is allowed for a "full" and for a
    "read_only" (suspended) organization alike; this makes no role or
    entitlement decision. A database failure propagates and is answered as a
    server error, never as "not found"."""
    tenant = resolve_operational_tenant(user["id"], org_slug, branch_slug)
    if tenant is None:
        raise _tenant_not_found()
    return tenant


def _verify_tenant_context(conn: Connection, tenant: ResolvedOperationalTenant) -> None:
    """Raises TenantContextError unless both settings on `conn` are exactly
    the resolved tenant's ids.

    PostgreSQL returns a setting as text, and tenant_db writes each id as
    str(id), so the comparison is text to text: the value read back must be
    identical to str() of the resolved id. Nothing is parsed or trimmed, so a
    missing (NULL), blank, padded, non-numeric or simply different value all
    fail the same way.
    """
    row = conn.execute(_READ_TENANT_CONTEXT_SQL).mappings().first()

    if (
        row is None
        or row["customer_id"] != str(tenant.operational_customer_id)
        or row["branch_id"] != str(tenant.operational_branch_id)
    ):
        raise TenantContextError()


@contextmanager
def open_customer_tenant_connection(tenant: ResolvedOperationalTenant) -> Iterator[Connection]:
    """A connection scoped to `tenant` by row level security, with that scope
    verified before it is yielded. Leaving the block closes the connection,
    which rolls back its transaction and ends the transaction-local context;
    that happens whether the block finishes, the verification fails or a
    query inside it raises."""
    with tenant_connection(get_engine(), tenant.operational_customer_id, tenant.operational_branch_id) as conn:
        _verify_tenant_context(conn, tenant)
        yield conn
