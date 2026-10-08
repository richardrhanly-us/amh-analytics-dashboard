import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import streamlit as st

from services.admin_lock_service import without_security

# The v1 hold classifier's rules live in a module of their own, with no Streamlit, so the customer API can use them
# too. Re-exported here: everything that took them from this module still does.
from services.hold_rules import (  # noqa: F401
    INTERNAL_ROUTING_KEY,
    V1_HOLD_LISTS,
    V1HoldRules,
    v1_hold_rules,
)
from services.privacy_hardening import log_safe_exception
from services.tenant_service import get_effective_settings

logger = logging.getLogger("sortview.settings")

# What `settings_error` holds when the database settings could not be loaded and the file fallback
# was used instead. It is a stable code, never exception text: the settings dict is cached
# process-wide by st.cache_data for _SETTINGS_CACHE_TTL_SECONDS, and a driver's error message
# quotes SQL, bound values and the failing row. The exception itself is logged as a safe summary.
SETTINGS_ERROR_DATABASE_UNAVAILABLE = "database_unavailable"

# load_runtime_settings (via get_effective_settings: 4 sequential
# queries -- org, branch, subscription, entitlements) previously ran on
# every single Streamlit rerun -- every auto-refresh tick, every nav
# click -- to answer a question (what are this org/branch's effective
# settings right now) that only changes on an admin settings change.
# ttl=120 keeps it feeling live for anyone actively editing settings
# while eliminating repeat round trips otherwise. Cache key is
# (settings_file, org_slug, branch_slug, prefer_database), so this is
# naturally tenant- and branch-scoped.
_SETTINGS_CACHE_TTL_SECONDS = 120


def _dedupe_transit_destinations(destinations: list[dict]) -> list[dict]:
    seen = set()
    deduped = []

    for d in destinations:
        label = str(d.get("label", "")).strip()
        if not label:
            continue

        key = label.lower()
        if key in seen:
            continue

        seen.add(key)
        deduped.append(d)

    return deduped

