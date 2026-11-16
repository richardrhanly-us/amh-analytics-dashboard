"""R8K: the holds report for one sorter site -- the service (services.hold_report_service).

The statements are PostgreSQL's (JSONB rules, `btrim`, typed time bounds) and run for real in
tests/test_hold_report_postgres.py. Here a connection that answers each of the service's own statements with given
rows stands in, so that what the service does with the rows -- which era is read, which classifier counts it, what
comes back -- is pinned without a server. How the classifiers themselves count is pinned in
tests/test_v1_hold_classification.py and tests/test_metrics_v2_parity.py, and is not repeated.

Every name and account here is synthetic.
"""

from __future__ import annotations

import dataclasses
import inspect
import itertools
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from services import hold_report_service
from services.hold_report_service import (
    HoldsReport,
    get_holds_report,
    legacy_hold_rules,
)
from services.hold_rules import V1HoldRules
from services.operational_report_service import ReportWindow, local_range
from services.tenant_resolution_service import ResolvedOperationalTenant

TENANT = ResolvedOperationalTenant(org_slug="acme", branch_slug="main", access_mode="full", operational_customer_id=8101,
                                   operational_branch_id=11)
ZONE = ZoneInfo("America/Chicago")
RANGE = local_range(date(2026, 9, 1), date(2026, 9, 30), ZONE)
def wall(*parts) -> datetime:
    return datetime(*parts)  # noqa: DTZ001 -- naive on purpose: the legacy era is local wall-clock time


V1_SPAN = (wall(2026, 9, 1), wall(2026, 9, 15, 12))
V2_SPAN = (datetime(2026, 9, 15, 17, tzinfo=UTC), datetime(2026, 10, 1, 5, tzinfo=UTC))

ACCOUNT = "EXAMPLE (ST)STAFF ACCOUNT A"
RULES = {"branch_services_names": [ACCOUNT], "collection_services_names": [], "branch_services_da_patterns": [],
         "collection_services_da_patterns": []}


def window(*, v1=True, v2=True) -> ReportWindow:
    return ReportWindow(local_range=RANGE, v1_span=V1_SPAN if v1 else None, v2_span=V2_SPAN if v2 else None)


# --- legacy and current rows, as the statements select them ---------------------------------------------------------

def v1_hold(barcode, patron="", when=None, hold=True):
    prefix = "101YNY" if hold else "101YNN"
    return (when or wall(2026, 9, 3, 10), "10", barcode, "Main", patron, f"{prefix}20260903    100000|AO1|AB{barcode}|AJSynthetic Title|")


def v1_patron(patron, name, kind="ADULT", when=None):
    return (when or wall(2026, 9, 3, 9), "64", "", "", patron, f"64              00120260903    090000|AA{patron}|AE{name}|PT{kind}|")


def v2_hold(n, when=datetime(2026, 9, 20, 15, tzinfo=UTC), row_id=None, **flags):
    return (when, f"{n:064x}", "hold", "main", flags.get("is_ill", False), flags.get("is_branch_services", False),
            flags.get("is_collection_services", False), n if row_id is None else row_id)


def v2_retraction(n, row_id, when=datetime(2026, 9, 20, 15, tzinfo=UTC)):
    return (when, f"{n:064x}", "non_hold_101", None, None, None, None, row_id)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def mappings(self):
        return self


class Connection:
    """Answers the service's three statements with the rows given, and records what each was asked."""

    def __init__(self, *, v1=(), v2=(), rules=(RULES, True, None, False)):
        self.v1, self.v2 = list(v1), list(v2)
        organization_rules, organization_has, branch_rules, branch_has = rules
        self.rules = [{"organization_rules": organization_rules, "organization_has_rules": organization_has,
                       "branch_rules": branch_rules, "branch_has_rules": branch_has}]
        self.asked: list[tuple[str, dict]] = []

    def execute(self, statement, parameters):
        # As the statements do: only the rows in [span_start, span_end), in event_time order. The sort is stable, so
        # rows of one instant keep the order given (the tie tests vary it).
        def within(rows):
            return _Result(sorted((row for row in rows if parameters["span_start"] <= row[0] < parameters["span_end"]),
                                  key=lambda row: row[0]))

        if statement is hold_report_service._V1_ROWS:
            self.asked.append(("v1", parameters))
            return within(self.v1)
        if statement is hold_report_service._V1_PATRONS:
            self.asked.append(("v1 patrons", parameters))
            return within([row for row in self.v1 if str(row[1]).strip() == "64"])
        if statement is hold_report_service._V2_ROWS:
            self.asked.append(("v2", parameters))
            return within(self.v2)
        if statement is hold_report_service._V1_RULES:
            self.asked.append(("rules", parameters))
            return _Result(self.rules)
        raise AssertionError("the service ran a statement that is not one of its own")


