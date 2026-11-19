"""Collector 1.0.16: CV-aware hold states and the CY hold patron, through the real transform (docs/hold-count-accuracy.md, phase C).

`101YNY` is a check-in with an ALERT; CV01/CV02 (or no CV) is a hold, CV04 a transit (`non_hold_101`), any other CV an `unknown_alert`. The
hold patron is CY, else AA; its cached profile classifies the hold, and its late profile corrects it. Nothing about CV, CY or AA leaves the
machine. Every identifier, name and title is synthetic.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from collector_v2_support import NAME_ADULT, PATRON_ADULT, acs_line, checkin_line, item_message, make_context, naive, patron_message

from collector import v2_identity
from collector import v2_transform as tf

BASE = naive(2026, 11, 18, 11, 0, 0)
ITEM = "CANARY-CV-ITEM-1"
CY_PATRON = "CANARY-CY-PATRON-7"
CY_ILL = "CANARY-CY-BORROWING-LIB"
AA_ILL = "CANARY-AA-BORROWING-LIB"
WESTSIDE = "Westside"
LIBRARY_EXPRESS = "Library Express"
HOLD_FIELDS = {"state", "event_key", "event_time", "item_key", "destination", "is_ill", "is_branch_services", "is_collection_services",
               "ruleset_id"}
NON_HOLD_FIELDS = {"state", "event_key", "event_time", "item_key"}


@pytest.fixture
def ctx(tmp_path):
    context, cache = make_context(tmp_path)
    yield context
    cache.close()


def run(lines, context):
    events, counters = tf.transform_lines("acs", lines, context)
    context.cache.commit()
    return events, counters


def at(seconds: int):
    return BASE + timedelta(seconds=seconds)


def item(seconds, *, cv=None, cy=None, aa=None, destination="", prefix="101YNY", barcode=ITEM, title="CANARY-TITLE"):
    extra = (f"|CV{cv}" if cv is not None else "") + (f"|CY{cy}" if cy is not None else "")
    return acs_line(at(seconds), item_message(barcode, aa, destination, prefix=prefix, extra=extra, title=title))


def profile(seconds, patron, kind="ADULT", name=NAME_ADULT):
    return acs_line(at(seconds), patron_message(patron, name, kind))


def item_key(ctx, barcode=ITEM):
    return v2_identity.item_key(ctx.keys, barcode)


# =====================================================================================================================
# States
# =====================================================================================================================

@pytest.mark.parametrize(("cv", "destination", "state", "slug"), [
    ("01", "", "hold", "unknown"),                       # Main hold: no CT
    ("02", WESTSIDE, "hold", "westside"),
    ("02", LIBRARY_EXPRESS, "hold", "library_express"),
    (None, "", "hold", "unknown"),                       # no CV: the approved fallback
    ("04", WESTSIDE, "non_hold_101", None),              # transit
    ("04", "", "non_hold_101", None),
    ("00", "", "unknown_alert", None),
    ("03", WESTSIDE, "unknown_alert", None),
    ("99", "", "unknown_alert", None),
])
def test_each_alert_type_becomes_its_state_and_shape(ctx, cv, destination, state, slug):
    (event,), counters = run([item(0, cv=cv, cy=CY_PATRON, destination=destination)], ctx)

    assert event.state == state and event.destination == slug
    assert set(event.payload()) == (HOLD_FIELDS if state == "hold" else NON_HOLD_FIELDS)
    assert counters.acs_states[state] == 1


@pytest.mark.parametrize(("prefix", "state"), [("101YNN", "non_hold_101"), ("101NNY", "non_hold_101"), ("100NNY", "other_code10")])
def test_non_alert_records_keep_their_states_whatever_their_cv(ctx, prefix, state):
    (event,), _ = run([item(0, cv="99", prefix=prefix)], ctx)

    assert event.state == state


def test_a_hold_then_a_transit_of_the_same_item_retracts_it_and_clears_the_ledger(ctx):
    events, _ = run([profile(0, CY_PATRON), item(1, cv="02", cy=CY_PATRON, destination=WESTSIDE), item(2, cv="04", destination=WESTSIDE)], ctx)

    assert [e.state for e in events] == ["hold", "non_hold_101"]
    assert events[1].event_time > events[0].event_time
    assert ctx.cache.hold_get(item_key(ctx)) is None
    # A later profile for the (former) hold patron therefore corrects nothing.
    corrections, counters = run([profile(10, CY_PATRON, kind="ILL")], ctx)
    assert corrections == [] and counters.acs_corrections == 0


def test_a_transit_then_a_hold_of_the_same_item_is_a_hold_with_a_ledger_row(ctx):
    events, _ = run([item(1, cv="04", destination=WESTSIDE), item(2, cv="02", cy=CY_PATRON, destination=WESTSIDE)], ctx)

    assert [e.state for e in events] == ["non_hold_101", "hold"]
    row = ctx.cache.hold_get(item_key(ctx))
    assert row is not None and row.patron_pk == v2_identity.patron_id_hmac(ctx.keys, CY_PATRON)


def test_an_unknown_alert_after_a_hold_retracts_it_and_a_hold_after_an_unknown_alert_stands(ctx):
    events, _ = run([item(1, cv="01", cy=CY_PATRON), item(2, cv="03")], ctx)
    assert [e.state for e in events] == ["hold", "unknown_alert"]
    assert ctx.cache.hold_get(item_key(ctx)) is None

    events, _ = run([item(3, cv="03"), item(4, cv="01", cy=CY_PATRON)], ctx)
    assert [e.state for e in events] == ["unknown_alert", "hold"]
    assert ctx.cache.hold_get(item_key(ctx)) is not None


def test_an_earlier_unknown_alert_read_late_does_not_retract_a_later_hold(ctx):
    run([item(5, cv="01", cy=CY_PATRON)], ctx)
    run([item(4, cv="03")], ctx)

    assert ctx.cache.hold_get(item_key(ctx)) is not None


# =====================================================================================================================
# The hold patron: CY, else AA
# =====================================================================================================================

def test_the_hold_patron_cy_decides_ill_not_the_item_record_patron_aa(ctx):
    events, _ = run([profile(0, AA_ILL, kind="ILL"), profile(0, CY_PATRON),
                     item(1, cv="01", aa=AA_ILL, cy=CY_PATRON, barcode="CANARY-CV-ITEM-A"),
                     profile(2, CY_ILL, kind="ILL"),
                     item(3, cv="01", aa=CY_PATRON, cy=CY_ILL, barcode="CANARY-CV-ITEM-B")], ctx)

    assert [(e.state, e.is_ill) for e in events] == [("hold", False), ("hold", True)]


def test_an_ill_hold_patron_with_no_aa_is_ill(ctx):
    events, _ = run([profile(0, CY_ILL, kind="ILL"), item(1, cv="01", cy=CY_ILL)], ctx)

    assert [(e.state, e.is_ill) for e in events] == [("hold", True)]


def test_without_cy_the_item_record_patron_aa_is_the_fallback(ctx):
    events, _ = run([profile(0, AA_ILL, kind="ILL"), item(1, cv="01", aa=AA_ILL)], ctx)

    assert [(e.state, e.is_ill) for e in events] == [("hold", True)]


def test_a_title_with_the_word_ill_does_not_make_a_hold_ill(ctx):
    events, _ = run([profile(0, CY_PATRON), item(1, cv="01", cy=CY_PATRON, title="The Ill-Made Knight")], ctx)

    assert [(e.state, e.is_ill) for e in events] == [("hold", False)]


# =====================================================================================================================
# Late profiles, restart and re-reading
# =====================================================================================================================

def test_a_late_profile_of_the_hold_patron_corrects_the_hold_and_a_late_profile_of_aa_does_not(ctx):
    (hold,), _ = run([item(1, cv="01", aa=AA_ILL, cy=CY_PATRON)], ctx)
    assert hold.is_ill is False

    nothing, _ = run([profile(10, AA_ILL, kind="ILL")], ctx)                       # the item record's patron: irrelevant
    assert nothing == []

    (correction,), counters = run([profile(20, CY_PATRON, kind="ILL")], ctx)      # the hold patron: corrects
    assert (correction.state, correction.is_ill, correction.item_key) == ("hold", True, hold.item_key)
    assert correction.event_time == hold.event_time and correction.event_key != hold.event_key
    assert counters.acs_corrections == 1


def test_the_ledger_keyed_by_the_hold_patron_survives_a_restart(tmp_path):
    first, cache = make_context(tmp_path)
    (hold,), _ = run([item(1, cv="02", cy=CY_PATRON, aa=AA_ILL, destination=WESTSIDE)], first)
    cache.close()

    second, cache = make_context(tmp_path)                                         # a new run: the same files, a fresh process
    try:
        (correction,), _ = run([profile(30, CY_PATRON, kind="ILL")], second)
    finally:
        cache.close()

    assert (correction.item_key, correction.event_time, correction.is_ill, correction.destination) == (
        hold.item_key, hold.event_time, True, "westside")


def test_reading_the_same_records_again_gives_the_same_event_keys(tmp_path):
    lines = [profile(0, CY_PATRON), item(1, cv="01", cy=CY_PATRON), item(2, cv="04", barcode="CANARY-CV-ITEM-2"),
             item(3, cv="03", barcode="CANARY-CV-ITEM-3")]
    first, cache = make_context(tmp_path)
    once, _ = run(lines, first)
    cache.close()
    second, cache = make_context(tmp_path)                                         # recovery: the cursor rewound, the lines re-read
    try:
        again, _ = run(lines, second)
    finally:
        cache.close()

    assert [(e.state, e.event_key) for e in again] == [(e.state, e.event_key) for e in once]


# =====================================================================================================================
# Privacy
# =====================================================================================================================

def test_no_payload_carries_cv_cy_aa_a_name_a_title_or_a_raw_message(ctx):
    lines = [profile(0, CY_ILL, kind="ILL", name="CANARY-HOLD-PATRON-NAME"), profile(0, AA_ILL, kind="ILL"),
             item(1, cv="01", cy=CY_ILL, aa=AA_ILL, title="CANARY-SECRET-TITLE"),
             item(2, cv="02", cy=CY_PATRON, destination=WESTSIDE, barcode="CANARY-CV-ITEM-2"),
             item(3, cv="04", aa=AA_ILL, destination=WESTSIDE, barcode="CANARY-CV-ITEM-3"),
             item(4, cv="03", cy=CY_PATRON, barcode="CANARY-CV-ITEM-4")]
    events, counters = run(lines, ctx)

    blob = json.dumps([e.payload() for e in events]) + json.dumps(counters.flat())
    for secret in (CY_ILL, CY_PATRON, AA_ILL, "CANARY-HOLD-PATRON-NAME", "CANARY-SECRET-TITLE", ITEM, "CANARY-CV-ITEM",
                   "|CV", "CV01", "CV04", "CY", "101YNY", WESTSIDE):
        assert secret not in blob, secret
    assert {e.state for e in events} == {"hold", "non_hold_101", "unknown_alert"}


def test_a_cy_seen_on_an_item_record_is_known_to_the_patron_card_guard(ctx):
    run([item(1, cv="01", cy=CY_PATRON)], ctx)

    events, counters = tf.transform_lines("checkins", [checkin_line(at(2), CY_PATRON)], ctx)

    assert events == [] and counters.dropped_patron_card == 1


# =====================================================================================================================
# Backward compatibility with the server (phase B)
# =====================================================================================================================

def test_every_state_the_collector_emits_is_accepted_by_the_server_and_so_is_an_older_collectors_payload(ctx):
    from collector_v2_support import KEY_ID

    from src.services.ingest_v2_models import UploadV2Request

    lines = [profile(0, CY_PATRON), item(1, cv="01", cy=CY_PATRON), item(2, cv="04", barcode="CANARY-CV-ITEM-2"),
             item(3, cv="03", barcode="CANARY-CV-ITEM-3"), item(4, prefix="100NNY", barcode="CANARY-CV-ITEM-4")]
    events, _ = run(lines, ctx)
    body = {"contract_version": 2, "key_id": KEY_ID, "acs_items": [e.payload() for e in events]}

    accepted = UploadV2Request.model_validate(body)
    assert [e.state for e in accepted.acs_items] == ["hold", "non_hold_101", "unknown_alert", "other_code10"]

    # What 1.0.13 / 1.0.15 send: the same shapes, never unknown_alert.
    older = {**body, "acs_items": [e.payload() for e in events if e.state != "unknown_alert"]}
    assert len(UploadV2Request.model_validate(older).acs_items) == 3
