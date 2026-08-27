#***************************************************************
#
#  Author:       Richard Hanly
#
#  File:         database.py
#
#  Description: Provides shared database connection utilities for
#               the SortView dashboard. This file retrieves the
#               DATABASE_URL from environment variables or Streamlit
#               secrets and creates a reusable SQLAlchemy database
#               engine for the application.
#
#***************************************************************

from __future__ import annotations

import os

import streamlit as st
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

#***************************************************************
#
#  Function:     get_database_url
#
#  Description: Retrieves the database connection URL used by the
#               application. The function first checks the operating
#               system environment variables, then falls back to
#               Streamlit secrets when running in Streamlit Cloud.
#
#  Parameters:  None
#
#  Returns:     str - Database connection URL.
#
#  Raises:      RuntimeError - If DATABASE_URL is not found in either
#                              environment variables or Streamlit
#                              secrets.
#
#***************************************************************

def get_database_url() -> str:
    # First try to load the database URL from the system environment.
    database_url = os.getenv("DATABASE_URL")

    # If the environment variable is not available, try Streamlit secrets.
    if not database_url:
        try:
            database_url = st.secrets.get("DATABASE_URL")
        except Exception:
            database_url = None

    # Stop the application if no database URL is configured.
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set.")

    return database_url


#***************************************************************
# Shared Database Engine
#
# Stores a reusable SQLAlchemy engine so the application does not
# create a new database engine every time data is loaded.
#***************************************************************

_engine: Engine | None = None


#***************************************************************
#
#  Function:     get_engine
#
#  Description: Creates and returns the shared SQLAlchemy database
#               engine. The engine is created only once and then
#               reused by later database calls.
#
#  Parameters:  None
#
#  Returns:     Engine - SQLAlchemy database engine connected to the
#                        configured database.
#
#***************************************************************

def get_engine() -> Engine:
    global _engine

    # Create the database engine the first time this function is called.
    if _engine is None:
        _engine = create_engine(
            get_database_url(),
            pool_pre_ping=True,
            pool_recycle=300,
            connect_args={"sslmode": "require"},
            future=True,
        )

    return _engine
