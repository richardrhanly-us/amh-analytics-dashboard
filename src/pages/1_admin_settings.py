from __future__ import annotations

from typing import Any

import streamlit as st
from sqlalchemy import text

from database import get_engine
from services.app_ui_service import apply_page_chrome
from services.access_service import get_user_memberships, get_org_branches
from services.entitlement_service import build_entitlement_context
from services.permission_service import can_manage_settings
from services.sidebar_service import render_main_sidebar
from services.tenant_service import get_effective_settings

st.set_page_config(
    page_title="Admin Settings",
    page_icon="⚙️",
    layout="wide",
)

apply_page_chrome()

DEFAULT_SETTINGS = {
    "library": {
        "library_name": "New Braunfels Public Library",
        "branch_name": "Main Branch",
        "system_name": "Tech Logic UltraSort",
    },
    "security": {
        "admin_enabled": True,
        "admin_password": "",
    },
    "transit": {
        "home_branch_label": "Main",
        "destinations": [
            {
                "key": "westside",
                "label": "Westside",
                "enabled": True,
            },
            {
                "key": "library_express",
                "label": "Library Express",
                "enabled": True,
            },
        ],
    },
    "internal_routing": {
        "branch_services_names": [],
        "collection_services_names": [],
        "collection_services_da_patterns": [],
        "branch_services_da_patterns": [],
    },
    "account_settings": {
        "organization_name": "",
        "contact_name": "",
        "contact_email": "",
        "plan_name": "",
        "notes": "",
    },
}


def deep_merge_defaults(base: dict, defaults: dict) -> dict:
    merged = dict(defaults)
    for key, value in base.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_defaults(value, merged[key])
        else:
            merged[key] = value
    return merged


