"""A sorter site's hold-classification marker: the `hold_classification` key of its branch's settings document.

    branch_settings.settings_json["hold_classification"] = {"corrected_from": "YYYY-MM-DD"}

`corrected_from` is the first full LOCAL day of verified corrected collection (Collector 1.0.16 or later, CV-aware): from
that day on, the site's v2 hold records are accurate; before it, Westside holds cannot be told from Westside transits
(docs/hold-count-accuracy.md, decision D5). It is set and cleared only by an operator, with
scripts/set_hold_corrected_from.py (audited); no customer route writes it.

Anything other than a valid marker -- absent, not an object, a missing or malformed date -- reads as NO marker: every v2
day is then uncorrected. That is the conservative side: a corrected day may be labelled partial, never the reverse.
"""

from __future__ import annotations

from datetime import date
from typing import Any

HOLD_CLASSIFICATION_KEY = "hold_classification"
CORRECTED_FROM_KEY = "corrected_from"


def parse_corrected_from(block: Any) -> date | None:
    """The marker's date, or None for anything that is not exactly {"corrected_from": "YYYY-MM-DD"}."""
    if not isinstance(block, dict) or set(block) != {CORRECTED_FROM_KEY}:
        return None
    value = block[CORRECTED_FROM_KEY]
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def marker_block(corrected_from: date) -> dict[str, str]:
    """The stored form of a marker."""
    return {CORRECTED_FROM_KEY: corrected_from.isoformat()}