def counts(report: HoldsReport) -> tuple[int, int]:
    return report.public_hold_count, report.ill_hold_count


# =====================================================================================================================
# Which era is read, and by which classifier
# =====================================================================================================================

def test_a_legacy_only_range_reads_the_legacy_rows_and_rules_and_classifies_them_with_the_rules():
    conn = Connection(v1=[v1_patron("p1", ACCOUNT), v1_hold("b1", "p1"), v1_hold("b2"), v1_hold("b3", hold=False)])

    report = get_holds_report(conn, TENANT, window(v2=False))

    # b1 is for a configured service account, b3 is not a hold: one public hold.
    assert counts(report) == (1, 0)
    assert [what for what, _ in conn.asked] == ["rules", "v1 patrons", "v1"]
    rules_asked, patrons_asked, rows_asked = (parameters for _, parameters in conn.asked)
    assert rules_asked == {"customer_id": 8101, "branch_id": 11}
    # The patron records of the lookback before the span, once; then the span's own rows.
    lookback = hold_report_service.V1_PATRON_LOOKBACK
    assert patrons_asked == {"customer_id": 8101, "branch_id": 11, "span_start": V1_SPAN[0] - lookback, "span_end": V1_SPAN[0]}
    assert rows_asked == {"customer_id": 8101, "branch_id": 11, "span_start": V1_SPAN[0], "span_end": V1_SPAN[1]}


def test_a_current_only_range_reads_only_the_current_rows_and_never_the_legacy_rules():
    conn = Connection(v2=[v2_hold(1), v2_hold(2, is_collection_services=True), v2_hold(3, is_ill=True)])

    report = get_holds_report(conn, TENANT, window(v1=False))

    assert counts(report) == (1, 1)
    assert conn.asked == [("v2", {"customer_id": 8101, "branch_id": 11, "span_start": V2_SPAN[0], "span_end": V2_SPAN[1]})]


def test_a_range_across_the_cutover_counts_each_era_by_its_own_classifier_and_adds_them():
    conn = Connection(
        v1=[v1_patron("p1", ACCOUNT), v1_hold("b1", "p1"), v1_hold("b2"), v1_patron("p2", "SAMPLE PATRON", kind="ILL"), v1_hold("b3", "p2")],
        v2=[v2_hold(1), v2_hold(2), v2_hold(3, is_branch_services=True), v2_hold(4, is_ill=True)],
    )

    report = get_holds_report(conn, TENANT, window())

    # Legacy: b2 public, b3 ILL. Current: 1 and 2 public, 4 ILL.
    assert counts(report) == (3, 2)
    assert [what for what, _ in conn.asked] == ["rules", "v1 patrons", "v1", "v2"]


def test_the_legacy_rules_reach_legacy_holds_only():
    rows = {"v1": [v1_patron("p1", ACCOUNT), v1_hold("b1", "p1")], "v2": [v2_hold(1)]}

    with_rules = get_holds_report(Connection(**rows), TENANT, window())
    without = get_holds_report(Connection(**rows, rules=(None, False, None, False)), TENANT, window())

    assert counts(with_rules) == (1, 0)     # the current hold is public; the legacy one is the service account's
    assert counts(without) == (2, 0)        # no rules: the legacy hold is public too, and the current one is unchanged


def test_overlapping_classifications_never_make_a_hold_public_in_either_era():
    conn = Connection(
        v1=[v1_patron("p1", ACCOUNT, kind="ILL"), v1_hold("b1", "p1")],
        v2=[v2_hold(1, is_ill=True, is_collection_services=True), v2_hold(2, is_branch_services=True, is_collection_services=True)],
    )

    assert counts(get_holds_report(conn, TENANT, window())) == (0, 2)


