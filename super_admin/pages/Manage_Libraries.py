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
    assign_operational_identity,
    build_collector_agent_config,
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


ORGANIZATION_STATUS_LABELS = {
    "active": "Active",
    "trial": "Trial",
    "suspended": "Suspended",
    "cancelled": "Cancelled",
}


def operational_id(value):
    """int for a mapped operational ID, None for NULL/NaN (pandas turns a
    nullable integer column into floats with NaN)."""
    return None if pd.isna(value) else int(value)


def build_agent_config(row):
    """Collector config for the library, or None if it has no complete
    operational identity. customer_id/branch_id are the OPERATIONAL pair
    (operational_customer_id / operational_branch_id) -- never the SaaS
    organization/branch IDs, and there is no fallback to them."""
    api_base_url = st.secrets.get(
        "AGENT_API_BASE_URL",
        "https://sortview-app-2p336.ondigitalocean.app",
    )

    try:
        return build_collector_agent_config(
            operational_customer_id=operational_id(row.get("operational_customer_id")),
            operational_branch_id=operational_id(row.get("operational_branch_id")),
            api_url=api_base_url,
        )
    except ValueError:
        return None


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

selected_customer_id = operational_id(selected_row.get("operational_customer_id"))
selected_operational_branch_id = operational_id(selected_row.get("operational_branch_id"))

detail_col1, detail_col2 = st.columns([2, 1])

with detail_col1:
    st.json(selected_row)

with detail_col2:
    st.markdown("#### Operational Identity")

    if selected_customer_id is not None and selected_operational_branch_id is not None:
        st.metric("Operational Customer ID", selected_customer_id)
        st.metric("Operational Branch ID", selected_operational_branch_id)
    else:
        if selected_customer_id is None and selected_operational_branch_id is None:
            st.warning("Operational identity: Not assigned")
        else:
            st.warning("Operational identity: Partially assigned")
            st.write(f"Operational Customer ID: {selected_customer_id}")
            st.write(f"Operational Branch ID: {selected_operational_branch_id}")

        st.caption(
            "No Collector config or agent token should be issued until this is "
            "assigned. Assignment is safe to re-run."
        )
        if pd.isna(selected_row.get("branch_id")):
            st.error("This library has no primary branch to assign an identity to.")
        elif st.button("Assign operational identity", type="primary"):
            try:
                assigned = assign_operational_identity(
                    organization_id=int(selected_row["organization_id"]),
                    branch_id=int(selected_row["branch_id"]),
                )
            except Exception as e:
                st.error(f"Assignment failed: {type(e).__name__}: {e}")
            else:
                st.success(
                    "Operational identity assigned: customer "
                    f"{assigned['operational_customer_id']}, branch "
                    f"{assigned['operational_branch_id']}."
                )
                st.rerun()

    st.markdown("#### Agent Config")
    if agent_config is not None:
        st.download_button(
            "Download agent_config.json",
            data=json.dumps(agent_config, indent=2),
            file_name=f"{selected_row['organization_slug']}_agent_config.json",
            mime="application/json",
        )
    else:
        st.info("Unavailable until the operational identity is assigned.")

    st.markdown("#### Library Controls")

    org_status = str(selected_row.get("organization_status") or "").lower()
    branch_status = selected_row.get("branch_status")
    branch_status = None if pd.isna(branch_status) else str(branch_status).lower()
    org_status_label = ORGANIZATION_STATUS_LABELS.get(org_status, org_status or "Unknown")

    st.write(f"Library status: **{org_status_label}**")
    if branch_status is not None:
        st.write(f"Primary branch status: **{branch_status.title()}**")
        if branch_status != "active":
            st.warning(
                "The primary branch is not active, so Collector uploads are rejected "
                "regardless of the library status. Suspend/reactivate does not change "
                "branch status."
            )

    library_organization_id = int(selected_row["organization_id"])

    if org_status in ("active", "trial"):
        st.caption(
            "Suspending is reversible. While suspended, Collector uploads for this "
            "library are rejected. Data, operational identity, installations, "
            "subscriptions and agent tokens are all preserved."
        )
        if st.button("Suspend Library", type="secondary"):
            try:
                set_library_active_status(organization_id=library_organization_id, is_active=False)
            except Exception as e:
                st.error(f"Suspend failed: {type(e).__name__}: {e}")
            else:
                st.success("Library suspended.")
                st.rerun()
    elif org_status == "suspended":
        st.caption(
            "Reactivating sets the library to Active. It does not reactivate any "
            "agent token; tokens that are still active work again, deactivated "
            "tokens stay deactivated."
        )
        if st.button("Reactivate Library", type="primary"):
            try:
                set_library_active_status(organization_id=library_organization_id, is_active=True)
            except Exception as e:
                st.error(f"Reactivate failed: {type(e).__name__}: {e}")
            else:
                st.success("Library reactivated.")
                st.rerun()
    elif org_status == "cancelled":
        st.error(
            "This library is Cancelled. It cannot be suspended or reactivated from "
            "here."
        )
    else:
        st.error(f"Unrecognised library status {org_status!r}; no action is available.")

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
