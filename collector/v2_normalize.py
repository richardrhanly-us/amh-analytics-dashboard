"""Contract v2: raw routing labels and reject text -> controlled v2 values -- RAW layer (docs/collector-v2.md).

Only collector/v2_transform.py may import this. Its inputs are raw Tech Logic text; its outputs are members of small closed sets (a slug, a
numeric code, an enum value), so nothing raw survives the call.

DESTINATION. The same mapping as agent/parser/checkins.py::normalize_destination, into lower-case slugs:
    "1" / "LOCAL" / "MAIN"      -> main
    contains WESTSIDE           -> westside
    contains LIBRARY EXPRESS    -> library_express
    contains NO AGENCY DESTINATION -> no_agency_destination
then any `destinations` entries of the local rules file; anything else, or nothing, is `unknown`. There is NO pseudonymous or hash-derived
fallback: an unmapped raw label is reported as `unknown` and no more.

BIN. A short number (1-4 digits) is its own code; anything else is `unknown`.

REJECT CLASS. The mapping the dashboard already applies (agent/parser/rejects.py::simplify_error_message plus the dashboard's "no item found"),
in the same precedence, into the closed v2 enum. Empty text is `unknown`; unmatched text is `other`. `communication_error` is in the enum but
NO text pattern maps to it: no reject wording has been observed that warrants one, and inventing a pattern would misclassify real rejects.
The raw text is never kept.
"""

from __future__ import annotations

import re

from .v2_rules import Rules

_HOME_ALIASES = frozenset({"1", "LOCAL", "MAIN"})
_BUILTIN = (("WESTSIDE", "westside"), ("LIBRARY EXPRESS", "library_express"), ("NO AGENCY DESTINATION", "no_agency_destination"))
_NUMERIC_BIN = re.compile(r"[0-9]{1,4}")

UNKNOWN = "unknown"


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def normalize_destination(raw: object, rules: Rules) -> str:
    upper = _text(raw).upper()
    if not upper:
        return UNKNOWN
    if upper in _HOME_ALIASES:
        return "main"
    for contains, slug in _BUILTIN:
        if contains in upper:
            return slug
    for contains, slug in rules.destinations:
        if contains in upper:
            return slug
    return UNKNOWN


def normalize_bin(raw: object) -> str:
    text = _text(raw)
    return text if _NUMERIC_BIN.fullmatch(text) else UNKNOWN


def classify_reject(message: object) -> str:
    text = _text(message).lower()
    if not text or text == "nan":
        return "unknown"
    if "item not found" in text or "no item found" in text:
        return "item_not_found"
    if "acs" in text:
        return "ils_acs_failure"
    if "multiple rfid" in text or "multiple tags" in text:
        return "rfid_collision"
    if "collection code" in text:
        return "configuration_error"
    if "library not found" in text:
        return "routing_error"
    return "other"
