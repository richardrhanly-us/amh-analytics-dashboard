"""Redaction canary: a standalone Streamlit app for verifying, on a NON-PRODUCTION deployment, that an uncaught exception
neither reaches the browser nor the hosting log.

NOT part of the dashboard or the Super Admin app: nothing imports it, it sits outside `src/` and `super_admin/`, and it is
INERT unless SORTVIEW_REDACTION_CANARY_ENABLED=true is set (an environment variable or an app secret). Deploy it as its
OWN Streamlit app (main file `scripts/redaction_canary/app.py`), never inside the production apps, and delete that app when
the check is done. docs/production-verification-runbook.md has the procedure and the pass/fail criteria.

It raises exceptions on purpose. Their messages carry SYNTHETIC canaries only -- a fake password, database URL, API token,
patron/card number and e-mail, a fake SQL statement and bound value -- so a hosting log or a browser can be searched for the
prefix `CANARY-` (and `canary-`): finding one anywhere means an exception's text leaked.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR / "src") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "src"))

from services.privacy_hardening import (
    install_streamlit_log_scrubber,
    is_streamlit_log_scrubber_installed,
    streamlit_error_details_startup,
)

# First, exactly as every real entry script does (see tests/test_streamlit_log_scrubbing.py).
install_streamlit_log_scrubber()

ENABLE_FLAG = "SORTVIEW_REDACTION_CANARY_ENABLED"
CANARY_PASSWORD = "CANARY-DB-PASSWORD-LIVE-0001"  # nosec B105 - a deliberately fake canary, not a credential
CANARY_HOST = "canary-db-host-live.example.invalid"
CANARY_DATABASE_URL = f"postgresql://canary_svc_user:{CANARY_PASSWORD}@{CANARY_HOST}:5432/canary_db"
CANARY_TOKEN = "CANARY-API-TOKEN-LIVE-0002"  # nosec B105 - a deliberately fake canary, not a credential
CANARY_CARD = "CANARY-PATRON-CARD-2300000000003"
CANARY_EMAIL = "canary.patron.0004@example.invalid"
CANARY_BOUND_VALUE = "CANARY-BOUND-VALUE-LIVE-0005"
CANARY_TABLE = "canary_table_live_0006"


def canary_text(label: str) -> str:
    """What a real database failure's message looks like: the SQL, its bound values, the connection string, a token and
    the failing row -- all synthetic."""
    return (
        f"{label}: could not run [SQL: SELECT * FROM {CANARY_TABLE} WHERE card = %(card)s AND email = %(email)s] "  # nosec B608 - fake error text, no query runs
        f"[parameters: {{'card': '{CANARY_CARD}', 'email': '{CANARY_EMAIL}', 'v': '{CANARY_BOUND_VALUE}'}}]; "
        f"connection {CANARY_DATABASE_URL}; Authorization: Bearer {CANARY_TOKEN}; "
        f"DETAIL: Failing row contains ({CANARY_CARD}, {CANARY_EMAIL})"
    )


def _enabled() -> bool:
    value = os.environ.get(ENABLE_FLAG, "")
    if not value:
        try:
            value = str(st.secrets.get(ENABLE_FLAG, ""))
        except Exception:
            value = ""
    return value.strip().lower() == "true"


def raise_database_error() -> None:
    from sqlalchemy.exc import DataError

    class DriverError(Exception):
        pgcode = "22P02"

    raise DataError(canary_text("database"), {"v": CANARY_BOUND_VALUE}, DriverError(canary_text("driver")))


def raise_plain_error() -> None:
    raise RuntimeError(canary_text("plain"))


def raise_in_callback() -> None:
    raise RuntimeError(canary_text("callback"))


def diagnostics() -> dict:
    from streamlit import config

    # SortView pins client.showErrorDetails to "none" in code (install_streamlit_log_scrubber), which overwrites whatever the
    # platform or an environment variable set. So report BOTH: the value the process started with (Streamlit Community Cloud
    # supplies "false") and the effective value after enforcement -- never only the latter, which would hide the platform.
    startup = streamlit_error_details_startup()
    return {
        "streamlit_version": st.__version__,
        "working_directory": os.getcwd(),
        "repository_config_file_in_working_directory": Path(".streamlit/config.toml").is_file(),
        "client.showErrorDetails at startup (before enforcement)": startup[0] if startup else None,
        "client.showErrorDetails at startup defined in": startup[1] if startup else None,
        "client.showErrorDetails effective (after enforcement)": config.get_option("client.showErrorDetails"),
        "client.showErrorDetails effective defined in": config.get_where_defined("client.showErrorDetails"),
        "logger.enableRich": config.get_option("logger.enableRich"),
        "log_scrubber_installed": is_streamlit_log_scrubber_installed(),
    }


st.set_page_config(page_title="Redaction canary", page_icon="🐤")
st.title("Redaction canary")

if not _enabled():
    st.info(f"Disabled. Set {ENABLE_FLAG}=true (environment variable or app secret) on a NON-PRODUCTION deployment to use it.")
    st.stop()

st.warning("Non-production verification page. Every button below raises an uncaught exception ON PURPOSE.")
st.subheader("Effective configuration")
st.json(diagnostics())

st.subheader("Trigger an uncaught exception")
database_clicked = st.button("Raise a database-style error")
plain_clicked = st.button("Raise a plain error")
st.button("Raise an error inside a button callback", on_click=raise_in_callback)

if database_clicked:
    raise_database_error()
if plain_clicked:
    raise_plain_error()
