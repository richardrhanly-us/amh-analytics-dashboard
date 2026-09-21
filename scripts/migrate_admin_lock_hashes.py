"""One-time migration: legacy PLAINTEXT organization admin-lock passwords -> one-way hashes.

Background. The Admin Settings page keeps an optional per-organization "admin lock" in
`organization_settings.settings_json` under `security`. Older versions of the page stored it as PLAINTEXT
(`security.admin_password`). The merged application stores only a salted hash (`security.admin_password_hash`) and
still ACCEPTS a legacy plaintext value so nobody is locked out, converting it when an admin next saves settings. This
script converts the remaining legacy rows in one controlled step instead of waiting for that.

What it does, per row of `organization_settings`:

    no_lock         nothing to do (no password stored; an empty `admin_password` key is not a credential)
    plaintext_only  hash the plaintext IN MEMORY with the application's own helper, store the hash, remove the plaintext
    hash_only       already migrated: nothing to do
    both            drop the stale plaintext key only (a stored hash already takes precedence over it)
    unexpected      the JSON is not shaped as the application writes it: STOP, change nothing

SAFE BY DEFAULT. Without --execute it is a DRY RUN in a READ ONLY transaction: it cannot write. It prints only counts,
row ids, organization ids/slugs and state names. It never prints, logs or sends back to the database a password value.

Usage (dry run; DATABASE_URL is read from the environment, never from the command line):

    DATABASE_URL=postgresql://... python scripts/migrate_admin_lock_hashes.py

Usage (apply, ONLY the rows a reviewed dry run listed):

    DATABASE_URL=postgresql://... python scripts/migrate_admin_lock_hashes.py \\
        --execute --confirm-database <the database name shown by the dry run> --row-id 12 --row-id 15

--execute needs all three: the flag, --confirm-database equal to the actual target database name, and explicit
--row-id values. It runs ONE transaction: it locks those rows (SELECT ... FOR UPDATE), re-checks their state, and for
each row runs a guarded UPDATE (`jsonb_set` / `#-`, so every unrelated setting is untouched) whose WHERE clause tests the
row id and the old state on the server, so the plaintext is never sent back. It must update exactly one row, re-reads it,
and compares the whole JSON with the expected result. Any failure rolls the whole transaction back.

Re-running is safe: migrated rows are `hash_only` and are skipped.

Exit codes: 0 = done / nothing to do; 1 = bad arguments or could not connect; 2 = refused or failed closed (an
unexpected JSON shape, a credential stored at branch level, a state that changed, a failed post-check). See
docs/production-verification-runbook.md for the procedure, the read-only inventory SQL and the rollback notes.
"""

from __future__ import annotations

import argparse
import copy
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url

from services.admin_lock_service import (
    HASH_KEY,
    LEGACY_PLAINTEXT_KEY,
    hash_admin_password,
    verify_admin_password,
)
from services.privacy_hardening import safe_exception_summary

NO_LOCK = "no_lock"
PLAINTEXT_ONLY = "plaintext_only"
HASH_ONLY = "hash_only"
BOTH = "both"
UNEXPECTED = "unexpected_shape"
STATES = (NO_LOCK, PLAINTEXT_ONLY, HASH_ONLY, BOTH, UNEXPECTED)

# werkzeug's generate_password_hash writes "<method>:<params>$<salt>$<hash>"
_HASH_FORMAT = re.compile(r"^(scrypt|pbkdf2):")


# --- classification: pure, no database, never returns a value ---------------------------------------------------------

@dataclass(frozen=True)
class Classification:
    state: str
    reason: str = ""  # only for UNEXPECTED: a fixed phrase, never a value from the data


