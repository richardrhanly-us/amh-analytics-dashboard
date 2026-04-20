import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

CHECKINS_FILE = "data/processed/checkins_clean.csv"
REJECTS_FILE = "data/processed/rejects_clean.csv"
STATUS_FILE = "data/processed/pipeline_status.json"
CHECKINS_HISTORY_FILE = "data/processed/checkins_history.csv"
REJECTS_HISTORY_FILE = "data/processed/rejects_history.csv"

# Local-dev escape hatch only.
# Leave this false in production.
ALLOW_FILE_FALLBACK = os.getenv("SORTVIEW_ALLOW_FILE_FALLBACK", "false").lower() == "true"

# Adjust these only if your tenant columns use different names.
CHECKINS_ORG_COLUMN = os.getenv("SORTVIEW_CHECKINS_ORG_COLUMN", "customer_id")
CHECKINS_BRANCH_COLUMN = os.getenv("SORTVIEW_CHECKINS_BRANCH_COLUMN", "branch_id")

REJECTS_ORG_COLUMN = os.getenv("SORTVIEW_REJECTS_ORG_COLUMN", "customer_id")
REJECTS_BRANCH_COLUMN = os.getenv("SORTVIEW_REJECTS_BRANCH_COLUMN", "branch_id")

ACS_ORG_COLUMN = os.getenv("SORTVIEW_ACS_ORG_COLUMN", "customer_id")
ACS_BRANCH_COLUMN = os.getenv("SORTVIEW_ACS_BRANCH_COLUMN", "branch_id")

PIPELINE_ORG_COLUMN = os.getenv("SORTVIEW_PIPELINE_ORG_COLUMN", "customer_id")
PIPELINE_BRANCH_COLUMN = os.getenv("SORTVIEW_PIPELINE_BRANCH_COLUMN", "branch_id")


def get_database_url():
    db_url = os.getenv("DATABASE_URL")

    if not db_url:
        try:
            db_url = st.secrets.get("DATABASE_URL")
        except Exception:
            db_url = None

    return db_url


@st.cache_resource
def get_engine():
    db_url = get_database_url()
    if not db_url:
        return None

    return create_engine(
        db_url,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={"sslmode": "require"},
    )


def get_file_mtime(path):
    file_path = Path(path)
    if file_path.exists():
        return file_path.stat().st_mtime
    return 0


def _safe_identifier(name):
    if not name:
        raise ValueError("SQL identifier cannot be empty.")

    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
    if any(ch not in allowed for ch in name):
        raise ValueError(f"Unsafe SQL identifier: {name}")

    return name


def _require_scope(org_slug, branch_slug):
    if not org_slug or not branch_slug:
        raise ValueError("Tenant scope is required: org_slug and branch_slug must be provided.")


def _read_table(query, params=None):
    engine = get_engine()

    if engine is None:
        st.error("DATABASE_URL is missing. App cannot connect to Neon.")
        return pd.DataFrame()

    try:
        return pd.read_sql(text(query), engine, params=params or {})
    except Exception as e:
        st.error(f"Database query failed: {e}")
        return pd.DataFrame()


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


def _load_checkins_from_csv(path):
    file_path = Path(path)

    if not file_path.exists():
        return pd.DataFrame()

    try:
        df = pd.read_csv(file_path, low_memory=False)
        return _normalize_checkins_df(df)
    except Exception:
        return pd.DataFrame()


def _load_rejects_from_csv(path):
    file_path = Path(path)

    if not file_path.exists():
        return pd.DataFrame()

    try:
        df = pd.read_csv(file_path, low_memory=False)
        return _normalize_rejects_df(df)
    except Exception:
        return pd.DataFrame()


LIVE_DASHBOARD_TIMEZONE = os.getenv("SORTVIEW_LIVE_TIMEZONE", "America/Chicago")


def _today_filter_sql():
    return f"(now() AT TIME ZONE '{LIVE_DASHBOARD_TIMEZONE}')::date"


def _scoped_query(table_name, org_column, branch_column, live_only=False):
    org_column = _safe_identifier(org_column)
    branch_column = _safe_identifier(branch_column)
    table_name = _safe_identifier(table_name)

    if live_only:
        return f"""
            SELECT *
            FROM {table_name}
            WHERE {org_column} = :org_slug
              AND {branch_column} = :branch_slug
              AND event_time::date = {_today_filter_sql()}
            ORDER BY event_time
        """

    return f"""
        SELECT *
        FROM {table_name}
        WHERE {org_column} = :org_slug
          AND {branch_column} = :branch_slug
        ORDER BY event_time
    """


def _load_checkins_history_from_db(org_slug, branch_slug):
    params = {"org_slug": org_slug, "branch_slug": branch_slug}

    query = _scoped_query(
        table_name="checkins_routed",
        org_column=CHECKINS_ORG_COLUMN,
        branch_column=CHECKINS_BRANCH_COLUMN,
        live_only=False,
    )
    df = _read_table(query, params=params)

    if not df.empty:
        return _normalize_checkins_df(df)

    fallback_query = _scoped_query(
        table_name="checkins_clean",
        org_column=CHECKINS_ORG_COLUMN,
        branch_column=CHECKINS_BRANCH_COLUMN,
        live_only=False,
    )
    df = _read_table(fallback_query, params=params)
    return _normalize_checkins_df(df)


def _load_checkins_live_from_db(org_slug, branch_slug):
    params = {"org_slug": org_slug, "branch_slug": branch_slug}

    query = _scoped_query(
        table_name="checkins_routed",
        org_column=CHECKINS_ORG_COLUMN,
        branch_column=CHECKINS_BRANCH_COLUMN,
        live_only=True,
    )
    df = _read_table(query, params=params)

    if not df.empty:
        return _normalize_checkins_df(df)

    fallback_query = _scoped_query(
        table_name="checkins_clean",
        org_column=CHECKINS_ORG_COLUMN,
        branch_column=CHECKINS_BRANCH_COLUMN,
        live_only=True,
    )
    df = _read_table(fallback_query, params=params)
    return _normalize_checkins_df(df)


