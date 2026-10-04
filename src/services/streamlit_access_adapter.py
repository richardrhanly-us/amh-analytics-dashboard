"""Streamlit-cached copies of access_service's two lookup functions.

access_service is the uncached, framework-neutral core. This module adds only
the dashboard's st.cache_data layer on top of it, for the Streamlit entry
scripts (app.py and the admin pages) that build their organization/branch
pickers on every rerun.

get_org_branches/get_user_memberships previously ran on every single
Streamlit rerun -- every auto-refresh tick, every nav click, every filter
change -- even though membership/branch data changes on the order of admin
actions, not seconds. ttl=120 keeps them feeling live for anyone actually
managing memberships/branches while eliminating repeat round trips for the
overwhelmingly common case of "nothing changed since the last rerun." Cache
keys are the function arguments themselves (user_id/org_slug), so this is
naturally tenant-scoped -- one user's cached memberships can never be
returned for another user_id, and one org's branches can never be returned
for another org_slug.

The security gates (access_service.user_can_access_org and
access_service.get_org_access_mode) are deliberately NOT wrapped here:
caching a gate would let a just-revoked user keep passing it for up to the
TTL. Callers import those from access_service directly.

Streamlit-specific by design: nothing outside a Streamlit script should
import this module, and access_service must never import it.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from services import access_service

_CHROME_CACHE_TTL_SECONDS = 120


@st.cache_data(ttl=_CHROME_CACHE_TTL_SECONDS, show_spinner=False)
def get_org_branches(org_slug: str) -> list[dict]:
    return access_service.get_org_branches(org_slug)


@st.cache_data(ttl=_CHROME_CACHE_TTL_SECONDS, show_spinner=False)
def get_user_memberships(user_id: int) -> list[dict[str, Any]]:
    return access_service.get_user_memberships(user_id)