def classify(settings: Any) -> Classification:
    """Which of the five states a `settings_json` document is in. Fails CLOSED: anything the application would not have
    written (or could not interpret) is `unexpected_shape`, never guessed at."""
    if not isinstance(settings, dict):
        return Classification(UNEXPECTED, "settings_json is not a JSON object")
    if "security" not in settings:
        return Classification(NO_LOCK)
    security = settings["security"]
    if not isinstance(security, dict):
        return Classification(UNEXPECTED, "security is not a JSON object")

    legacy = security.get(LEGACY_PLAINTEXT_KEY)
    stored_hash = security.get(HASH_KEY)
    if legacy is not None and not isinstance(legacy, str):
        return Classification(UNEXPECTED, "admin_password is not a string")
    if stored_hash is not None and not isinstance(stored_hash, str):
        return Classification(UNEXPECTED, "admin_password_hash is not a string")

    legacy_set = bool(legacy)
    hash_set = bool(stored_hash)
    if hash_set and not _HASH_FORMAT.match(stored_hash or ""):
        return Classification(UNEXPECTED, "admin_password_hash is not a recognised hash format")

    if legacy_set and hash_set:
        return Classification(BOTH)
    if legacy_set:
        return Classification(PLAINTEXT_ONLY)
    if hash_set:
        return Classification(HASH_ONLY)
    return Classification(NO_LOCK)


def expected_after_migrating(old: dict[str, Any], password_hash: str) -> dict[str, Any]:
    """The exact document a `plaintext_only` row must become: the plaintext key replaced by the hash, nothing else changed."""
    new = copy.deepcopy(old)
    del new["security"][LEGACY_PLAINTEXT_KEY]
    new["security"][HASH_KEY] = password_hash
    return new


def expected_after_dropping_stale(old: dict[str, Any]) -> dict[str, Any]:
    """The exact document a `both` row must become: only the stale plaintext key removed."""
    new = copy.deepcopy(old)
    del new["security"][LEGACY_PLAINTEXT_KEY]
    return new


# --- statements (module constants so the tests and the runbook show exactly what runs) --------------------------------

SELECT_ORG_ROWS = """
    SELECT os.id AS settings_id, os.organization_id AS owner_id, o.slug AS slug, os.settings_json AS settings
    FROM organization_settings os
    JOIN organizations o ON o.id = os.organization_id
    ORDER BY os.id
"""
SELECT_BRANCH_ROWS = """
    SELECT bs.id AS settings_id, bs.branch_id AS owner_id, b.slug AS slug, bs.settings_json AS settings
    FROM branch_settings bs
    JOIN branches b ON b.id = bs.branch_id
    ORDER BY bs.id
"""
LOCK_SELECTED_ORG_ROWS = """
    SELECT os.id AS settings_id, os.organization_id AS owner_id, o.slug AS slug, os.settings_json AS settings
    FROM organization_settings os
    JOIN organizations o ON o.id = os.organization_id
    WHERE os.id = ANY(CAST(:ids AS bigint[]))
    ORDER BY os.id
    FOR UPDATE OF os
"""
# The WHERE clause re-tests the old state ON THE SERVER, so the plaintext never has to be sent back to prove it is unchanged.
MIGRATE_UPDATE = """
    UPDATE organization_settings
    SET settings_json = jsonb_set(
            settings_json #- '{security,admin_password}',
            '{security,admin_password_hash}',
            to_jsonb(CAST(:password_hash AS text))),
        updated_at = CURRENT_TIMESTAMP
    WHERE id = :settings_id
      AND jsonb_typeof(settings_json #> '{security,admin_password}') = 'string'
      AND (settings_json #>> '{security,admin_password}') <> ''
      AND coalesce(settings_json #>> '{security,admin_password_hash}', '') = ''
"""
DROP_STALE_UPDATE = """
    UPDATE organization_settings
    SET settings_json = settings_json #- '{security,admin_password}',
        updated_at = CURRENT_TIMESTAMP
    WHERE id = :settings_id
      AND jsonb_typeof(settings_json #> '{security,admin_password}') = 'string'
      AND (settings_json #>> '{security,admin_password}') <> ''
      AND jsonb_typeof(settings_json #> '{security,admin_password_hash}') = 'string'
      AND (settings_json #>> '{security,admin_password_hash}') <> ''
"""
SELECT_ONE_SETTINGS = "SELECT settings_json AS settings FROM organization_settings WHERE id = :settings_id"


