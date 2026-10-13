from __future__ import annotations

import json
import logging
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

# The repository root, for `collector` -- the one authority for the Collector version
# (collector/__init__.py; importing the package runs nothing else). Appended, not
# inserted first, so nothing that is already importable can be shadowed by it.
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from super_auth import require_super_admin

from collector import __version__ as COLLECTOR_VERSION
from services.collector_enrollment_service import (
    ALLOWED_INSTALLATION_STATUSES as ENROLLABLE_INSTALLATION_STATUSES,
)
from services.collector_enrollment_service import (
    EnrollmentError,
    generate_enrollment_code_for_installation,
)
from services.collector_rules import (
    CollectorSettingsError,
    collector_timezone,
    require_rules_reviewed,
)
from services.platform_admin_service import (
    list_libraries_with_status,
    offboard_library,
    set_library_active_status,
)
from services.privacy_hardening import (
    install_streamlit_log_scrubber,
    log_safe_exception,
)
from services.tenant_service import (
    COLLECTOR_CONTRACT_LEGACY,
    COLLECTOR_CONTRACT_V2,
    COLLECTOR_INSTALLATION_STATUSES,
    assign_operational_identity,
    build_collector_agent_config,
    create_collector_installation,
    format_collector_install_parameters,
    get_collector_site_settings,
    list_collector_installations_for_organization,
    set_collector_site_settings,
    transit_destination_problems,
    update_collector_installation,
)

logger = logging.getLogger("sortview.super_admin.libraries")

# --- which ingest contract a NEW installation is created on (onboarding stages A3a, A3b) ------------------------------
#
# Chosen explicitly, per installation, when it is created -- and passed explicitly to create_collector_installation,
# whose own default is not relied on. THE DEFAULT HERE IS CONTRACT V2 (A3b); the legacy contract is still offered and
# still fully supported. An existing installation's contract is never changed by this page.
COLLECTOR_CONTRACT_OPTIONS = (COLLECTOR_CONTRACT_LEGACY, COLLECTOR_CONTRACT_V2)
DEFAULT_COLLECTOR_CONTRACT = COLLECTOR_CONTRACT_V2
# The first Collector release whose setup can enroll on Contract v2. A fixed fact about that release, NOT "the current
# version" (which is collector.__version__ and nowhere else): it does not move when the Collector is next released. Held
# as a version tuple, the way a minimum is compared, and only formatted for display.
CONTRACT_V2_MINIMUM_COLLECTOR_VERSION = (1, 0, 14)
CONTRACT_V2_MINIMUM_COLLECTOR = ".".join(str(part) for part in CONTRACT_V2_MINIMUM_COLLECTOR_VERSION)


def collector_contract_label(contract: int) -> str:
    """What the contract selector shows for each choice."""
    if contract == COLLECTOR_CONTRACT_V2:
        return f"2 - Contract v2 (requires SortView Collector {CONTRACT_V2_MINIMUM_COLLECTOR} or later)"
    return "1 - Legacy contract"


def contract_two_site_problems(site_settings: object) -> list[str]:
    """What still stops this library getting a Contract v2 installation, from its ORGANIZATION's stored settings (empty
    = nothing): the Collector time zone and the record that its classification lists were reviewed -- the two things a
    Contract v2 enrollment reads there and refuses without (services.collector_rules). Fixed sentences: no settings
    value is repeated."""
    problems = []
    try:
        collector_timezone(site_settings)
    except CollectorSettingsError:
        problems.append("This library has no valid Collector time zone recorded.")
    try:
        require_rules_reviewed(site_settings)
    except CollectorSettingsError:
        problems.append("This library's classification lists have not been recorded as reviewed.")
    return problems


# Keep an uncaught page exception's text out of Streamlit's own server log (see services/privacy_hardening.py).
install_streamlit_log_scrubber()

