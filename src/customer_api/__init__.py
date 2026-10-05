"""Customer-facing HTTP API: the browser (React) routes served under /api.

Structurally separate from collector ingestion (root main.py and
services/ingest_v2_*), from the Streamlit pages, and from the Streamlit
adapters (services/streamlit_*_adapter).

Import identity. This package and everything in it is imported "flat", with
src/ as the import root -- `customer_api.router`, `services.auth_service`,
`database`, `tenant_db` -- the same identity Streamlit and the tests use.
Code here must never import `src.services.*` (or `src.` anything): that is a
second, separate module object for the same file, so a patch or a call made
through one name would silently miss the other. Root main.py adds src/ to
sys.path once before importing this package; this package must not import
from main.py.
"""
