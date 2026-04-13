from pathlib import Path
import json

from services.tenant_service import get_effective_settings


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

def load_branch_settings(settings_file: Path) -> dict:
    with open(settings_file, "r", encoding="utf-8") as f:
        return json.load(f)


def load_app_settings_from_file(settings_file: Path) -> dict:
    branch_settings = load_branch_settings(settings_file)

    library_settings = branch_settings.get("library", {})
    transit_settings = branch_settings.get("transit", {})
    internal_routing = branch_settings.get("internal_routing", {})

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

    branch_services_names = {
        str(x).strip().upper()
        for x in internal_routing.get("branch_services_names", [])
    }

    collection_services_names = {
        str(x).strip().upper()
        for x in internal_routing.get("collection_services_names", [])
    }

    branch_services_da_patterns = [
        str(x).strip().upper()
        for x in internal_routing.get("branch_services_da_patterns", [])
    ]

    collection_services_da_patterns = [
        str(x).strip().upper()
        for x in internal_routing.get("collection_services_da_patterns", [])
    ]

    return {
        "source": "file",
        "branch_settings": branch_settings,
        "LIBRARY_SETTINGS": library_settings,
        "TRANSIT_SETTINGS": transit_settings,
        "INTERNAL_ROUTING": internal_routing,
        "LIBRARY_NAME": library_settings.get("library_name", "New Braunfels Public Library"),
        "BRANCH_NAME": library_settings.get("branch_name", "Main Branch"),
        "SYSTEM_NAME": library_settings.get("system_name", "Tech Logic UltraSort"),
        "TRANSIT_HOME_LABEL": transit_home_label,
        "TRANSIT_DESTINATIONS": transit_destinations,
        "ENABLED_TRANSIT_DESTINATIONS": enabled_transit_destinations,
        "TRANSIT_LABELS": transit_labels,
        "BRANCH_SERVICES_NAMES": branch_services_names,
        "COLLECTION_SERVICES_NAMES": collection_services_names,
        "BRANCH_SERVICES_DA_PATTERNS": branch_services_da_patterns,
        "COLLECTION_SERVICES_DA_PATTERNS": collection_services_da_patterns,
    }


def load_app_settings_from_db(org_slug: str, branch_slug: str | None = None) -> dict:
    effective = get_effective_settings(org_slug=org_slug, branch_slug=branch_slug)
    settings = effective.get("settings", {}) or {}

    transit_settings = settings.get("transit", {})
    internal_routing = settings.get("internal_routing", {})

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

    branch_services_names = {
        str(x).strip().upper()
        for x in internal_routing.get("branch_services_names", [])
    }

    collection_services_names = {
        str(x).strip().upper()
        for x in internal_routing.get("collection_services_names", [])
    }

    branch_services_da_patterns = [
        str(x).strip().upper()
        for x in internal_routing.get("branch_services_da_patterns", [])
    ]

    collection_services_da_patterns = [
        str(x).strip().upper()
        for x in internal_routing.get("collection_services_da_patterns", [])
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
        "INTERNAL_ROUTING": internal_routing,
        "LIBRARY_NAME": library_name,
        "BRANCH_NAME": branch_name,
        "SYSTEM_NAME": system_name,
        "TRANSIT_HOME_LABEL": transit_home_label,
        "TRANSIT_DESTINATIONS": transit_destinations,
        "ENABLED_TRANSIT_DESTINATIONS": enabled_transit_destinations,
        "TRANSIT_LABELS": transit_labels,
        "BRANCH_SERVICES_NAMES": branch_services_names,
        "COLLECTION_SERVICES_NAMES": collection_services_names,
        "BRANCH_SERVICES_DA_PATTERNS": branch_services_da_patterns,
        "COLLECTION_SERVICES_DA_PATTERNS": collection_services_da_patterns,
    }


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
        except Exception as e:
            fallback = load_app_settings_from_file(settings_file)
            fallback["source"] = "file_fallback"
            fallback["settings_error"] = f"{type(e).__name__}: {e}"
            return fallback

    return load_app_settings_from_file(settings_file)