def test_no_window_no_rows_and_no_holds_are_zero_and_an_era_that_owns_nothing_is_not_read():
    assert counts(get_holds_report(Connection(), TENANT, window())) == (0, 0)
    assert counts(get_holds_report(Connection(v1=[v1_hold("b1", hold=False)]), TENANT, window(v2=False))) == (0, 0)

    nothing = Connection(v1=[v1_hold("b1")], v2=[v2_hold(1)])
    assert counts(get_holds_report(nothing, TENANT, window(v1=False, v2=False))) == (0, 0)
    assert nothing.asked == []


def test_a_range_total_is_the_sum_of_its_days_each_day_decided_by_its_own_latest_record():
    # UI2 v7. Held on the 3rd, released on the 4th: a hold on the 3rd, none on the 4th -- one over the range.
    conn = Connection(v1=[v1_hold("b1", when=wall(2026, 9, 3, 10)), v1_hold("b1", when=wall(2026, 9, 4, 10), hold=False)])

    assert counts(get_holds_report(conn, TENANT, window(v2=False))) == (1, 0)


# =====================================================================================================================
# Records of one item at one instant: the greatest (event_time, id) decides, however the rows arrive
# =====================================================================================================================

@pytest.mark.parametrize(("rows", "expected"), [
    pytest.param([v2_hold(1, row_id=10), v2_hold(1, row_id=11, is_ill=True)], (0, 1), id="public-corrected-to-ill"),
    pytest.param([v2_hold(1, row_id=10, is_ill=True), v2_hold(1, row_id=11)], (1, 0), id="ill-corrected-to-public"),
    pytest.param([v2_hold(1, row_id=10), v2_hold(1, row_id=11, is_ill=True), v2_hold(1, row_id=12)], (1, 0), id="a-b-a"),
    pytest.param([v2_hold(1, row_id=10), v2_retraction(1, row_id=11)], (0, 0), id="public-retracted-in-its-second"),
    pytest.param([v2_hold(1, row_id=10, is_ill=True), v2_retraction(1, row_id=11)], (0, 0), id="ill-retracted-in-its-second"),
])
def test_a_tie_is_decided_by_the_database_id_whichever_way_the_rows_arrive(rows, expected):
    for arrival in itertools.permutations(rows):
        assert counts(get_holds_report(Connection(v2=arrival), TENANT, window(v1=False))) == expected


def test_dense_ties_across_many_items_count_each_items_greatest_id():
    instants = [datetime(2026, 9, 20, 15, second, tzinfo=UTC) for second in range(3)]
    rows, row_id = [], 0
    for n in range(1, 61):
        when, is_ill = instants[n % 3], n % 2 == 0
        rows += [v2_hold(n, when, row_id + 1, is_ill=is_ill), v2_hold(n, when, row_id + 2, is_ill=not is_ill)]
        rows += [v2_retraction(n, row_id + 3, when)] if n % 5 == 0 else []
        row_id += 3

    # 12 of the 60 items end retracted. Of the other 48, an odd n ends ILL (24) and an even n public (24).
    assert counts(get_holds_report(Connection(v2=rows), TENANT, window(v1=False))) == (24, 24)
    assert counts(get_holds_report(Connection(v2=rows[::-1]), TENANT, window(v1=False))) == (24, 24)


def test_the_current_statement_selects_the_summarys_columns_in_order_and_orders_by_event_time_then_id():
    statement = str(hold_report_service._V2_ROWS)
    selected = statement.split("SELECT", 1)[1].split("FROM", 1)[0]

    assert [column.strip() for column in selected.split(",")] == ["event_time", *hold_report_service._V2_COLUMNS[1:]]
    assert hold_report_service._V2_COLUMNS[0] == "datetime" and "id" in hold_report_service._V2_COLUMNS
    assert "ORDER BY event_time, id" in statement


# =====================================================================================================================
# UI2 v7: a sum of days, read a block of days at a time
# =====================================================================================================================

