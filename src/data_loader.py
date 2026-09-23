#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         data_loader.py
#
#  Description: Loads SortView dashboard data from the database and,
#               when enabled for local development, from processed CSV
#               or JSON files. This file handles tenant-scoped database
#               queries, dataframe normalization, Streamlit caching,
#               pipeline status loading, and tenant schema validation.
#
#***************************************************************

import json
import logging
import os
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import text

from database import get_engine
from services.privacy_hardening import log_safe_exception

#***************************************************************
# File Paths and Logger Setup
#
# Defines local processed file paths used only when file fallback is
# enabled, and prepares a logger for database/data loading errors.
#***************************************************************

CHECKINS_FILE = "data/processed/checkins_clean.csv"
REJECTS_FILE = "data/processed/rejects_clean.csv"
STATUS_FILE = "data/processed/pipeline_status.json"
logger = logging.getLogger("sortview.data_loader")


#***************************************************************
# Local Development Fallback Setting
#
# Allows the dashboard to fall back to local processed files when
# database results are unavailable. This should remain disabled in
# production.
#***************************************************************

# Local-dev escape hatch only.
# Leave this false in production.
ALLOW_FILE_FALLBACK = os.getenv("SORTVIEW_ALLOW_FILE_FALLBACK", "false").lower() == "true"


#***************************************************************
# Tenant Column Configuration
#
# Defines the database column names used to scope data by organization
# and branch. Environment variables allow these names to be overridden
# if a deployment uses different tenant column names.
#***************************************************************

# Adjust these only if your tenant columns use different names.
CHECKINS_ORG_COLUMN = os.getenv("SORTVIEW_CHECKINS_ORG_COLUMN", "customer_id")
CHECKINS_BRANCH_COLUMN = os.getenv("SORTVIEW_CHECKINS_BRANCH_COLUMN", "branch_id")

REJECTS_ORG_COLUMN = os.getenv("SORTVIEW_REJECTS_ORG_COLUMN", "customer_id")
REJECTS_BRANCH_COLUMN = os.getenv("SORTVIEW_REJECTS_BRANCH_COLUMN", "branch_id")

ACS_ORG_COLUMN = os.getenv("SORTVIEW_ACS_ORG_COLUMN", "customer_id")
ACS_BRANCH_COLUMN = os.getenv("SORTVIEW_ACS_BRANCH_COLUMN", "branch_id")

PIPELINE_ORG_COLUMN = os.getenv("SORTVIEW_PIPELINE_ORG_COLUMN", "customer_id")
PIPELINE_BRANCH_COLUMN = os.getenv("SORTVIEW_PIPELINE_BRANCH_COLUMN", "branch_id")


#***************************************************************
# ACS Columns Loaded Into Dashboard Memory (privacy containment, Step 2)
#
# acs_events used to be read with SELECT *, pulling every stored column --
# including ones nothing uses -- into the app's memory and its 15-minute
# st.cache_data cache. Only the columns below are loaded now.
#
# TEMPORARY, under the v1 Collector contract: patron_id and raw_message are
# STILL loaded, and ONLY because the current cloud-side classifier in
# metrics.build_acs_item_summary still reads them:
#   raw_message -- the "101"/"101YNY" hold prefixes, the "|AE"/"|PT"
#                  patron-name/type fields of message 64, the ILL keyword
#                  test, and the configured "|DA<name>|" service patterns;
#   patron_id   -- the join key from an item message to its message-64
#                  patron record.
# Both go away when classification moves to the collector (Contract v2);
# the barcode is likewise only for its distinct-item de-duplication.
# Not loaded any more: id, customer_id, branch_id, title, barcode_key,
# source_file, source_event_id, created_at (no consumer reads them).
# checkins/rejects are still read with SELECT * (no patron fields; a later
# step narrows them with the v2 dashboard migration).
#***************************************************************

ACS_LOAD_COLUMNS = (
    "event_time",
    "message_code",
    "barcode",
    "destination",
    "patron_id",
    "raw_message",
)
ACS_PATRON_COLUMNS_STILL_LOADED = ("patron_id", "raw_message")


#***************************************************************
#
#  Function:     get_file_mtime
#
#  Description: Returns the last modified timestamp for a local file.
#               This can be used as a cache-busting value when local
#               file fallback is enabled.
#
#  Parameters:  path - Path to the file being checked.
#
#  Returns:     float - File modified timestamp, or 0 if the file does
#                       not exist.
#
#***************************************************************

