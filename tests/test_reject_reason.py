"""Block 8a: the customer API's reject reasons.

    REJECT_REASONS / RejectReason              the closed set of eight public codes, in their fixed order
    classify_legacy_reject_message(message)    a legacy rejects.error_message value -> one of those codes
    reason_for_error_class(error_class)        a stored reject_events.error_class -> itself if it is a code, else None

Everything here is pure: no database, no request.

The module copies the Contract v2 collector's classification rules rather than
importing them, and deliberately differs from the Streamlit dashboard for a
message with no text in it. Both relationships are pinned below, from this
file only -- the collector and the dashboard are imported HERE, never by the
module under test, and neither is changed by it.

Imported the "flat" way (services.reject_reason), the identity the API
process uses.
"""

from __future__ import annotations

import ast
import inspect
import sys
from typing import get_args

import pytest

from services import reject_reason
from services.reject_reason import (
    REJECT_REASONS,
    RejectReason,
    classify_legacy_reject_message,
    reason_for_error_class,
)

APPROVED_REASONS = (
    "item_not_found",
    "ils_acs_failure",
    "rfid_collision",
    "configuration_error",
    "routing_error",
    "communication_error",
    "other",
    "unknown",
)


# =====================================================================================================================
# The closed set
# =====================================================================================================================

def test_the_reasons_are_exactly_the_eight_approved_codes_in_their_fixed_order():
    assert REJECT_REASONS == APPROVED_REASONS
    assert isinstance(REJECT_REASONS, tuple)   # an order, and not something a caller can append to
    assert len(set(REJECT_REASONS)) == 8


def test_the_reason_type_names_the_same_eight_codes_in_the_same_order():
    assert get_args(RejectReason) == REJECT_REASONS


# =====================================================================================================================
# classify_legacy_reject_message: each rule
# =====================================================================================================================

# One entry per rule and per alternative wording within a rule.
RULES = [
    ("item not found", "item_not_found"),
    ("no item found", "item_not_found"),
    ("acs", "ils_acs_failure"),
    ("multiple rfid", "rfid_collision"),
    ("multiple tags", "rfid_collision"),
    ("collection code", "configuration_error"),
    ("library not found", "routing_error"),
]


@pytest.mark.parametrize(("phrase", "expected"), RULES)
def test_each_rule_matches_its_phrase_alone(phrase, expected):
    assert classify_legacy_reject_message(phrase) == expected


@pytest.mark.parametrize(("phrase", "expected"), RULES)
def test_each_rule_matches_its_phrase_anywhere_in_a_message(phrase, expected):
    assert classify_legacy_reject_message(f"Error 17: {phrase} (sorter 2)") == expected


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Item not found in database", "item_not_found"),
        ("No item found for this tag", "item_not_found"),
        ("ACS connection failure", "ils_acs_failure"),
        ("Multiple RFID tags detected", "rfid_collision"),
        ("Multiple tags in the field", "rfid_collision"),
        ("Collection code mismatch", "configuration_error"),
        ("Library not found", "routing_error"),
    ],
)
def test_realistic_messages_are_classified(message, expected):
    assert classify_legacy_reject_message(message) == expected


@pytest.mark.parametrize(
    "message",
    ["Something else entirely", "jam", "connection timed out", "0", "none", "null", "unknown", "item_not_found",
     "rfid_collision", "nanny", "not a nan"],
)
def test_text_that_matches_no_rule_is_other(message):
    # Including the codes themselves: this function reads sentences, and "item_not_found" is not one.
    assert classify_legacy_reject_message(message) == "other"


# --- case and surrounding whitespace ----------------------------------------------------------------------------------

@pytest.mark.parametrize(("phrase", "expected"), RULES)
def test_matching_ignores_case(phrase, expected):
    mixed = "".join(c.upper() if i % 2 else c.lower() for i, c in enumerate(phrase))

    assert classify_legacy_reject_message(phrase.lower()) == expected
    assert classify_legacy_reject_message(phrase.upper()) == expected
    assert classify_legacy_reject_message(phrase.title()) == expected
    assert classify_legacy_reject_message(mixed) == expected


