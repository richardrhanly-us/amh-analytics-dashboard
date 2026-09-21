"""Tests for scripts/check_pipeline_health.py -- specifically the Phase 3
compatibility rule: prefer health_status (heartbeat) when a branch has
ever reported one, fall back to legacy status parsing otherwise. Both
vocabularies can be live simultaneously during Continuous Ingestion
Phase 0's parallel-validation coexistence window.

conn.execute(...).mappings().all() is faked with plain dicts -- a dict
already satisfies both row["key"] access and **row unpacking, which is
all find_unhealthy_branches needs from a SQLAlchemy Row mapping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from scripts.check_pipeline_health import find_unhealthy_branches

NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)  # freshness: allow FRESH004 -- passed to find_unhealthy_branches as an explicit cutoff, never read from the clock
STALE_AFTER = NOW - timedelta(minutes=60)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class FakeConnection:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, stmt, params=None):
        return FakeResult(self._rows)


def _row(**overrides):
    row = {
        "organization_name": "Test Library",
        "branch_name": "Main",
        "status": None,
        "last_run": None,
        "last_attempt": None,
        "updated_at": NOW - timedelta(minutes=1),
        "health_status": None,
        "quarantined_count": None,
        "last_error": None,
    }
    row.update(overrides)
    return row


def test_healthy_heartbeat_not_flagged():
    conn = FakeConnection([_row(health_status="healthy")])
    assert find_unhealthy_branches(conn, STALE_AFTER) == []


def test_degraded_heartbeat_flagged():
    conn = FakeConnection([_row(health_status="degraded", last_error="backlog stuck")])
    unhealthy = find_unhealthy_branches(conn, STALE_AFTER)

    assert len(unhealthy) == 1
    assert any("degraded" in reason for reason in unhealthy[0]["reasons"])
    assert "backlog stuck" in unhealthy[0]["reasons"][0]


def test_auth_failure_heartbeat_flagged():
    conn = FakeConnection([_row(health_status="auth_failure")])
    unhealthy = find_unhealthy_branches(conn, STALE_AFTER)

    assert len(unhealthy) == 1
    assert any("auth_failure" in reason for reason in unhealthy[0]["reasons"])


def test_stale_no_heartbeat_flagged():
    conn = FakeConnection([_row(health_status="healthy", updated_at=NOW - timedelta(hours=2))])
    unhealthy = find_unhealthy_branches(conn, STALE_AFTER)

    assert len(unhealthy) == 1
    assert any("stale" in reason for reason in unhealthy[0]["reasons"])


def test_never_reported_flagged():
    conn = FakeConnection([_row(updated_at=None)])
    unhealthy = find_unhealthy_branches(conn, STALE_AFTER)

    assert len(unhealthy) == 1
    assert "has never reported a pipeline run" in unhealthy[0]["reasons"]


def test_legacy_failed_status_flagged_when_health_status_absent():
    conn = FakeConnection([_row(status="failed_parse_error")])
    unhealthy = find_unhealthy_branches(conn, STALE_AFTER)

    assert len(unhealthy) == 1
    assert any("failed_parse_error" in reason for reason in unhealthy[0]["reasons"])


def test_legacy_completed_status_not_flagged_when_health_status_absent():
    conn = FakeConnection([_row(status="completed")])
    assert find_unhealthy_branches(conn, STALE_AFTER) == []


def test_health_status_takes_precedence_over_stale_legacy_status():
    # A branch that has cut over to heartbeat reporting no longer has its
    # legacy `status` refreshed -- health_status healthy must win even
    # though the frozen legacy status says "failed".
    conn = FakeConnection([_row(health_status="healthy", status="failed")])
    assert find_unhealthy_branches(conn, STALE_AFTER) == []


def test_idle_branch_with_recent_heartbeat_not_flagged_as_stale():
    # Heartbeat/updated_at freshness is the staleness signal, not AMH log
    # activity -- a branch with a fresh heartbeat and nothing else wrong
    # must not be flagged, regardless of how quiet the source logs are.
    conn = FakeConnection([_row(health_status="healthy", updated_at=NOW - timedelta(seconds=30))])
    assert find_unhealthy_branches(conn, STALE_AFTER) == []


def test_multiple_branches_only_unhealthy_ones_reported():
    conn = FakeConnection([
        _row(branch_name="Main", health_status="healthy"),
        _row(branch_name="Annex", health_status="auth_failure"),
    ])
    unhealthy = find_unhealthy_branches(conn, STALE_AFTER)

    assert len(unhealthy) == 1
    assert unhealthy[0]["branch_name"] == "Annex"


# --- organization / branch scope (real query against SQLite) --------------------
#
# The tests above feed find_unhealthy_branches canned rows, so they cannot see
# which tenants the SQL selects. These run the actual query against SQLite
# (plain ANSI: joins, IN, EXISTS, ORDER BY) to pin WHICH branches are evaluated.
#
# A branch is evaluated only when its organization is active or trial, the
# branch is active, AND at least one collector_installations row for that SaaS
# organization/branch is active. A suspended/cancelled organization is
# intentionally non-operational (the API rejects its Collector traffic), and a
# branch with no ACTIVE installation (inactive / retired / provisioning / none)
# is not expected to be reporting -- either way it must not produce
# stale/never-reported alerts. Every excluded tenant below is deliberately in a
# state that WOULD alert if it were evaluated.


class _ParsedConn:
    """SQLite hands timestamps back as strings; the real driver returns
    datetimes. Parse updated_at so the health logic runs unmodified."""

    def __init__(self, conn) -> None:
        self._conn = conn
        self.statements: list[str] = []

    def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        rows = [dict(r) for r in self._conn.execute(stmt, params or {}).mappings().all()]
        for row in rows:
            if isinstance(row.get("updated_at"), str):
                row["updated_at"] = datetime.fromisoformat(row["updated_at"])
        return FakeResult(rows)


@pytest.fixture
def scope_db():
    engine = create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as conn:
        for ddl in (
            """CREATE TABLE organizations (
                id INTEGER PRIMARY KEY, name TEXT, status TEXT,
                operational_customer_id INTEGER)""",
            """CREATE TABLE branches (
                id INTEGER PRIMARY KEY, organization_id INTEGER, name TEXT, status TEXT,
                operational_branch_id INTEGER)""",
            # organization_id / branch_id are SaaS ids (organizations.id / branches.id).
            """CREATE TABLE collector_installations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, organization_id INTEGER,
                branch_id INTEGER, name TEXT, status TEXT)""",
            """CREATE TABLE pipeline_status (
                customer_id INTEGER, branch_id INTEGER, status TEXT, last_run TEXT,
                last_attempt TEXT, updated_at TEXT, health_status TEXT,
                quarantined_count INTEGER, last_error TEXT)""",
        ):
            conn.execute(text(ddl))
    return engine


def _add_status(engine, customer_id, branch_id, reported_at, health_status="healthy"):
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO pipeline_status (customer_id, branch_id, updated_at, "
                 "health_status) VALUES (:c, :b, :u, :h)"),
            {"c": customer_id, "b": branch_id,
             "u": reported_at.replace(tzinfo=None).isoformat(sep=" "), "h": health_status},
        )


def _add_tenant(engine, *, org_id, name, org_status="active", branch_status="active",
                operational_customer_id="same", operational_branch_id="same",
                reported_at=None):
    """Adds an organization + primary branch and, if reported_at is given, a
    pipeline_status row at the tenant's OPERATIONAL ids. The bridge defaults to
    the org/branch ids; pass an explicit value (or None) to override it."""
    if operational_customer_id == "same":
        operational_customer_id = org_id
    if operational_branch_id == "same":
        operational_branch_id = org_id
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO organizations VALUES (:i, :n, :s, :c)"),
            {"i": org_id, "n": name, "s": org_status, "c": operational_customer_id},
        )
        conn.execute(
            text("INSERT INTO branches VALUES (:i, :o, :n, :s, :b)"),
            {"i": org_id, "o": org_id, "n": f"{name} Main", "s": branch_status,
             "b": operational_branch_id},
        )
    if reported_at is not None:
        _add_status(engine, operational_customer_id, operational_branch_id, reported_at)


def _add_installation(engine, org_id, status="active", *, branch_id="same", name="Sorter"):
    """Adds one collector_installations row at the SaaS ids (the branch id
    defaults to the org id, matching _add_tenant's convention)."""
    if branch_id == "same":
        branch_id = org_id
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO collector_installations (organization_id, branch_id, name, status) "
                 "VALUES (:o, :b, :n, :s)"),
            {"o": org_id, "b": branch_id, "n": name, "s": status},
        )


