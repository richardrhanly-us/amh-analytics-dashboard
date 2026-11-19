"""Contract v2: local ACS classification -- RAW layer (docs/collector-v2.md).

Only collector/v2_transform.py may import this. It is the dashboard's legacy classifier (src/metrics.py::build_acs_item_summary) moved onto this
machine, with the same rules (docs/hold-count-accuracy.md, sections 3 and 5; Collector 1.0.16):

  * HOLD STATE. `101YNY` is a check-in with an ALERT, and the alert type CV decides: CV01 (local hold) and CV02 (hold for another
    destination) are `hold`; no CV is `hold` (the approved fallback); CV04 (transit) is `non_hold_101`; any other CV is `unknown_alert`
    (neither a confirmed hold nor a confirmed non-hold). Another `101...` is `non_hold_101`; any other code-10 message is `other_code10`.
  * THE HOLD PATRON is CY, falling back to the item record's AA only when CY is absent (`hold_patron`). Its cached profile classifies the hold.
  * `is_ill` is true if ANY of: the hold patron's TYPE is "ILL"; the destination text contains the whole word ILL or INTERLIBRARY; the hold
    patron's NAME does; or the raw message WITHOUT its title field (AJ), upper-cased, contains `\\bILL\\b`, INTERLIBRARY, `|DAILL`, `|AEILL`
    or `|PTILL`. (Before 1.0.16 the title was searched too, so a title such as "The Ill-Made Knight" marked a hold ILL -- decision D2 removed it.)
  * `is_collection_services` / `is_branch_services` (the dashboard's "programming"): the hold patron's NAME is in the configured list, or the
    message contains a configured `|<pattern>|` marker.

Patron names are never compared as text here: the cache holds a keyed name HMAC, and the rules hold keyed name HMACs, so membership is an HMAC
lookup. The patron NAME's ILL-keyword flag is computed once, when the message-64 record is read, and stored as a boolean. The CV code itself
and the patron identifiers never leave this machine: only the derived state and flags do.
"""

from __future__ import annotations

import re

from .v2_patrons import PatronInfo
from .v2_rules import Rules

_ILL_WORD = re.compile(r"\bILL\b|INTERLIBRARY")
_ILL_RAW = re.compile(r"\bILL\b|INTERLIBRARY|\|DAILL\b|\|AEILL\b|\|PTILL\b")
_AE = re.compile(r"\|AE([^|]*)")
_PT = re.compile(r"\|PT([^|]*)")
_CV = re.compile(r"\|CV(\d\d)")
_CY = re.compile(r"\|CY([^|]*)")
_AJ = re.compile(r"\|AJ[^|]*")
_HOLD_CV = frozenset({"01", "02"})
_TRANSIT_CV = frozenset({"04"})


def item_state(raw_message: str) -> str:
    """hold | non_hold_101 | unknown_alert | other_code10, from the message prefix and alert type (CV). Call only for a code-10 message."""
    if raw_message.startswith("101YNY"):
        found = _CV.search(raw_message)
        if found is None or found.group(1) in _HOLD_CV:
            return "hold"
        return "non_hold_101" if found.group(1) in _TRANSIT_CV else "unknown_alert"
    if raw_message.startswith("101"):
        return "non_hold_101"
    return "other_code10"


def hold_patron(raw_message: str, item_patron: str) -> str:
    """The hold's patron identifier: CY from the message, else the item record's AA (`item_patron`), else "". A transient raw string for the
    caller to key at once; never stored or sent."""
    found = _CY.search(raw_message)
    cy = found.group(1).strip() if found else ""
    return cy or item_patron


def patron_profile(raw_message: str) -> tuple[str, str]:
    """(name, type) from a message-64 record: the first `|AE` and `|PT` fields, stripped. Transient raw strings for the caller to reduce."""
    name = _AE.search(raw_message)
    kind = _PT.search(raw_message)
    return (name.group(1).strip() if name else ""), (kind.group(1).strip() if kind else "")


def name_is_ill_like(patron_name: str) -> bool:
    return bool(_ILL_WORD.search(patron_name.strip().upper()))


def type_is_ill(patron_type: str) -> bool:
    return patron_type.strip().upper() == "ILL"


def static_flags(*, raw_message: str, destination_raw: object, rules: Rules) -> tuple[bool, bool, bool]:
    """(is_ill, is_branch_services, is_collection_services) contributions that come from the HOLD'S OWN message and never change: the
    destination text, the raw message without its title (ILL keywords), and the configured `|DA...|` patterns."""
    raw_upper = raw_message.upper()
    destination_upper = destination_raw.strip().upper() if isinstance(destination_raw, str) else ""
    ill = bool(_ILL_WORD.search(destination_upper) or _ILL_RAW.search(_AJ.sub("", raw_upper)))
    collection = any(marker in raw_upper for marker in rules.collection_da_markers)
    branch = any(marker in raw_upper for marker in rules.branch_da_markers)
    return ill, branch, collection


def profile_outcome(patron: PatronInfo | None, rules: Rules) -> tuple[bool, bool, bool]:
    """(is_ill, is_branch_services, is_collection_services) contributions that come from the patron's PROFILE -- the only part a message-64
    record that arrives later can change: the patron type or name says ILL, or the name is a configured service account."""
    known = patron or PatronInfo(None, False, False)
    in_branch = known.name_k is not None and known.name_k in rules.branch_name_hmacs
    in_collection = known.name_k is not None and known.name_k in rules.collection_name_hmacs
    return bool(known.type_ill or known.name_ill), in_branch, in_collection


def combine(static: tuple[bool, bool, bool], outcome: tuple[bool, bool, bool]) -> tuple[bool, bool, bool]:
    """A hold's flags: each is true if EITHER its own message or the patron profile makes it so (the dashboard's OR)."""
    return static[0] or outcome[0], static[1] or outcome[1], static[2] or outcome[2]


def classify_hold(*, raw_message: str, destination_raw: object, patron: PatronInfo | None, rules: Rules) -> tuple[bool, bool, bool]:
    """(is_ill, is_branch_services, is_collection_services) for one hold, from its own message and the cached patron profile."""
    return combine(static_flags(raw_message=raw_message, destination_raw=destination_raw, rules=rules), profile_outcome(patron, rules))