@pytest.mark.parametrize(("phrase", "expected"), RULES)
def test_surrounding_whitespace_does_not_change_the_reason(phrase, expected):
    assert classify_legacy_reject_message(f"  \t{phrase}\r\n") == expected


def test_the_fallback_ignores_case_too():
    assert classify_legacy_reject_message("SOMETHING ELSE ENTIRELY") == "other"


# =====================================================================================================================
# classify_legacy_reject_message: no text is `unknown`
# =====================================================================================================================

@pytest.mark.parametrize("message", ["", " ", "   ", "\t", "\n", " \t\r\n "])
def test_an_empty_or_whitespace_only_message_is_unknown(message):
    assert classify_legacy_reject_message(message) == "unknown"


@pytest.mark.parametrize("message", ["nan", "NaN", "NAN", " nan ", " NaN ", "\tnan\n"])
def test_the_literal_nan_is_unknown_whatever_its_case_or_padding(message):
    assert classify_legacy_reject_message(message) == "unknown"


def test_nan_is_only_unknown_when_it_is_the_whole_message():
    assert classify_legacy_reject_message("nan acs") == "ils_acs_failure"
    assert classify_legacy_reject_message("nanny") == "other"


class _Unprintable:
    def __str__(self):
        raise RuntimeError("never asked for")

    __repr__ = __str__


@pytest.mark.parametrize(
    "message",
    [None, float("nan"), 0, 1, 0.0, True, False, b"acs", b"item not found", bytearray(b"acs"), ["acs"], ("acs",),
     {"acs": 1}, {"acs"}, object(), _Unprintable()],
    ids=lambda value: type(value).__name__,
)
def test_a_value_that_is_not_a_string_is_unknown_and_never_raises(message):
    # Never converted to text: bytes that spell a rule, or a list holding one, still say nothing.
    assert classify_legacy_reject_message(message) == "unknown"


# =====================================================================================================================
# classify_legacy_reject_message: the first rule that matches decides
# =====================================================================================================================

@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # item_not_found is ahead of everything
        ("item not found in ACS", "item_not_found"),
        ("ACS says: no item found", "item_not_found"),
        ("multiple tags, item not found", "item_not_found"),
        ("collection code: no item found", "item_not_found"),
        ("library not found / item not found", "item_not_found"),
        # then ils_acs_failure
        ("ACS: multiple RFID tags", "ils_acs_failure"),
        ("multiple tags reported by acs", "ils_acs_failure"),
        ("ACS rejected the collection code", "ils_acs_failure"),
        ("library not found (ACS)", "ils_acs_failure"),
        # then rfid_collision
        ("multiple rfid, bad collection code", "rfid_collision"),
        ("collection code unread: multiple tags", "rfid_collision"),
        ("multiple tags; library not found", "rfid_collision"),
        # then configuration_error
        ("collection code / library not found", "configuration_error"),
        ("library not found for collection code", "configuration_error"),
        # every rule at once, written in the reverse of the rule order
        ("library not found, collection code, multiple tags, acs, item not found", "item_not_found"),
        ("library not found, collection code, multiple tags, acs", "ils_acs_failure"),
        ("library not found, collection code, multiple tags", "rfid_collision"),
        ("library not found, collection code", "configuration_error"),
    ],
)
def test_a_message_matching_several_rules_takes_the_first_in_rule_order(message, expected):
    # Rule order, never the order the phrases appear in the message.
    assert classify_legacy_reject_message(message) == expected


@pytest.mark.parametrize("message", ["pacs", "facsimile", "Macsween", "xACSx", "tracs-17"])
def test_acs_is_matched_as_a_bare_substring(message):
    # Not a word match: this is the rule the collector and the dashboard both apply, kept as it is.
    assert classify_legacy_reject_message(message) == "ils_acs_failure"


# =====================================================================================================================
# classify_legacy_reject_message: the function as a whole
# =====================================================================================================================