def get_file_mtime(path):
    file_path = Path(path)
    if file_path.exists():
        return file_path.stat().st_mtime
    return 0


#***************************************************************
#
#  Function:     _safe_identifier
#
#  Description: Validates a SQL identifier before it is inserted into
#               a SQL query string. This protects table and column
#               names from unsafe characters.
#
#  Parameters:  name - SQL table or column identifier to validate.
#
#  Returns:     str - The validated identifier.
#
#***************************************************************

def _safe_identifier(name):
    if not name:
        raise ValueError("SQL identifier cannot be empty.")

    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
    if any(ch not in allowed for ch in name):
        raise ValueError(f"Unsafe SQL identifier: {name}")

    return name


#***************************************************************
#
#  Function:     _require_scope
#
#  Description: Ensures that both organization and branch scope values
#               are provided before tenant-scoped data is loaded.
#
#  Parameters:  org_slug - Organization/customer identifier.
#               branch_slug - Branch identifier.
#
#  Returns:     None
#
#***************************************************************

def _require_scope(org_slug, branch_slug):
    if not org_slug or not branch_slug:
        raise ValueError("Tenant scope is required: org_slug and branch_slug must be provided.")


#***************************************************************
#
#  Function:     _show_db_error_once
#
#  Description: Displays a Streamlit database error only once per
#               session for the supplied error key. This prevents the
#               dashboard from repeatedly showing the same error.
#
#  Parameters:  key - Unique key for the error type.
#               message - Error message to display in the dashboard.
#
#  Returns:     None
#
#***************************************************************

def _show_db_error_once(key, message):
    state_key = f"_db_error_shown_{key}"

    if state_key not in st.session_state:
        st.session_state[state_key] = False

    if not st.session_state[state_key]:
        st.error(message)
        st.session_state[state_key] = True


#***************************************************************
#
#  Function:     _read_table
#
#  Description: Runs a SQL query against the configured database and
#               returns the results as a pandas dataframe. Database
#               connection or query errors are logged and shown once
#               in the dashboard.
#
#  Parameters:  query - SQL query string to execute.
#               params - Optional dictionary of SQL query parameters.
#
#  Returns:     DataFrame - Query results, or an empty dataframe if
#                           the query fails.
#
#***************************************************************

# What a user sees when data cannot be loaded. Deliberately generic: a database
# error's text can contain the SQL statement, bound values, the connection string
# or host, and schema names, none of which belong on a customer's screen. The
# detail goes to the application log instead, as a structured summary (see
# privacy_hardening.log_safe_exception).
DATA_LOAD_FAILED_MESSAGE = (
    "Dashboard data could not be loaded right now. Please try again in a few "
    "minutes. If this keeps happening, contact SortView support."
)


def _read_table(query, params=None, *, customer_id=None, branch_id=None):
    try:
        engine = get_engine()
    except Exception as exc:
        log_safe_exception(logger, "Database engine creation failed", exc)
        _show_db_error_once("engine_creation", DATA_LOAD_FAILED_MESSAGE)
        return pd.DataFrame()

    try:
        # Tenant context (RLS) must be set on the SAME connection/transaction
        # as the read that follows -- passing a bare Engine to pd.read_sql
        # lets pandas check out its own connection internally, with no hook
        # to set anything on it first. customer_id/branch_id are optional:
        # information_schema callers (validate_tenant_schema) pass neither,
        # and simply skip context-setting.
        with engine.connect() as conn:
            if customer_id is not None and branch_id is not None:
                conn.execute(
                    text("SELECT set_config('app.operational_customer_id', :v, true)"),
                    {"v": str(customer_id)},
                )
                conn.execute(
                    text("SELECT set_config('app.operational_branch_id', :v, true)"),
                    {"v": str(branch_id)},
                )
            return pd.read_sql(text(query), conn, params=params or {})
    except Exception as exc:
        # The query text is our own statement (placeholders, no values); only the
        # NAMES of the bound parameters are logged, never their values.
        log_safe_exception(
            logger,
            f"Database query failed | params={sorted(params or {})} | query={' '.join(str(query).split())}",
            exc,
        )
        _show_db_error_once("query_failed", DATA_LOAD_FAILED_MESSAGE)
        return pd.DataFrame()