def _add_monitored_tenant(engine, **kwargs):
    """A tenant that IS eligible on the installation condition: _add_tenant plus
    one ACTIVE installation. Whether it is evaluated then depends only on the
    organization/branch status a test sets."""
    _add_tenant(engine, **kwargs)
    _add_installation(engine, kwargs["org_id"], "active")


def _unhealthy(engine):
    with engine.connect() as conn:
        return find_unhealthy_branches(_ParsedConn(conn), STALE_AFTER)


def _flagged(engine):
    return [row["organization_name"] for row in _unhealthy(engine)]


FRESH = NOW - timedelta(minutes=1)
STALE = NOW - timedelta(hours=3)


def test_active_org_with_active_branch_is_evaluated(scope_db):
    _add_monitored_tenant(scope_db, org_id=1, name="Active Library", org_status="active")  # never reported

    assert _flagged(scope_db) == ["Active Library"]


def test_trial_org_with_active_branch_is_evaluated(scope_db):
    _add_monitored_tenant(scope_db, org_id=1, name="Trial Library", org_status="trial",
                reported_at=STALE)

    assert _flagged(scope_db) == ["Trial Library"]


def test_suspended_org_is_excluded_even_though_it_would_alert(scope_db):
    _add_monitored_tenant(scope_db, org_id=1, name="Suspended Library", org_status="suspended",
                reported_at=STALE)

    assert _flagged(scope_db) == []