def _span_window(first: date, last: date, *, cutover: datetime | None = None) -> ReportWindow:
    """A window over first..last, all legacy (no cutover) or split at an aware `cutover` instant."""
    local = local_range(first, last, ZONE)
    v1_start, v1_end = local.v1_day_boundaries_local[0], local.v1_day_boundaries_local[-1]
    v2_start, v2_end = local.v2_day_boundaries_utc[0], local.v2_day_boundaries_utc[-1]
    if cutover is None:
        return ReportWindow(local_range=local, v1_span=(v1_start, v1_end), v2_span=None)
    cut_local = cutover.astimezone(ZONE).replace(tzinfo=None)
    return ReportWindow(local_range=local, v1_span=(v1_start, min(v1_end, cut_local)) if v1_start < cut_local else None,
                        v2_span=(max(v2_start, cutover), v2_end) if cutover < v2_end else None)


def test_an_item_held_on_several_days_counts_on_each_day_and_a_correction_within_a_day_counts_once():
    rows = [
        v2_hold(1, datetime(2026, 9, 16, 15, tzinfo=UTC), row_id=1),
        v2_hold(1, datetime(2026, 9, 16, 16, tzinfo=UTC), row_id=2, is_ill=True),   # corrected the same day: ILL, once
        v2_hold(1, datetime(2026, 9, 18, 15, tzinfo=UTC), row_id=3),                # held again two days later: public
        v2_hold(1, datetime(2026, 9, 22, 15, tzinfo=UTC), row_id=4),
        v2_retraction(1, row_id=5, when=datetime(2026, 9, 22, 18, tzinfo=UTC)),     # retracted the same day: nothing
    ]

    report = get_holds_report(Connection(v2=rows), TENANT, window(v1=False))

    assert counts(report) == (1, 1) and report.days_with_data == 3


def test_a_one_day_range_is_that_days_count_and_a_range_is_the_sum_of_its_one_day_reports():
    rows = [v2_hold(n, datetime(2026, 9, day, 15, tzinfo=UTC), is_ill=n % 3 == 0) for n, day in enumerate([16, 16, 17, 20, 20, 20], 1)]
    rows += [v2_hold(1, datetime(2026, 9, 17, 15, 30, tzinfo=UTC))]                  # item 1 again on the 17th

    whole = counts(get_holds_report(Connection(v2=rows), TENANT, _span_window(date(2026, 9, 16), date(2026, 9, 20), cutover=V2_SPAN[0])))
    days = [counts(get_holds_report(Connection(v2=rows), TENANT, _span_window(day, day, cutover=V2_SPAN[0])))
            for day in (date(2026, 9, 16), date(2026, 9, 17), date(2026, 9, 18), date(2026, 9, 19), date(2026, 9, 20))]

    assert whole == tuple(map(sum, zip(*days, strict=True)))
    assert whole == (5, 2)