#***************************************************************
#
#  Function:     _normalize_checkins_df
#
#  Description: Standardizes checkin dataframe columns used by the
#               dashboard. This creates a normalized datetime column,
#               maps sort bin values, and fills destination values from
#               available destination columns when needed.
#
#  Parameters:  df - Checkin dataframe to normalize.
#
#  Returns:     DataFrame - Normalized checkin dataframe.
#
#***************************************************************

def _normalize_checkins_df(df):
    if df.empty:
        return df

    if "event_time" in df.columns:
        df["datetime"] = pd.to_datetime(df["event_time"], errors="coerce")
    elif "checkin_datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["checkin_datetime"], errors="coerce")
    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    if "bin" not in df.columns and "sort_bin" in df.columns:
        df["bin"] = df["sort_bin"]

    if "destination" not in df.columns:
        if "destination_raw" in df.columns:
            df["destination"] = df["destination_raw"]
        elif "destination_clean" in df.columns:
            df["destination"] = df["destination_clean"]

    return df


#***************************************************************
#
#  Function:     _normalize_rejects_df
#
#  Description: Standardizes reject dataframe columns used by the
#               dashboard. This creates a normalized datetime column
#               and ensures an error_message column is always present.
#
#  Parameters:  df - Reject dataframe to normalize.
#
#  Returns:     DataFrame - Normalized reject dataframe.
#
#***************************************************************

def _normalize_rejects_df(df):
    if df.empty:
        return df

    if "event_time" in df.columns:
        df["datetime"] = pd.to_datetime(df["event_time"], errors="coerce")
    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    if "error_message" not in df.columns:
        df["error_message"] = ""

    return df


#***************************************************************
#
#  Function:     _normalize_acs_df
#
#  Description: Standardizes ACS dataframe columns used by the
#               dashboard. This creates a normalized datetime column
#               and ensures expected ACS fields exist.
#
#  Parameters:  df - ACS dataframe to normalize.
#
#  Returns:     DataFrame - Normalized ACS dataframe.
#
#***************************************************************

def _normalize_acs_df(df):
    if df.empty:
        return df

    if "event_time" in df.columns:
        df["datetime"] = pd.to_datetime(df["event_time"], errors="coerce")
    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    for col in [
        "message_code",
        "barcode",
        "title",
        "patron_id",
        "destination",
        "raw_message",
        "source_file",
    ]:
        if col not in df.columns:
            df[col] = None

    return df


#***************************************************************
#
#  Function:     _load_checkins_from_csv
#
#  Description: Loads checkin data from a local processed CSV file
#               and normalizes it for dashboard use. This is intended
#               for local development fallback only.
#
#  Parameters:  path - Local path to the processed checkins CSV file.
#
#  Returns:     DataFrame - Normalized checkin dataframe, or an empty
#                           dataframe if loading fails.
#
#***************************************************************

def _load_checkins_from_csv(path):
    file_path = Path(path)

    if not file_path.exists():
        return pd.DataFrame()

    try:
        df = pd.read_csv(file_path, low_memory=False)
        return _normalize_checkins_df(df)
    except Exception:
        return pd.DataFrame()


#***************************************************************
#
#  Function:     _load_rejects_from_csv
#
#  Description: Loads reject data from a local processed CSV file
#               and normalizes it for dashboard use. This is intended
#               for local development fallback only.
#
#  Parameters:  path - Local path to the processed rejects CSV file.
#
#  Returns:     DataFrame - Normalized reject dataframe, or an empty
#                           dataframe if loading fails.
#
#***************************************************************

def _load_rejects_from_csv(path):
    file_path = Path(path)

    if not file_path.exists():
        return pd.DataFrame()

    try:
        df = pd.read_csv(file_path, low_memory=False)
        return _normalize_rejects_df(df)
    except Exception:
        return pd.DataFrame()


#***************************************************************
# Live Dashboard Timezone
#
# Defines the timezone used by database queries that filter data to
# the current local day.
#***************************************************************

LIVE_DASHBOARD_TIMEZONE = os.getenv("SORTVIEW_LIVE_TIMEZONE", "America/Chicago")


