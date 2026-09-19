from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

import streamlit as st

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
SUPER_ADMIN_DIR = os.path.join(ROOT_DIR, "super_admin")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

if SUPER_ADMIN_DIR not in sys.path:
    sys.path.insert(0, SUPER_ADMIN_DIR)

from super_auth import require_super_admin

from services.tenant_service import (
    assign_operational_identity,
    build_collector_agent_config,
    create_collector_installation,
    create_organization_with_primary_branch,
)

st.set_page_config(
    page_title="Provision Library",
    page_icon="🏗️",
    layout="wide",
)

auth_user = require_super_admin()

DEFAULT_ORG_SETTINGS = {
    "library_name": "",
    "system_name": "Tech Logic UltraSort",
    "security": {
        "admin_enabled": True,
        # Empty default in a settings template, not a real credential.
        "admin_password": "",  # nosec B105
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

def run_operational_stage(
    organization_id: int, branch_id: int, config_inputs: dict[str, str]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    """Stage 2 of provisioning: assign operational identity, then build the
    Collector config from the MAPPED operational IDs (never the SaaS IDs).

    Returns (operational_identity, agent_config, error). On any failure the
    identity and config are None and error describes it; assignment is atomic
    and idempotent, so this can simply be run again.
    """
    try:
        identity = assign_operational_identity(
            organization_id=organization_id,
            branch_id=branch_id,
        )
        agent_config = build_collector_agent_config(
            operational_customer_id=identity["operational_customer_id"],
            operational_branch_id=identity["operational_branch_id"],
            api_url=config_inputs["api_url"],
            raw_checkins_file=config_inputs["raw_checkins_file"],
            raw_rejects_file=config_inputs["raw_rejects_file"],
            raw_acs_file=config_inputs["raw_acs_file"],
        )
    except Exception as e:
        return None, None, f"{type(e).__name__}: {e}"

    return identity, agent_config, None


auth_user = st.session_state["auth_user"]

st.title("Provision Library")
st.caption(
    "Create a new library tenant, assign its operational identity, and "
    "generate its agent config."
)

if "provision_result" not in st.session_state:
    st.session_state["provision_result"] = None

if "provision_config_inputs" not in st.session_state:
    st.session_state["provision_config_inputs"] = None

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

    st.subheader("Collector Installation")
    st.caption(
        "Server-side record of the deployed Collector. "
        "Does not change the generated Collector configuration."
    )
    create_installation = st.checkbox("Create initial installation record", value=True)
    installation_name = st.text_input("Installation name", value="Main AMH Sorter")
    installation_hostname = st.text_input("Installation hostname (optional)")
    installation_version = st.text_input("Collector version", value="1.0.2")

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

    if create_installation and not installation_name.strip():
        st.error("Installation name is required to create an installation record.")
        st.stop()

    org_settings: dict[str, Any] = dict(DEFAULT_ORG_SETTINGS)
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

    # Stage 2: operational identity. The SaaS tenant above is already
    # committed, so a failure here must not hide it -- it is reported as
    # "operational provisioning incomplete" and no Collector config is
    # produced until a (re-runnable) assignment succeeds.
    config_inputs = {
        "api_url": api_base_url,
        "raw_checkins_file": raw_checkins_file,
        "raw_rejects_file": raw_rejects_file,
        "raw_acs_file": raw_acs_file,
    }
    operational_identity, agent_config, operational_error = run_operational_stage(
        organization["id"], branch["id"], config_inputs
    )

    # The library is already committed at this point; an installation-record
    # failure must not hide that, so it is reported alongside the result and
    # the record can be added later from Manage Libraries.
    installation = None
    installation_error = None
    if create_installation:
        try:
            installation = create_collector_installation(
                organization_id=organization["id"],
                branch_id=branch["id"],
                name=installation_name,
                hostname=installation_hostname,
                collector_version=installation_version,
                status="provisioning",
            )
        except Exception as e:
            installation_error = f"{type(e).__name__}: {e}"

    st.session_state["provision_result"] = {
        "organization": organization,
        "branch": branch,
        "plan": result["plan"],
        "subscription": result["subscription"],
        "operational_identity": operational_identity,
        "operational_error": operational_error,
        "collector_installation": installation,
        "agent_config": agent_config,
    }
    st.session_state["provision_config_inputs"] = config_inputs

    if operational_error:
        st.warning(
            "SaaS organization and branch were created, but operational "
            f"provisioning is INCOMPLETE ({operational_error}). Retry below or "
            "from Manage Libraries."
        )
    else:
        st.success("Library provisioned.")
    if installation_error:
        st.warning(
            "Library provisioned, but the installation record could not be created "
            f"({installation_error}). Add it from Manage Libraries."
        )

if st.session_state["provision_result"]:
    provision_result = st.session_state["provision_result"]

    st.subheader("Provision Result")

    if provision_result.get("operational_error"):
        st.error(
            "Operational provisioning is incomplete. The SaaS organization and "
            "branch exist, but no operational identity is assigned, so no "
            "Collector configuration is available yet."
        )
        config_inputs_saved = st.session_state["provision_config_inputs"]
        if config_inputs_saved and st.button("Retry operational identity assignment"):
            identity, config, error = run_operational_stage(
                provision_result["organization"]["id"],
                provision_result["branch"]["id"],
                config_inputs_saved,
            )
            provision_result["operational_identity"] = identity
            provision_result["operational_error"] = error
            provision_result["agent_config"] = config
            st.session_state["provision_result"] = provision_result
            st.rerun()

    st.json(provision_result)

    if provision_result.get("agent_config"):
        st.download_button(
            "Download agent_config.json",
            data=json.dumps(provision_result["agent_config"], indent=2),
            file_name="agent_config.json",
            mime="application/json",
        )