# Fixed, support-safe failure messages. A failure is logged as a safe summary (log_safe_exception:
# error type, SQLSTATE, code location -- never the message); the exception's own text is never
# shown, because a database driver's message quotes SQL, bound values and the failing row.
_SUPPORT_HINT = "If this keeps happening, contact SortView support."
ASSIGN_FAILED_MESSAGE = (
    f"The operational identity could not be assigned. Assignment is safe to run again. {_SUPPORT_HINT}"
)
SUSPEND_FAILED_MESSAGE = f"The library could not be suspended. Please try again. {_SUPPORT_HINT}"
REACTIVATE_FAILED_MESSAGE = f"The library could not be reactivated. Please try again. {_SUPPORT_HINT}"
OFFBOARD_FAILED_MESSAGE = (
    f"The library could not be offboarded; nothing was changed. Offboarding is safe to run again. {_SUPPORT_HINT}"
)
OFFBOARD_CONFIRMATION_MESSAGE = "The Org Slug you typed does not match this library. Nothing was changed."
CANCELLED_READ_ONLY_MESSAGE = (
    "This library is Cancelled (permanently offboarded). Its installation records are read-only: no installation "
    "can be added or edited, and no enrollment code or installer values can be issued."
)
INSTALLATION_UPDATE_FAILED_MESSAGE = (
    f"The installation could not be updated. Check the values and try again. {_SUPPORT_HINT}"
)
INSTALLATION_ADD_FAILED_MESSAGE = (
    f"The installation could not be added. Check the values and try again. {_SUPPORT_HINT}"
)
ENROLLMENT_CODE_FAILED_MESSAGE = f"An enrollment code could not be generated. Please try again. {_SUPPORT_HINT}"
INSTALLATION_NAME_REQUIRED_MESSAGE = "Installation name is required."
SITE_SETTINGS_READ_FAILED_MESSAGE = "This library's Collector site settings could not be read."
SITE_SETTINGS_SAVE_FAILED_MESSAGE = (
    f"The Collector site settings could not be saved; nothing was changed. Please try again. {_SUPPORT_HINT}"
)
SITE_SETTINGS_TIMEZONE_MESSAGE = (
    "Collector time zone is required, and must be a known IANA time zone name such as America/New_York. "
    "Nothing was saved."
)
SITE_SETTINGS_REVIEW_MESSAGE = (
    "Confirm that this organization's collector routing configuration has been reviewed. Nothing was saved."
)
# The four classification lists, named as Admin Settings names them. Only how many entries each has is ever shown here.
INTERNAL_ROUTING_LIST_LABELS = {
    "branch_services_names": "Branch Services Names",
    "collection_services_names": "Collection Services Names",
    "branch_services_da_patterns": "Branch Services DA Patterns",
    "collection_services_da_patterns": "Collection Services DA Patterns",
}

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


# A heartbeat is only accepted for a provisioning/active installation, so
# installer values are only offered for one of those.
INSTALLER_INSTALLATION_STATUSES = ("provisioning", "active")


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
            except Exception as exc:
                log_safe_exception(logger, "Operational identity assignment failed", exc)
                st.error(ASSIGN_FAILED_MESSAGE)
            else:
                st.success(
                    "Operational identity assigned: customer "
                    f"{assigned['operational_customer_id']}, branch "
                    f"{assigned['operational_branch_id']}."
                )
                st.rerun()

    st.markdown("#### Agent Config")
    st.caption(
        "Legacy agent_config.json -- for the legacy agent only. It is NOT the scheduled "
        "Collector's collector_config.json and contains no Installation ID; see "
        "Collector Installer Values below."
    )
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
            except Exception as exc:
                log_safe_exception(logger, "Library suspend failed", exc)
                st.error(SUSPEND_FAILED_MESSAGE)
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
            except Exception as exc:
                log_safe_exception(logger, "Library reactivate failed", exc)
                st.error(REACTIVATE_FAILED_MESSAGE)
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

    # Permanent offboarding is a separate action from Suspend/Reactivate above and is
    # never reached through them. The service re-checks the slug; this page's check
    # only saves a round trip.
    if org_status in ("active", "trial", "suspended"):
        with st.expander("Offboard Library (permanent)"):
            st.warning(
                "PERMANENT. Offboarding sets the library to Cancelled and revokes every agent "
                "token, retires every Collector installation, revokes unused enrollment codes "
                "and retires ingest keys. It cannot be undone from this page and the library "
                "cannot be reactivated. No data is deleted: deleting data is a separate, "
                "operator-run purge. To pause a library instead, use Suspend."
            )
            offboard_confirmation = st.text_input(
                "Type the Org Slug to confirm",
                key=f"offboard_confirmation_{library_organization_id}",
            )
            if st.button("Offboard Library Permanently", key=f"offboard_library_{library_organization_id}"):
                if offboard_confirmation.strip() != str(selected_row["organization_slug"]):
                    st.error(OFFBOARD_CONFIRMATION_MESSAGE)
                else:
                    try:
                        offboard_library(
                            organization_id=library_organization_id,
                            confirm_slug=offboard_confirmation,
                            actor_user_id=int(auth_user["id"]),
                            actor_label=str(auth_user["email"]),
                        )
                    except Exception as exc:
                        log_safe_exception(logger, "Library offboard failed", exc)
                        st.error(OFFBOARD_FAILED_MESSAGE)
                    else:
                        st.success("Library offboarded. All Collector and dashboard access is cut off.")
                        st.rerun()