#***************************************************************
#
#  Function:     _today_filter_sql
#
#  Description: Builds the SQL expression used to compare event dates
#               against the current day in the dashboard timezone.
#
#  Parameters:  None
#
#  Returns:     str - SQL date expression for today's local date.
#
#***************************************************************

def _today_filter_sql():
    return f"(now() AT TIME ZONE '{LIVE_DASHBOARD_TIMEZONE}')::date"


#***************************************************************
#
#  Function:     _scoped_query
#
#  Description: Builds a tenant-scoped SQL SELECT query for a specific
#               table. The query filters by organization and branch,
#               and can optionally limit results to today's live data.
#
#  Parameters:  table_name - Database table to query.
#               org_column - Organization/customer column name.
#               branch_column - Branch column name.
#               live_only - Boolean flag for loading only today's data.
#               columns - Optional explicit column names to SELECT
#                         (each validated like every other identifier);
#                         None keeps the historical SELECT *.
#
#  Returns:     str - SQL query string.
#
#***************************************************************

def _scoped_query(table_name, org_column, branch_column, live_only=False, columns=None):
    org_column = _safe_identifier(org_column)
    branch_column = _safe_identifier(branch_column)
    table_name = _safe_identifier(table_name)
    select_list = "*" if columns is None else ", ".join(_safe_identifier(c) for c in columns)

    if live_only:
        live_template = """
            SELECT {select_list}
            FROM {table_name}
            WHERE {org_column} = :org_slug
              AND {branch_column} = :branch_slug
              AND event_time::date = {today_filter}
            ORDER BY event_time
        """
        # table_name/org_column/branch_column/select_list pass through the
        # _safe_identifier() allowlist above; org_slug/branch_slug stay bound
        # parameters.
        return live_template.format(  # nosec B608
            select_list=select_list,
            table_name=table_name,
            org_column=org_column,
            branch_column=branch_column,
            today_filter=_today_filter_sql(),
        )

    range_template = """
        SELECT {select_list}
        FROM {table_name}
        WHERE {org_column} = :org_slug
          AND {branch_column} = :branch_slug
        ORDER BY event_time
    """
    # table_name/org_column/branch_column/select_list pass through the
    # _safe_identifier() allowlist above; org_slug/branch_slug stay bound
    # parameters.
    return range_template.format(  # nosec B608
        select_list=select_list,
        table_name=table_name,
        org_column=org_column,
        branch_column=branch_column,
    )


#***************************************************************
#
#  Function:     _load_checkins_history_from_db
#
#  Description: Loads all historical checkin records for the selected
#               organization and branch from the database.
#
#  Parameters:  org_slug - Organization/customer identifier.
#               branch_slug - Branch identifier.
#
#  Returns:     DataFrame - Normalized historical checkin dataframe.
#
#***************************************************************

def _load_checkins_history_from_db(org_slug, branch_slug):
    params = {"org_slug": org_slug, "branch_slug": branch_slug}

    query = _scoped_query(
        table_name="checkins",
        org_column=CHECKINS_ORG_COLUMN,
        branch_column=CHECKINS_BRANCH_COLUMN,
        live_only=False,
    )
    df = _read_table(query, params=params, customer_id=org_slug, branch_id=branch_slug)
    return _normalize_checkins_df(df)


#***************************************************************
#
#  Function:     _load_checkins_live_from_db
#
#  Description: Loads today's live checkin records for the selected
#               organization and branch from the database.
#
#  Parameters:  org_slug - Organization/customer identifier.
#               branch_slug - Branch identifier.
#
#  Returns:     DataFrame - Normalized live checkin dataframe.
#
#***************************************************************

def _load_checkins_live_from_db(org_slug, branch_slug):
    params = {"org_slug": org_slug, "branch_slug": branch_slug}

    query = _scoped_query(
        table_name="checkins",
        org_column=CHECKINS_ORG_COLUMN,
        branch_column=CHECKINS_BRANCH_COLUMN,
        live_only=True,
    )
    df = _read_table(query, params=params, customer_id=org_slug, branch_id=branch_slug)
    return _normalize_checkins_df(df)


#***************************************************************
#
#  Function:     _load_rejects_history_from_db
#
#  Description: Loads all historical reject records for the selected
#               organization and branch from the database.
#
#  Parameters:  org_slug - Organization/customer identifier.
#               branch_slug - Branch identifier.
#
#  Returns:     DataFrame - Normalized historical reject dataframe.
#
#***************************************************************

