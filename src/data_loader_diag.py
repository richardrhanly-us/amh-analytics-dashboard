"""Temporary Streamlit Cloud diagnostic wrapper for data_loader imports."""

import importlib.util
import logging
import sys

logger = logging.getLogger("sortview.data_loader_diag")

_data_loader_spec = importlib.util.find_spec("data_loader")

logger.error(
    "IMPORT DIAG | python=%s | data_loader_origin=%s",
    sys.version,
    getattr(_data_loader_spec, "origin", None),
)

try:
    from data_loader import (
        load_pipeline_status,
        load_v2_ingest_status,
        validate_tenant_schema,
    )
except ImportError as exc:
    logger.error(
        "IMPORT DIAG FAILED | type=%s | module=%s | path=%s",
        type(exc).__name__,
        getattr(exc, "name", None),
        getattr(exc, "path", None),
    )
    raise

__all__ = [
    "load_pipeline_status",
    "load_v2_ingest_status",
    "validate_tenant_schema",
]