@dataclass(frozen=True)
class Row:
    table: str
    settings_id: int
    owner_id: int
    slug: str
    settings: Any = field(repr=False)  # the document itself: held in memory only, never printed
    classification: Classification = field(default_factory=lambda: Classification(NO_LOCK))


def _fetch(conn: Connection, sql: str, table: str, params: dict[str, Any] | None = None) -> list[Row]:
    rows = []
    for record in conn.execute(text(sql), params or {}).mappings():
        rows.append(Row(table, record["settings_id"], record["owner_id"], record["slug"], record["settings"],
                        classify(record["settings"])))
    return rows


class Refused(Exception):
    """The run was stopped on purpose. The message is a fixed phrase and never contains data from the database."""


# --- the run --------------------------------------------------------------------------------------------------------

def _counts(rows: list[Row]) -> dict[str, int]:
    return {state: sum(1 for r in rows if r.classification.state == state) for state in STATES}


def _describe(row: Row) -> str:
    return f"settings_row_id={row.settings_id} (organization_id={row.owner_id}, slug={row.slug})"


def inspect(conn: Connection) -> tuple[list[Row], list[Row]]:
    return _fetch(conn, SELECT_ORG_ROWS, "organization_settings"), _fetch(conn, SELECT_BRANCH_ROWS, "branch_settings")


def refusals(org_rows: list[Row], branch_rows: list[Row]) -> list[str]:
    """Reasons to stop before changing anything (empty when it is safe to go on)."""
    reasons = [f"unexpected JSON shape in organization_settings {_describe(r)}: {r.classification.reason}"
               for r in org_rows if r.classification.state == UNEXPECTED]
    reasons += [
        f"branch_settings {_describe(r)} holds an admin-lock credential or an unexpected shape "
        f"({r.classification.state}); branch-level values override the organization's, so this needs a manual decision"
        for r in branch_rows if r.classification.state != NO_LOCK
    ]
    return reasons


def report(org_rows: list[Row], branch_rows: list[Row], out) -> list[Row]:
    counts = _counts(org_rows)
    print(f"organization_settings: {len(org_rows)} rows -> " + ", ".join(f"{s} {counts[s]}" for s in STATES), file=out)
    print(f"branch_settings: {len(branch_rows)} rows, {sum(1 for r in branch_rows if r.classification.state != NO_LOCK)} "
          "with any admin-lock key or unexpected shape", file=out)
    actionable = [r for r in org_rows if r.classification.state in (PLAINTEXT_ONLY, BOTH)]
    for row in actionable:
        verb = "would hash the plaintext" if row.classification.state == PLAINTEXT_ONLY else "would drop the stale plaintext key"
        print(f"  {verb}: {_describe(row)}", file=out)
    return actionable


def dry_run(engine, out) -> int:
    with engine.connect() as conn, conn.begin():
        conn.execute(text("SET TRANSACTION READ ONLY"))  # it cannot write, even by mistake
        org_rows, branch_rows = inspect(conn)
    print("Mode: DRY RUN (read-only transaction; nothing was changed)", file=out)
    actionable = report(org_rows, branch_rows, out)
    stop = refusals(org_rows, branch_rows)
    for reason in stop:
        print(f"STOP: {reason}", file=out)
    if stop:
        print("Fail closed: --execute would refuse until these are resolved.", file=out)
        return 2
    if actionable:
        ids = " ".join(f"--row-id {r.settings_id}" for r in actionable)
        print(f"To apply exactly these rows: --execute --confirm-database {engine.url.database} {ids}", file=out)
    else:
        print("Nothing to migrate.", file=out)
    return 0


