"""scripts/check_test_freshness.py: catches stale-test hazards without flagging legitimate literals.

The guard is only useful if it is both SENSITIVE (the two real failure modes -- a copied "current version" and a
fixed calendar timestamp standing in for "now" -- are caught, in every shape they take) and QUIET (historical
versions, parser fixtures, explicit expiry timestamps and injected clocks are not). Each is proven here, and the
whole repository is held to the guard by test_the_repository_passes_its_own_freshness_guard.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from scripts import check_test_freshness as guard

REPO_ROOT = Path(__file__).resolve().parent.parent
CURRENT = "5.6.7"  # an invented "current release" for the snippets; the real one is never written in this file


def scan(source: str, **kwargs):
    return guard.scan_source(textwrap.dedent(source), "tests/test_sample.py", **kwargs)


def rules(source: str, **kwargs) -> list[str]:
    return [f.rule for f in scan(source, **kwargs)]


# --- 1. a stale "current version" assertion is caught --------------------------------------------------------------

def test_a_stale_current_version_assertion_is_caught():
    source = '''
        import collector

        def test_the_build_reports_its_version():
            assert collector.__version__ == "1.0.3"
    '''
    (finding,) = scan(source, current_version=CURRENT)

    assert finding.rule == "FRESH001" and finding.line == 5
    assert "collector.__version__" in finding.message and "check_release_readiness" in finding.message  # says what to do


def test_the_literal_is_caught_whether_it_is_stale_or_currently_true():
    for literal in ("1.0.3", CURRENT):
        source = f'''
            import collector

            def test_version():
                assert collector.__version__ == "{literal}"
        '''
        assert rules(source, current_version=CURRENT) == ["FRESH001"], literal


@pytest.mark.parametrize(
    "assertion",
    [
        'assert "1.0.3" == collector.__version__',
        'assert collector.__version__ != "1.0.3"',
        'assert collector.__version__ in ("1.0.3", "1.0.4")',
        'assert collector.__version__.startswith("1.0") and collector.__version__ == "1.0.3"',
    ],
)
def test_the_shape_of_the_comparison_does_not_hide_it(assertion):
    source = f"import collector\n\ndef test_v():\n    {assertion}\n"

    assert "FRESH001" in rules(source, current_version=CURRENT)


def test_an_alias_of_the_authority_is_recognised():
    source = '''
        from collector import __version__ as SOURCE_VERSION

        def test_v():
            assert SOURCE_VERSION == "1.0.3"
    '''

    assert rules(source, current_version=CURRENT) == ["FRESH001"]


def test_comparing_against_the_authority_or_a_reported_value_is_fine():
    source = '''
        import collector
        from collector import __version__ as SOURCE_VERSION

        def test_reports_the_authority():
            assert payload["collector_version"] == collector.__version__
            assert manifest["version"] == SOURCE_VERSION
            assert info.collector_version == collector.__version__
            assert collector.__version__            # non-empty
            assert output == f"{collector.__version__}\\n"
    '''

    assert rules(source, current_version=CURRENT) == []


def test_the_current_release_literal_hardcoded_in_any_assertion_is_caught():
    source = f'''
        def test_heartbeat():
            assert row["collector_version"] == "{CURRENT}"
            assert "{CURRENT}" in token["description"]
    '''

    findings = scan(source, current_version=CURRENT)

    assert [f.rule for f in findings] == ["FRESH002", "FRESH002"]
    assert "collector.__version__" in findings[0].message


def test_the_current_release_literal_is_only_a_problem_inside_an_assertion():
    source = f'''
        def test_a_heartbeat_reports_its_version():
            response = _heartbeat(collector_version="{CURRENT}")
            assert response.status_code == 200
    '''

    assert rules(source, current_version=CURRENT) == []  # request data, not an assertion of "the current version"


# --- 2. an allowed historical version fixture is NOT caught -----------------------------------------------------------

def test_an_allowed_historical_version_fixture_is_not_caught():
    source = '''
        # A verifier's table of outputs: none of these is "the current version".
        PROBE_CASES = {
            "match": {"ExpectedVersion": "1.0.3", "ActualOutput": ["1.0.3"], "ExitCode": 0},
            "mismatch": {"ExpectedVersion": "1.0.3", "ActualOutput": ["1.0.2"], "ExitCode": 0},
        }
        WRONG_VERSION = "1.0.2" if collector_version != "1.0.2" else "1.0.1"

        def _installation(engine, version="1.0.2"):
            ...

        def test_the_admin_entered_version_is_not_overwritten_by_enrollment():
            _installation(engine, version="1.0.2")
            enroll(code)
            assert _row(101)["collector_version"] == "1.0.2"  # admin-entered, older than the running release

        def test_a_newer_heartbeat_replaces_it():
            _heartbeat(collector_version="1.0.3")
            assert _row(101)["collector_version"] == "1.0.3"
    '''

    assert rules(source, current_version=CURRENT) == []


def test_parser_and_migration_fixtures_with_release_shaped_text_are_not_matched():
    source = '''
        def test_migration_seed_data():
            assert seeded_rows == [("agent", "0.1.0"), ("sorter", "2.4.11")]

        def test_schema_revision():
            assert revision == "20260918_0001"
    '''

    assert rules(source, current_version=CURRENT) == []


# --- release-named tests --------------------------------------------------------------------------------------------

def test_a_current_state_test_name_naming_a_release_is_caught():
    source = '''
        def test_the_next_build_reports_1_0_3():
            assert True
    '''
    (finding,) = scan(source)

    assert finding.rule == "FRESH003" and "test_the_next_build_reports_1_0_3" in finding.message


@pytest.mark.parametrize("name", ["test_release_1_0_3", "test_v1_0_2_layout", "test_reports_10_2_44_correctly"])
def test_release_shaped_names_are_caught(name):
    assert rules(f"def {name}():\n    pass\n") == ["FRESH003"]


@pytest.mark.parametrize(
    "name",
    ["test_collector_reports_current_release_version", "test_sha256_of_the_manifest", "test_bad_rows_2026_09_20",
     "test_state_schema_1", "_fixture_for_1_0_3", "helper_1_0_3"],
)
def test_ordinary_names_and_non_test_helpers_are_not_caught(name):
    assert rules(f"def {name}():\n    pass\n") == []


def test_a_test_class_name_is_checked_too():
    assert rules("class TestBuild_1_0_3:\n    pass\n") == ["FRESH003"]


# --- 3. a fixed "current time" hazard is caught ------------------------------------------------------------------------

def test_a_fixed_current_time_hazard_is_caught():
    # The exact shape that expired 22 enrollment tests: a fixed NOW stamped into fixtures while the code
    # under test reads the real clock.
    source = '''
        from datetime import UTC, datetime, timedelta

        NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)

        def _generate(engine, installation_id, **kwargs):
            kwargs.setdefault("now", NOW)
            return create_enrollment_code(engine, installation_id, **kwargs)
    '''
    (finding,) = scan(source)

    assert finding.rule == "FRESH004" and finding.line == 4
    assert "NOW" in finding.message and "controlled clock" in finding.message


@pytest.mark.parametrize(
    "assignment",
    [
        "NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)",
        "CURRENT_TIME = datetime(2026, 9, 20, tzinfo=UTC)",
        "FROZEN_NOW = datetime.fromisoformat('2026-09-20T12:00:00+00:00')",
        "TODAY = pd.Timestamp('2026-03-30').date()",
        "NOW_CT = datetime(2026, 3, 30, 12, 0, tzinfo=APP_TZ)",
        "TEST_TODAY = date(2026, 3, 30)",
        "UTCNOW = '2026-09-20T12:00:00Z'",
        "ENDPOINT_NOW: datetime = datetime(2026, 9, 20, 12, 1, tzinfo=UTC)",
    ],
)
def test_every_spelling_of_a_fixed_now_constant_is_caught(assignment):
    source = f"from datetime import *\n{assignment}\n"

    assert rules(source) == ["FRESH004"]


def test_a_now_constant_derived_from_the_real_clock_is_fine():
    source = "NOW = datetime.now(UTC)\nTODAY = date.today()\nSTARTED = datetime.now(UTC)\n"

    assert rules(source) == []  # (a different hazard, and not a FIXED timestamp)


def test_a_fixed_now_is_fine_when_the_module_uses_the_shared_controlled_clock():
    source = '''
        from controlled_clock import ControlledClock

        CLOCK = ControlledClock()
        NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    '''

    assert rules(source) == []  # the code under test reads CLOCK, so no fixed instant can go stale against it


def test_only_module_level_now_like_names_count():
    source = '''
        def test_x():
            now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)          # a local, handed to the code under test
            NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)          # not module level
            assert find_stale(conn, now=now)

        class Fixtures:
            NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    '''

    assert rules(source) == []


# --- 4. an explicit historical / expiry timestamp is allowed -----------------------------------------------------------

def test_an_explicit_historical_or_expiry_timestamp_is_allowed():
    source = '''
        from datetime import UTC, datetime

        EXPIRED_AT = datetime(2020, 1, 1, tzinfo=UTC)
        LONG_AGO = datetime(1999, 12, 31, 23, 59, tzinfo=UTC)
        HISTORICAL_CHECKIN_TIME = "2026-01-01T09:15:00"
        BOUNDARY_EXPIRES_AT = datetime(2026, 9, 20, 12, 30, tzinfo=UTC)
        REJECT_DATE = pd.Timestamp("2025-06-01").date()

        def test_an_expired_code_is_refused(world):
            code = _insert_code(world, 101, expires_at=datetime(2020, 1, 1, tzinfo=UTC))
            assert _enroll(code).status_code == 400

        def test_a_parser_fixture():
            assert parse("2026-09-20 09:15:00 barcode") == {"event_time": "2026-09-20T09:15:00"}
    '''

    assert rules(source) == []


# --- annotations ----------------------------------------------------------------------------------------------------------

def test_an_annotation_with_a_reason_allows_a_legitimate_exception():
    source = '''
        NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)  # freshness: allow FRESH004 -- passed as an explicit cutoff at every call
    '''

    assert rules(source) == []


def test_an_annotation_on_the_line_above_is_honoured():
    source = '''
        # freshness: allow FRESH004 -- injected into every function under test
        NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    '''

    assert rules(source) == []


def test_an_annotation_must_carry_a_reason_and_a_known_rule():
    for annotation in ("# freshness: allow FRESH004", "# freshness: allow FRESH004 --", "# freshness: allow FRESH999 -- because",
                       "# freshness: skip FRESH004 -- because"):
        source = f"NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)  {annotation}\n"

        assert rules(source) == ["FRESH000", "FRESH004"], annotation  # malformed AND the hazard still reported


def test_an_annotation_only_allows_the_rule_it_names():
    source = "NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)  # freshness: allow FRESH002 -- wrong rule for this\n"

    assert rules(source) == ["FRESH004"]


def test_text_that_merely_looks_like_an_annotation_inside_a_string_is_not_one():
    source = '''
        HELP = "# freshness: allow FRESH004 -- not a real comment"
        NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    '''

    assert rules(source) == ["FRESH004"]


def test_an_annotation_above_a_decorated_test_covers_its_name():
    source = '''
        # freshness: allow FRESH003 -- deliberately pinned to the 1.0.3 layout
        @pytest.mark.parametrize("x", [1])
        def test_legacy_layout_1_0_3(x):
            pass
    '''

    assert rules(source) == []


# --- wall-clock-free modules ------------------------------------------------------------------------------------------------

_CLEAN_CLOCKED = textwrap.dedent('''
    from controlled_clock import ControlledClock

    CLOCK = ControlledClock()

    def test_expiry():
        code = generate(now=CLOCK.instant)
        CLOCK.advance(timedelta(minutes=31))
        assert redeem(code, now=CLOCK.instant) is None
''')


def test_a_clock_free_module_using_the_controlled_clock_is_clean():
    assert rules(_CLEAN_CLOCKED, wall_clock_free=True) == []


@pytest.mark.parametrize(
    "read",
    ["datetime.now(UTC)", "datetime.utcnow()", "datetime.today()", "date.today()", "time.time()", "enrollment.datetime.now(UTC)",
     "dt.datetime.now()", "pd.Timestamp.now()"],
)
def test_reading_the_wall_clock_in_a_clock_free_module_is_caught(read):
    source = _CLEAN_CLOCKED + f"\ndef test_uses_today():\n    assert code.expires_at > {read}\n"

    assert rules(source, wall_clock_free=True) == ["FRESH005"]


def test_the_same_read_is_ignored_in_a_module_that_is_not_clock_free():
    source = "def test_x():\n    assert code.expires_at > datetime.now(UTC)\n"

    assert rules(source) == []


def test_a_clock_free_module_that_does_not_use_the_controlled_clock_is_caught():
    source = "def test_x():\n    assert True\n"

    (finding,) = scan(source, wall_clock_free=True)

    assert finding.rule == "FRESH005" and "controlled clock" in finding.message


def test_a_wall_clock_read_can_be_annotated_where_it_is_the_point():
    source = _CLEAN_CLOCKED + textwrap.dedent('''
        def test_the_service_reads_the_controlled_clock():
            assert enrollment.datetime.now(UTC) == CLOCK.instant  # freshness: allow FRESH005 -- reads the controlled clock through the module under test
    ''')

    assert rules(source, wall_clock_free=True) == []


def test_reading_the_controlled_clock_is_not_a_wall_clock_read():
    source = _CLEAN_CLOCKED + "\ndef test_more():\n    assert CLOCK.now(UTC) == CLOCK.instant\n"

    assert rules(source, wall_clock_free=True) == []


# --- robustness ---------------------------------------------------------------------------------------------------------------

def test_a_test_file_that_cannot_be_parsed_is_reported_not_skipped():
    (finding,) = scan("def broken(:\n")

    assert finding.rule == "FRESH000" and "cannot be parsed" in finding.message


def test_findings_are_ordered_and_print_as_path_line_rule_message():
    source = "NOW = datetime(2026, 8, 28, tzinfo=UTC)\n\ndef test_a_1_0_3():\n    assert collector.__version__ == 'x'\n"

    findings = scan(source, current_version=CURRENT)

    assert [f.line for f in findings] == sorted(f.line for f in findings)
    assert str(findings[0]).startswith("tests/test_sample.py:1: FRESH004 ")


# --- scanning a repository -------------------------------------------------------------------------------------------------------

def _repo(tmp_path: Path, version: str = CURRENT, **tests: str) -> Path:
    (tmp_path / "collector").mkdir(parents=True)
    (tmp_path / "collector" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (tmp_path / "tests").mkdir()
    for name, body in tests.items():
        (tmp_path / "tests" / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return tmp_path


def test_the_current_release_is_read_from_the_authority_without_importing_it(tmp_path):
    repo = _repo(tmp_path)

    assert guard.read_release_version(repo) == CURRENT
    (repo / "collector" / "__init__.py").write_text("__version__ = compute()\n", encoding="utf-8")
    assert guard.read_release_version(repo) is None  # not a literal: refused rather than evaluated


def test_a_repository_scan_applies_the_current_release_to_every_test_file(tmp_path):
    repo = _repo(tmp_path, test_a=f'def test_v():\n    assert row["v"] == "{CURRENT}"\n', test_b="def test_ok():\n    pass\n")

    findings = guard.scan_tests(repo, wall_clock_free=())

    assert [(f.path, f.rule) for f in findings] == [("tests/test_a.py", "FRESH002")]


def test_a_repository_scan_applies_the_clock_free_rule_only_to_listed_modules(tmp_path):
    read = "def test_x():\n    assert code.expires_at > datetime.now(UTC)\n"
    repo = _repo(tmp_path, test_listed=read, test_other=read)

    findings = guard.scan_tests(repo, wall_clock_free=("tests/test_listed.py",))

    assert {(f.path, f.rule) for f in findings} == {("tests/test_listed.py", "FRESH005")}


def test_a_listed_clock_free_module_that_no_longer_exists_is_reported(tmp_path):
    repo = _repo(tmp_path, test_other="def test_x():\n    pass\n")

    (finding,) = guard.scan_tests(repo, wall_clock_free=("tests/test_gone.py",))

    assert finding.rule == "FRESH005" and "does not exist" in finding.message


def test_cache_directories_are_not_scanned(tmp_path):
    repo = _repo(tmp_path)
    cache = repo / "tests" / "__pycache__"
    cache.mkdir()
    (cache / "junk.py").write_text("NOW = datetime(2026, 1, 1, tzinfo=UTC)\n", encoding="utf-8")

    assert guard.scan_tests(repo, wall_clock_free=()) == []


def test_the_command_line_exits_zero_when_clean_and_one_with_findings(tmp_path, capsys):
    # (the repository's own clock-free module must exist, so the tiny repos carry a clean one)
    clean = _repo(tmp_path / "clean", test_collector_enrollment=_CLEAN_CLOCKED, test_ok="def test_ok():\n    pass\n")
    dirty = _repo(tmp_path / "dirty", test_collector_enrollment=_CLEAN_CLOCKED, test_bad="NOW = datetime(2026, 8, 28, tzinfo=UTC)\n")

    assert guard.main(["--repo-root", str(clean)]) == 0
    assert "Test freshness OK" in capsys.readouterr().out
    assert guard.main(["--repo-root", str(dirty)]) == 1
    captured = capsys.readouterr()
    assert "tests/test_bad.py:1: FRESH004" in captured.out and "1 freshness finding" in captured.err


# --- production defaults: a hardcoded Collector release (FRESH006) -----------------------------------------------------

def production(source: str, path: str = "super_admin/pages/Example.py") -> list[str]:
    return [f.rule for f in guard.scan_production_source(textwrap.dedent(source), path)]


def test_the_stale_super_admin_defaults_that_motivated_this_rule_are_caught():
    # Verbatim shapes of the two fields that shipped hardcoded to the previous release.
    provision = '''
        with st.form("provision_library_form"):
            installation_version = st.text_input("Collector version", value="1.0.3")
    '''
    manage = '''
        with st.form("add_installation_form"):
            new_version = st.text_input("Collector version", value="1.0.3")
    '''

    for source in (provision, manage):
        (finding,) = guard.scan_production_source(textwrap.dedent(source), "super_admin/pages/Example.py")
        assert finding.rule == "FRESH006" and "'1.0.3'" in finding.message
        assert "from collector import __version__" in finding.message  # says how to fix it


@pytest.mark.parametrize(
    "code",
    [
        'st.text_input("Collector version", value="1.0.3")',
        'st.text_input(label="Collector version", value="2.0.0")',
        'st.text_input("Installed Collector Version", value="1.0.3-rc1")',
        'st.multiselect("Collector version", options=[], default="1.0.3")',
        'create_collector_installation(name="x", collector_version="1.0.3")',
        "DEFAULT_COLLECTOR_VERSION = '1.0.3'",
        "collectorVersion = '1.0.3'",
        "payload['collector_version'] = '1.0.3'",
        "self.collector_version: str = '1.0.3'",
        "row = {'collector_version': '1.0.3', 'status': 'provisioning'}",
        "def register(name, collector_version='1.0.3'):\n    pass",
        "def register(*, collector_version='1.0.3'):\n    pass",
        "handler = lambda collector_version='1.0.3': collector_version",
    ],
)
def test_every_shape_of_a_hardcoded_collector_release_default_is_caught(code):
    assert production(code) == ["FRESH006"]


@pytest.mark.parametrize(
    "code",
    [
        'st.text_input("Collector version", value=COLLECTOR_VERSION)',                     # the fix
        'st.text_input("Collector version", value=installation["collector_version"] or "")',  # a stored value
        'st.text_input("Collector version", value="")',
        "create_collector_installation(name='x', collector_version=data.collector_version)",
        "create_collector_installation(name='x', collector_version=None)",
        # other versions are not the Collector's release: schema, API, agent, protocol ...
        'st.text_input("API version", value="2.0.0")',
        'st.text_input("Schema version", value="1.0.0")',
        "SCHEMA_VERSION = '1.0.0'",
        "agent_version = '0.1.0'",
        "def f(api_version='2.0.0'):\n    pass",
        "row = {'schema_version': '1.0.0'}",
        # not release-shaped, so not a release
        "collector_version = 'unknown'",
        "collector_version = ''",
        "collector_version = '1.0'",
        # SQL / prose mentioning one are strings, not defaults
        'sql = "UPDATE collector_installations SET collector_version = \'1.0.3\'"',
        '"""Docs: the Collector version defaults to 1.0.3 in the old form."""',
    ],
)
def test_other_versions_stored_values_and_prose_are_not_flagged(code):
    assert production(code) == []


def test_only_a_collector_version_label_makes_a_widget_value_a_release_default():
    source = 'st.text_input("Notes", value="1.0.3")\nst.text_input("Hostname", value="1.2.3")\n'

    assert production(source) == []  # a release-shaped string in an unrelated field is not this rule's business


def test_a_hardcoded_release_default_can_be_annotated_where_it_is_the_point():
    source = 'st.text_input("Collector version", value="1.0.3")  # freshness: allow FRESH006 -- a deliberately pinned legacy field'

    assert production(source) == []


def test_the_annotation_on_a_multiline_call_goes_on_the_offending_keyword():
    source = '''
        st.text_input(
            "Collector version",
            value="1.0.3",  # freshness: allow FRESH006 -- the legacy collector predates the authority
        )
    '''

    assert production(source) == []


def test_production_scanning_covers_the_ui_and_services_but_not_tests_or_the_legacy_agent(tmp_path):
    hazard = 'st.text_input("Collector version", value="1.0.3")\n'
    repo = _repo(tmp_path)
    for relative in ("super_admin/pages/A.py", "src/services/b.py", "tests/test_c.py", "agent/legacy.py", "collector/d.py"):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(hazard, encoding="utf-8")
    (repo / "main.py").write_text(hazard, encoding="utf-8")

    findings = guard.scan_production(repo)

    assert sorted(f.path for f in findings) == ["main.py", "src/services/b.py", "super_admin/pages/A.py"]
    assert {f.rule for f in findings} == {"FRESH006"}


def test_an_unparseable_production_file_is_reported(tmp_path):
    repo = _repo(tmp_path)
    (repo / "main.py").write_text("def broken(:\n", encoding="utf-8")

    (finding,) = guard.scan_production(repo)

    assert finding.rule == "FRESH000" and finding.path == "main.py"


def test_the_repository_scan_and_the_command_line_include_production_defaults(tmp_path, capsys):
    repo = _repo(tmp_path, test_collector_enrollment=_CLEAN_CLOCKED)
    page = repo / "super_admin" / "pages" / "Provision_Library.py"
    page.parent.mkdir(parents=True)
    page.write_text('st.text_input("Collector version", value="1.0.3")\n', encoding="utf-8")

    assert [(f.path, f.rule) for f in guard.scan_repository(repo)] == [("super_admin/pages/Provision_Library.py", "FRESH006")]
    assert guard.main(["--repo-root", str(repo)]) == 1
    assert "super_admin/pages/Provision_Library.py:1: FRESH006" in capsys.readouterr().out


# --- the repository itself ---------------------------------------------------------------------------------------------------------

def test_the_repository_passes_its_own_freshness_guard():
    findings = guard.scan_repository(REPO_ROOT)  # tests AND production defaults

    assert findings == [], "\n".join(str(f) for f in findings)


def test_the_enrollment_tests_are_held_to_the_clock_free_rule():
    assert "tests/test_collector_enrollment.py" in guard.WALL_CLOCK_FREE_TESTS
    assert (REPO_ROOT / "tests" / "test_collector_enrollment.py").is_file()
    assert (REPO_ROOT / "tests" / "controlled_clock.py").is_file()


def test_every_rule_the_guard_can_report_is_documented():
    documented = set(guard.RULES)

    for source in ("assert collector.__version__ == 'x'\n", "def test_a_1_0_3(): pass\n", "NOW = datetime(2026, 1, 1)\n",
                   "def broken(:\n", "NOW = datetime(2026, 1, 1)  # freshness: allow\n"):
        assert {f.rule for f in guard.scan_source(source, wall_clock_free=False)} <= documented
    assert {f.rule for f in guard.scan_source("x = 1\n", wall_clock_free=True)} <= documented
    assert {f.rule for f in guard.scan_production_source("collector_version = '1.0.3'\n")} <= documented
    assert "FRESH006" in documented
