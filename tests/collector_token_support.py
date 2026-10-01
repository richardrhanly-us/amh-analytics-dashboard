"""Shared stand-in for the Collector's stored API token in tests.

The Collector's ONLY token source is <root>\\secrets\\api_token.dpapi (collector/api_token_store.py) -- there is no
environment variable to set. Real DPAPI and a real Administrators+SYSTEM-only ACL exist only on Windows and only for an
elevated process, so a test that just needs load_config() to succeed stubs the store's `load` here instead. The store
itself (real DPAPI, real ACLs, tenant binding) is covered by tests/test_collector_api_token_store.py, and the resolution
contract (what load_config does with each store outcome) by tests/test_collector_config.py.
"""

from __future__ import annotations

from collector import api_token_store


def store_api_token(monkeypatch, token: str = "test-token") -> list[tuple]:
    """A usable api_token.dpapi holding `token`, for any tenant. Returns the (path, customer_id, branch_id) of every load."""
    seen: list[tuple] = []

    def load(self, customer_id, branch_id):
        seen.append((self.path, customer_id, branch_id))
        return token

    monkeypatch.setattr(api_token_store.DpapiTokenStore, "load", load)
    return seen


def break_api_token(monkeypatch, code: str = "token_missing") -> None:
    """api_token.dpapi is absent (`token_missing`, the default) or present but unusable (any other fixed code)."""

    def load(self, customer_id, branch_id):
        raise api_token_store.ApiTokenStoreError(code)

    monkeypatch.setattr(api_token_store.DpapiTokenStore, "load", load)
