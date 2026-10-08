"""An organization's plan features, for customer API tests whose database has no plan tables.

    grant(monkeypatch)                                  every feature the customer API reads, with no history limit
    grant(monkeypatch, transits=None)                   the same without transit routing (None: the feature is absent)
    grant(monkeypatch, history_days=feature(True, 30))  the same with a 30-day reporting window

It replaces services.entitlement_service.build_entitlement_context -- the one lookup every plan check in the customer
API goes through -- so what each feature MEANS is still the service's own reading of it. Nothing here names a plan.
"""

from __future__ import annotations

from typing import Any

from services import entitlement_service


def feature(enabled: bool = True, limit_value: Any = None) -> dict[str, Any]:
    return {"enabled": enabled, "limit_value": limit_value}


EVERY_FEATURE = {
    "transits": feature(),
    "history_days": feature(),
    "internal_workflow": feature(),
}


def grant(monkeypatch, **changes: dict[str, Any] | None) -> list[tuple[int, str]]:
    """Every organization's plan has EVERY_FEATURE, changed by `changes` (a None value removes that feature).
    Returns the (user id, organization slug) of each lookup, in order."""
    features = {**EVERY_FEATURE, **changes}
    entitlements = {key: value for key, value in features.items() if value is not None}
    asked: list[tuple[int, str]] = []

    def context(user_id: int, org_slug: str) -> dict[str, Any]:
        asked.append((user_id, org_slug))
        return {"role": None, "subscription": None, "entitlements": dict(entitlements)}

    monkeypatch.setattr(entitlement_service, "build_entitlement_context", context)
    return asked
