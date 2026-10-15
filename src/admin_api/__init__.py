"""SortView administration HTTP API: the routes the separate React admin app calls, served under /api/admin.

Separate from the customer API (src/customer_api/), which grants a platform administrator nothing. Every route here
is registered through admin_api.router.create_admin_router, whose router-level dependencies require a signed-in
platform administrator on every request and, for a state-changing request, an Origin listed in
SORTVIEW_ADMIN_ALLOWED_ORIGINS. A route added later inherits both without naming them.

The session is the customer API's own: the same cookie, the same login and logout (/api/auth/*), the same
current-user lookup. Nothing here issues or reads a credential of its own.

Import identity: flat, with src/ as the import root, exactly as for customer_api (see its package docstring). Code
here must never import `src.` anything, or main.py.
"""