def _load_rejects_history_from_db(org_slug, branch_slug):
    params = {"org_slug": org_slug, "branch_slug": branch_slug}

    query = _scoped_query(
        table_name="rejects",
        org_column=REJECTS_ORG_COLUMN,
        branch_column=REJECTS_BRANCH_COLUMN,
        live_only=False,
    )
    df = _read_table(query, params=params, customer_id=org_slug, branch_id=branch_slug)
    return _normalize_rejects_df(df)


#***************************************************************
#
#  Function:     _load_rejects_live_from_db
#
#  Description: Loads today's live reject records for the selected
#               organization and branch from the database.
#
#  Parameters:  org_slug - Organization/customer identifier.
#               branch_slug - Branch identifier.
#
#  Returns:     DataFrame - Normalized live reject dataframe.
#
#***************************************************************

def _load_rejects_live_from_db(org_slug, branch_slug):
    params = {"org_slug": org_slug, "branch_slug": branch_slug}

    query = _scoped_query(
        table_name="rejects",
        org_column=REJECTS_ORG_COLUMN,
        branch_column=REJECTS_BRANCH_COLUMN,
        live_only=True,
    )
    df = _read_table(query, params=params, customer_id=org_slug, branch_id=branch_slug)
    return _normalize_rejects_df(df)


#***************************************************************
#
#  Function:     _load_acs_history_from_db
#
#  Description: Loads all historical ACS event records for the selected
#               organization and branch from the database.
#
#  Parameters:  org_slug - Organization/customer identifier.
#               branch_slug - Branch identifier.
#
#  Returns:     DataFrame - Normalized historical ACS dataframe.
#
#***************************************************************

def _load_acs_history_from_db(org_slug, branch_slug):
    params = {"org_slug": org_slug, "branch_slug": branch_slug}

    query = _scoped_query(
        table_name="acs_events",
        org_column=ACS_ORG_COLUMN,
        branch_column=ACS_BRANCH_COLUMN,
        live_only=False,
        columns=ACS_LOAD_COLUMNS,
    )
    df = _read_table(query, params=params, customer_id=org_slug, branch_id=branch_slug)
    return _normalize_acs_df(df)


#***************************************************************
#
#  Function:     _load_acs_live_from_db
#
#  Description: Loads today's live ACS event records for the selected
#               organization and branch from the database.
#
#  Parameters:  org_slug - Organization/customer identifier.
#               branch_slug - Branch identifier.
#
#  Returns:     DataFrame - Normalized live ACS dataframe.
#
#***************************************************************

def _load_acs_live_from_db(org_slug, branch_slug):
    params = {"org_slug": org_slug, "branch_slug": branch_slug}

    query = _scoped_query(
        table_name="acs_events",
        org_column=ACS_ORG_COLUMN,
        branch_column=ACS_BRANCH_COLUMN,
        live_only=True,
        columns=ACS_LOAD_COLUMNS,
    )
    df = _read_table(query, params=params, customer_id=org_slug, branch_id=branch_slug)
    return _normalize_acs_df(df)


#***************************************************************
#
#  Function:     load_checkins_history_df
#
#  Description: Public cached loader for historical checkin data.
#               Validates tenant scope and returns historical checkins
#               for the selected organization and branch.
#
#  Parameters:  org_slug - Organization/customer identifier.
#               branch_slug - Branch identifier.
#
#  Returns:     DataFrame - Historical checkin dataframe.
#
#***************************************************************

# Continuous Ingestion Phase 4: deliberately does NOT take refresh_count
# or mtime. This is an all-time, unbounded history query (see
# _load_checkins_history_from_db / _scoped_query) -- tying it to the live
# refresh cadence would re-run it every automatic poll (every 180s by
# default -- see dashboard_refresh_service.DEFAULT_REFRESH_SECONDS) for
# data that doesn't need that kind of freshness, and mtime would
# reintroduce the same problem via pipeline_status.updated_at, which the
# Phase 3 heartbeat now touches roughly every 60s on any cut-over branch.
# This loader relies solely on its own ttl=900 (15 min) instead.
@st.cache_data(ttl=900, show_spinner=False)
def load_checkins_history_df(org_slug, branch_slug):
    try:
        _require_scope(org_slug, branch_slug)
    except ValueError as e:
        st.error(str(e))
        return pd.DataFrame()

    return _load_checkins_history_from_db(org_slug, branch_slug)