def test_suspended_org_that_never_reported_is_excluded(scope_db):
    _add_monitored_tenant(scope_db, org_id=1, name="Suspended Library", org_status="suspended")

    assert _flagged(scope_db) == []


def test_cancelled_org_is_excluded_even_though_it_would_alert(scope_db):
    _add_monitored_tenant(scope_db, org_id=1, name="Cancelled Library", org_status="cancelled",
                reported_at=STALE)

    assert _flagged(scope_db) == []


@pytest.mark.parametrize("org_status", ["active", "trial"])
def test_inactive_branch_is_excluded(scope_db, org_status):
    _add_monitored_tenant(scope_db, org_id=1, name="Library", org_status=org_status,
                branch_status="inactive", reported_at=STALE)

    assert _flagged(scope_db) == []


def test_only_active_and_trial_organizations_are_evaluated_among_all_statuses(scope_db):
    tenants = [("A Active", "active"), ("B Trial", "trial"),
               ("C Suspended", "suspended"), ("D Cancelled", "cancelled")]
    for org_id, (name, status) in enumerate(tenants, start=1):
        _add_monitored_tenant(scope_db, org_id=org_id, name=name, org_status=status, reported_at=STALE)

    assert _flagged(scope_db) == ["A Active", "B Trial"]


def test_suspended_tenant_does_not_affect_another_active_tenant(scope_db):
    _add_monitored_tenant(scope_db, org_id=1, name="Suspended Library", org_status="suspended",
                reported_at=STALE)
    _add_monitored_tenant(scope_db, org_id=2, name="Healthy Library", reported_at=FRESH)

    # The suspended tenant's silence raises nothing, and the healthy tenant is
    # not flagged on its behalf.
    assert _flagged(scope_db) == []


def test_active_tenant_problems_are_still_reported_alongside_a_suspended_tenant(scope_db):
    _add_monitored_tenant(scope_db, org_id=1, name="Suspended Library", org_status="suspended",
                reported_at=STALE)
    _add_monitored_tenant(scope_db, org_id=2, name="Broken Library", reported_at=STALE)

    assert _flagged(scope_db) == ["Broken Library"]


# --- Collector installation lifecycle scopes monitoring --------------------------
#
# Every tenant below reported long ago (STALE), i.e. it WOULD alert if evaluated;
# only the installation state differs.

def test_branch_with_an_active_installation_is_evaluated(scope_db):
    _add_tenant(scope_db, org_id=1, name="Live Library", reported_at=STALE)
    _add_installation(scope_db, 1, "active")

    assert _flagged(scope_db) == ["Live Library"]


@pytest.mark.parametrize("installation_status", ["inactive", "retired", "provisioning"])
def test_branch_whose_only_installation_is_not_active_is_excluded(scope_db, installation_status):
    # e.g. the Clean Install Test installation: deliberately inactive while its
    # branch stays active for future QA, with an old pipeline_status row.
    _add_tenant(scope_db, org_id=1, name="Clean Install Test", reported_at=STALE)
    _add_installation(scope_db, 1, installation_status)

    assert _flagged(scope_db) == []


def test_branch_with_no_installation_at_all_is_excluded(scope_db):
    _add_tenant(scope_db, org_id=1, name="No Collector Yet", reported_at=STALE)

    assert _flagged(scope_db) == []


def test_branch_with_no_installation_and_no_report_is_excluded(scope_db):
    _add_tenant(scope_db, org_id=1, name="No Collector Yet")  # never reported either

    assert _flagged(scope_db) == []


