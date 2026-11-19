"""Hold-count accuracy regression tests (docs/hold-count-accuracy.md, sections 3 and 7).

The SIP2 `101YNY` response is a check-in with an ALERT; the alert type `CV` says whether it is a hold (CV01 local, CV02
for another destination) or a transit (CV04). The hold patron is `CY`; `AA` is the item record's patron.

These began as strict expected failures, one marker per phase, and each marker was removed when its phase made the
tests pass (phase A, the legacy classifier; phase C, Collector 1.0.16). They now pin the corrected rules on both sides.

Legacy cases go through the dashboard classifier (`src/metrics.py`). Collector cases test the pure functions of
`collector/v2_classify.py`; the transform-level Collector cases are in tests/test_collector_v2_hold_cv.py.

Every barcode, patron id, name and title here is synthetic. Records are shaped like the NBPL tests of 2026-11-18.
"""

from __future__ import annotations

import pandas as pd
import pytest

from collector import v2_classify
from collector.v2_rules import Rules
from metrics import build_acs_item_summary


WESTSIDE = "Westside (Synthetic Branch)"
LIBRARY_EXPRESS = "Library Express"

_clock = iter(range(10_000))


def _when(minute: int | None = None) -> pd.Timestamp:
    base = pd.Timestamp("2026-11-18T11:00:00")
    if minute is not None:
        return base + pd.Timedelta(minutes=minute)
    return base + pd.Timedelta(seconds=next(_clock))


def raw(barcode: str, *, prefix: str = "101YNY", cv: str | None = None, ct: str = "", aa: str = "",
        cy: str = "", da: str = "", title: str = "A Synthetic Title") -> str:
    """A code-10 check-in response, fields in the order the NBPL ACS log shows them."""
    fields = [f"{prefix}20261118    111256", "AO1", f"AB{barcode}", f"AJ{title}"]
    if aa:
        fields.append(f"AA{aa}")
    if ct:
        fields.append(f"CT{ct}")
    if cv is not None:
        fields.append(f"CV{cv}")
    if cy:
        fields.append(f"CY{cy}")
    if da:
        fields.append(f"DA{da}")
    return "|".join(fields) + "|"


def item(barcode: str, *, when=None, destination: str = "", **fields) -> dict:
    """A legacy `acs_events` item row: `patron_id` is the item record's AA, as the parser stores it."""
    return {
        "datetime": when if when is not None else _when(),
        "message_code": "10",
        "barcode": barcode,
        "destination": destination or fields.get("ct", ""),
        "patron_id": fields.get("aa", ""),
        "raw_message": raw(barcode, **fields),
    }


def patron(patron_id: str, name: str, patron_type: str = "ADULT") -> dict:
    """A legacy message-64 patron record."""
    return {
        "datetime": _when(),
        "message_code": "64",
        "barcode": "",
        "destination": "",
        "patron_id": patron_id,
        "raw_message": f"64              00120261118    111257|AO1|AA{patron_id}|AE{name}|PT{patron_type}|",
    }


def counts(rows) -> tuple[int, int]:
    """(public holds, ILL holds) from the legacy classifier, with nothing configured."""
    result = build_acs_item_summary(pd.DataFrame(rows), [], set(), set(), [], [])
    return result["holds_total"], result["ill_total"]


# =====================================================================================================================
# Legacy classifier: what a hold is (CV)
# =====================================================================================================================

def test_cv01_main_hold_is_a_public_hold():
    assert counts([patron("p1", "SAMPLE PATRON ONE"), item("b1", cv="01", cy="p1", da="SAMPLE PATRON ONE")]) == (1, 0)


@pytest.mark.parametrize("destination", [WESTSIDE, LIBRARY_EXPRESS])
def test_cv02_hold_for_another_destination_is_a_public_hold(destination):
    rows = [patron("p1", "SAMPLE PATRON ONE"), item("b1", cv="02", ct=destination, cy="p1", da="SAMPLE PATRON ONE")]
    assert counts(rows) == (1, 0)


def test_101yny_with_no_cv_is_still_a_hold():
    # Section 3: two such records in April 2026, both flagged as holds by the sorter (gap G4 keeps their meaning open).
    assert counts([item("b1")]) == (1, 0)


def test_101ynn_normal_or_exception_check_in_is_not_a_hold():
    assert counts([item("b1", prefix="101YNN"), item("b2", prefix="101YNN", ct=WESTSIDE)]) == (0, 0)


def test_a_failed_check_in_with_cv99_is_not_a_hold():
    assert counts([item("b1", prefix="100NNY", cv="99")]) == (0, 0)


@pytest.mark.parametrize("destination", [WESTSIDE, ""])
def test_cv04_transit_is_not_a_hold(destination):
    assert counts([item("b1", cv="04", ct=destination, aa="p9")]) == (0, 0)


@pytest.mark.parametrize("cv", ["00", "03", "99"])
def test_101yny_with_an_unrecognised_cv_is_in_neither_kpi(cv):
    # D4: an unknown alert is never a confirmed hold; it is counted apart, as uncertain.
    result = build_acs_item_summary(pd.DataFrame([item("b1", cv=cv), item("b2", cv="01")]), [], set(), set(), [], [])
    assert (result["holds_total"], result["ill_total"], result["unknown_alert_total"]) == (1, 0, 1)


def test_known_alert_types_and_non_alerts_are_never_unknown_alerts():
    rows = [item("b1", cv="01"), item("b2", cv="02", ct=WESTSIDE), item("b3", cv="04"), item("b4"),
            item("b5", prefix="101YNN"), item("b6", prefix="100NNY", cv="99")]
    assert build_acs_item_summary(pd.DataFrame(rows), [], set(), set(), [], [])["unknown_alert_total"] == 0


