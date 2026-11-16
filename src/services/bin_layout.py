"""A sorter's BIN LAYOUT: every physical bin the machine has, and where each
stands along the belt. Pure: no database, no clock, no request.

WHY IT IS STORED. The check-ins say which bins received items, never which
bins a machine has: a bin that received nothing today leaves no trace. So a
machine's full set of bins, and their places, can only be what an operator
wrote down for that one machine. Nothing here is inferred, and no layout is
ever assumed of another sorter: a sorter without one is drawn from its
check-ins alone, as before.

WHERE IT IS STORED. Under the `bin_layout` key of the sorter site's own
settings document -- a sorter site is one host branch
(services.sorter_inventory_service), so the branch's document is the
sorter's:

    branch_settings.settings_json["bin_layout"] =
        {"end": "0", "columns": [["6", "5"], ["4", "3"], ["2", "1"]]}

    end      the bin standing alone at the left end of the line, on the belt
    columns  from the left end along the belt: each [the bin above the belt, the bin below it]

The organization's document is never read for it: a layout belongs to one
machine and is never handed down.

STRICT. A layout is exactly those two keys. `end` is a bin key; `columns` is
a list of 1 to MAX_COLUMNS pairs of bin keys. A bin key is written as the
reports write it (services.sort_bin.bin_key): one to four ASCII digits with
no leading zero, as text -- "04" and the JSON number 4 are refused, not read
as bin 4, so a layout can only name a bin the way its check-ins are counted
under. No bin appears twice.

INVALID IS REFUSED, NEVER REPAIRED. parse_bin_layout raises BinLayoutError
naming what is wrong by code, and quotes nothing stored. A reader that must
not fail (the organization's sorter list) treats an invalid layout as none.

Standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

BIN_LAYOUT_KEY = "bin_layout"

# A bin is the reports' bin key: one to four ASCII digits, no leading zero ([0-9], not \d).
_BIN_KEY = re.compile(r"0|[1-9][0-9]{0,3}")
# Far more than any sorter's line: 64 bins along the belt and one at its end.
MAX_COLUMNS = 32

NOT_AN_OBJECT = "not_an_object"
WRONG_FIELDS = "wrong_fields"
END_INVALID = "end_invalid"
COLUMNS_INVALID = "columns_invalid"
TOO_MANY_COLUMNS = "too_many_columns"
COLUMN_INVALID = "column_invalid"
BIN_INVALID = "bin_invalid"
DUPLICATE_BIN = "duplicate_bin"


class BinLayoutError(ValueError):
    """A layout that is not one. `code` says why; nothing stored is quoted."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class BinLayout:
    end: str
    columns: tuple[tuple[str, str], ...]


def _bin(value: object) -> str:
    if not isinstance(value, str) or _BIN_KEY.fullmatch(value) is None:
        raise BinLayoutError(BIN_INVALID)
    return value


def parse_bin_layout(value: object) -> BinLayout:
    """The layout `value` describes. Raises BinLayoutError if it is not exactly
    a layout; see the module docstring."""
    if not isinstance(value, Mapping):
        raise BinLayoutError(NOT_AN_OBJECT)
    if set(value) != {"end", "columns"}:
        raise BinLayoutError(WRONG_FIELDS)
    if not isinstance(value["end"], str):
        raise BinLayoutError(END_INVALID)
    end = _bin(value["end"])

    stored = value["columns"]
    if not isinstance(stored, list) or not stored:
        raise BinLayoutError(COLUMNS_INVALID)
    if len(stored) > MAX_COLUMNS:
        raise BinLayoutError(TOO_MANY_COLUMNS)
    columns = []
    for column in stored:
        if not isinstance(column, list) or len(column) != 2:
            raise BinLayoutError(COLUMN_INVALID)
        columns.append((_bin(column[0]), _bin(column[1])))

    bins = [end, *(key for column in columns for key in column)]
    if len(set(bins)) != len(bins):
        raise BinLayoutError(DUPLICATE_BIN)
    return BinLayout(end=end, columns=tuple(columns))


def serialize_bin_layout(layout: BinLayout) -> dict[str, Any]:
    """A layout as it is stored and sent: parse_bin_layout reads it back unchanged."""
    return {"end": layout.end, "columns": [list(column) for column in layout.columns]}