def _load_rejects_history_from_db(org_slug, branch_slug):
    params = {"org_slug": org_slug, "branch_slug": branch_slug}

    query = _scoped_query(
        table_name="rejects_clean",
        org_column=REJECTS_ORG_COLUMN,
        branch_column=REJECTS_BRANCH_COLUMN,
        live_only=False,
    )
    df = _read_table(query, params=params)
    return _normalize_rejects_df(df)


def _load_rejects_live_from_db(org_slug, branch_slug):
    params = {"org_slug": org_slug, "branch_slug": branch_slug}

    query = _scoped_query(
        table_name="rejects_clean",
        org_column=REJECTS_ORG_COLUMN,
        branch_column=REJECTS_BRANCH_COLUMN,
        live_only=True,
    )
    df = _read_table(query, params=params)
    return _normalize_rejects_df(df)


def _load_acs_history_from_db(org_slug, branch_slug):
    params = {"org_slug": org_slug, "branch_slug": branch_slug}

    query = _scoped_query(
        table_name="acs_events",
        org_column=ACS_ORG_COLUMN,
        branch_column=ACS_BRANCH_COLUMN,
        live_only=False,
    )
    df = _read_table(query, params=params)
    return _normalize_acs_df(df)


def _load_acs_live_from_db(org_slug, branch_slug):
    params = {"org_slug": org_slug, "branch_slug": branch_slug}

    query = _scoped_query(
        table_name="acs_events",
        org_column=ACS_ORG_COLUMN,
        branch_column=ACS_BRANCH_COLUMN,
        live_only=True,
    )
    df = _read_table(query, params=params)
    return _normalize_acs_df(df)


@st.cache_data(ttl=600, show_spinner=False)
def load_checkins_history_df(org_slug, branch_slug, mtime=None, refresh_count=0):
    try:
        _require_scope(org_slug, branch_slug)
    except ValueError as e:
        st.error(str(e))
        return pd.DataFrame()

    df = _load_checkins_history_from_db(org_slug, branch_slug)

    if not df.empty:
        return df

    if ALLOW_FILE_FALLBACK:
        return _load_checkins_from_csv(CHECKINS_HISTORY_FILE)

    return pd.DataFrame()


@st.cache_data(ttl=60, show_spinner=False)
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


@st.cache_data(ttl=60, show_spinner=False)
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


@st.cache_data(ttl=600, show_spinner=False)
def load_rejects_history_df(org_slug, branch_slug, path=REJECTS_HISTORY_FILE, mtime=None, refresh_count=0):
    try:
        _require_scope(org_slug, branch_slug)
    except ValueError as e:
        st.error(str(e))
        return pd.DataFrame()

    df = _load_rejects_history_from_db(org_slug, branch_slug)

    if not df.empty:
        return df

    if ALLOW_FILE_FALLBACK:
        return _load_rejects_from_csv(path)

    return pd.DataFrame()


@st.cache_data(ttl=600, show_spinner=False)
def load_acs_history_df(org_slug, branch_slug, mtime=None, refresh_count=0):
    try:
        _require_scope(org_slug, branch_slug)
    except ValueError as e:
        st.error(str(e))
        return pd.DataFrame()

    return _load_acs_history_from_db(org_slug, branch_slug)


@st.cache_data(ttl=60, show_spinner=False)
def load_acs_df(org_slug, branch_slug, mtime=None, refresh_count=0):
    try:
        _require_scope(org_slug, branch_slug)
    except ValueError as e:
        st.error(str(e))
        return pd.DataFrame()

    return _load_acs_live_from_db(org_slug, branch_slug)


@st.cache_data(ttl=60, show_spinner=False)
def load_pipeline_status(org_slug, branch_slug, path=STATUS_FILE, mtime=None, refresh_count=0):
    try:
        _require_scope(org_slug, branch_slug)
    except ValueError as e:
        st.error(str(e))
        return {}

    org_column = _safe_identifier(PIPELINE_ORG_COLUMN)
    branch_column = _safe_identifier(PIPELINE_BRANCH_COLUMN)

    query = f"""
        SELECT
            customer_id,
            branch_id,
            last_attempt,
            last_run,
            status,
            checkins_rows,
            rejects_rows,
            uploaded_checkins_rows,
            uploaded_rejects_rows,
            checkins_history_rows,
            rejects_history_rows,
            checkins_bad_datetime_rows,
            rejects_bad_datetime_rows,
            transit_items,
            problem_items,
            destination_breakdown,
            updated_at
        FROM pipeline_status
        WHERE {org_column} = :org_slug
          AND {branch_column} = :branch_slug
        ORDER BY updated_at DESC
        LIMIT 1
    """
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

    for key in ["last_attempt", "last_run", "updated_at"]:
        value = row.get(key)
        if pd.notna(value):
            if hasattr(value, "isoformat"):
                row[key] = value.isoformat()
            else:
                row[key] = str(value)
        else:
            row[key] = None

    destination_breakdown = row.get("destination_breakdown")

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


def validate_tenant_schema():
    checks = []

    checks.append((
        "checkins_routed",
        [CHECKINS_ORG_COLUMN, CHECKINS_BRANCH_COLUMN, "event_time"],
    ))
    checks.append((
        "checkins_clean",
        [CHECKINS_ORG_COLUMN, CHECKINS_BRANCH_COLUMN, "event_time"],
    ))
    checks.append((
        "rejects_clean",
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


