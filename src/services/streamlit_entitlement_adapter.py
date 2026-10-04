"""Streamlit-cached entitlement lookups for the dashboard.

entitlement_service is the uncached, framework-neutral core. This module adds
only the dashboard's st.cache_data layer over its subscription and
plan-entitlement lookups, plus a build_entitlement_context that uses those
cached copies, for the Streamlit entry scripts (app.py and the admin pages)
that build an entitlement context on every rerun.

Subscription and plan-entitlement data only change on an admin action (rare)
and aren't security-sensitive to serve slightly stale, so they're cached here
(cache key is org_slug/plan_id, naturally tenant-scoped).

Role is deliberately NOT cached, here or anywhere: build_entitlement_context
below hands only the two cached lookups to
entitlement_service.build_entitlement_context_with, which always calls the
core's own uncached get_org_role_for_user. A role change therefore takes
effect on the very next rerun instead of remaining valid for up to this TTL,
and build_entitlement_context itself must never be cached.

Streamlit-specific by design: nothing outside a Streamlit script should
import this module, and entitlement_service must never import it.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from services import entitlement_service

_ENTITLEMENT_CACHE_TTL_SECONDS = 120


@st.cache_data(ttl=_ENTITLEMENT_CACHE_TTL_SECONDS, show_spinner=False)
def get_org_subscription(org_slug: str) -> dict[str, Any] | None:
    return entitlement_service.get_org_subscription(org_slug)


@st.cache_data(ttl=_ENTITLEMENT_CACHE_TTL_SECONDS, show_spinner=False)
def get_plan_entitlements(plan_id: int) -> dict[str, dict[str, Any]]:
    return entitlement_service.get_plan_entitlements(plan_id)


def build_entitlement_context(user_id: int, org_slug: str) -> dict[str, Any]:
    return entitlement_service.build_entitlement_context_with(
        user_id,
        org_slug,
        load_subscription=get_org_subscription,
        load_plan_entitlements=get_plan_entitlements,
    )