library_cancelled = org_status == "cancelled"

st.subheader("Collector Installations")
st.caption(
    "Server-side records of deployed Collectors for this library. Installed At is the first "
    "confirmed contact from the Collector (its install-time preflight or a scheduled-run "
    "heartbeat carrying its Installation ID), which also moves the installation from "
    "provisioning to active; Last Seen At is the most recent such contact; Collector "
    "Version is what that Collector last reported. Pipeline health is shown separately below."
)

organization_id = int(selected_row["organization_id"])
primary_branch_id = selected_row.get("branch_id")
installations = list_collector_installations_for_organization(organization_id)

if installations:
    installations_df = pd.DataFrame(installations).rename(
        columns={
            "id": "Installation ID",
            "name": "Installation",
            "branch_name": "Branch",
            "hostname": "Hostname",
            "collector_version": "Collector Version",
            "status": "Status",
            # The ingest contract a code for this installation requires (1 = legacy, 2 = Contract v2). Shown here, never
            # edited: it is chosen once, when an installation is ADDED (below), and an existing one is never changed.
            "required_contract": "Required Contract",
            "installed_at": "Installed At",
            "last_seen_at": "Last Seen At",
        }
    )
    st.dataframe(
        installations_df[
            [
                "Installation ID",
                "Installation",
                "Branch",
                "Hostname",
                "Collector Version",
                "Status",
                "Required Contract",
                "Installed At",
                "Last Seen At",
            ]
        ],
        width="stretch",
        hide_index=True,
    )

# The service functions refuse a cancelled organization themselves; hiding the
# controls here only keeps the page from offering what would be refused.
if library_cancelled:
    st.info(CANCELLED_READ_ONLY_MESSAGE)
elif installations:
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

    if save_installation and not edit_name.strip():
        st.error(INSTALLATION_NAME_REQUIRED_MESSAGE)
    elif save_installation:
        try:
            update_collector_installation(
                installation_id=selected_installation_id,
                organization_id=organization_id,
                name=edit_name,
                hostname=edit_hostname,
                collector_version=edit_version,
                status=edit_status,
            )
        except Exception as exc:
            log_safe_exception(logger, "Collector installation update failed", exc)
            st.error(INSTALLATION_UPDATE_FAILED_MESSAGE)
        else:
            st.success("Installation updated.")
            st.rerun()

    st.markdown("#### Enrollment Code")
    st.caption(
        "A one-time code the Collector setup redeems over HTTPS for its own agent token, "
        f"for the selected installation only (Installation ID {selected_installation_id}). "
        "Generating a new code invalidates any earlier unused code for this installation. "
        "Existing agent tokens are not touched."
    )
    if installation["status"] not in ENROLLABLE_INSTALLATION_STATUSES:
        st.info(
            "Enrollment codes are only available for provisioning or active installations "
            f"(this one is {installation['status']})."
        )
    elif st.button(
        "Generate Enrollment Code",
        key=f"generate_enrollment_code_{selected_installation_id}",
    ):
        try:
            generated = generate_enrollment_code_for_installation(
                installation_id=selected_installation_id,
                created_by_user_id=int(auth_user["id"]),
                expected_organization_id=organization_id,
            )
        except EnrollmentError as e:
            st.error(f"Cannot generate an enrollment code: {e.reason.replace('_', ' ')}.")
        except Exception as exc:
            log_safe_exception(logger, "Enrollment code generation failed", exc)
            st.error(ENROLLMENT_CODE_FAILED_MESSAGE)
        else:
            # Shown once, in this run only: deliberately NOT kept in session state,
            # so it is gone the next time the page refreshes.
            st.success(
                f"Enrollment code for {generated['installation_name']} "
                f"(Installation ID {generated['installation_id']})"
            )
            st.code(generated["enrollment_code"], language=None)
            st.write(
                f"Expires: {generated['expires_at']:%Y-%m-%d %H:%M:%S} UTC "
                f"({generated['ttl_minutes']} minutes from now)"
            )
            st.warning(
                "SINGLE USE. This code is shown only now -- copy it before this page refreshes; "
                "it cannot be displayed again. It expires at the time above and stops working "
                "the moment it is used or a new code is generated."
            )
            if generated["revoked_previous_count"]:
                st.info(
                    f"{generated['revoked_previous_count']} earlier unused code(s) for this "
                    "installation were invalidated."
                )
