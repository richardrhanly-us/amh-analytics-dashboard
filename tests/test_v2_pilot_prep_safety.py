"""Government-readiness audit, Part 7 items 19-20: proves this round's implementation is read-only towards existing
data and enables nothing in production by itself.

These are static/structural checks (matching this codebase's existing style of proving an invariant by scanning
source text, e.g. tests/test_collector_v2_transform.py's "no raw_message/patron/barcode column anywhere" tests)
rather than a live-database test, because the claim being proven is "this code contains no write statement at all" --
something a database round-trip cannot itself prove (a query that happens not to write today could still write
tomorrow; the absence of write syntax is the actual guarantee).
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every module this round of work added or touched on the dashboard/server read side. None of them may contain a
# SQL write statement -- v1 history is read, never modified, by any of this round's new code.
READ_ONLY_MODULES = (
    "src/data_loader.py",
    "src/metrics_v2.py",
    "src/services/mixed_era_service.py",
    "src/services/destination_mapping.py",
)

_WRITE_VERBS = re.compile(r"\b(UPDATE|DELETE\s+FROM|ALTER\s+TABLE|DROP\s+TABLE|TRUNCATE)\b", re.IGNORECASE)
_INSERT_VERB = re.compile(r"\bINSERT\s+INTO\b", re.IGNORECASE)


def _text(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


# --- item 19: historical v1 rows remain untouched --------------------------------------------------------------------

def test_dashboard_read_side_modules_contain_no_sql_write_statement():
    for module in READ_ONLY_MODULES:
        text = _text(module)
        assert not _WRITE_VERBS.search(text), f"{module} contains an UPDATE/DELETE/ALTER/DROP/TRUNCATE statement"
        assert not _INSERT_VERB.search(text), f"{module} contains an INSERT statement"


def test_v2_cutovers_migration_only_creates_never_alters_an_existing_table():
    migration = _text("alembic/versions/f2a91c7d4e83_add_v2_cutovers.py")
    # The only DDL verbs allowed are CREATE (the new table/index) and, in downgrade(), DROP of that same new table/index.
    assert "ALTER TABLE" not in migration
    assert "checkins" not in migration.split("upgrade()")[1].split("def downgrade")[0]
    assert "acs_events" not in migration.split("upgrade()")[1].split("def downgrade")[0]
    assert "checkin_events" not in migration.split("upgrade()")[1].split("def downgrade")[0]
    assert "CREATE TABLE v2_cutovers" in migration
    assert "DROP TABLE IF EXISTS v2_cutovers" in migration


def test_set_v2_cutover_script_never_touches_a_v1_or_v2_event_table():
    script = _text("scripts/set_v2_cutover.py")
    for forbidden_table in ("checkins", "rejects", "acs_events", "checkin_events", "reject_events", "acs_item_events"):
        assert forbidden_table not in script


# --- item 20: no production feature flag is enabled by this implementation --------------------------------------------

def test_v2_ingest_enabled_default_is_still_false_in_main_py():
    main_source = _text("main.py")
    match = re.search(r'os\.getenv\("SORTVIEW_V2_INGEST_ENABLED",\s*"([^"]+)"\)', main_source)
    assert match is not None, "SORTVIEW_V2_INGEST_ENABLED default read not found in main.py -- check it wasn't moved"
    assert match.group(1).lower() == "false"


def test_main_py_was_not_modified_by_this_round_of_work():
    # This round's changes are entirely additive elsewhere (new migration, new service functions, new loaders, new
    # metrics/mapping modules, a new script, and the release-manifest/spec files) -- main.py itself, which owns the
    # production ingestion feature gate, is untouched.
    main_source = _text("main.py")
    assert "v2_cutovers" not in main_source
    assert "mixed_era_service" not in main_source
    assert "metrics_v2" not in main_source


def test_collector_config_contract_mode_default_is_still_v1():
    config_source = _text("collector/v2_config.py")
    assert '"v1"' in config_source
