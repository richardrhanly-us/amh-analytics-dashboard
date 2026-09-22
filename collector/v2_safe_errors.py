"""Contract v2: errors that can never carry a record (docs/collector-v2.md).

Everything raw the v2 collector touches (a Tech Logic line, a barcode, a patron identifier, a title, a reject message) can end up
in an exception's MESSAGE -- pandas, the standard library and any driver quote the offending value. So no v2 code ever logs,
stores or sends `str(exc)`, a traceback's exception line, or a chained cause:

  * `summarize(exc)` names only the exception TYPE and the CODE LOCATIONS (module, function, line) of the last few frames;
  * `run_guarded(code, fn, ...)` runs raw-touching code and, on any failure, raises a `CollectorV2Error` with a fixed `code` and
    that summary. The new error is raised OUTSIDE the `except` block, so it has no `__context__` to leak the original message.

A `CollectorV2Error`'s message is its fixed code, never a value.
"""

from __future__ import annotations

import os
import traceback
from collections.abc import Callable
from typing import Any

_MAX_FRAMES = 3


class CollectorV2Error(Exception):
    """Base of every v2 error. `code` is a fixed, lower-case identifier; the message IS the code."""

    def __init__(self, code: str, summary: str = ""):
        super().__init__(code)
        self.code = code
        self.summary = summary


class TransformError(CollectorV2Error):
    """Raw-touching code failed. Carries a fixed code and a type-and-location summary only."""


def summarize(exc: BaseException) -> str:
    """The exception's type and where it happened -- never its message, never its cause. For example
    `ValueError at v2_transform.py:parse_acs:212 < v2_transform.py:transform_chunk:98`."""
    kind = f"{type(exc).__module__}.{type(exc).__qualname__}"
    frames = traceback.extract_tb(exc.__traceback__)[-_MAX_FRAMES:]
    where = " < ".join(f"{os.path.basename(frame.filename)}:{frame.name}:{frame.lineno}" for frame in reversed(frames))
    return f"{kind} at {where}" if where else kind


def run_guarded(code: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Runs `fn`; any Exception becomes `TransformError(code, summary)` with no message and no chained cause."""
    try:
        return fn(*args, **kwargs)
    except CollectorV2Error:
        raise
    except Exception as exc:
        summary = summarize(exc)
    raise TransformError(code, summary)  # outside the except block: nothing of the original is chained


def describe(exc: BaseException) -> str:
    """What a log line may say about ANY exception: a fixed code for our own errors, otherwise type and location."""
    if isinstance(exc, CollectorV2Error):
        return f"code={exc.code}" + (f" {exc.summary}" if exc.summary else "")
    return summarize(exc)