else:
    st.info("No collector installations recorded for this library.")

# --- collector site settings: what a Contract v2 enrollment needs, shown and repaired here ----------------------------
#
# Read from, and written to, the ORGANIZATION's own settings -- where the enrollment itself reads the time zone and the
# review record. The service returns only those two, the transit destination labels and how many entries each
# classification list has: the lists' contents (patron account names) never reach this page.
st.subheader("Collector Site Settings (Contract v2)")
st.caption(
    "What a Contract v2 Collector enrollment needs from this library: its Collector time zone, its transit "
    "destinations, and the record that its collector routing configuration was reviewed. A legacy (contract 1) "
    "installation needs none of them."
)

site_settings: dict | None
try:
    site_settings = get_collector_site_settings(organization_id)
except Exception as exc:
    log_safe_exception(logger, "Collector site settings could not be read", exc)
    site_settings = None

if site_settings is None:
    st.error(SITE_SETTINGS_READ_FAILED_MESSAGE)
else:
    site_problems = contract_two_site_problems(site_settings)
    if site_problems:
        st.warning("Not ready for a Contract v2 installation: " + " ".join(site_problems))
    else:
        st.success("Ready for a Contract v2 installation.")

    routing_counts = site_settings.get("internal_routing_counts") or {}
    st.caption(
        "Internal Routing lists (entries are not shown here; they are edited in the library's Admin Settings): "
        + "; ".join(
            f"{label}: {int(routing_counts.get(name, 0))}" for name, label in INTERNAL_ROUTING_LIST_LABELS.items()
        )
        + "."
    )

    if not library_cancelled:
        stored_timezone = site_settings.get("collector_timezone")
        with st.form(f"collector_site_settings_form_{organization_id}", enter_to_submit=False):
            site_timezone = st.text_input(
                "Collector time zone (IANA name, e.g. America/New_York) *",
                value=stored_timezone if isinstance(stored_timezone, str) else "",
                help="The zone the library's Tech Logic machine keeps its clock in. It does not change how reports are drawn.",
            )
            site_destinations_text = st.text_area(
                "Transit destinations (one per line)",
                value="\n".join(site_settings.get("transit_destination_labels") or []),
                help=(
                    "This library's enabled transit destination labels, exactly as its sorter shows them. They become "
                    "its Contract v2 classification rules. Leave empty if it has none."
                ),
            )
            site_reviewed = st.checkbox(
                "I confirm this organization's collector routing configuration has been reviewed (the transit "
                "destinations above and its four Internal Routing lists, including where a list is empty) *",
                value=False,
                key=f"collector_site_reviewed_{organization_id}",
            )
            save_site_settings = st.form_submit_button("Save Collector Site Settings")

        if save_site_settings:
            timezone_valid = True
            try:
                collector_timezone({"collector_timezone": site_timezone})
            except CollectorSettingsError:
                timezone_valid = False

            # A label that means home (by the contract, or by this library's stored home label) is not a transit
            # destination, and two different labels that share one canonical key would be ONE destination to the
            # Collector and the reports.
            destination_problems = transit_destination_problems(
                site_settings.get("home_branch_label"), site_destinations_text.splitlines()
            )

            if not timezone_valid:
                st.error(SITE_SETTINGS_TIMEZONE_MESSAGE)
            elif destination_problems:
                for destination_problem in destination_problems:
                    st.error(destination_problem)
                st.error("Nothing was saved.")
            elif not site_reviewed:
                st.error(SITE_SETTINGS_REVIEW_MESSAGE)
            else:
                try:
                    set_collector_site_settings(
                        organization_id,
                        timezone=site_timezone,
                        transit_destination_labels=site_destinations_text.splitlines(),
                        reviewed_by_user_id=int(auth_user["id"]),
                    )
                except Exception as exc:
                    log_safe_exception(logger, "Collector site settings save failed", exc)
                    st.error(SITE_SETTINGS_SAVE_FAILED_MESSAGE)
                else:
                    # A review is confirmed for one save only: the box is unticked again for the next one.
                    st.session_state.pop(f"collector_site_reviewed_{organization_id}", None)
                    st.success("Collector site settings saved.")
                    st.rerun()

