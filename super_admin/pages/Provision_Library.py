from __future__ import annotations

import json
import os
import re
import sys

import streamlit as st

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from services.app_ui_service import apply_page_chrome
from services.platform_admin_service import is_platform_admin
from services.tenant_service import create_organization_with_primary_branch

st.set_page_config(
    page_title="Provision Library",
    page_icon="🏗️",
    layout="wide",
)

apply_page_chrome()

DEFAULT_ORG_SETTINGS = {
    "library_name": "",
    "system_name": "Tech Logic UltraSort",
    "security": {
        "admin_enabled": True,
        "admin_password": "",
    },
    "transit": {
        "home_branch_label": "Main",
        "destinations": [
            {"key": "westside", "label": "Westside", "enabled": True},
            {"key": "library_express", "label": "Library Express", "enabled": True},
        ],
    },
    "internal_routing": {
        "branch_services_names": [],
        "collection_services_names": [],
        "branch_services_da_patterns": [],
        "collection_services_da_patterns": [],
    },
    "account_settings": {
        "organization_name": "",
        "contact_name": "",
        "contact_email": "",
        "plan_name": "",
        "notes": "",
    },
}

DEFAULT_BRANCH_SETTINGS = {
    "branch_name": "Main Branch",
}


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value)
    return value.strip("-")


def lines_to_list(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


if "auth_user" not in st.session_state or st.session_state["auth_user"] is None:
    st.error("Please log in first.")
    st.stop()

auth_user = st.session_state["auth_user"]

if not is_platform_admin(auth_user["id"]):
    st.error("You do not have platform admin access.")
    st.stop()

st.title("Provision Library")
st.caption("Create a new library tenant and generate its agent config.")

if "provision_result" not in st.session_state:
    st.session_state["provision_result"] = None

with st.form("provision_library_form"):
    col1, col2 = st.columns(2)

    with col1:
        org_name = st.text_input("Organization name")
        branch_name = st.text_input("Primary branch name", value="Main Branch")
        plan_code = st.text_input("Plan code", value="trial")
        contact_name = st.text_input("Contact name")
        contact_email = st.text_input("Contact email")

    with col2:
        org_slug_input = st.text_input("Organization slug")
        branch_slug_input = st.text_input("Primary branch slug")
        library_name = st.text_input("Library display name")
        system_name = st.text_input("System name", value="Tech Logic UltraSort")
        notes = st.text_area("Notes", height=100)

    st.subheader("Transit")
    home_branch_label = st.text_input("Home branch label", value="Main")
    transit_1_label = st.text_input("Transit destination 1", value="Westside")
    transit_2_label = st.text_input("Transit destination 2", value="Library Express")

    st.subheader("Internal Routing")
    route_col1, route_col2 = st.columns(2)

    with route_col1:
        branch_services_names_text = st.text_area("Branch Services Names", height=140)
        branch_services_da_patterns_text = st.text_area("Branch Services DA Patterns", height=140)

    with route_col2:
        collection_services_names_text = st.text_area("Collection Services Names", height=140)
        collection_services_da_patterns_text = st.text_area("Collection Services DA Patterns", height=140)

    st.subheader("Agent Config")
    api_base_url = st.text_input(
        "API base URL",
        value="https://sortview-app-2p336.ondigitalocean.app",
    )
    raw_checkins_file = st.text_input("Raw checkins file", value=r"C:\TLCFinalDlls\Checkins.txt")
    raw_rejects_file = st.text_input("Raw rejects file", value=r"C:\TLCFinalDlls\Rejects.txt")
    raw_acs_file = st.text_input("Raw ACS file", value=r"C:\TLCFinalDlls\ACS Log.txt")

    submitted = st.form_submit_button("Provision Library", type="primary")

if submitted:
    org_slug = slugify(org_slug_input or org_name)
    branch_slug = slugify(branch_slug_input or branch_name)

    if not org_name.strip():
        st.error("Organization name is required.")
        st.stop()

    if not branch_name.strip():
        st.error("Primary branch name is required.")
        st.stop()

    org_settings = dict(DEFAULT_ORG_SETTINGS)
    org_settings.update({
        "library_name": library_name.strip() or org_name.strip(),
        "system_name": system_name.strip(),
        "transit": {
            "home_branch_label": home_branch_label.strip(),
            "destinations": [
                {"key": "dest_1", "label": transit_1_label.strip(), "enabled": True} if transit_1_label.strip() else None,
                {"key": "dest_2", "label": transit_2_label.strip(), "enabled": True} if transit_2_label.strip() else None,
            ],
        },
        "internal_routing": {
            "branch_services_names": lines_to_list(branch_services_names_text),
            "collection_services_names": lines_to_list(collection_services_names_text),
            "branch_services_da_patterns": lines_to_list(branch_services_da_patterns_text),
            "collection_services_da_patterns": lines_to_list(collection_services_da_patterns_text),
        },
        "account_settings": {
            "organization_name": org_name.strip(),
            "contact_name": contact_name.strip(),
            "contact_email": contact_email.strip(),
            "plan_name": plan_code.strip(),
            "notes": notes.strip(),
        },
    })
    org_settings["transit"]["destinations"] = [
        d for d in org_settings["transit"]["destinations"] if d is not None
    ]

    branch_settings = dict(DEFAULT_BRANCH_SETTINGS)
    branch_settings.update({
        "branch_name": branch_name.strip(),
    })

    try:
        result = create_organization_with_primary_branch(
            org_name=org_name.strip(),
            org_slug=org_slug,
            branch_name=branch_name.strip(),
            branch_slug=branch_slug,
            plan_code=plan_code.strip(),
            org_settings=org_settings,
            branch_settings=branch_settings,
        )
    except Exception as e:
        st.error(f"Provisioning failed: {type(e).__name__}: {e}")
        st.stop()

    organization = result["organization"]
    branch = result["branch"]

    agent_config = {
        "database_url": "",
        "customer_id": organization["id"],
        "branch_id": branch["id"],
        "raw_checkins_file": raw_checkins_file.strip(),
        "raw_rejects_file": raw_rejects_file.strip(),
        "processed_checkins_file": r"data\processed\checkins_clean.csv",
        "processed_rejects_file": r"data\processed\rejects_clean.csv",
        "checkins_history_file": r"data\processed\checkins_history.csv",
        "rejects_history_file": r"data\processed\rejects_history.csv",
        "status_file": r"data\processed\pipeline_status.json",
        "api_url": api_base_url.strip().rstrip("/"),
        "raw_acs_file": raw_acs_file.strip(),
        "processed_acs_file": r"data\processed\acs_clean.csv",
        "acs_history_file": r"data\processed\acs_history.csv",
    }

    st.session_state["provision_result"] = {
        "organization": organization,
        "branch": branch,
        "plan": result["plan"],
        "subscription": result["subscription"],
        "agent_config": agent_config,
    }

    st.success("Library provisioned.")

if st.session_state["provision_result"]:
    provision_result = st.session_state["provision_result"]

    st.subheader("Provision Result")
    st.json(provision_result)

    st.download_button(
        "Download agent_config.json",
        data=json.dumps(provision_result["agent_config"], indent=2),
        file_name="agent_config.json",
        mime="application/json",
    )