def test_a_hold_then_a_transit_of_the_same_item_the_same_day_counts_nothing():
    rows = [item("b1", when=_when(1), cv="01", cy="p1"), item("b1", when=_when(2), cv="04", ct=WESTSIDE)]
    assert counts(rows) == (0, 0)


def test_a_transit_then_a_hold_of_the_same_item_the_same_day_counts_one():
    rows = [item("b1", when=_when(1), cv="04", ct=WESTSIDE), item("b1", when=_when(2), cv="02", ct=WESTSIDE, cy="p1")]
    assert counts(rows) == (1, 0)


def test_the_2026_11_18_controlled_test_day_counts_two_holds():
    # Wreckage: normal. A Secret Affair: CV01 Main hold. The High Season: three scans, the last a CV02 Westside hold.
    # Tom and Jerry: CV04 Westside transit. When I'm Gone: 101YNN repair exception. Expected: Holds 2, ILL 0.
    rows = [
        patron("p1", "SAMPLE PATRON ONE"), patron("p2", "SAMPLE PATRON TWO"),
        item("wreckage", when=_when(1), prefix="101YNN"),
        item("secret-affair", when=_when(2), cv="01", cy="p1", da="SAMPLE PATRON ONE"),
        item("high-season", when=_when(3), prefix="101YNN"),
        item("high-season", when=_when(4), cv="04", ct=WESTSIDE),
        item("high-season", when=_when(5), cv="02", ct=WESTSIDE, cy="p2", da="SAMPLE PATRON TWO"),
        item("tom-and-jerry", when=_when(6), cv="04", ct=WESTSIDE, aa="p9"),
        item("when-im-gone", when=_when(7), prefix="101YNN"),
    ]
    assert counts(rows) == (2, 0)


# =====================================================================================================================
# Legacy classifier: ILL is the HOLD patron's type (CY), and never the title
# =====================================================================================================================

def test_with_no_cy_the_item_record_patron_aa_is_the_fallback():
    assert counts([patron("p1", "SAMPLE ILL DESK", "ILL"), item("b1", cv="01", aa="p1")]) == (0, 1)


def test_a_staff_hold_is_a_public_hold():
    assert counts([patron("p1", "SAMPLE STAFF MEMBER", "EMPLOYEES"), item("b1", cv="01", aa="p1")]) == (1, 0)


def test_a_hold_for_an_ill_patron_cy_with_no_aa_is_ill():
    # 9 of the 345 legacy CY-confirmed ILL item-days had no AA (production, 2026-11-18).
    rows = [patron("p1", "SAMPLE BORROWING LIBRARY", "ILL"), item("b1", cv="01", cy="p1", da="SAMPLE BORROWING LIBRARY")]
    assert counts(rows) == (0, 1)


def test_a_hold_for_an_ordinary_patron_cy_is_public_even_if_aa_is_an_ill_patron():
    rows = [patron("p1", "SAMPLE PATRON ONE"), patron("p9", "SAMPLE BORROWING LIBRARY", "ILL"),
            item("b1", cv="01", aa="p9", cy="p1", da="SAMPLE PATRON ONE")]
    assert counts(rows) == (1, 0)


def test_a_title_containing_ill_does_not_make_a_hold_ill():
    # D2: the raw-message ILL match leaves out the title field (AJ).
    rows = [patron("p1", "SAMPLE PATRON ONE"), item("b1", cv="01", cy="p1", da="SAMPLE PATRON ONE", title="The Ill-Made Knight")]
    assert counts(rows) == (1, 0)


# =====================================================================================================================
# Collector v2 classifier (collector/v2_classify.py): states for 1.0.16. The payload carries the state only, never CV.
# =====================================================================================================================

NO_RULES = Rules(destinations=(), branch_name_hmacs=frozenset(), collection_name_hmacs=frozenset(),
                 branch_da_markers=(), collection_da_markers=(), fingerprint=b"")


@pytest.mark.parametrize(("message", "state"), [
    (raw("b1", cv="01", cy="p1"), "hold"),
    (raw("b1", cv="02", ct=WESTSIDE, cy="p1"), "hold"),
    (raw("b1", cv="02", ct=LIBRARY_EXPRESS, cy="p1"), "hold"),
    (raw("b1"), "hold"),
    (raw("b1", prefix="101YNN"), "non_hold_101"),
    (raw("b1", prefix="100NNY", cv="99"), "other_code10"),
])
def test_collector_states_that_stay_the_same(message, state):
    assert v2_classify.item_state(message) == state


@pytest.mark.parametrize("ct", [WESTSIDE, ""])
def test_collector_cv04_transit_is_non_hold_101(ct):
    # An existing state: it already retracts an earlier hold of the same item under the latest-record rule.
    assert v2_classify.item_state(raw("b1", cv="04", ct=ct)) == "non_hold_101"


@pytest.mark.parametrize("cv", ["00", "03", "99"])
def test_collector_unrecognised_alert_is_unknown_alert(cv):
    assert v2_classify.item_state(raw("b1", cv=cv)) == "unknown_alert"


def test_collector_title_containing_ill_does_not_make_a_hold_ill():
    # D2. The old behaviour is pinned on purpose in tests/test_collector_v2_transform.py
    # (test_the_ill_title_quirk_is_reproduced_on_purpose); phase C rewrites it together with this marker.
    ill, _, _ = v2_classify.static_flags(raw_message=raw("b1", cv="01", title="The Ill-Made Knight"),
                                         destination_raw="", rules=NO_RULES)
    assert ill is False