if pd.notna(primary_branch_id) and not library_cancelled:
    with st.expander("Add installation"):
        with st.form("add_installation_form"):
            new_name = st.text_input("Installation name", value="Main AMH Sorter")
            new_hostname = st.text_input("Hostname (optional)")
            new_version = st.text_input("Collector version", value=COLLECTOR_VERSION)
            new_contract = st.selectbox(
                "Ingest contract for the new installation",
                options=list(COLLECTOR_CONTRACT_OPTIONS),
                index=COLLECTOR_CONTRACT_OPTIONS.index(DEFAULT_COLLECTOR_CONTRACT),
                format_func=collector_contract_label,
                help=(
                    "The contract an enrollment code for this installation will require. It is set once, here, and "
                    "cannot be changed afterwards; existing installations are not affected."
                ),
            )
            st.caption(
                f"Contract v2 needs the SortView Collector {CONTRACT_V2_MINIMUM_COLLECTOR} release bundle or later, and "
                "this library's Collector time zone and reviewed classification lists (Collector Site Settings, "
                "above). A branch has ONE Contract v2 "
                "Collector: if this branch already has one (it has enrolled on Contract v2 before), a code for a "
                "second or replacement installation will be refused at install time, whichever contract is chosen "
                "here. Replacing a machine is not supported from this page yet -- contact SortView engineering."
            )
            add_installation = st.form_submit_button("Add Installation")

        contract_problems: list[str] = []
        if add_installation and new_name.strip() and new_contract == COLLECTOR_CONTRACT_V2:
            # Advisory, read again now from the organization's own stored settings -- the same place the enrollment
            # reads them; the enrollment itself checks again and is the authority.
            try:
                contract_problems = contract_two_site_problems(get_collector_site_settings(organization_id))
            except Exception as exc:
                log_safe_exception(logger, "Collector site settings could not be read", exc)
                contract_problems = [SITE_SETTINGS_READ_FAILED_MESSAGE]

        if add_installation and not new_name.strip():
            st.error(INSTALLATION_NAME_REQUIRED_MESSAGE)
        elif add_installation and contract_problems:
            for problem in contract_problems:
                st.error(problem)
            st.error(
                "No installation was added. A Contract v2 installation needs both, recorded for this library first "
                "(Collector Site Settings, above)."
            )
        elif add_installation:
            try:
                create_collector_installation(
                    organization_id=organization_id,
                    branch_id=int(primary_branch_id),
                    name=new_name,
                    hostname=new_hostname,
                    collector_version=new_version,
                    status="provisioning",
                    required_contract=new_contract,
                )
            except Exception as exc:
                log_safe_exception(logger, "Collector installation creation failed", exc)
                st.error(INSTALLATION_ADD_FAILED_MESSAGE)
            else:
                st.success("Installation added.")
                st.rerun()

st.subheader("Collector Installer Values")
st.caption(
    "The scheduled Collector's collector_config.json is written on the library's machine by "
    "install.ps1 from these three values. Copy them exactly. Only provisioning/active "
    "installations of the primary branch are listed: the API rejects a heartbeat from an "
    "inactive or retired installation, or one belonging to another branch."
)
installer_installations = {
    int(row["id"]): row
    for row in installations
    if pd.notna(primary_branch_id)
    and int(row["branch_id"]) == int(primary_branch_id)
    and row["status"] in INSTALLER_INSTALLATION_STATUSES
}
if library_cancelled:
    st.info("Unavailable: this library is Cancelled.")
elif selected_customer_id is None or selected_operational_branch_id is None:
    st.info("Unavailable until the operational identity is assigned.")
elif not installer_installations:
    st.info(
        "Unavailable until the primary branch has a provisioning or active installation "
        "record (add one above)."
    )
else:
    installer_installation_id = st.selectbox(
        "Installation",
        list(installer_installations),
        format_func=lambda installation_id: (
            f"Installation ID {installation_id} - {installer_installations[installation_id]['name']}"
        ),
        key=f"installer_installation_{organization_id}",
    )
    installer_col1, installer_col2, installer_col3 = st.columns(3)
    installer_col1.metric("Operational Customer ID", selected_customer_id)
    installer_col2.metric("Operational Branch ID", selected_operational_branch_id)
    installer_col3.metric("Installation ID", installer_installation_id)
    st.code(
        format_collector_install_parameters(
            selected_customer_id, selected_operational_branch_id, installer_installation_id
        ),
        language="powershell",
    )

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