def test_a_multi_year_range_is_read_in_contiguous_blocks_of_at_most_the_block_length():
    first, last = date(2023, 1, 1), date(2025, 12, 31)                              # 1,096 days, two clock changes a year
    rows = [v1_hold(f"b{n}", when=wall(2023, 1, 1, 10) + timedelta(days=n * 10)) for n in range(110)]

    conn = Connection(v1=rows)
    report = get_holds_report(conn, TENANT, _span_window(first, last))

    reads = [parameters for what, parameters in conn.asked if what == "v1"]
    patron_reads = [parameters for what, parameters in conn.asked if what == "v1 patrons"]
    assert counts(report) == (110, 0) and report.days_with_data == 110
    assert len(reads) == -(-1096 // hold_report_service.DAYS_PER_READ)
    assert reads[0]["span_start"] == wall(2023, 1, 1) and reads[-1]["span_end"] == wall(2026, 1, 1)
    for before, after in itertools.pairwise(reads):
        assert before["span_end"] == after["span_start"]                            # nothing skipped, nothing read twice
    for read in reads:
        assert read["span_end"] - read["span_start"] <= timedelta(days=hold_report_service.DAYS_PER_READ, hours=1)
    # The lookback before the range is read once, and only for patron records: overlapping windows read nothing again.
    lookback = hold_report_service.V1_PATRON_LOOKBACK
    assert patron_reads == [{"customer_id": 8101, "branch_id": 11, "span_start": wall(2023, 1, 1) - lookback, "span_end": wall(2023, 1, 1)}]


def test_a_legacy_hold_is_classified_by_its_patrons_record_from_an_earlier_day_and_alike_in_every_range():
    # The ILL patron's record is on the 1st; its hold on the 20th. The service account's record is a month before its hold.
    rows = [v1_patron("p1", "SAMPLE PATRON", kind="ILL", when=wall(2026, 9, 1, 8)), v1_hold("b1", "p1", when=wall(2026, 9, 20, 9)),
            v1_patron("p2", ACCOUNT, when=wall(2026, 8, 25, 8)), v1_hold("b2", "p2", when=wall(2026, 9, 21, 9)),
            v1_hold("b3", when=wall(2026, 9, 21, 10))]
    one_day = [counts(get_holds_report(Connection(v1=rows), TENANT, _span_window(day, day)))
               for day in (date(2026, 9, 20), date(2026, 9, 21))]
    month = counts(get_holds_report(Connection(v1=rows), TENANT, _span_window(date(2026, 9, 1), date(2026, 9, 30))))

    assert one_day == [(0, 1), (1, 0)]                                              # b1 ILL; b2 the account's; b3 public
    assert month == (1, 1) == tuple(map(sum, zip(*one_day, strict=True)))


def test_a_patron_record_older_than_the_lookback_is_not_used_and_patron_records_never_count_as_a_day_with_data():
    lookback = hold_report_service.V1_PATRON_LOOKBACK
    rows = [v1_patron("p1", "SAMPLE PATRON", kind="ILL", when=wall(2026, 9, 20, 8) - lookback - timedelta(days=1)),
            v1_hold("b1", "p1", when=wall(2026, 9, 20, 9))]
    report = get_holds_report(Connection(v1=rows), TENANT, _span_window(date(2026, 3, 1), date(2026, 9, 30)))

    assert counts(report) == (1, 0)                                                 # its patron is too long ago: public
    assert report.days_with_data == 2                                               # the patron's own day had a record


def test_patron_records_carried_across_blocks_classify_each_day_as_its_own_one_day_report_does():
    # Patron records 175 days before their holds (NBPL's longest), one inside the lookback and one just outside it,
    # a patron whose newer record changes its type, and holds spread over many blocks.
    rows = [
        v1_patron("p1", "SAMPLE ILL", kind="ILL", when=wall(2026, 3, 31, 8)),
        v1_hold("b1", "p1", when=wall(2026, 9, 22, 9)),                             # 175 days later: ILL
        v1_patron("p2", ACCOUNT, when=wall(2026, 3, 1, 8)),
        v1_hold("b2", "p2", when=wall(2026, 9, 1, 9)),                              # 184 days later: too old, public
        v1_patron("p3", "SAMPLE ILL 3", kind="ILL", when=wall(2026, 4, 2, 8)),
        v1_hold("b3", "p3", when=wall(2026, 5, 2, 9)),                              # ILL then
        v1_patron("p3", "SAMPLE PATRON 3", kind="ADULT", when=wall(2026, 6, 1, 8)),
        v1_hold("b4", "p3", when=wall(2026, 7, 2, 9)),                              # its newer record: public
        *[v1_hold(f"x{n}", when=wall(2026, 4, 1, 10) + timedelta(days=n * 3)) for n in range(55)],
    ]
    whole = get_holds_report(Connection(v1=rows), TENANT, _span_window(date(2026, 3, 1), date(2026, 9, 30)))
    days = [day for day in (wall(2026, 3, 1) + timedelta(days=n) for n in range(214))]
    one_day = [counts(get_holds_report(Connection(v1=rows), TENANT, _span_window(day.date(), day.date()))) for day in days]

    assert counts(whole) == tuple(map(sum, zip(*one_day, strict=True)))
    assert counts(whole) == (55 + 2, 2)                                             # x*, b2, b4 public; b1, b3 ILL


def test_the_cutover_day_is_two_parts_each_counted_by_its_own_era_and_once_as_a_day_with_data():
    cutover = datetime(2026, 9, 15, 17, tzinfo=UTC)                                  # noon on the 15th in Chicago
    conn = Connection(v1=[v1_hold("b1", when=wall(2026, 9, 15, 9))], v2=[v2_hold(1, datetime(2026, 9, 15, 20, tzinfo=UTC))])

    report = get_holds_report(conn, TENANT, _span_window(date(2026, 9, 14), date(2026, 9, 16), cutover=cutover))

    assert counts(report) == (2, 0) and report.days_with_data == 1


def test_empty_and_partly_filled_ranges_say_how_many_days_had_data():
    assert get_holds_report(Connection(), TENANT, _span_window(date(2024, 1, 1), date(2025, 12, 31))).days_with_data == 0
    partly = Connection(v1=[v1_hold("b1", when=wall(2025, 6, 2, 10)), v1_hold("b2", when=wall(2025, 6, 2, 11), hold=False)])
    report = get_holds_report(partly, TENANT, _span_window(date(2025, 1, 1), date(2025, 12, 31)))
    assert counts(report) == (1, 0) and report.days_with_data == 1                   # a day with only a non-hold still had data


# =====================================================================================================================
# The legacy rules of the site
# =====================================================================================================================

@pytest.mark.parametrize(("rules", "expected"), [
    ((RULES, True, None, False), frozenset({ACCOUNT})),                                            # the organization's
    ((None, False, None, False), frozenset()),                                                     # none at all
    ((RULES, True, {"branch_services_names": ["OTHER ACCOUNT"]}, True), frozenset({"OTHER ACCOUNT"})),  # the site's list replaces it
    ((RULES, True, {"collection_services_names": ["DEPT"]}, True), frozenset({ACCOUNT})),         # ... key by key: the rest stays
    ((RULES, True, None, True), frozenset()),                                                      # a site block that is null: none
    ((None, False, {"branch_services_names": ["SITE ONLY"]}, True), frozenset({"SITE ONLY"})),
])
def test_the_sites_rules_are_the_organizations_with_the_sites_own_merged_over_them_as_the_dashboard_does(rules, expected):
    assert legacy_hold_rules(Connection(rules=rules), TENANT).branch_services_names == expected


def test_a_resolved_tenant_without_exactly_one_settings_row_is_a_fault_not_an_empty_rule_set():
    conn = Connection()
    conn.rules = conn.rules * 2
    with pytest.raises(RuntimeError):
        legacy_hold_rules(conn, TENANT)

    conn.rules = []
    with pytest.raises(RuntimeError):
        get_holds_report(conn, TENANT, window(v2=False))


# =====================================================================================================================
# What comes out
# =====================================================================================================================

def test_the_report_is_three_integers_and_nothing_else_read():
    conn = Connection(v1=[v1_patron("p1", ACCOUNT), v1_hold("b1", "p1"), v1_hold("b2")], v2=[v2_hold(1, is_branch_services=True)])

    report = get_holds_report(conn, TENANT, window())

    assert [field.name for field in dataclasses.fields(HoldsReport)] == ["public_hold_count", "ill_hold_count", "days_with_data"]
    assert all(type(value) is int for value in dataclasses.astuple(report))
    text = repr(report)
    for private in (ACCOUNT, "p1", "b2", "101YNY", "Synthetic Title", "programming", "collection", "branch_services", "0" * 63):
        assert private not in text, private


def test_the_database_id_never_reaches_the_report():
    report = get_holds_report(Connection(v2=[v2_hold(1, row_id=987654321)]), TENANT, window(v1=False))

    assert "987654321" not in repr(report) and counts(report) == (1, 0)


def test_the_service_reads_only_the_columns_and_tables_it_needs_and_logs_nothing():
    source = inspect.getsource(hold_report_service)
    code = source.split('"""', 2)[2]

    assert "SELECT event_time, message_code, barcode, destination, patron_id, raw_message" in str(hold_report_service._V1_ROWS)
    assert "FROM acs_events" in str(hold_report_service._V1_ROWS)
    assert "FROM acs_item_events" in str(hold_report_service._V2_ROWS)
    assert "settings_json -> 'internal_routing'" in str(hold_report_service._V1_RULES)
    assert "SELECT os.settings_json AS" not in str(hold_report_service._V1_RULES)
    for statement in (hold_report_service._V1_ROWS, hold_report_service._V2_ROWS):
        assert "customer_id = :customer_id" in str(statement) and "branch_id = :branch_id" in str(statement)
    for forbidden in ("logger", "logging", "print(", "streamlit", "data_loader", "mixed_era_service", "settings_service", "get_engine",
                      "routing_config", "get_routing"):
        assert forbidden not in code, forbidden
    # The two dashboard classifiers, used as they are: nothing of either is rewritten here.
    assert "metrics.build_acs_item_summary(" in code and "metrics_v2.build_acs_item_summary_v2(" in code
    assert isinstance(legacy_hold_rules(Connection(), TENANT), V1HoldRules)
