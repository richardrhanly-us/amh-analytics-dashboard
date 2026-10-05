"""The customer API's reject reasons: one closed set of codes, and how a
stored reject becomes one of them.

A branch's rejects live in two tables that say WHY an item was rejected in
different ways:

    rejects.error_message        TEXT  (legacy "v1")
        The sorter's own free text, stored as it was read. It is raw: it is
        classified here and never returned, logged or raised.

    reject_events.error_class    TEXT  (Contract "v2")
        Already one of the eight codes below -- the collector classified the
        text before sending it and kept none of it.

So a v1 message is CLASSIFIED (classify_legacy_reject_message) and a v2 class
is only RECOGNISED (reason_for_error_class). A v2 class is never classified
as if it were text: "rfid_collision" is a code, and reading it as a sentence
would file it under "other".

The classification rules are those of the Contract v2 collector
(collector/v2_normalize.py::classify_reject), copied here on purpose: this
module must not import collector code, and the collector must not change for
it. tests/test_reject_reason.py compares the two so they cannot drift apart.
A legacy reject therefore gets the code the collector would have given the
same text, and does not change reason at a branch's cutover.

That differs from the Streamlit dashboard (reject_logic.py::simplify_error)
in one deliberate way: a message that is empty, only whitespace or the
literal "nan" is `unknown` here, where the dashboard calls it "Other". The
dashboard is separate from this module and is not changed by it.

Framework-neutral: standard library only. No pandas, no Streamlit, no
SQLAlchemy, no database access.
"""

from __future__ import annotations

from typing import Literal, cast

# The public reason codes, in the one order every answer lists them. The set
# is closed: nothing outside it is ever returned to a caller.
REJECT_REASONS: tuple[str, ...] = (
    "item_not_found",
    "ils_acs_failure",
    "rfid_collision",
    "configuration_error",
    "routing_error",
    "communication_error",
    "other",
    "unknown",
)
RejectReason = Literal[
    "item_not_found", "ils_acs_failure", "rfid_collision", "configuration_error",
    "routing_error", "communication_error", "other", "unknown",
]


def classify_legacy_reject_message(message: object) -> RejectReason:
    """The reason code for one legacy rejects.error_message value.

    Total: any value at all gets a code and nothing is raised. Matching is by
    substring on the stripped, lower-cased text, and the FIRST rule that
    matches decides -- a message can satisfy several ("item not found in ACS"
    is `item_not_found`). "acs" is matched wherever those three letters occur.

    `unknown` means there was no text to classify: a value that is not a
    string (None included), an empty or whitespace-only string, or the
    literal "nan". `other` means there was text and no rule matched it.

    `communication_error` is never returned: no reject wording maps to it.
    """
    if not isinstance(message, str):
        return "unknown"

    text = message.strip().lower()

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


def reason_for_error_class(error_class: object) -> RejectReason | None:
    """`error_class` itself if it is exactly one of REJECT_REASONS, else None.

    Nothing is normalized, stripped, lower-cased or coerced: a stored class
    either is one of the eight codes or is not recognised. What an
    unrecognised class then counts as is the caller's decision; it is never
    handed back from here.
    """
    if isinstance(error_class, str) and error_class in REJECT_REASONS:
        return cast(RejectReason, error_class)
    return None