#***************************************************************
#
#  Function:     load_checkins_df
#
#  Description: Public cached loader for today's live checkin data.
#               Loads tenant-scoped data from the database and, when
#               enabled for local development, falls back to a local
#               processed CSV file if no database data is returned.
#
#  Parameters:  org_slug - Organization/customer identifier.
#               branch_slug - Branch identifier.
#               path - Local CSV fallback path.
#               mtime - Optional cache-busting value.
#               refresh_count - Optional auto-refresh cache-busting value.
#
#  Returns:     DataFrame - Live checkin dataframe.
#
#***************************************************************

@st.cache_data(ttl=900, show_spinner=False)
def load_checkins_df(org_slug, branch_slug, path=CHECKINS_FILE, mtime=None, refresh_count=0):
    try:
        _require_scope(org_slug, branch_slug)
    except ValueError as e:
        st.error(str(e))
        return pd.DataFrame()

    df = _load_checkins_live_from_db(org_slug, branch_slug)

    if not df.empty:
        return df

    if ALLOW_FILE_FALLBACK:
        return _load_checkins_from_csv(path)

    return pd.DataFrame()


#***************************************************************
#
#  Function:     load_rejects_df
#
#  Description: Public cached loader for today's live reject data.
#               Loads tenant-scoped data from the database and, when
#               enabled for local development, falls back to a local
#               processed CSV file if no database data is returned.
#
#  Parameters:  org_slug - Organization/customer identifier.
#               branch_slug - Branch identifier.
#               path - Local CSV fallback path.
#               mtime - Optional cache-busting value.
#               refresh_count - Optional auto-refresh cache-busting value.
#
#  Returns:     DataFrame - Live reject dataframe.
#
#***************************************************************

@st.cache_data(ttl=900, show_spinner=False)
def load_rejects_df(org_slug, branch_slug, path=REJECTS_FILE, mtime=None, refresh_count=0):
    try:
        _require_scope(org_slug, branch_slug)
    except ValueError as e:
        st.error(str(e))
        return pd.DataFrame()

    df = _load_rejects_live_from_db(org_slug, branch_slug)

    if not df.empty:
        return df

    if ALLOW_FILE_FALLBACK:
        return _load_rejects_from_csv(path)

    return pd.DataFrame()


#***************************************************************
#
#  Function:     load_rejects_history_df
#
#  Description: Public cached loader for historical reject data.
#               Validates tenant scope and returns historical rejects
#               for the selected organization and branch.
#
#  Parameters:  org_slug - Organization/customer identifier.
#               branch_slug - Branch identifier.
#
#  Returns:     DataFrame - Historical reject dataframe.
#
#***************************************************************

# See load_checkins_history_df above -- same Phase 4 reasoning for why
# refresh_count/mtime are deliberately absent here.
@st.cache_data(ttl=900, show_spinner=False)
def load_rejects_history_df(org_slug, branch_slug):
    try:
        _require_scope(org_slug, branch_slug)
    except ValueError as e:
        st.error(str(e))
        return pd.DataFrame()

    return _load_rejects_history_from_db(org_slug, branch_slug)


#***************************************************************
#
#  Function:     load_acs_history_df
#
#  Description: Public cached loader for historical ACS event data.
#               Validates tenant scope and returns historical ACS
#               events for the selected organization and branch.
#
#  Parameters:  org_slug - Organization/customer identifier.
#               branch_slug - Branch identifier.
#
#  Returns:     DataFrame - Historical ACS dataframe.
#
#***************************************************************

# See load_checkins_history_df above -- same Phase 4 reasoning for why
# refresh_count/mtime are deliberately absent here.
@st.cache_data(ttl=900, show_spinner=False)
def load_acs_history_df(org_slug, branch_slug):
    try:
        _require_scope(org_slug, branch_slug)
    except ValueError as e:
        st.error(str(e))
        return pd.DataFrame()

    return _load_acs_history_from_db(org_slug, branch_slug)