def without_internal_routing(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    """A copy of `settings` without the internal routing block, for anything that is cached or handed to the
    dashboard as its settings."""
    return {key: value for key, value in (settings or {}).items() if key != INTERNAL_ROUTING_KEY}


def public_internal_routing_view(internal_routing: object) -> dict[str, str]:
    """What may be displayed of the block outside its own form: how many entries each list has -- never an entry."""
    block: Mapping[str, Any] = internal_routing if isinstance(internal_routing, Mapping) else {}
    view = {}
    for key in V1_HOLD_LISTS:
        values = block.get(key, [])
        count = sum(1 for value in values if str(value).strip()) if isinstance(values, list) else 0
        view[key] = f"{count} {'entry' if count == 1 else 'entries'} (not shown)"
    return view


def load_branch_settings(settings_file: Path) -> dict:
    with open(settings_file, "r", encoding="utf-8") as f:
        return json.load(f)


def load_app_settings_from_file(settings_file: Path) -> dict:
    # The `security` block (the Admin Settings lock) is never part of the dashboard's cached settings, and the
    # internal routing block is kept apart from them, in V1_HOLD_RULES.
    stored = without_security(load_branch_settings(settings_file))
    internal_routing = stored.get(INTERNAL_ROUTING_KEY, {})
    branch_settings = without_internal_routing(stored)

    library_settings = branch_settings.get("library", {})
    transit_settings = branch_settings.get("transit", {})

    transit_home_label = transit_settings.get("home_branch_label", "Main")
    transit_destinations = transit_settings.get("destinations", [])

    enabled_transit_destinations = _dedupe_transit_destinations([
        d for d in transit_destinations
        if bool(d.get("enabled", True)) and str(d.get("label", "")).strip()
    ])
    
    transit_labels = [
        str(d.get("label", "")).strip()
        for d in enabled_transit_destinations
    ]


    return {
        "source": "file",
        "branch_settings": branch_settings,
        "LIBRARY_SETTINGS": library_settings,
        "TRANSIT_SETTINGS": transit_settings,
        "LIBRARY_NAME": library_settings.get("library_name", "New Braunfels Public Library"),
        "BRANCH_NAME": library_settings.get("branch_name", "Main Branch"),
        "SYSTEM_NAME": library_settings.get("system_name", "Tech Logic UltraSort"),
        "TRANSIT_HOME_LABEL": transit_home_label,
        "TRANSIT_DESTINATIONS": transit_destinations,
        "ENABLED_TRANSIT_DESTINATIONS": enabled_transit_destinations,
        "TRANSIT_LABELS": transit_labels,
        # The v1 hold classifier's lists, and nowhere else in these settings (see V1HoldRules).
        "V1_HOLD_RULES": v1_hold_rules(internal_routing),
    }


def load_app_settings_from_db(org_slug: str, branch_slug: str | None = None) -> dict:
    effective = get_effective_settings(org_slug=org_slug, branch_slug=branch_slug)
    # This dict is cached process-wide (st.cache_data) and built for every user of the organization, so the
    # `security` block (the Admin Settings lock -- a hash, or a legacy plaintext password) is dropped from it.
    # Only the Admin Settings page reads that block, straight from the database, when it verifies an unlock.
    stored = without_security(effective.get("settings"))
    # The internal routing block is kept apart too: it reaches the v1 hold classifier as V1_HOLD_RULES, and is in
    # neither the settings document nor the tenant handed to the dashboard.
    internal_routing = stored.get(INTERNAL_ROUTING_KEY, {})
    settings = without_internal_routing(stored)
    effective = {**effective, "settings": settings}

    transit_settings = settings.get("transit", {})

    transit_home_label = transit_settings.get("home_branch_label", "Main")
    transit_destinations = transit_settings.get("destinations", [])

    enabled_transit_destinations = _dedupe_transit_destinations([
        d for d in transit_destinations
        if bool(d.get("enabled", True)) and str(d.get("label", "")).strip()
    ])
    
    transit_labels = [
        str(d.get("label", "")).strip()
        for d in enabled_transit_destinations
    ]


    library_name = settings.get("library_name", effective["organization"]["name"])
    branch_name = settings.get("branch_name", effective["branch"]["name"])
    system_name = settings.get("system_name", "Tech Logic UltraSort")

    return {
        "source": "database",
        "tenant": effective,
        "branch_settings": settings,
        "LIBRARY_SETTINGS": {
            "library_name": library_name,
            "branch_name": branch_name,
            "system_name": system_name,
        },
        "TRANSIT_SETTINGS": transit_settings,
        "LIBRARY_NAME": library_name,
        "BRANCH_NAME": branch_name,
        "SYSTEM_NAME": system_name,
        "TRANSIT_HOME_LABEL": transit_home_label,
        "TRANSIT_DESTINATIONS": transit_destinations,
        "ENABLED_TRANSIT_DESTINATIONS": enabled_transit_destinations,
        "TRANSIT_LABELS": transit_labels,
        # The v1 hold classifier's lists, and nowhere else in these settings (see V1HoldRules).
        "V1_HOLD_RULES": v1_hold_rules(internal_routing),
    }


@st.cache_data(ttl=_SETTINGS_CACHE_TTL_SECONDS, show_spinner=False)
def load_runtime_settings(
    settings_file: Path,
    org_slug: str | None = None,
    branch_slug: str | None = None,
    prefer_database: bool = True,
) -> dict:
    if prefer_database and org_slug:
        try:
            return load_app_settings_from_db(
                org_slug=org_slug,
                branch_slug=branch_slug,
            )
        except Exception as exc:
            log_safe_exception(logger, "Database settings load failed; using the settings file", exc)
            fallback = load_app_settings_from_file(settings_file)
            fallback["source"] = "file_fallback"
            fallback["settings_error"] = SETTINGS_ERROR_DATABASE_UNAVAILABLE
            return fallback

    return load_app_settings_from_file(settings_file)