# A representative corpus: every case above in one place, plus wording that sounds like a communication failure.
CORPUS: list[object] = [
    *(phrase for phrase, _ in RULES),
    *(phrase.upper() for phrase, _ in RULES),
    *(f"  Error 17: {phrase.title()} (sorter 2)  " for phrase, _ in RULES),
    "Item not found in database", "No item found for this tag", "ACS connection failure", "Multiple RFID tags detected",
    "Collection code mismatch", "Library not found", "Something else entirely", "jam", "0", "none", "nanny",
    "item not found in ACS", "ACS: multiple RFID tags", "multiple rfid, bad collection code",
    "collection code / library not found", "library not found, collection code, multiple tags, acs, item not found",
    "pacs", "facsimile", "item_not_found", "ils_acs_failure", "rfid_collision", "configuration_error", "routing_error",
    "communication_error", "other", "unknown",
    "communication error", "COMMUNICATION ERROR while talking to the ILS", "comm failure", "connection timed out",
    "network failure", "timeout", "socket closed", "no response", "unable to communicate",
    "", " ", " \t\r\n ", "nan", " NaN ",
    None, float("nan"), 0, 1.5, True, b"acs", ["acs"], object(),
]


def test_every_value_gets_one_of_the_eight_codes():
    for message in CORPUS:
        assert classify_legacy_reject_message(message) in REJECT_REASONS, repr(message)


def test_no_message_is_ever_classified_as_a_communication_error():
    # The code is in the set because a v2 row may carry it. No wording maps to it.
    assert "communication_error" not in {classify_legacy_reject_message(message) for message in CORPUS}
    assert "communication_error" not in inspect.getsource(classify_legacy_reject_message).split('"""')[2]


def test_the_corpus_reaches_every_code_a_message_can_have():
    reached = {classify_legacy_reject_message(message) for message in CORPUS}

    assert reached == set(REJECT_REASONS) - {"communication_error"}


def test_classifying_the_same_message_twice_gives_the_same_answer():
    for message in CORPUS:
        assert classify_legacy_reject_message(message) == classify_legacy_reject_message(message), repr(message)


# =====================================================================================================================
# reason_for_error_class
# =====================================================================================================================

@pytest.mark.parametrize("error_class", APPROVED_REASONS)
def test_each_of_the_eight_codes_is_returned_unchanged(error_class):
    assert reason_for_error_class(error_class) == error_class


@pytest.mark.parametrize(
    "error_class",
    ["jam", "", None, "Item Not Found", "ITEM_NOT_FOUND", "Item_Not_Found", " item_not_found", "item_not_found ",
     "item_not_found\n", "item not found", "item-not-found", "items_not_found", "nan", "Other", "Unknown",
     "retryable_infra", "auth_failure"],
)
def test_anything_that_is_not_exactly_one_of_the_eight_is_not_recognised(error_class):
    # No stripping, no lower-casing: a class is one of the codes as stored, or it is not.
    assert reason_for_error_class(error_class) is None


@pytest.mark.parametrize(
    "error_class",
    [0, 1, 7, True, False, float("nan"), b"other", ["other"], ("other",), {"other"}, {"other": 1}, object(),
     _Unprintable(), APPROVED_REASONS],
    ids=lambda value: type(value).__name__,
)
def test_a_class_that_is_not_a_string_is_not_recognised_and_never_raises(error_class):
    assert reason_for_error_class(error_class) is None


def test_a_class_is_recognised_never_classified():
    # The Streamlit defect this block must not repeat: a v2 class read as if it were a sentence. As text,
    # six of the eight codes would land on a different reason; recognised, each is itself.
    as_text = {code: classify_legacy_reject_message(code) for code in REJECT_REASONS}

    assert as_text == {
        "item_not_found": "other",
        "ils_acs_failure": "ils_acs_failure",   # right only because the code happens to contain "acs"
        "rfid_collision": "other",
        "configuration_error": "other",
        "routing_error": "other",
        "communication_error": "other",
        "other": "other",
        "unknown": "other",
    }
    assert {code: reason_for_error_class(code) for code in REJECT_REASONS} == {code: code for code in REJECT_REASONS}


# =====================================================================================================================
# Against the Contract v2 collector and the ingestion API (test-only imports; neither is changed)
# =====================================================================================================================

def test_the_reasons_are_the_error_classes_the_collector_sends():
    from collector import v2_events

    assert REJECT_REASONS == v2_events.ERROR_CLASSES


