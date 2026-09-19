from __future__ import annotations

import json
import os
import sys

import pandas as pd
import streamlit as st

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
SUPER_ADMIN_DIR = os.path.join(ROOT_DIR, "super_admin")

if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

if SUPER_ADMIN_DIR not in sys.path:
    sys.path.insert(0, SUPER_ADMIN_DIR)

from super_auth import require_super_admin

from services.platform_admin_service import (
    list_libraries_with_status,
    set_library_active_status,
)
from services.tenant_service import (
    COLLECTOR_INSTALLATION_STATUSES,
    create_collector_installation,
    list_collector_installations_for_organization,
    update_collector_installation,
)

st.set_page_config(
    page_title="Manage Libraries",
    page_icon="📚",
    layout="wide",
)

auth_user = require_super_admin()

st.title("Manage Libraries")
st.caption("Master list of provisioned libraries and current status.")

rows = list_libraries_with_status()

if not rows:
    st.info("No libraries have been provisioned yet.")
    st.stop()


def build_agent_config(row):
    api_base_url = st.secrets.get(
        "AGENT_API_BASE_URL",
        "https://sortview-app-2p336.ondigitalocean.app",
    )

    return {
        "database_url": "",
        "customer_id": int(row["organization_id"]),
        "branch_id": int(row["branch_id"]),
        "raw_checkins_file": r"C:\TLCFinalDlls\Checkins.txt",
        "raw_rejects_file": r"C:\TLCFinalDlls\Rejects.txt",
        "processed_checkins_file": r"data\processed\checkins_clean.csv",
        "processed_rejects_file": r"data\processed\rejects_clean.csv",
        "checkins_history_file": r"data\processed\checkins_history.csv",
        "rejects_history_file": r"data\processed\rejects_history.csv",
        "status_file": r"data\processed\pipeline_status.json",
        "api_url": api_base_url.rstrip("/"),
        "raw_acs_file": r"C:\TLCFinalDlls\ACS Log.txt",
        "processed_acs_file": r"data\processed\acs_clean.csv",
        "acs_history_file": r"data\processed\acs_history.csv",
    }


df = pd.DataFrame(rows)

if "last_run" in df.columns:
    df["last_run"] = pd.to_datetime(df["last_run"], errors="coerce")

if "last_attempt" in df.columns:
    df["last_attempt"] = pd.to_datetime(df["last_attempt"], errors="coerce")

total_orgs = df["organization_id"].nunique()
active_orgs = df[df["organization_status"] == "active"]["organization_id"].nunique()
reporting_orgs = df[df["pipeline_status"].notna()]["organization_id"].nunique()

col1, col2, col3 = st.columns(3)

with col1:
    st.metric("Libraries", total_orgs)

with col2:
    st.metric("Active", active_orgs)

with col3:
    st.metric("Reporting", reporting_orgs)

display_df = df.copy()

display_df = display_df.rename(
    columns={
        "organization_name": "Library",
        "organization_slug": "Org Slug",
        "organization_status": "Org Status",
        "branch_name": "Primary Branch",
        "branch_slug": "Branch Slug",
        "subscription_status": "Subscription",
        "plan_name": "Plan",
        "pipeline_status": "Agent Status",
        "last_run": "Last Run",
        "last_attempt": "Last Attempt",
    }
)

display_columns = [
    "Library",
    "Org Slug",
    "Org Status",
    "Primary Branch",
    "Branch Slug",
    "Subscription",
    "Plan",
    "Agent Status",
    "Last Run",
    "Last Attempt",
]

st.dataframe(
    display_df[display_columns],
    width="stretch",
    hide_index=True,
)

st.subheader("Selected Library Details")

library_names = df["organization_name"].dropna().tolist()
selected_library = st.selectbox("Choose a library", library_names)

selected_row = df[df["organization_name"] == selected_library].iloc[0].to_dict()
agent_config = build_agent_config(selected_row)

detail_col1, detail_col2 = st.columns([2, 1])

with detail_col1:
    st.json(selected_row)