def _apply_row(conn: Connection, row: Row) -> str:
    """Change one locked row, verify it, and return what was done. Raises Refused if anything is not as expected."""
    old = row.settings
    plaintext: str | None = None
    if row.classification.state == PLAINTEXT_ONLY:
        plaintext = old["security"][LEGACY_PLAINTEXT_KEY]
        password_hash = hash_admin_password(plaintext)  # the application's own helper, in memory only
        if not verify_admin_password(plaintext, {HASH_KEY: password_hash}):
            raise Refused(f"the computed hash did not verify for {_describe(row)}")
        result = conn.execute(text(MIGRATE_UPDATE), {"settings_id": row.settings_id, "password_hash": password_hash})
        expected = expected_after_migrating(old, password_hash)
        action = "hashed"
    else:  # BOTH: the stored hash already wins, so only the stale plaintext key goes
        result = conn.execute(text(DROP_STALE_UPDATE), {"settings_id": row.settings_id})
        expected = expected_after_dropping_stale(old)
        action = "dropped stale plaintext"
    if result.rowcount != 1:
        raise Refused(f"the guarded update did not match exactly one row for {_describe(row)}")

    after = conn.execute(text(SELECT_ONE_SETTINGS), {"settings_id": row.settings_id}).mappings().one()["settings"]
    if after != expected:
        raise Refused(f"the row's JSON after the update is not exactly the expected result for {_describe(row)}")
    if plaintext is not None and not verify_admin_password(plaintext, after.get("security")):
        raise Refused(f"the stored hash does not verify the original password for {_describe(row)}")
    return action


def execute(engine, ids: list[int], out) -> int:
    with engine.begin() as conn:  # ONE transaction: any exception below rolls everything back
        org_rows, branch_rows = inspect(conn)
        stop = refusals(org_rows, branch_rows)
        if stop:
            for reason in stop:
                print(f"STOP: {reason}", file=out)
            raise Refused("unresolved findings; nothing was changed")

        locked = _fetch(conn, LOCK_SELECTED_ORG_ROWS, "organization_settings", {"ids": ids})
        found = {r.settings_id for r in locked}
        missing = sorted(set(ids) - found)
        if missing:
            raise Refused(f"row ids not found in organization_settings: {missing}")

        done = []
        for row in locked:  # re-classified UNDER the row lock
            state = row.classification.state
            if state == UNEXPECTED:
                raise Refused(f"unexpected JSON shape under lock for {_describe(row)}")
            if state in (PLAINTEXT_ONLY, BOTH):
                done.append((row, _apply_row(conn, row)))
            else:
                print(f"skipped {_describe(row)}: already {state}", file=out)
    print("Mode: EXECUTE (committed)", file=out)
    for row, action in done:
        print(f"  {action}: {_describe(row)}", file=out)
    print(f"Done: {len(done)} row(s) changed. Re-run the dry run to confirm it reports nothing left.", file=out)
    return 0


# --- command line ---------------------------------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert legacy plaintext admin-lock passwords to hashes (dry run by default).")
    parser.add_argument("--execute", action="store_true", help="Actually change rows. Without it: read-only dry run.")
    parser.add_argument("--confirm-database", metavar="NAME",
                        help="With --execute: must equal the target database name (shown by the dry run).")
    parser.add_argument("--row-id", type=int, action="append", default=[], metavar="ID",
                        help="With --execute: an organization_settings.id from the reviewed dry run. Repeat for each row.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    args = parse_args(argv)
    url_text = os.environ.get("DATABASE_URL", "")
    if not url_text:
        print("DATABASE_URL is not set.", file=out)
        return 1
    if args.execute and (not args.confirm_database or not args.row_id):
        print("--execute needs --confirm-database <name> and at least one --row-id from a reviewed dry run.", file=out)
        return 1
    try:
        url = make_url(url_text)
    except Exception:
        print("DATABASE_URL could not be parsed.", file=out)
        return 1
    if args.execute and args.confirm_database != url.database:
        print("--confirm-database does not match the target database; nothing was changed.", file=out)
        return 1

    print(f"Target: host={url.host} database={url.database}", file=out)  # never the user name or password
    engine = create_engine(url, hide_parameters=True)
    try:
        if args.execute:
            return execute(engine, sorted(set(args.row_id)), out)
        return dry_run(engine, out)
    except Refused as refused:
        print(f"REFUSED: {refused}. The transaction was rolled back; nothing was changed.", file=out)
        return 2
    except Exception as exc:
        # Never the exception's own message: a driver's text quotes rows and values.
        print(f"FAILED: {safe_exception_summary(exc)}. Nothing was committed.", file=out)
        return 2 if args.execute else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