def test_the_reasons_are_the_error_classes_the_ingestion_api_accepts():
    # If ingestion ever accepts a ninth class this fails on purpose: whether that class becomes a public reason
    # is a decision, not something the read side picks up by itself.
    from src.services import ingest_v2_models

    assert REJECT_REASONS == ingest_v2_models.ERROR_CLASSES
    assert get_args(RejectReason) == get_args(ingest_v2_models.ErrorClass)


def test_a_legacy_message_gets_the_class_the_collector_would_have_given_it():
    from collector import v2_normalize

    for message in CORPUS:
        assert classify_legacy_reject_message(message) == v2_normalize.classify_reject(message), repr(message)


# =====================================================================================================================
# Against the Streamlit dashboard (characterization only; the dashboard is not changed)
# =====================================================================================================================

DASHBOARD_LABEL_REASONS = {
    "Item Not Found": "item_not_found",
    "ILS / ACS Failure": "ils_acs_failure",
    "RFID Collision": "rfid_collision",
    "Call Number / Config Error": "configuration_error",
    "Routing Error": "routing_error",
    "Other": "other",
    "Unknown": "unknown",
}

NO_TEXT = ["", " ", "   ", "\t", " \t\r\n ", "nan", "NaN", " NaN "]


def _has_text(message: object) -> bool:
    return isinstance(message, str) and message.strip().lower() not in ("", "nan")


def test_the_dashboard_has_a_label_for_every_reason_a_message_can_have():
    assert set(DASHBOARD_LABEL_REASONS.values()) == set(REJECT_REASONS) - {"communication_error"}


def test_for_a_message_with_text_the_reason_is_the_dashboards_category():
    from reject_logic import simplify_error

    with_text = [message for message in CORPUS if _has_text(message)]
    assert len(with_text) > 40   # the comparison really runs

    for message in with_text:
        assert classify_legacy_reject_message(message) == DASHBOARD_LABEL_REASONS[simplify_error(message)], message


def test_a_missing_message_is_unknown_as_on_the_dashboard():
    from reject_logic import simplify_error

    assert simplify_error(None) == "Unknown"
    assert classify_legacy_reject_message(None) == "unknown"


@pytest.mark.parametrize("message", NO_TEXT)
def test_a_message_with_no_text_is_intentionally_unknown_where_the_dashboard_says_other(message):
    # The approved divergence. The dashboard only treats a NULL as missing; here an empty, blank or "nan"
    # message is missing too, as it is for the collector.
    from reject_logic import simplify_error

    assert simplify_error(message) == "Other"
    assert classify_legacy_reject_message(message) == "unknown"


# =====================================================================================================================
# Framework-neutral
# =====================================================================================================================

def _imported_modules() -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(reject_reason))):
        if isinstance(node, ast.Import):
            found |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative import"
            found.add(node.module or "")
    return found


def test_the_module_imports_only_the_standard_library():
    imported = _imported_modules()

    assert imported == {"__future__", "typing"}
    assert {name.split(".")[0] for name in imported} <= set(sys.stdlib_module_names) | {"__future__"}


def test_the_module_does_not_depend_on_pandas_streamlit_sqlalchemy_the_dashboard_or_the_collector():
    for imported in _imported_modules():
        for forbidden in ("pandas", "streamlit", "sqlalchemy", "reject_logic", "collector", "agent", "data_loader",
                          "dashboard_context", "mixed_era"):
            assert forbidden not in imported, imported

    # Nothing is imported lazily or by name either.
    code = inspect.getsource(reject_reason).split('"""', 2)[2]
    for forbidden in ("import_module", "__import__", "pd.", "st."):
        assert forbidden not in code, forbidden


def test_the_module_holds_only_the_approved_public_names():
    public = {name for name in vars(reject_reason) if not name.startswith("_")}

    assert public == {"REJECT_REASONS", "RejectReason", "classify_legacy_reject_message", "reason_for_error_class",
                      "annotations", "Literal", "cast"}
    assert list(inspect.signature(classify_legacy_reject_message).parameters) == ["message"]
    assert list(inspect.signature(reason_for_error_class).parameters) == ["error_class"]