with detail_col2:
    st.markdown("#### Agent Config")
    st.download_button(
        "Download agent_config.json",
        data=json.dumps(agent_config, indent=2),
        file_name=f"{selected_row['organization_slug']}_agent_config.json",
        mime="application/json",
    )

    st.markdown("#### Library Controls")

    is_active = str(selected_row.get("organization_status", "")).lower() == "active"

    if is_active:
        if st.button("Deactivate Library", type="secondary"):
            set_library_active_status(
                organization_id=int(selected_row["organization_id"]),
                branch_id=int(selected_row["branch_id"]),
                is_active=False,
            )
            st.success("Library deactivated.")
            st.rerun()
    else:
        if st.button("Reactivate Library", type="primary"):
            set_library_active_status(
                organization_id=int(selected_row["organization_id"]),
                branch_id=int(selected_row["branch_id"]),
                is_active=True,
            )
            st.success("Library reactivated.")
            st.rerun()

st.subheader("Collector Installations")
st.caption(
    "Server-side records of deployed Collectors for this library. "
    "These are bookkeeping only; pipeline health is shown separately below."
)

organization_id = int(selected_row["organization_id"])
primary_branch_id = selected_row.get("branch_id")
installations = list_collector_installations_for_organization(organization_id)

if installations:
    installations_df = pd.DataFrame(installations).rename(
        columns={
            "name": "Installation",
            "branch_name": "Branch",
            "hostname": "Hostname",
            "collector_version": "Collector Version",
            "status": "Status",
            "installed_at": "Installed At",
            "last_seen_at": "Last Seen At",
        }
    )
    st.dataframe(
        installations_df[
            [
                "Installation",
                "Branch",
                "Hostname",
                "Collector Version",
                "Status",
                "Installed At",
                "Last Seen At",
            ]
        ],
        width="stretch",
        hide_index=True,
    )

    installations_by_id = {int(row["id"]): row for row in installations}
    selected_installation_id = st.selectbox(
        "Edit installation",
        list(installations_by_id),
        format_func=lambda installation_id: (
            f"{installations_by_id[installation_id]['name']} "
            f"({installations_by_id[installation_id]['branch_name']})"
        ),
    )
    installation = installations_by_id[selected_installation_id]

    with st.form(f"edit_installation_form_{selected_installation_id}"):
        edit_name = st.text_input("Installation name", value=installation["name"])
        edit_hostname = st.text_input("Hostname", value=installation["hostname"] or "")
        edit_version = st.text_input(
            "Collector version", value=installation["collector_version"] or ""
        )
        edit_status = st.selectbox(
            "Status",
            COLLECTOR_INSTALLATION_STATUSES,
            index=COLLECTOR_INSTALLATION_STATUSES.index(installation["status"]),
        )
        save_installation = st.form_submit_button("Save Installation", type="primary")

    if save_installation:
        try:
            update_collector_installation(
                installation_id=selected_installation_id,
                organization_id=organization_id,
                name=edit_name,
                hostname=edit_hostname,
                collector_version=edit_version,
                status=edit_status,
            )
        except Exception as e:
            st.error(f"Update failed: {type(e).__name__}: {e}")
        else:
            st.success("Installation updated.")
            st.rerun()
else:
    st.info("No collector installations recorded for this library.")

if pd.notna(primary_branch_id):
    with st.expander("Add installation"):
        with st.form("add_installation_form"):
            new_name = st.text_input("Installation name", value="Main AMH Sorter")
            new_hostname = st.text_input("Hostname (optional)")
            new_version = st.text_input("Collector version", value="1.0.2")
            add_installation = st.form_submit_button("Add Installation")

        if add_installation:
            try:
                create_collector_installation(
                    organization_id=organization_id,
                    branch_id=int(primary_branch_id),
                    name=new_name,
                    hostname=new_hostname,
                    collector_version=new_version,
                    status="provisioning",
                )
            except Exception as e:
                st.error(f"Create failed: {type(e).__name__}: {e}")
            else:
                st.success("Installation added.")
                st.rerun()

st.subheader("Pipeline Status")
st.caption(
    "From pipeline_status for the primary branch. Independent of the "
    "installation records above."
)
st.dataframe(
    pd.DataFrame(
        [
            {
                "Agent Status": selected_row.get("pipeline_status"),
                "Last Run": selected_row.get("last_run"),
                "Last Attempt": selected_row.get("last_attempt"),
            }
        ]
    ),
    width="stretch",
    hide_index=True,
)
