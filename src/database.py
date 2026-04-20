import os

import streamlit as st
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


def get_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        try:
            database_url = st.secrets.get("DATABASE_URL")
        except Exception:
            database_url = None

    if not database_url:
        raise RuntimeError("DATABASE_URL is not set.")

    return database_url


_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine

    if _engine is None:
        _engine = create_engine(
            get_database_url(),
            pool_pre_ping=True,
            pool_recycle=300,
            connect_args={"sslmode": "require"},
            future=True,
        )

    return _engine
