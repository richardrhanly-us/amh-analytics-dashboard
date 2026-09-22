"""Contract v2 collector: the transformation layer (collector/v2_transform.py and the raw modules only it imports).

Covers ACS state mapping, classifier PARITY with the dashboard's classifier, destination/bin normalization, the reject mapping, time zone and
DST handling, the patron-card guard, and the invariance of event_key under changes to prohibited fields.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from collector_v2_support import (
    BARCODE_CI,
    BARCODE_HOLD,
    BARCODE_LONG,
    BARCODE_NON_HOLD,
    BARCODE_OTHER10,
    BARCODE_REJ,
    MASTER,
    NAME_ADULT,
    NAME_COLL,
    NAME_PROG,
    PATRON_ADULT,
    PATRON_CARD,
    PATRON_COLL,
    PATRON_PROG,
    PATTERN_COLL,
    RULES_DOC,
    ZONE,
    acs_line,
    checkin_line,
    full_corpus,
    item_message,
    make_context,
    minutes_ago,
    naive,
    patron_message,
    reject_line,
)

import metrics
from agent.parser import acs as acs_parser
from collector import v2_events as ev
from collector import v2_identity as ident
from collector import v2_transform as tf

BASE = minutes_ago(60)  # a naive local time an hour ago, from the real clock


@pytest.fixture
def ctx(tmp_path):
    context, cache = make_context(tmp_path)
    yield context
    cache.close()


def run(source, lines, context):
    events, counters = tf.transform_lines(source, lines, context)
    context.cache.commit()
    return events, counters


def by_state(events):
    return {e.state: e for e in events}


# =====================================================================================================================
# ACS state mapping and message-64 handling
# =====================================================================================================================

def test_acs_codes_map_to_hold_non_hold_101_and_other_code10_and_message_64_makes_no_event(ctx):
    lines = [
        acs_line(BASE, patron_message(PATRON_ADULT, NAME_ADULT, "ADULT")),
        acs_line(BASE + timedelta(seconds=1), item_message(BARCODE_HOLD, PATRON_ADULT, "Main")),
        acs_line(BASE + timedelta(seconds=2), item_message(BARCODE_NON_HOLD, PATRON_ADULT, "Main", prefix="101NNY")),
        acs_line(BASE + timedelta(seconds=3), item_message(BARCODE_OTHER10, PATRON_ADULT, "Main", prefix="100NUN")),
    ]
    events, counters = run("acs", lines, ctx)
    assert sorted(e.state for e in events) == ["hold", "non_hold_101", "other_code10"]
    assert counters.patron_records == 1
    assert counters.events["acs_items"] == 3 and counters.acs_states == {"hold": 1, "non_hold_101": 1, "other_code10": 1}
    assert all(isinstance(e, ev.AcsItemV2) for e in events)          # nothing is emitted for the message-64 record


@pytest.mark.parametrize("prefix", ["101YNY", "101YNY-anything", "101NNY", "101YYN", "1010", "101"])
def test_every_101_prefix_other_than_101yny_is_non_hold(ctx, prefix):
    events, _ = run("acs", [acs_line(BASE, item_message(BARCODE_HOLD, None, "Main", prefix=prefix))], ctx)
    assert [e.state for e in events] == (["hold"] if prefix.startswith("101YNY") else ["non_hold_101"])


@pytest.mark.parametrize("prefix", ["100NUN", "102", "10", "100"])
def test_other_code_10_records_are_other_code10(ctx, prefix):
    events, _ = run("acs", [acs_line(BASE, item_message(BARCODE_HOLD, None, "Main", prefix=prefix))], ctx)
    assert [e.state for e in events] == ["other_code10"]


def test_records_that_are_neither_10_nor_64_produce_nothing(ctx):
    events, counters = run("acs", [acs_line(BASE, "98|AB" + BARCODE_HOLD), acs_line(BASE, "24|AA" + PATRON_ADULT)], ctx)
    assert events == [] and counters.records_parsed == 2


def test_a_non_hold_carries_no_destination_flags_or_ruleset(ctx):
    events, _ = run("acs", [acs_line(BASE, item_message(BARCODE_NON_HOLD, PATRON_ADULT, "Westside", prefix="101NNY"))], ctx)
    (event,) = events
    assert event.payload().keys() == {"state", "event_key", "event_time", "item_key"}
    assert (event.destination, event.is_ill, event.is_branch_services, event.is_collection_services, event.ruleset_id) == (None,) * 5


def test_a_hold_carries_the_run_ruleset_id_and_its_classification(ctx):
    events, _ = run("acs", [acs_line(BASE, item_message(BARCODE_HOLD, None, "Westside"))], ctx)
    (event,) = events
    assert event.ruleset_id == ctx.ruleset_id and event.destination == "westside"
    assert (event.is_ill, event.is_branch_services, event.is_collection_services) == (False, False, False)


def test_an_acs_item_without_a_barcode_is_dropped_and_counted(ctx):
    events, counters = run("acs", [acs_line(BASE, "101YNY|AB|AJx|AAp|CTMain")], ctx)
    assert events == [] and counters.dropped_missing_barcode == 1


def test_a_record_with_an_unreadable_time_is_dropped_and_counted(ctx):
    bad = "13/45/2026\x0299:99:99 XM\x02" + item_message(BARCODE_HOLD, None, "Main") + "\n"
    events, counters = run("acs", [bad], ctx)
    assert events == [] and counters.dropped_bad_time == 1


def test_patron_profile_from_a_later_message_64_in_the_same_chunk_applies_like_the_dashboard(ctx):
    lines = [acs_line(BASE, item_message(BARCODE_HOLD, PATRON_PROG, "Main")),
             acs_line(BASE + timedelta(seconds=5), patron_message(PATRON_PROG, NAME_PROG, "STAFF"))]
    (event,), _ = run("acs", lines, ctx)
    assert event.is_branch_services is True


def test_the_profile_persists_in_the_cache_for_a_later_chunk(ctx):
    run("acs", [acs_line(BASE, patron_message(PATRON_COLL, NAME_COLL, "STAFF"))], ctx)
    (event,), _ = run("acs", [acs_line(BASE + timedelta(seconds=9), item_message(BARCODE_HOLD, PATRON_COLL, "Main"))], ctx)
    assert event.is_collection_services is True


# =====================================================================================================================
# Classifier parity with src/metrics.py::build_acs_item_summary
# =====================================================================================================================

PARITY_RULES = {
    **RULES_DOC,
    "branch_services_da_patterns": ["DABRANCH PATTERN"],
}


def _dashboard_flags(lines, rules):
    frame = acs_parser.parse_lines(lines)
    summary = metrics.build_acs_item_summary(
        frame, ["Westside", "Library Express"], rules["branch_services_names"], rules["collection_services_names"],
        rules["branch_services_da_patterns"], rules["collection_services_da_patterns"])
    return {"ill": summary["ill_total"], "programming": summary["programming_total"],
            "collection": summary["collection_services_total"], "public": summary["holds_total"]}


PARITY_SCENARIOS = {
    "public adult": {"name": "CANARY-NAME ONE", "ptype": "ADULT", "dest": "Main", "prefix": "101YNY"},
    "patron type ILL": {"name": "CANARY-NAME TWO", "ptype": "ILL", "dest": "Main", "prefix": "101YNY"},
    "patron type lower-case ill": {"name": "CANARY-NAME TWO", "ptype": " ill ", "dest": "Main", "prefix": "101YNY"},
    "name has INTERLIBRARY": {"name": "Interlibrary Loan Desk", "ptype": "STAFF", "dest": "Main", "prefix": "101YNY"},
    "name has whole word ILL": {"name": "the ILL office", "ptype": "STAFF", "dest": "Main", "prefix": "101YNY"},
    "name has ILL only inside a word": {"name": "SKILLED WORKER", "ptype": "ADULT", "dest": "Main", "prefix": "101YNY"},
    "destination has ILL": {"name": "CANARY-NAME ONE", "ptype": "ADULT", "dest": "ILL Westside", "prefix": "101YNY"},
    "destination has interlibrary": {"name": "CANARY-NAME ONE", "ptype": "ADULT", "dest": "interlibrary loan", "prefix": "101YNY"},
    "TITLE quirk: a title containing the word Ill marks ILL": {"name": "CANARY-NAME ONE", "ptype": "ADULT", "dest": "Main",
                                                                   "prefix": "101YNY", "title": "The Ill-Made Knight"},
    "title with a word merely containing ill": {"name": "CANARY-NAME ONE", "ptype": "ADULT", "dest": "Main", "prefix": "101YNY",
                                                    "title": "Skillful Bills"},
    "raw |DAILL": {"name": "CANARY-NAME ONE", "ptype": "ADULT", "dest": "Main", "prefix": "101YNY", "extra": "|DAILL"},
    "raw |AEILL": {"name": "CANARY-NAME ONE", "ptype": "ADULT", "dest": "Main", "prefix": "101YNY", "extra": "|AEILL"},
    "raw |PTILL": {"name": "CANARY-NAME ONE", "ptype": "ADULT", "dest": "Main", "prefix": "101YNY", "extra": "|PTILL"},
    "programming name": {"name": NAME_PROG, "ptype": "STAFF", "dest": "Main", "prefix": "101YNY"},
    "programming name with odd case and spaces": {"name": "  canary programming account ", "ptype": "STAFF", "dest": "Main", "prefix": "101YNY"},
    "collection services name": {"name": NAME_COLL, "ptype": "STAFF", "dest": "Main", "prefix": "101YNY"},
    "collection DA pattern": {"name": "CANARY-NAME ONE", "ptype": "ADULT", "dest": "Main", "prefix": "101YNY", "extra": f"|{PATTERN_COLL}|"},
    "branch DA pattern": {"name": "CANARY-NAME ONE", "ptype": "ADULT", "dest": "Main", "prefix": "101YNY", "extra": "|DABRANCH PATTERN|"},
    "DA pattern without the closing delimiter is not a match": {"name": "CANARY-NAME ONE", "ptype": "ADULT", "dest": "Main", "prefix": "101YNY",
                                                                     "extra": f"|{PATTERN_COLL}"},
    "ILL and collection services together": {"name": NAME_COLL, "ptype": "ILL", "dest": "Main", "prefix": "101YNY"},
    "no message-64 for the patron": {"name": None, "ptype": None, "dest": "Main", "prefix": "101YNY"},
}


@pytest.mark.parametrize("scenario", PARITY_SCENARIOS, ids=list(PARITY_SCENARIOS))
def test_hold_classification_matches_the_dashboard_classifier(tmp_path, scenario):
    spec = PARITY_SCENARIOS[scenario]
    context, cache = make_context(tmp_path, rules_doc=PARITY_RULES)
    try:
        lines = []
        if spec["name"] is not None:
            lines.append(acs_line(BASE, patron_message(PATRON_ADULT, spec["name"], spec["ptype"])))
        lines.append(acs_line(BASE + timedelta(seconds=1), item_message(
            BARCODE_HOLD, PATRON_ADULT, spec["dest"], prefix=spec["prefix"], extra=spec.get("extra", ""),
            title=spec.get("title", "CANARY-TITLE"))))
        (event,), _ = run("acs", lines, context)
        theirs = _dashboard_flags(lines, PARITY_RULES)
    finally:
        cache.close()
    assert event.state == "hold"
    assert (event.is_ill, event.is_branch_services, event.is_collection_services) == (
        bool(theirs["ill"]), bool(theirs["programming"]), bool(theirs["collection"]))
    # the dashboard's "public hold" is a hold that is none of the three
    assert theirs["public"] == int(not (event.is_ill or event.is_branch_services or event.is_collection_services))


def test_the_ill_title_quirk_is_reproduced_on_purpose(ctx):
    lines = [acs_line(BASE, patron_message(PATRON_ADULT, NAME_ADULT, "ADULT")),
             acs_line(BASE + timedelta(seconds=1), item_message(BARCODE_HOLD, PATRON_ADULT, "Main", title="The Ill-Made Knight"))]
    (event,), _ = run("acs", lines, ctx)
    assert event.is_ill is True


def test_non_hold_records_are_never_classified(ctx):
    lines = [acs_line(BASE, patron_message(PATRON_PROG, NAME_PROG, "ILL")),
             acs_line(BASE + timedelta(seconds=1), item_message(BARCODE_NON_HOLD, PATRON_PROG, "Main", prefix="101NNY"))]
    (event,), _ = run("acs", lines, ctx)
    assert event.state == "non_hold_101" and event.is_ill is None


# =====================================================================================================================
# Destination and bin normalization
# =====================================================================================================================

@pytest.mark.parametrize(("raw", "slug"), [
    ("1", "main"), ("Main", "main"), ("LOCAL", "main"), (" local ", "main"),
    ("Westside", "westside"), ("WESTSIDE BRANCH", "westside"), ("Library Express", "library_express"),
    ("No Agency Destination", "no_agency_destination"), ("", "unknown"), ("Some Unmapped Branch", "unknown"),
    ("CANARY-UNMAPPED-DEST", "unknown"), ("12345", "unknown"),
])
def test_checkin_destination_normalization(ctx, raw, slug):
    (event,), counters = run("checkins", [checkin_line(BASE, BARCODE_CI, raw, "3")], ctx)
    assert event.destination == slug
    assert counters.unknown_destination == int(slug == "unknown")


def test_an_unmapped_destination_is_unknown_never_a_pseudonymous_label(ctx):
    events, _ = run("checkins", [checkin_line(BASE + timedelta(seconds=i), BARCODE_CI, f"Branch Number {i}") for i in range(5)], ctx)
    assert {e.destination for e in events} == {"unknown"}


def test_local_rules_can_add_a_destination_slug(tmp_path):
    rules = {**RULES_DOC, "destinations": [{"slug": "west_annex", "contains": "west annex"}]}
    context, cache = make_context(tmp_path, rules_doc=rules)
    try:
        (event,), _ = run("checkins", [checkin_line(BASE, BARCODE_CI, "The West Annex Branch")], context)
    finally:
        cache.close()
    assert event.destination == "west_annex"


@pytest.mark.parametrize(("raw", "expected"), [("3", "3"), ("0", "0"), ("12", "12"), ("9999", "9999"), ("", "unknown"), ("abc", "unknown"),
                                                ("3A", "unknown"), ("12345", "unknown"), ("-1", "unknown"), (" 7 ", "7"), ("٣", "unknown")])
def test_bin_is_a_numeric_code_or_unknown(ctx, raw, expected):
    (event,), counters = run("checkins", [checkin_line(BASE, BARCODE_CI, "1", raw)], ctx)
    assert event.bin == expected and counters.unknown_bin == int(expected == "unknown")


# =====================================================================================================================
# Reject mapping
# =====================================================================================================================

@pytest.mark.parametrize(("text", "expected"), [
    ("Item not found in CANARY database", "item_not_found"),
    ("ITEM NOT FOUND", "item_not_found"),
    ("no item found for CANARY", "item_not_found"),
    ("ACS returned failure", "ils_acs_failure"),
    ("Multiple RFID tags detected", "rfid_collision"),
    ("multiple tags", "rfid_collision"),
    ("Invalid collection code", "configuration_error"),
    ("Library not found", "routing_error"),
    ("Something never seen before", "other"),
    ("", "unknown"),
    ("communication error while talking to the ILS", "other"),      # communication_error is NEVER inferred from text
    ("connection timed out", "other"),
    ("network failure", "other"),
    ("item not found AND acs down", "item_not_found"),              # the dashboard's precedence
])
def test_reject_text_maps_to_the_closed_enum(ctx, text, expected):
    (event,), _ = run("rejects", [reject_line(BASE, BARCODE_REJ, text)], ctx)
    assert event.error_class == expected and event.error_class in ev.ERROR_CLASSES


def test_no_text_can_ever_produce_communication_error(ctx):
    texts = ["communication error", "COMM ERROR", "timeout", "socket closed", "no response", "unable to communicate", "comm failure"]
    events, _ = run("rejects", [reject_line(BASE + timedelta(seconds=i), BARCODE_REJ, t) for i, t in enumerate(texts)], ctx)
    assert "communication_error" not in {e.error_class for e in events}


def test_reject_parity_with_the_dashboards_simplified_categories(ctx):
    from agent.parser.rejects import simplify_error_message
    table = {"Item Not Found": "item_not_found", "ILS / ACS Failure": "ils_acs_failure", "RFID Collision": "rfid_collision",
             "Call Number / Config Error": "configuration_error", "Routing Error": "routing_error", "Other": "other"}
    for text in ("item not found", "acs boom", "multiple rfid", "collection code bad", "library not found", "zzz"):
        (event,), _ = run("rejects", [reject_line(BASE, BARCODE_REJ, text)], ctx)
        assert table[simplify_error_message(text)] == event.error_class, text


def test_a_reject_without_a_barcode_has_no_item_key(ctx):
    (event,), _ = run("rejects", [reject_line(BASE, "", "item not found")], ctx)
    assert event.item_key is None and "item_key" not in event.payload()


# =====================================================================================================================
# Time zones and DST
# =====================================================================================================================

def test_naive_local_times_are_converted_with_the_configured_zone(tmp_path):
    local = naive(2025, 7, 4, 9, 30, 15)                          # CDT: UTC-5
    for zone, expected in (("America/Chicago", "2025-07-04T14:30:15Z"), ("America/New_York", "2025-07-04T13:30:15Z"),
                           ("UTC", "2025-07-04T09:30:15Z"), ("Asia/Kolkata", "2025-07-04T04:00:15Z")):
        context, cache = make_context(tmp_path / zone.replace("/", "_"), zone=zone)
        try:
            (event,), _ = run("checkins", [checkin_line(local, BARCODE_CI)], context)
        finally:
            cache.close()
        assert ev.format_time(event.event_time) == expected, zone


def test_winter_and_summer_offsets_differ(ctx):
    (winter,), _ = run("checkins", [checkin_line(naive(2025, 1, 15, 12, 0, 0), BARCODE_CI)], ctx)
    (summer,), _ = run("checkins", [checkin_line(naive(2025, 7, 15, 12, 0, 0), BARCODE_CI)], ctx)
    assert ev.format_time(winter.event_time) == "2025-01-15T18:00:00Z" and ev.format_time(summer.event_time) == "2025-07-15T17:00:00Z"


def test_fall_back_ambiguous_time_uses_the_first_occurrence_and_is_counted(ctx):
    ambiguous = naive(2025, 11, 2, 1, 30, 0)                      # 01:30 happens twice in Chicago
    (event,), counters = run("checkins", [checkin_line(ambiguous, BARCODE_CI)], ctx)
    assert ev.format_time(event.event_time) == "2025-11-02T06:30:00Z"  # first occurrence: still CDT (UTC-5)
    assert counters.dst_ambiguous == 1 and counters.dst_nonexistent == 0


def test_spring_forward_nonexistent_time_is_counted_not_dropped(ctx):
    missing = naive(2025, 3, 9, 2, 30, 0)                         # 02:30 never happens in Chicago
    (event,), counters = run("checkins", [checkin_line(missing, BARCODE_CI)], ctx)
    assert counters.dst_nonexistent == 1 and counters.dst_ambiguous == 0 and event.event_time.tzinfo is not None


def test_to_utc_directly_for_ordinary_ambiguous_and_missing_times():
    zone = ZoneInfo(ZONE)
    assert tf.to_utc(naive(2025, 6, 1, 12, 0), zone) == (datetime(2025, 6, 1, 17, 0, tzinfo=UTC), False, False)
    assert tf.to_utc(naive(2025, 11, 2, 1, 30), zone)[1:] == (True, False)
    assert tf.to_utc(naive(2025, 11, 2, 1, 30), zone)[0] == datetime(2025, 11, 2, 6, 30, tzinfo=UTC)
    assert tf.to_utc(naive(2025, 3, 9, 2, 30), zone)[1:] == (False, True)
    # the hour after the fall-back is not ambiguous
    assert tf.to_utc(naive(2025, 11, 2, 2, 30), zone)[1:] == (False, False)


def test_the_two_ambiguous_scans_share_the_first_occurrence_so_the_dst_hour_never_gets_two_offsets(ctx):
    a = naive(2025, 11, 2, 1, 10, 0)
    b = naive(2025, 11, 2, 1, 50, 0)
    events, counters = run("checkins", [checkin_line(a, BARCODE_CI), checkin_line(b, BARCODE_LONG)], ctx)
    assert counters.dst_ambiguous == 2
    assert events[1].event_time - events[0].event_time == timedelta(minutes=40)


def test_times_before_2000_and_far_in_the_future_are_dropped_and_counted(ctx):
    old = naive(1999, 12, 31, 12, 0, 0)
    future = (datetime.now(UTC) + timedelta(days=30)).astimezone(ZoneInfo(ZONE)).replace(tzinfo=None, microsecond=0)
    events, counters = run("checkins", [checkin_line(old, BARCODE_CI), checkin_line(future, BARCODE_LONG)], ctx)
    assert events == [] and counters.dropped_out_of_range_time == 2


def test_event_times_are_whole_seconds_in_utc(ctx):
    (event,), _ = run("checkins", [checkin_line(BASE, BARCODE_CI)], ctx)
    assert event.event_time.utcoffset() == timedelta(0) and event.event_time.microsecond == 0


# =====================================================================================================================
# The patron-card guard
# =====================================================================================================================

def test_a_known_patron_card_is_dropped_from_checkins_rejects_and_acs_items(ctx):
    run("acs", [acs_line(BASE, patron_message(PATRON_CARD, "CANARY-NAME CARD", "ADULT"))], ctx)
    ci, ci_counters = run("checkins", [checkin_line(BASE + timedelta(seconds=1), PATRON_CARD)], ctx)
    rj, rj_counters = run("rejects", [reject_line(BASE + timedelta(seconds=2), PATRON_CARD)], ctx)
    ac, ac_counters = run("acs", [acs_line(BASE + timedelta(seconds=3), item_message(PATRON_CARD, None, "Main"))], ctx)
    assert ci == rj == ac == []
    assert (ci_counters.dropped_patron_card, rj_counters.dropped_patron_card, ac_counters.dropped_patron_card) == (1, 1, 1)


def test_a_14_digit_item_barcode_that_looks_like_a_card_is_not_blocked_without_a_cache_match(ctx):
    run("acs", [acs_line(BASE, patron_message(PATRON_CARD, "CANARY-NAME CARD", "ADULT"))], ctx)
    events, counters = run("checkins", [checkin_line(BASE + timedelta(seconds=1), BARCODE_LONG),
                                        checkin_line(BASE + timedelta(seconds=2), "21234000123456")], ctx)
    assert len(events) == 2 and counters.dropped_patron_card == 0


def test_the_guard_never_decides_by_length_or_pattern(ctx):
    """No cache entry, so nothing is a card: not a 14-digit number, not a P-prefixed one, not one starting like a library card."""
    barcodes = ["12345678901234", "P2300000000003", "2" * 14, "29999123456789", "A12345"]
    events, counters = run("checkins", [checkin_line(BASE + timedelta(seconds=i), b) for i, b in enumerate(barcodes)], ctx)
    assert len(events) == len(barcodes) and counters.dropped_patron_card == 0


def test_a_patron_identifier_quoted_by_an_acs_message_is_a_guarded_card_afterward(ctx):
    # the patron id appears only as the AA field of an item record: still a "sighting", so a scan of that string is a card
    run("acs", [acs_line(BASE, item_message(BARCODE_HOLD, "CANARY-PATRON-SEEN-ONLY", "Main"))], ctx)
    events, counters = run("checkins", [checkin_line(BASE + timedelta(seconds=1), "CANARY-PATRON-SEEN-ONLY")], ctx)
    assert events == [] and counters.dropped_patron_card == 1


def test_the_guard_is_keyed_so_another_master_secret_does_not_recognize_the_card(tmp_path):
    context, cache = make_context(tmp_path / "a")
    run("acs", [acs_line(BASE, patron_message(PATRON_CARD, "CANARY-NAME CARD", "ADULT"))], context)
    other, other_cache = make_context(tmp_path / "b", master=bytes(reversed(MASTER)))
    try:
        # the SAME cache file read with a different master: nothing matches, nothing crashes
        other.cache.close()
        other_cache.path.write_bytes(cache.path.read_bytes())
        reopened, reopened_cache = make_context(tmp_path / "b", master=bytes(reversed(MASTER)))
        events, counters = run("checkins", [checkin_line(BASE, PATRON_CARD)], reopened)
        assert len(events) == 1 and counters.dropped_patron_card == 0
        reopened_cache.close()
    finally:
        cache.close()


# =====================================================================================================================
# item_key, missing barcodes, and identical events
# =====================================================================================================================

def test_a_checkin_without_a_barcode_is_kept_with_no_item_key_and_never_a_placeholder(ctx):
    (event,), _ = run("checkins", [checkin_line(BASE, "", "Westside", "2")], ctx)
    assert event.item_key is None and "item_key" not in event.payload()


def test_item_key_is_the_keyed_hmac_of_the_barcode_and_equal_across_kinds(ctx):
    (checkin,), _ = run("checkins", [checkin_line(BASE, BARCODE_CI)], ctx)
    (reject,), _ = run("rejects", [reject_line(BASE, BARCODE_CI)], ctx)
    (hold,), _ = run("acs", [acs_line(BASE, item_message(BARCODE_CI, None, "Main"))], ctx)
    expected = ident.item_key(ctx.keys, BARCODE_CI)
    assert checkin.item_key == reject.item_key == hold.item_key == expected


def test_identical_safe_fields_share_an_event_key_and_are_counted(ctx):
    same = [checkin_line(BASE, BARCODE_CI, "Westside", "3", title="first title"),
            checkin_line(BASE, BARCODE_CI, "Westside", "3", title="second title", call_number="other call number")]
    events, counters = run("checkins", same, ctx)
    assert len(events) == 2 and events[0].event_key == events[1].event_key
    assert counters.identical_identity_events == 1


def test_distinct_items_in_the_same_second_have_distinct_event_keys(ctx):
    events, counters = run("checkins", [checkin_line(BASE, BARCODE_CI), checkin_line(BASE, BARCODE_LONG)], ctx)
    assert events[0].event_key != events[1].event_key and counters.identical_identity_events == 0


# =====================================================================================================================
# event_key invariance under prohibited fields
# =====================================================================================================================

def _keys_of(tmp_path, lines_by_source, master=MASTER):
    context, cache = make_context(tmp_path, master=master)
    try:
        out = {}
        for source in ("acs", "checkins", "rejects"):
            events, _ = run(source, lines_by_source[source], context)
            out[source] = [(e.kind, e.event_key, e.payload()) for e in events]
        return out
    finally:
        cache.close()


def test_changing_only_prohibited_fields_never_changes_any_event_key_or_payload(tmp_path):
    corpus = full_corpus(BASE)
    first = _keys_of(tmp_path / "one", corpus)
    # rebuild EVERY prohibited field differently: titles, call numbers, collections, shelves, messages, flags, addresses, e-mail,
    # patron names/ids that do not change classification, reject wording that maps to the same class, extra SIP2 fields
    changed = full_corpus(BASE)
    changed["acs"] = [line.replace("CANARY-TITLE", "COMPLETELY DIFFERENT TITLE").replace("CANARY-ADDR-1 MAIN ST", "99 OTHER ROAD")
                      .replace("CANARY-MAIL@example.invalid", "someone-else@example.invalid").replace("|AA", "|XX9|AA") for line in corpus["acs"]]
    changed["checkins"] = [line.replace("CANARY-CI-TITLE", "another title").replace("CANARY-CI-CALLNO", "QA76 .X9")
                           .replace("CANARY-CI-COLLECTION", "coll-2").replace("CANARY-CI-SHELF", "shelf-9")
                           .replace("CANARY-CI-MESSAGE", "different free text").replace("CANARY-CI-FLAG", "other") for line in corpus["checkins"]]
    changed["rejects"] = [line.replace("CANARY-RAW-REJECT item not found CANARY-RAW-DETAIL", "ITEM NOT FOUND: totally different wording")
                          for line in corpus["rejects"]]
    second = _keys_of(tmp_path / "two", changed)
    assert first == second


def test_changing_a_permitted_field_does_change_the_event_key(tmp_path):
    a = _keys_of(tmp_path / "one", full_corpus(BASE))
    b = _keys_of(tmp_path / "two", full_corpus(BASE + timedelta(seconds=1)))
    assert [k for _kind, k, _ in a["checkins"]] != [k for _kind, k, _ in b["checkins"]]


def test_a_different_master_secret_gives_different_keys_for_identical_logs(tmp_path):
    corpus = full_corpus(BASE)
    a = _keys_of(tmp_path / "one", corpus)
    b = _keys_of(tmp_path / "two", corpus, master=bytes(reversed(MASTER)))
    assert {k for _kind, k, _ in a["checkins"]}.isdisjoint({k for _kind, k, _ in b["checkins"]})


def test_the_whole_corpus_is_transformed_with_the_expected_shape(tmp_path):
    keys = _keys_of(tmp_path, full_corpus(BASE))
    assert len(keys["acs"]) == 7 and len(keys["checkins"]) == 3 and len(keys["rejects"]) == 1
    assert {p["state"] for _k, _e, p in keys["acs"]} == {"hold", "non_hold_101", "other_code10"}


# =====================================================================================================================
# robustness
# =====================================================================================================================

def test_blank_short_and_garbage_lines_are_skipped_without_error(ctx):
    events, counters = run("checkins", ["\n", "  \n", "not a record\n", "a|b|c\n", checkin_line(BASE, BARCODE_CI)], ctx)
    assert len(events) == 1 and counters.lines_read == 5


def test_an_unknown_source_is_a_fixed_code_error(ctx):
    from collector.v2_safe_errors import TransformError
    with pytest.raises(TransformError) as caught:
        tf.transform_lines("nope", [], ctx)
    assert caught.value.code == "unknown_source"


def test_an_unexpected_failure_carries_the_type_and_location_but_never_the_message(ctx, monkeypatch):
    from collector.v2_safe_errors import TransformError

    def boom(_lines, _ctx):
        raise ValueError("CANARY-EXCEPTION-TEXT with a barcode CANARY-BARCODE-XYZ")

    monkeypatch.setitem(tf._TRANSFORMS, "checkins", boom)
    with pytest.raises(TransformError) as caught:
        tf.transform_lines("checkins", [checkin_line(BASE, BARCODE_CI)], ctx)
    error = caught.value
    rendered = " ".join([str(error), repr(error), error.code, error.summary, repr(error.args)])
    assert "CANARY" not in rendered and "ValueError" in error.summary
    assert error.__cause__ is None and error.__context__ is None and error.__suppress_context__ is False


def test_pandas_parse_failures_do_not_leak_the_value_through_the_error(ctx, monkeypatch):
    from collector.v2_safe_errors import TransformError

    def boom(_lines):
        return pd.to_datetime(["CANARY-NOT-A-DATE-XYZ"], format="%Y-%m-%d")

    monkeypatch.setattr(acs_parser, "parse_lines", boom)
    with pytest.raises(TransformError) as caught:
        tf.transform_lines("acs", [acs_line(BASE, "64 |AAx")], ctx)
    assert "CANARY" not in " ".join([str(caught.value), caught.value.summary])
    assert caught.value.__context__ is None


def test_parser_loggers_are_silenced_during_a_transform_and_restored_afterward(ctx, tmp_path):
    import logging
    names = ("parser.acs", "parser.checkins", "parser.rejects")
    before = [logging.getLogger(n).disabled for n in names]
    run("checkins", [checkin_line(BASE, BARCODE_CI, UNMAPPED)], ctx)
    assert [logging.getLogger(n).disabled for n in names] == before


UNMAPPED = "CANARY-UNMAPPED-DEST"


def test_the_v1_parsers_never_log_a_destination_label_during_a_v2_transform(ctx, caplog):
    import logging
    with caplog.at_level(logging.DEBUG):
        run("checkins", [checkin_line(BASE, BARCODE_CI, UNMAPPED)], ctx)
        run("rejects", [reject_line(BASE, BARCODE_REJ)], ctx)
        run("acs", [acs_line(BASE, item_message(BARCODE_HOLD, None, "Main"))], ctx)
    assert UNMAPPED not in caplog.text and "CANARY" not in caplog.text


def test_a_chunk_of_many_lines_is_transformed_in_one_call(ctx):
    lines = [checkin_line(BASE + timedelta(seconds=i), BARCODE_CI) for i in range(500)]
    events, counters = run("checkins", lines, ctx)
    assert len(events) == 500 and len({e.event_key for e in events}) == 500
    assert pd.Series([e.event_time for e in events]).is_monotonic_increasing and counters.lines_read == 500


# =====================================================================================================================
# ruleset_id is provenance, not identity
# =====================================================================================================================

def _holds_under(tmp_path, ruleset_id, *, rules_doc=None):
    context, cache = make_context(tmp_path, rules_doc=rules_doc)
    context.ruleset_id = ruleset_id
    try:
        lines = [acs_line(BASE, patron_message(PATRON_PROG, NAME_PROG, "STAFF")),
                 acs_line(BASE + timedelta(seconds=1), item_message(BARCODE_HOLD, PATRON_PROG, "Main"))]
        events, _ = run("acs", lines, context)
    finally:
        cache.close()
    return events


def test_the_same_holds_under_a_different_ruleset_id_have_the_same_event_keys_but_carry_their_own_ruleset_id(tmp_path):
    first = _holds_under(tmp_path / "a", "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d")
    second = _holds_under(tmp_path / "b", "1b2c3d4e-5f6a-4b7c-8d8e-9f0a1b2c3d4e")
    assert [e.event_key for e in first] == [e.event_key for e in second] and first
    assert [e.ruleset_id for e in first] != [e.ruleset_id for e in second]
    assert all(e.payload()["ruleset_id"] == e.ruleset_id for e in first + second)


def test_a_changed_classification_result_gives_a_different_event_key_even_under_the_same_ruleset_id(tmp_path):
    same_ruleset = "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d"
    with_name_listed = _holds_under(tmp_path / "a", same_ruleset)                                    # the staff name is a configured account
    without = _holds_under(tmp_path / "b", same_ruleset, rules_doc={**RULES_DOC, "branch_services_names": []})
    (listed,), (unlisted,) = with_name_listed, without
    assert listed.is_branch_services is True and unlisted.is_branch_services is False
    assert listed.event_key != unlisted.event_key and listed.ruleset_id == unlisted.ruleset_id


def test_a_changed_destination_gives_a_different_event_key(tmp_path):
    context, cache = make_context(tmp_path)
    try:
        (main,), _ = run("acs", [acs_line(BASE, item_message(BARCODE_HOLD, None, "Main"))], context)
        (west,), _ = run("acs", [acs_line(BASE, item_message(BARCODE_HOLD, None, "Westside"))], context)
    finally:
        cache.close()
    assert main.event_key != west.event_key