def test_branch_with_several_non_active_installations_is_excluded(scope_db):
    _add_tenant(scope_db, org_id=1, name="Library", reported_at=STALE)
    for status in ("inactive", "retired", "provisioning", "inactive"):
        _add_installation(scope_db, 1, status)

    assert _flagged(scope_db) == []


def test_one_active_installation_among_non_active_ones_is_enough(scope_db):
    _add_tenant(scope_db, org_id=1, name="Library", reported_at=STALE)
    for status in ("retired", "active", "inactive", "provisioning"):
        _add_installation(scope_db, 1, status)

    assert _flagged(scope_db) == ["Library"]


def test_multiple_active_installations_evaluate_the_branch_exactly_once(scope_db):
    # EXISTS, not a JOIN: three active installations must not triplicate the branch.
    _add_tenant(scope_db, org_id=1, name="Busy Library", reported_at=STALE)
    for name in ("Sorter A", "Sorter B", "Sorter C"):
        _add_installation(scope_db, 1, "active", name=name)

    result = _unhealthy(scope_db)

    assert [r["organization_name"] for r in result] == ["Busy Library"]
    assert len(result) == 1


def test_multiple_active_installations_do_not_duplicate_a_healthy_branch_either(scope_db):
    _add_tenant(scope_db, org_id=1, name="Busy Library", reported_at=FRESH)
    for name in ("Sorter A", "Sorter B"):
        _add_installation(scope_db, 1, "active", name=name)

    assert _flagged(scope_db) == []


def test_the_clean_install_test_case_is_silent_while_a_real_library_still_alerts(scope_db):
    _add_tenant(scope_db, org_id=1, name="Clean Install Test", reported_at=STALE)
    _add_installation(scope_db, 1, "inactive")
    _add_monitored_tenant(scope_db, org_id=2, name="Real Library", reported_at=STALE)

    assert _flagged(scope_db) == ["Real Library"]


@pytest.mark.parametrize("org_status", ["suspended", "cancelled"])
def test_suspended_and_cancelled_orgs_stay_excluded_even_with_an_active_installation(scope_db, org_status):
    _add_tenant(scope_db, org_id=1, name="Library", org_status=org_status, reported_at=STALE)
    _add_installation(scope_db, 1, "active")

    assert _flagged(scope_db) == []


def test_an_inactive_branch_stays_excluded_even_with_an_active_installation(scope_db):
    _add_tenant(scope_db, org_id=1, name="Library", branch_status="inactive", reported_at=STALE)
    _add_installation(scope_db, 1, "active")

    assert _flagged(scope_db) == []


def test_an_installation_only_counts_for_its_own_branch_and_organization(scope_db):
    # Branch id 1 belongs to org 1. An active installation on a DIFFERENT branch
    # of the same org, or on the same branch id of a different org, is not this
    # branch's installation.
    _add_tenant(scope_db, org_id=1, name="Library One", reported_at=STALE)
    _add_installation(scope_db, 1, "active", branch_id=99)   # right org, some other branch
    _add_installation(scope_db, 77, "active", branch_id=1)   # some other org, same branch id

    assert _flagged(scope_db) == []


def test_installations_are_matched_on_saas_ids_not_operational_ids(scope_db):
    # SaaS org/branch 2 <-> operational customer 50 / branch 7.
    _add_tenant(scope_db, org_id=2, name="Mapped Library",
                operational_customer_id=50, operational_branch_id=7, reported_at=STALE)
    _add_installation(scope_db, 50, "active", branch_id=7)   # operational ids: NOT this tenant's installation

    assert _flagged(scope_db) == []

    _add_installation(scope_db, 2, "active", branch_id=2)    # its real (SaaS-id) installation
    assert _flagged(scope_db) == ["Mapped Library"]


@pytest.mark.parametrize("org_status", ["active", "trial", "suspended", "cancelled"])
@pytest.mark.parametrize("branch_status", ["active", "inactive"])
@pytest.mark.parametrize("installation_status", ["active", "inactive", "retired", "provisioning", None])
def test_a_branch_is_evaluated_only_when_org_branch_and_installation_are_all_eligible(
    scope_db, org_status, branch_status, installation_status
):
    _add_tenant(scope_db, org_id=1, name="Library", org_status=org_status,
                branch_status=branch_status, reported_at=STALE)
    if installation_status is not None:
        _add_installation(scope_db, 1, installation_status)

    evaluated = _flagged(scope_db) == ["Library"]

    assert evaluated == (
        org_status in ("active", "trial") and branch_status == "active" and installation_status == "active"
    )


# --- operational bridge is preserved ---------------------------------------------