#***************************************************************
#
#  Function:     load_acs_df
#
#  Description: Public cached loader for today's live ACS event data.
#               Validates tenant scope and returns live ACS activity
#               for the selected organization and branch.
#
#  Parameters:  org_slug - Organization/customer identifier.
#               branch_slug - Branch identifier.
#               mtime - Optional cache-busting value.
#               refresh_count - Optional auto-refresh cache-busting value.
#
#  Returns:     DataFrame - Live ACS dataframe.
#
#***************************************************************

@st.cache_data(ttl=900, show_spinner=False)
def load_acs_df(org_slug, branch_slug, mtime=None, refresh_count=0):
    try:
        _require_scope(org_slug, branch_slug)
    except ValueError as e:
        st.error(str(e))
        return pd.DataFrame()

    return _load_acs_live_from_db(org_slug, branch_slug)


#***************************************************************
# Pipeline Status Counter Normalization
#
# The legacy pipeline agent wrote every counter on every run. The
# Collector only sends the counters it computes, so when it is the
# first writer of a branch's pipeline_status row (an INSERT, where
# every omitted column becomes NULL) these integer columns come back
# as None. The alerts and Live Today views format or compare them
# (f"{value:,}", value > 0), so a None raises TypeError and takes the
# whole page down.
#
# They are normalized once, here at the loading boundary, so no view
# needs its own None handling. NULL means "nothing recorded", which
# every consumer already treats as 0.
#
# pending_outbox_count / quarantined_count are deliberately NOT in this
# list: NULL there means "no heartbeat was ever reported", not zero.
#***************************************************************

PIPELINE_STATUS_COUNTER_FIELDS = (
    "checkins_rows",
    "rejects_rows",
    "acs_rows",
    "uploaded_checkins_rows",
    "uploaded_rejects_rows",
    "uploaded_acs_rows",
    "checkins_bad_datetime_rows",
    "rejects_bad_datetime_rows",
    "acs_bad_datetime_rows",
    "transit_items",
    "problem_items",
)


def _normalize_pipeline_status_counters(row):
    """Replace NULL (None/NaN) counters in a pipeline_status row with 0, in place.

    Non-NULL values are left exactly as loaded, including a genuine 0.
    """
    for field in PIPELINE_STATUS_COUNTER_FIELDS:
        value = row.get(field)
        if value is None or pd.isna(value):
            row[field] = 0


#***************************************************************
#
#  Function:     load_pipeline_status
#
#  Description: Public cached loader for the latest pipeline status
#               record. Loads the most recent tenant-scoped pipeline
#               status from the database and normalizes timestamps,
#               NULL numeric counters (to 0) and destination
#               breakdown data for dashboard display.
#
#  Parameters:  org_slug - Organization/customer identifier.
#               branch_slug - Branch identifier.
#               path - Local JSON fallback path.
#               mtime - Optional cache-busting value.
#               refresh_count - Optional auto-refresh cache-busting value.
#
#  Returns:     dict - Latest pipeline status record, or an empty
#                      dictionary if no status is available.
#
#***************************************************************

