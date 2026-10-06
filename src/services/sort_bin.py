"""A check-in's SORT BIN: the one key a bin has, whichever way it was stored.
Pure: no database, no clock, no request.

WHAT A BIN IS HERE. The physical bin a sorter logged for a check-in, as a
number the sorter itself uses. That is all that is stored and all that is
known. It is not how full a bin is or what it can hold, not where the item
was routed (services.routing_destination), and not a kind of item. No bin
number means anything in particular: bin 0 is a bin like any other.

TWO WAYS OF STORING IT.

    checkins.bin          (legacy)   text or NULL -- whatever the legacy agent uploaded
    checkin_events.bin    (current)  a number of one to four digits, or the literal "unknown"

ONE RULE FOR BOTH. A stored value is a KNOWN bin when, apart from surrounding
whitespace, it is one to four ASCII digits: the collector contract's own rule
for a bin (collector/v2_normalize.normalize_bin; tests/test_sort_bin.py holds
this to it), so text that the collector would call "unknown" is unknown in a
legacy row too. Its key is that number without leading zeros -- "04" and "4"
are bin 4, "000" is bin 0 -- so one bin is one bin in either era.

Everything else is UNKNOWN (None): nothing stored, blank text, "unknown", a
word, a sign, a decimal point, digits that are not ASCII, or a number too
long to be a bin. An unknown bin is still a check-in; it is just not counted
under any bin.

Nothing is assumed about which bins exist: a key is only ever made from a
value that was stored.

Standard library only.
"""

from __future__ import annotations

import re

# One to four ASCII digits: the collector contract's bin (collector/v2_normalize._NUMERIC_BIN).
_NUMERIC_BIN = re.compile(r"[0-9]{1,4}")


def bin_key(stored: object) -> str | None:
    """The key of the bin a stored value names -- its number, without leading
    zeros -- or None when the value names no bin. Anything that is not text
    is None."""
    if not isinstance(stored, str):
        return None

    text = stored.strip()
    if _NUMERIC_BIN.fullmatch(text) is None:
        return None

    return text.lstrip("0") or "0"


def bin_order(key: str) -> int:
    """Where a bin key sorts: by its number, so 2 comes before 10."""
    return int(key)
