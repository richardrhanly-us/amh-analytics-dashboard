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
from datetime import UTC, date, datetime
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


def v2_hold(n, when=datetime(2026, 9, 20, 15, tzinfo=UTC), **flags):
    return (when, f"{n:064x}", "hold", "main", flags.get("is_ill", False), flags.get("is_branch_services", False),
            flags.get("is_collection_services", False))


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
        if statement is hold_report_service._V1_ROWS:
            self.asked.append(("v1", parameters))
            return _Result(self.v1)
        if statement is hold_report_service._V2_ROWS:
            self.asked.append(("v2", parameters))
            return _Result(self.v2)
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
    assert [what for what, _ in conn.asked] == ["rules", "v1"]
    rules_asked, rows_asked = conn.asked[0][1], conn.asked[1][1]
    assert rules_asked == {"customer_id": 8101, "branch_id": 11}
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
    assert [what for what, _ in conn.asked] == ["rules", "v1", "v2"]


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


def test_the_latest_record_of_an_item_in_its_era_decides_and_a_range_total_is_not_a_sum_of_days():
    # The same item held on the 3rd and released on the 4th is no hold over the range, whatever each day said.
    conn = Connection(v1=[v1_hold("b1", when=wall(2026, 9, 3, 10)), v1_hold("b1", when=wall(2026, 9, 4, 10), hold=False)])

    assert counts(get_holds_report(conn, TENANT, window(v2=False))) == (0, 0)


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

def test_the_report_is_two_integers_and_nothing_else_read():
    conn = Connection(v1=[v1_patron("p1", ACCOUNT), v1_hold("b1", "p1"), v1_hold("b2")], v2=[v2_hold(1, is_branch_services=True)])

    report = get_holds_report(conn, TENANT, window())

    assert [field.name for field in dataclasses.fields(HoldsReport)] == ["public_hold_count", "ill_hold_count"]
    assert all(type(value) is int for value in dataclasses.astuple(report))
    text = repr(report)
    for private in (ACCOUNT, "p1", "b2", "101YNY", "Synthetic Title", "programming", "collection", "branch_services", "0" * 63):
        assert private not in text, private


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