@st.cache_data(ttl=60, show_spinner=False)
def load_pipeline_status(org_slug, branch_slug, path=STATUS_FILE, mtime=None, refresh_count=0):
    try:
        _require_scope(org_slug, branch_slug)
    except ValueError as e:
        st.error(str(e))
        return {}

    org_column = _safe_identifier(PIPELINE_ORG_COLUMN)
    branch_column = _safe_identifier(PIPELINE_BRANCH_COLUMN)

    pipeline_status_template = """
        SELECT
            customer_id,
            branch_id,
            last_attempt,
            last_run,
            status,
            checkins_rows,
            rejects_rows,
            acs_rows,
            uploaded_checkins_rows,
            uploaded_rejects_rows,
            uploaded_acs_rows,
            checkins_bad_datetime_rows,
            rejects_bad_datetime_rows,
            acs_bad_datetime_rows,
            transit_items,
            problem_items,
            destination_breakdown,
            health_status,
            pending_outbox_count,
            quarantined_count,
            oldest_pending_event_at,
            last_success_at,
            last_failure_category,
            last_error,
            watcher_last_active_at,
            updated_at
        FROM pipeline_status
        WHERE {org_column} = :org_slug
          AND {branch_column} = :branch_slug
        ORDER BY updated_at DESC
        LIMIT 1
    """
    # org_column/branch_column pass through _safe_identifier() allowlist
    # above; org_slug/branch_slug stay bound parameters.
    query = pipeline_status_template.format(  # nosec B608
        org_column=org_column,
        branch_column=branch_column,
    )
    df = _read_table(
        query,
        params={"org_slug": org_slug, "branch_slug": branch_slug},
    )

    if df.empty:
        if ALLOW_FILE_FALLBACK:
            file_path = Path(path)

            if not file_path.exists():
                return {}

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}

        return {}

    row = df.iloc[0].to_dict()

    _normalize_pipeline_status_counters(row)

    # Convert timestamp values into strings so they are safe to store
    # in the returned status dictionary and easy for the UI to display.
    for key in [
        "last_attempt",
        "last_run",
        "updated_at",
        "oldest_pending_event_at",
        "last_success_at",
        "watcher_last_active_at",
    ]:
        value = row.get(key)
        if pd.notna(value):
            if hasattr(value, "isoformat"):
                row[key] = value.isoformat()
            else:
                row[key] = str(value)
        else:
            row[key] = None

    destination_breakdown = row.get("destination_breakdown")

    # Normalize destination_breakdown so the dashboard always receives
    # a dictionary, whether the database returned JSON text, JSON data,
    # null, or an invalid value.
    if isinstance(destination_breakdown, str):
        try:
            row["destination_breakdown"] = json.loads(destination_breakdown)
        except Exception:
            row["destination_breakdown"] = {}
    elif destination_breakdown is None or (
        isinstance(destination_breakdown, float) and pd.isna(destination_breakdown)
    ):
        row["destination_breakdown"] = {}

    return row


#***************************************************************
#
#  Function:     _table_has_columns
#
#  Description: Checks whether a database table contains the required
#               columns needed by the dashboard. This is used by the
#               tenant schema validation process.
#
#  Parameters:  table_name - Name of the database table to inspect.
#               required_columns - List of required column names.
#
#  Returns:     tuple - Boolean success flag and a list of missing
#                       columns or schema inspection errors.
#
#***************************************************************

def _table_has_columns(table_name, required_columns):
    engine = get_engine()

    if engine is None:
        return False, ["DATABASE_URL missing"]

    query = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = :table_name
    """
    df = _read_table(query, params={"table_name": table_name})

    if df.empty or "column_name" not in df.columns:
        return False, [f"Could not inspect schema for table: {table_name}"]

    existing = set(df["column_name"].tolist())
    missing = [col for col in required_columns if col not in existing]
    return len(missing) == 0, missing


#***************************************************************
#
#  Function:     validate_tenant_schema
#
#  Description: Validates that the database contains the required
#               tenant-scoped columns for checkins, rejects, ACS
#               events, and pipeline status data.
#
#  Parameters:  None
#
#  Returns:     list - A list of schema error dictionaries. Each
#                      dictionary identifies a table and the missing
#                      columns for that table.
#
#***************************************************************

# validate_tenant_schema previously ran 4 information_schema.columns
# queries on every single Streamlit rerun -- every auto-refresh tick,
# every nav click -- to check something that cannot change without a
# database migration/deploy. It takes no tenant-scoped arguments (schema
# is shared across the whole database, not per-tenant), so one cached
# value serves every session; ttl=3600 is generous since a real schema
# change requires a deploy, which restarts the process (and this
# in-memory cache) anyway.
@st.cache_data(ttl=3600, show_spinner=False)
def validate_tenant_schema():
    checks = []

    checks.append((
        "checkins",
        [CHECKINS_ORG_COLUMN, CHECKINS_BRANCH_COLUMN, "event_time"],
    ))
    checks.append((
        "rejects",
        [REJECTS_ORG_COLUMN, REJECTS_BRANCH_COLUMN, "event_time"],
    ))
    checks.append((
        "acs_events",
        [ACS_ORG_COLUMN, ACS_BRANCH_COLUMN, "event_time"],
    ))
    checks.append((
        "pipeline_status",
        [PIPELINE_ORG_COLUMN, PIPELINE_BRANCH_COLUMN, "updated_at"],
    ))

    errors = []

    for table_name, required_columns in checks:
        ok, missing = _table_has_columns(table_name, required_columns)
        if not ok:
            errors.append({
                "table": table_name,
                "missing": missing,
            })

    return errors