def lines_to_list(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def mask_password(value: str) -> str:
    return "" if not value else "********"


def _get_settings_rows(org_slug: str, branch_slug: str) -> dict[str, Any]:
    sql_org = text("""
        SELECT
            o.id AS organization_id,
            o.name AS organization_name,
            os.id AS org_settings_row_id,
            os.settings_json AS org_settings_json
        FROM organizations o
        LEFT JOIN organization_settings os
          ON os.organization_id = o.id
        WHERE o.slug = :org_slug
        LIMIT 1
    """)

    sql_branch = text("""
        SELECT
            b.id AS branch_id,
            b.name AS branch_name,
            bs.id AS branch_settings_row_id,
            bs.settings_json AS branch_settings_json
        FROM branches b
        JOIN organizations o
          ON o.id = b.organization_id
        LEFT JOIN branch_settings bs
          ON bs.branch_id = b.id
        WHERE o.slug = :org_slug
          AND b.slug = :branch_slug
        LIMIT 1
    """)

    engine = get_engine()
    with engine.connect() as conn:
        org_row = conn.execute(sql_org, {"org_slug": org_slug}).mappings().first()
        if not org_row:
            raise RuntimeError(f"Organization not found: {org_slug}")

        branch_row = conn.execute(
            sql_branch,
            {
                "org_slug": org_slug,
                "branch_slug": branch_slug,
            },
        ).mappings().first()

        if not branch_row:
            raise RuntimeError(
                f"Branch not found for organization '{org_slug}': {branch_slug}"
            )

    return {
        "organization_id": org_row["organization_id"],
        "organization_name": org_row["organization_name"],
        "org_settings_row_id": org_row["org_settings_row_id"],
        "org_settings_json": dict(org_row["org_settings_json"] or {}),
        "branch_id": branch_row["branch_id"],
        "branch_name": branch_row["branch_name"],
        "branch_settings_row_id": branch_row["branch_settings_row_id"],
        "branch_settings_json": dict(branch_row["branch_settings_json"] or {}),
    }


def load_settings(org_slug: str, branch_slug: str) -> dict:
    effective = get_effective_settings(org_slug=org_slug, branch_slug=branch_slug)
    settings = effective.get("settings", {}) or {}

    normalized = {
        "library": {
            "library_name": settings.get("library_name", effective["organization"]["name"]),
            "branch_name": settings.get("branch_name", effective["branch"]["name"]),
            "system_name": settings.get("system_name", "Tech Logic UltraSort"),
        },
        "security": settings.get("security", {}),
        "transit": settings.get("transit", {}),
        "internal_routing": settings.get("internal_routing", {}),
        "account_settings": settings.get("account_settings", {}),
    }

    return deep_merge_defaults(normalized, DEFAULT_SETTINGS)


def save_settings(org_slug: str, branch_slug: str, settings: dict) -> None:
    row_state = _get_settings_rows(org_slug=org_slug, branch_slug=branch_slug)

    current_org_settings = dict(row_state["org_settings_json"] or {})
    current_branch_settings = dict(row_state["branch_settings_json"] or {})

    org_payload = dict(current_org_settings)
    org_payload.update({
        "library_name": settings["library"]["library_name"].strip(),
        "system_name": settings["library"]["system_name"].strip(),
        "security": {
            "admin_enabled": bool(settings["security"]["admin_enabled"]),
            "admin_password": settings["security"]["admin_password"],
        },
        "transit": {
            "home_branch_label": settings["transit"]["home_branch_label"].strip(),
            "destinations": list(settings["transit"]["destinations"]),
        },
        "internal_routing": {
            "branch_services_names": list(settings["internal_routing"]["branch_services_names"]),
            "collection_services_names": list(settings["internal_routing"]["collection_services_names"]),
            "collection_services_da_patterns": list(settings["internal_routing"]["collection_services_da_patterns"]),
            "branch_services_da_patterns": list(settings["internal_routing"]["branch_services_da_patterns"]),
        },
        "account_settings": {
            "organization_name": settings["account_settings"]["organization_name"].strip(),
            "contact_name": settings["account_settings"]["contact_name"].strip(),
            "contact_email": settings["account_settings"]["contact_email"].strip(),
            "plan_name": settings["account_settings"]["plan_name"].strip(),
            "notes": settings["account_settings"]["notes"].strip(),
        },
    })

    branch_payload = dict(current_branch_settings)
    branch_payload.update({
        "branch_name": settings["library"]["branch_name"].strip(),
    })

    sql_insert_org_settings = text("""
        INSERT INTO organization_settings (organization_id, settings_json)
        VALUES (:organization_id, CAST(:settings_json AS JSONB))
    """)

    sql_update_org_settings = text("""
        UPDATE organization_settings
        SET settings_json = CAST(:settings_json AS JSONB),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = :settings_row_id
    """)

    sql_insert_branch_settings = text("""
        INSERT INTO branch_settings (branch_id, settings_json)
        VALUES (:branch_id, CAST(:settings_json AS JSONB))
    """)

    sql_update_branch_settings = text("""
        UPDATE branch_settings
        SET settings_json = CAST(:settings_json AS JSONB),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = :settings_row_id
    """)

    import json

    engine = get_engine()
    with engine.begin() as conn:
        if row_state["org_settings_row_id"] is None:
            conn.execute(
                sql_insert_org_settings,
                {
                    "organization_id": row_state["organization_id"],
                    "settings_json": json.dumps(org_payload),
                },
            )
        else:
            conn.execute(
                sql_update_org_settings,
                {
                    "settings_row_id": row_state["org_settings_row_id"],
                    "settings_json": json.dumps(org_payload),
                },
            )

        if row_state["branch_settings_row_id"] is None:
            conn.execute(
                sql_insert_branch_settings,
                {
                    "branch_id": row_state["branch_id"],
                    "settings_json": json.dumps(branch_payload),
                },
            )
        else:
            conn.execute(
                sql_update_branch_settings,
                {
                    "settings_row_id": row_state["branch_settings_row_id"],
                    "settings_json": json.dumps(branch_payload),
                },
            )


if "auth_user" not in st.session_state or st.session_state["auth_user"] is None:
    st.error("Please log in from the main app first.")
    st.stop()

auth_user = st.session_state["auth_user"]
user_memberships = get_user_memberships(auth_user["id"])

if not user_memberships:
    st.error("Your account does not have access to any organizations.")
    st.stop()

allowed_org_slugs = [m["organization_slug"] for m in user_memberships]

if (
    "selected_org_slug" not in st.session_state
    or st.session_state["selected_org_slug"] not in allowed_org_slugs
):
    st.session_state["selected_org_slug"] = allowed_org_slugs[0]

selected_org_slug = st.session_state["selected_org_slug"]

org_options = {
    m["organization_name"]: m["organization_slug"]
    for m in user_memberships
}

branch_rows = get_org_branches(selected_org_slug)

if not branch_rows:
    st.error("No active branches were found for this organization.")
    st.stop()

allowed_branch_slugs = [b["branch_slug"] for b in branch_rows]

if (
    "selected_branch_slug" not in st.session_state
    or st.session_state["selected_branch_slug"] not in allowed_branch_slugs
):
    primary_branch = next((b for b in branch_rows if b["is_primary"]), None)
    st.session_state["selected_branch_slug"] = (
        primary_branch["branch_slug"] if primary_branch else allowed_branch_slugs[0]
    )

selected_branch_slug = st.session_state["selected_branch_slug"]

branch_options = {
    b["branch_name"]: b["branch_slug"]
    for b in branch_rows
}

entitlement_context = build_entitlement_context(
    user_id=auth_user["id"],
    org_slug=selected_org_slug,
)

show_admin_button = can_manage_settings(entitlement_context)

if not show_admin_button:
    st.error("You do not have permission to manage settings.")
    st.stop()

render_main_sidebar(
    auth_user=auth_user,
    entitlement_context=entitlement_context,
    org_options=org_options,
    selected_org_slug=selected_org_slug,
    branch_options=branch_options,
    selected_branch_slug=selected_branch_slug,
    show_admin_button=show_admin_button,
)

selected_org_slug = st.session_state["selected_org_slug"]
selected_branch_slug = st.session_state["selected_branch_slug"]

if "admin_authenticated" not in st.session_state:
    st.session_state["admin_authenticated"] = False

try:
    settings = load_settings(
        org_slug=selected_org_slug,
        branch_slug=selected_branch_slug,
    )
except Exception as e:
    st.error(f"Could not load settings from database: {type(e).__name__}: {e}")
    st.stop()

library_settings = settings.get("library", {})
security_settings = settings.get("security", {})
transit_settings = settings.get("transit", {})
internal_routing = settings.get("internal_routing", {})
account_settings = settings.get("account_settings", {})

admin_enabled = bool(security_settings.get("admin_enabled", True))
stored_password = str(security_settings.get("admin_password", ""))

st.caption("SortView Admin")
st.title("Admin Settings")
st.caption("Manage branch configuration, routing rules, transit labels, and future account settings.")

if admin_enabled and stored_password and not st.session_state["admin_authenticated"]:
    st.info("Admin access required.")

    entered_password = st.text_input("Admin password", type="password")

    unlock_col1, unlock_col2 = st.columns([1, 6])

    with unlock_col1:
        if st.button("Unlock", type="primary", width="stretch"):
            if entered_password == stored_password:
                st.session_state["admin_authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")

    st.stop()

top_col1, top_col2 = st.columns([1, 6])

with top_col1:
    if admin_enabled and st.button("Lock", width="stretch"):
        st.session_state["admin_authenticated"] = False
        st.rerun()

with top_col2:
    if admin_enabled and stored_password:
        st.success("Admin access granted.")
    elif admin_enabled and not stored_password:
        st.warning("Admin password protection is enabled, but no password is currently set.")
    else:
        st.success("Admin password protection is currently disabled.")

with st.form("admin_settings_form"):
    st.subheader("Library")

    lib_col1, lib_col2, lib_col3 = st.columns(3)

    with lib_col1:
        library_name = st.text_input(
            "Library name",
            value=library_settings.get("library_name", ""),
        )

    with lib_col2:
        branch_name = st.text_input(
            "Branch name",
            value=library_settings.get("branch_name", ""),
        )

    with lib_col3:
        system_name = st.text_input(
            "System name",
            value=library_settings.get("system_name", ""),
        )

    st.divider()

    st.subheader("Security")

    sec_col1, sec_col2 = st.columns([1, 2])

    with sec_col1:
        admin_enabled_form = st.checkbox(
            "Require admin password",
            value=admin_enabled,
        )

    with sec_col2:
        admin_password = st.text_input(
            "Admin password",
            value=stored_password,
            type="password",
            help="Leave blank only if you intentionally want no password set.",
        )

    st.divider()

    st.subheader("Transit")

    home_branch_label = st.text_input(
        "Home branch label",
        value=transit_settings.get("home_branch_label", "Main"),
        help="This is the label used for the main/home branch.",
    )

    existing_destinations = transit_settings.get("destinations", [])
    existing_count = len(existing_destinations) if len(existing_destinations) > 0 else 2

    destination_count = st.number_input(
        "Number of transit destinations",
        min_value=0,
        max_value=20,
        value=existing_count,
        step=1,
        help="How many non-home transit destinations this library system uses.",
    )

    transit_destinations_form = []

    for i in range(int(destination_count)):
        if i < len(existing_destinations):
            existing_destination = existing_destinations[i]
        else:
            existing_destination = {
                "key": f"branch_{i+1}",
                "label": f"Branch {i+1}",
                "enabled": True,
            }

        st.markdown(f"##### Transit Destination {i + 1}")
        dest_col1, dest_col2, dest_col3 = st.columns([2, 3, 1])

        with dest_col1:
            destination_key = st.text_input(
                f"Key {i + 1}",
                value=str(existing_destination.get("key", f"branch_{i+1}")),
                help="Lowercase key with underscores, like westside or north_branch.",
                key=f"transit_key_{i}",
            )

        with dest_col2:
            destination_label = st.text_input(
                f"Label {i + 1}",
                value=str(existing_destination.get("label", f"Branch {i+1}")),
                key=f"transit_label_{i}",
            )

        with dest_col3:
            destination_enabled = st.checkbox(
                f"Enabled {i + 1}",
                value=bool(existing_destination.get("enabled", True)),
                key=f"transit_enabled_{i}",
            )

        transit_destinations_form.append(
            {
                "key": destination_key.strip().lower().replace(" ", "_"),
                "label": destination_label.strip(),
                "enabled": bool(destination_enabled),
            }
        )

    st.divider()

    st.subheader("Internal Routing")
    route_col1, route_col2 = st.columns(2)

    with route_col1:
        branch_services_names_text = st.text_area(
            "Branch Services Names (one per line)",
            value="\n".join(internal_routing.get("branch_services_names", [])),
            height=180,
        )

        branch_services_da_patterns_text = st.text_area(
            "Branch Services DA Patterns (one per line)",
            value="\n".join(internal_routing.get("branch_services_da_patterns", [])),
            height=180,
        )

    with route_col2:
        collection_services_names_text = st.text_area(
            "Collection Services Names (one per line)",
            value="\n".join(internal_routing.get("collection_services_names", [])),
            height=180,
        )

        collection_services_da_patterns_text = st.text_area(
            "Collection Services DA Patterns (one per line)",
            value="\n".join(internal_routing.get("collection_services_da_patterns", [])),
            height=180,
        )

    st.divider()

    st.subheader("Account Settings")

    acct_col1, acct_col2 = st.columns(2)

    with acct_col1:
        organization_name = st.text_input(
            "Organization name",
            value=account_settings.get("organization_name", ""),
        )

        contact_name = st.text_input(
            "Contact name",
            value=account_settings.get("contact_name", ""),
        )

        contact_email = st.text_input(
            "Contact email",
            value=account_settings.get("contact_email", ""),
        )

    with acct_col2:
        plan_name = st.text_input(
            "Plan name",
            value=account_settings.get("plan_name", ""),
        )

        notes = st.text_area(
            "Notes",
            value=account_settings.get("notes", ""),
            height=140,
        )

    submitted = st.form_submit_button("Save Settings", type="primary")

    if submitted:
        updated_settings = {
            "library": {
                "library_name": library_name.strip(),
                "branch_name": branch_name.strip(),
                "system_name": system_name.strip(),
            },
            "security": {
                "admin_enabled": bool(admin_enabled_form),
                "admin_password": admin_password,
            },
            "transit": {
                "home_branch_label": home_branch_label.strip(),
                "destinations": [
                    d for d in transit_destinations_form
                    if d["key"] and d["label"]
                ],
            },
            "internal_routing": {
                "branch_services_names": lines_to_list(branch_services_names_text),
                "collection_services_names": lines_to_list(collection_services_names_text),
                "collection_services_da_patterns": lines_to_list(collection_services_da_patterns_text),
                "branch_services_da_patterns": lines_to_list(branch_services_da_patterns_text),
            },
            "account_settings": {
                "organization_name": organization_name.strip(),
                "contact_name": contact_name.strip(),
                "contact_email": contact_email.strip(),
                "plan_name": plan_name.strip(),
                "notes": notes.strip(),
            },
        }

        try:
            save_settings(
                org_slug=selected_org_slug,
                branch_slug=selected_branch_slug,
                settings=updated_settings,
            )
            st.success("Settings saved to database.")
            st.rerun()
        except Exception as e:
            st.error(f"Could not save settings to database: {type(e).__name__}: {e}")

with st.expander("Current DB Preview", expanded=False):
    try:
        preview_settings = load_settings(
            org_slug=selected_org_slug,
            branch_slug=selected_branch_slug,
        )
        if "security" in preview_settings:
            preview_settings["security"] = dict(preview_settings["security"])
            preview_settings["security"]["admin_password"] = mask_password(
                str(preview_settings["security"].get("admin_password", ""))
            )
        st.json(preview_settings)
    except Exception as e:
        st.error(f"Could not load preview from database: {type(e).__name__}: {e}")