def test_nbpl_style_matching_ids_join_correctly(scope_db):
    _add_monitored_tenant(scope_db, org_id=1, name="NBPL", reported_at=FRESH)

    assert _flagged(scope_db) == []


def test_pipeline_status_is_matched_through_operational_ids_not_saas_ids(scope_db):
    # SaaS org/branch 2 <-> operational customer 50 / branch 2. Its heartbeat
    # sits at the OPERATIONAL ids, so it is healthy.
    _add_monitored_tenant(scope_db, org_id=2, name="Mapped Library",
                operational_customer_id=50, operational_branch_id=2, reported_at=FRESH)

    assert _flagged(scope_db) == []


def test_a_row_at_the_saas_ids_does_not_count_for_a_differently_mapped_tenant(scope_db):
    # A fresh, healthy row exists at the SaaS ids 2/2, but the tenant's
    # operational pair is 50/2 and has never reported: no SaaS-id fallback.
    _add_monitored_tenant(scope_db, org_id=2, name="Mapped Library",
                operational_customer_id=50, operational_branch_id=2)
    _add_status(scope_db, customer_id=2, branch_id=2, reported_at=FRESH)

    assert _flagged(scope_db) == ["Mapped Library"]


def test_unmapped_active_tenant_still_reads_as_never_reported(scope_db):
    _add_monitored_tenant(scope_db, org_id=2, name="Unmapped Library",
                operational_customer_id=None, operational_branch_id=None)
    _add_status(scope_db, customer_id=2, branch_id=2, reported_at=FRESH)  # historical, SaaS ids

    result = _unhealthy(scope_db)

    assert [r["organization_name"] for r in result] == ["Unmapped Library"]
    assert "has never reported a pipeline run" in result[0]["reasons"]


# --- read-only ------------------------------------------------------------------------

def test_health_check_only_reads_and_never_changes_pipeline_status(scope_db):
    _add_monitored_tenant(scope_db, org_id=1, name="Suspended Library", org_status="suspended",
                reported_at=STALE)
    _add_monitored_tenant(scope_db, org_id=2, name="Active Library", reported_at=STALE)
    with scope_db.connect() as conn:
        before = conn.execute(text("SELECT * FROM pipeline_status ORDER BY customer_id")).all()

    with scope_db.connect() as conn:
        parsed = _ParsedConn(conn)
        find_unhealthy_branches(parsed, STALE_AFTER)
    with scope_db.connect() as conn:
        after = conn.execute(text("SELECT * FROM pipeline_status ORDER BY customer_id")).all()

    assert after == before
    assert len(parsed.statements) == 1
    normalized = " ".join(parsed.statements[0].upper().split())
    assert normalized.startswith("SELECT")
    for verb in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER "):
        assert verb not in normalized


def test_query_filters_on_organization_status_and_keeps_the_operational_bridge(scope_db):
    with scope_db.connect() as conn:
        parsed = _ParsedConn(conn)
        find_unhealthy_branches(parsed, STALE_AFTER)

    sql = " ".join(parsed.statements[0].split())
    assert "o.status IN ('active', 'trial')" in sql
    assert "b.status = 'active'" in sql
    assert "ps.customer_id = o.operational_customer_id" in sql
    assert "ps.branch_id = b.operational_branch_id" in sql
    assert "ps.customer_id = o.id" not in sql
    assert "ps.branch_id = b.id" not in sql


def test_query_requires_an_active_installation_through_exists_and_never_a_join(scope_db):
    with scope_db.connect() as conn:
        parsed = _ParsedConn(conn)
        find_unhealthy_branches(parsed, STALE_AFTER)

    sql = " ".join(parsed.statements[0].split())
    assert "AND EXISTS (" in sql
    exists = sql[sql.index("AND EXISTS (") + len("AND EXISTS ("):]
    exists = exists[: exists.index(") ORDER BY")].strip()  # the subquery is the last predicate
    assert exists == (
        "SELECT 1 FROM collector_installations ci "
        "WHERE ci.organization_id = o.id AND ci.branch_id = b.id AND ci.status = 'active'"
    )
    # A JOIN would multiply a branch by its installations; the table appears only inside the EXISTS.
    assert sql.count("collector_installations") == 1
    assert "JOIN collector_installations" not in sql
    # The installation is matched on SaaS ids only -- no operational-id or other fallback in the subquery,
    # and the pipeline_status bridge is not weakened by it.
    assert "operational" not in exists
    assert "ps." not in exists
    assert "ci.organization_id = o.operational_customer_id" not in sql
    assert "ci.branch_id = b.operational_branch_id" not in sql
