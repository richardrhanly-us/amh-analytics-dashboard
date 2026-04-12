import json
from pathlib import Path

import streamlit as st


st.set_page_config(
    page_title="Admin Settings",
    page_icon="⚙️",
    layout="wide"
)

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "branch_settings.json"


DEFAULT_SETTINGS = {
    "library": {
        "library_name": "New Braunfels Public Library",
        "branch_name": "Main Branch",
        "system_name": "Tech Logic UltraSort"
    },
    "security": {
        "admin_enabled": True,
        "admin_password": ""
    },
    "transit": {
        "labels": {
            "main": "Main",
            "westside": "Westside",
            "library_express": "Library Express"
        }
    },
    "internal_routing": {
        "branch_services_names": [],
        "collection_services_names": [],
        "collection_services_da_patterns": [],
        "branch_services_da_patterns": []
    },
    "account_settings": {
        "organization_name": "",
        "contact_name": "",
        "contact_email": "",
        "plan_name": "",
        "notes": ""
    }
}


def deep_merge_defaults(base: dict, defaults: dict) -> dict:
    merged = dict(defaults)
    for key, value in base.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_defaults(value, merged[key])
        else:
            merged[key] = value
    return merged


def load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        save_settings(DEFAULT_SETTINGS)
        return DEFAULT_SETTINGS.copy()

    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        loaded = json.load(f)

    return deep_merge_defaults(loaded, DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def lines_to_list(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def mask_password(value: str) -> str:
    return "" if not value else "********"


if "admin_authenticated" not in st.session_state:
    st.session_state["admin_authenticated"] = False


settings = load_settings()

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


if admin_enabled and not st.session_state["admin_authenticated"]:
    st.info("Admin access required.")

    entered_password = st.text_input("Admin password", type="password")

    unlock_col1, unlock_col2 = st.columns([1, 6])

    with unlock_col1:
        if st.button("Unlock", type="primary", use_container_width=True):
            if stored_password == "":
                st.error("No admin password is set yet. Add one directly in branch_settings.json first.")
            elif entered_password == stored_password:
                st.session_state["admin_authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")

    st.stop()


top_col1, top_col2 = st.columns([1, 6])

with top_col1:
    if admin_enabled and st.button("Lock", use_container_width=True):
        st.session_state["admin_authenticated"] = False
        st.rerun()

with top_col2:
    st.success("Admin access granted." if admin_enabled else "Admin password protection is currently disabled.")


with st.form("admin_settings_form"):
    st.subheader("Library")

    lib_col1, lib_col2, lib_col3 = st.columns(3)

    with lib_col1:
        library_name = st.text_input(
            "Library name",
            value=library_settings.get("library_name", "")
        )

    with lib_col2:
        branch_name = st.text_input(
            "Branch name",
            value=library_settings.get("branch_name", "")
        )

    with lib_col3:
        system_name = st.text_input(
            "System name",
            value=library_settings.get("system_name", "")
        )

    st.divider()

    st.subheader("Security")

    sec_col1, sec_col2 = st.columns([1, 2])

    with sec_col1:
        admin_enabled_form = st.checkbox(
            "Require admin password",
            value=admin_enabled
        )

    with sec_col2:
        admin_password = st.text_input(
            "Admin password",
            value=stored_password,
            type="password",
            help="Leave blank only if you intentionally want no password set."
        )

    st.divider()

    st.subheader("Transit")

    transit_labels = transit_settings.get("labels", {})

    transit_col1, transit_col2, transit_col3 = st.columns(3)

    with transit_col1:
        transit_main = st.text_input(
            "Main label",
            value=transit_labels.get("main", "Main")
        )

    with transit_col2:
        transit_westside = st.text_input(
            "Westside label",
            value=transit_labels.get("westside", "Westside")
        )

    with transit_col3:
        transit_library_express = st.text_input(
            "Library Express label",
            value=transit_labels.get("library_express", "Library Express")
        )

    st.divider()

    st.subheader("Internal Routing")

    route_col1, route_col2 = st.columns(2)

    with route_col1:
        branch_services_names_text = st.text_area(
            "Branch Services Names (one per line)",
            value="\n".join(internal_routing.get("branch_services_names", [])),
            height=180
        )

        branch_services_da_patterns_text = st.text_area(
            "Branch Services DA Patterns (one per line)",
            value="\n".join(internal_routing.get("branch_services_da_patterns", [])),
            height=180
        )

    with route_col2:
        collection_services_names_text = st.text_area(
            "Collection Services Names (one per line)",
            value="\n".join(internal_routing.get("collection_services_names", [])),
            height=180
        )

        collection_services_da_patterns_text = st.text_area(
            "Collection Services DA Patterns (one per line)",
            value="\n".join(internal_routing.get("collection_services_da_patterns", [])),
            height=180
        )

    st.divider()

    st.subheader("Account Settings")

    acct_col1, acct_col2 = st.columns(2)

    with acct_col1:
        organization_name = st.text_input(
            "Organization name",
            value=account_settings.get("organization_name", "")
        )

        contact_name = st.text_input(
            "Contact name",
            value=account_settings.get("contact_name", "")
        )

        contact_email = st.text_input(
            "Contact email",
            value=account_settings.get("contact_email", "")
        )

    with acct_col2:
        plan_name = st.text_input(
            "Plan name",
            value=account_settings.get("plan_name", "")
        )

        notes = st.text_area(
            "Notes",
            value=account_settings.get("notes", ""),
            height=140
        )

    submitted = st.form_submit_button("Save Settings", type="primary")

    if submitted:
        updated_settings = {
            "library": {
                "library_name": library_name.strip(),
                "branch_name": branch_name.strip(),
                "system_name": system_name.strip()
            },
            "security": {
                "admin_enabled": bool(admin_enabled_form),
                "admin_password": admin_password
            },
            "transit": {
                "labels": {
                    "main": transit_main.strip(),
                    "westside": transit_westside.strip(),
                    "library_express": transit_library_express.strip()
                }
            },
            "internal_routing": {
                "branch_services_names": lines_to_list(branch_services_names_text),
                "collection_services_names": lines_to_list(collection_services_names_text),
                "collection_services_da_patterns": lines_to_list(collection_services_da_patterns_text),
                "branch_services_da_patterns": lines_to_list(branch_services_da_patterns_text)
            },
            "account_settings": {
                "organization_name": organization_name.strip(),
                "contact_name": contact_name.strip(),
                "contact_email": contact_email.strip(),
                "plan_name": plan_name.strip(),
                "notes": notes.strip()
            }
        }

        save_settings(updated_settings)
        st.success("Settings saved.")
        st.rerun()


with st.expander("Current JSON Preview", expanded=False):
    preview_settings = load_settings()
    if "security" in preview_settings:
        preview_settings["security"] = dict(preview_settings["security"])
        preview_settings["security"]["admin_password"] = mask_password(
            str(preview_settings["security"].get("admin_password", ""))
        )
    st.json(preview_settings)
