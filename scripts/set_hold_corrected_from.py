"""Reviews, sets or clears a sorter site's HOLD CLASSIFICATION MARKER: the `hold_classification` key of its branch's
settings document (services.hold_classification), {"corrected_from": "YYYY-MM-DD"}.

    python scripts/set_hold_corrected_from.py review [--org-slug SLUG] [--database-url URL]
    python scripts/set_hold_corrected_from.py set    --org-slug SLUG --sorter-slug SLUG --confirm-branch-name NAME \\
                                                     --corrected-from YYYY-MM-DD --operator-email EMAIL [--note TEXT]
    python scripts/set_hold_corrected_from.py clear  --org-slug SLUG --sorter-slug SLUG --confirm-branch-name NAME \\
                                                     --operator-email EMAIL [--note TEXT]

WHAT THE MARKER MEANS (docs/hold-count-accuracy.md, decision D5). From `corrected_from` on, the site's Contract v2 hold
records come from a CV-aware collector (1.0.16 or later) and are counted as accurate; before it, Westside holds cannot be
told from Westside transits, and the holds report labels those days partial. Without a marker every v2 day is
uncorrected.

WHEN TO SET IT. Only after the site's upgrade is VERIFIED (installation Active, reporting 1.0.16 or later, a scheduled run
with result 0), and to the FIRST FULL LOCAL DAY after that verification -- never the upgrade day, which mixes old and new
collector data. This script cannot see the verification; the operator vouches for it. It does refuse a date that is
today or earlier in the product's zone (the API's own day boundaries), so a marker can never reach back over a day that
had already begun when it was set.

WHEN TO CLEAR IT. Whenever the site is taken back below 1.0.16 (a rollback). Every v2 day is then uncorrected again --
the conservative side. After a new verified upgrade, set it again to the new first full day.

SAFE BY DEFAULT, AND AUDITED. Without --execute nothing touches a database. With --execute (a bare flag uses the
DATABASE_URL environment variable, or pass a URL) the change is made in ONE transaction that

  * finds exactly one sorter site for the two slugs, and refuses unless --confirm-branch-name is its branch name, exactly;
  * prints the marker stored before -- keep it;
  * changes ONLY the `hold_classification` key, in place (jsonb_set / the `-` operator): every other key is untouched;
  * writes one auth_audit_log row (event hold_classification_corrected_from_set / _cleared) with the site, the previous
    and new values and the operator, IN THE SAME TRANSACTION -- no change without its audit row, and no audit row
    without its change;
  * reads the document again and rolls everything back unless the other keys are exactly as they were and the marker
    is exactly what was asked for.

No customer route writes this key: the customer settings routes change only their own keys.

Never point --execute at production without an explicit go-ahead: production is changed by an operator, not by tests or
CI. Run this from an operator workstation, never from a city-owned machine: a city machine may not connect to the
database directly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT / "src", ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from sqlalchemy import create_engine, text

from customer_api import settings as api_settings
from services import auth_service
from services.hold_classification import (
    CORRECTED_FROM_KEY,
    HOLD_CLASSIFICATION_KEY,
    marker_block,
    parse_corrected_from,
)

EXIT_OK = 0
EXIT_REFUSED = 2
EVENT_SET = "hold_classification_corrected_from_set"
EVENT_CLEARED = "hold_classification_corrected_from_cleared"
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_REVIEW_SQL = text("""
    SELECT
        o.slug AS org_slug,
        o.name AS org_name,
        b.slug AS sorter_slug,
        b.name AS branch_name,
        ci.name AS installation_name,
        ci.status AS installation_status,
        bs.settings_json -> 'hold_classification' AS hold_classification
    FROM collector_installations ci
    JOIN organizations o
      ON o.id = ci.organization_id
    JOIN branches b
      ON b.id = ci.branch_id
     AND b.organization_id = o.id
    LEFT JOIN branch_settings bs
      ON bs.branch_id = b.id
    WHERE (CAST(:org_slug AS TEXT) IS NULL OR o.slug = CAST(:org_slug AS TEXT))
      AND b.status = 'active'
      AND ci.status IN ('active', 'provisioning', 'inactive')
    ORDER BY o.slug ASC, b.is_primary DESC, b.name ASC, b.id ASC, ci.id ASC
""")

# The one site the two slugs name (as scripts/set_sorter_bin_layout.py). Locked while this transaction runs.
_SITE_SQL = text("""
    SELECT b.id AS branch_id, b.name AS branch_name
    FROM organizations o
    JOIN branches b
      ON b.organization_id = o.id
    WHERE o.slug = :org_slug
      AND b.slug = :sorter_slug
      AND b.status = 'active'
      AND EXISTS (
          SELECT 1
          FROM collector_installations ci
          WHERE ci.organization_id = o.id
            AND ci.branch_id = b.id
            AND ci.status IN ('active', 'provisioning', 'inactive')
      )
    FOR UPDATE OF b
""")

_DOCUMENT_SQL = text("SELECT settings_json FROM branch_settings WHERE branch_id = :branch_id")

_SET_SQL = text("""
    INSERT INTO branch_settings (branch_id, settings_json)
    VALUES (:branch_id, jsonb_build_object('hold_classification', CAST(:marker AS JSONB)))
    ON CONFLICT (branch_id) DO UPDATE
    SET settings_json = jsonb_set(branch_settings.settings_json, '{hold_classification}', CAST(:marker AS JSONB), true),
        updated_at = NOW()
""")

_CLEAR_SQL = text("""
    UPDATE branch_settings
    SET settings_json = settings_json - 'hold_classification',
        updated_at = NOW()
    WHERE branch_id = :branch_id
      AND jsonb_exists(settings_json, 'hold_classification')
""")


class Refused(Exception):
    """Nothing was written, and this says why."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review, set or clear a sorter site's hold classification marker.")
    commands = parser.add_subparsers(dest="command", required=True)

    review = commands.add_parser("review", help="list sorter sites and their markers (read-only)")
    review.add_argument("--org-slug", default=None, help="Only this organization. Every organization if left out.")
    review.add_argument("--database-url", default=None, help="Defaults to the DATABASE_URL environment variable.")

    write_common = argparse.ArgumentParser(add_help=False)
    write_common.add_argument("--org-slug", required=True)
    write_common.add_argument("--sorter-slug", required=True, help="The site's slug, as `review` lists it.")
    write_common.add_argument("--confirm-branch-name", required=True,
                              help="The site's branch name exactly as `review` lists it: the change is refused otherwise.")
    write_common.add_argument("--operator-email", required=True, help="Who makes the change: recorded in the audit row.")
    write_common.add_argument("--note", default=None, help="Why (e.g. the verified upgrade, or the rollback): audited.")
    write_common.add_argument("--execute", nargs="?", const="__use_env__", default=None, metavar="DATABASE_URL",
                              help="Actually change the database. Without it nothing is written (a dry run).")

    set_cmd = commands.add_parser("set", parents=[write_common], help="store the marker for one sorter site")
    set_cmd.add_argument("--corrected-from", required=True,
                         help="YYYY-MM-DD: the first full local day of verified corrected collection.")
    commands.add_parser("clear", parents=[write_common], help="remove one sorter site's marker (e.g. on rollback)")
    return parser


def _json(value: object) -> object:
    """A selected JSON value: PostgreSQL hands it back parsed, a text driver as text."""
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _marker_state(stored: object) -> str:
    if stored is None:
        return "none (every v2 day uncorrected)"
    try:
        parsed = parse_corrected_from(_json(stored))
    except ValueError:
        parsed = None
    if parsed is None:
        return "INVALID -- read as none (every v2 day uncorrected)"
    return f"corrected_from {parsed.isoformat()}"


def _review(conn: Any, org_slug: str | None, out: TextIO) -> None:
    if conn.dialect.name == "postgresql":
        conn.execute(text("SET TRANSACTION READ ONLY"))
    rows = conn.execute(_REVIEW_SQL, {"org_slug": org_slug}).mappings().all()
    if not rows:
        print("No sorter sites found." if org_slug is None else f"No sorter sites found for org_slug={org_slug!r}.",
              file=out)
        return
    organization = site = None
    for row in rows:
        if row["org_slug"] != organization:
            organization, site = row["org_slug"], None
            print(f"\norganization  slug={row['org_slug']!r}  name={row['org_name']!r}", file=out)
        if row["sorter_slug"] != site:
            site = row["sorter_slug"]
            print(f"  sorter site  slug={row['sorter_slug']!r}  branch name={row['branch_name']!r}", file=out)
            print(f"    hold classification: {_marker_state(row['hold_classification'])}", file=out)
        print(f"    installation: {row['installation_name']!r} ({row['installation_status']})", file=out)


def _without_marker(document: object) -> dict[str, Any]:
    stored = _json(document) if document is not None else {}
    return {key: value for key, value in (stored or {}).items() if key != HOLD_CLASSIFICATION_KEY}


def _change(conn: Any, args: argparse.Namespace, marker: dict[str, str] | None, out: TextIO) -> None:
    sites = conn.execute(_SITE_SQL, {"org_slug": args.org_slug, "sorter_slug": args.sorter_slug}).mappings().all()
    if len(sites) != 1:
        raise Refused(f"no sorter site org_slug={args.org_slug!r} sorter_slug={args.sorter_slug!r} (run `review`)")
    (site,) = sites
    if site["branch_name"] != args.confirm_branch_name:
        raise Refused(f"--confirm-branch-name does not match: the site's branch name is {site['branch_name']!r}")

    before = conn.execute(_DOCUMENT_SQL, {"branch_id": site["branch_id"]}).scalar_one_or_none()
    previous = (_json(before) or {}).get(HOLD_CLASSIFICATION_KEY) if before is not None else None
    print(f"Marker stored before: {json.dumps(previous)}", file=out)
    if marker is None and previous is None:
        raise Refused("there is no marker to clear")

    if marker is None:
        conn.execute(_CLEAR_SQL, {"branch_id": site["branch_id"]})
    else:
        conn.execute(_SET_SQL, {"branch_id": site["branch_id"], "marker": json.dumps(marker)})

    after = conn.execute(_DOCUMENT_SQL, {"branch_id": site["branch_id"]}).scalar_one_or_none()
    stored = (_json(after) or {}).get(HOLD_CLASSIFICATION_KEY) if after is not None else None
    if _without_marker(after) != _without_marker(before) or stored != marker:
        raise Refused("the stored document is not as expected after the change; rolled back")

    auth_service.log_auth_event_with_connection(
        conn,
        event_type=EVENT_CLEARED if marker is None else EVENT_SET,
        is_success=True,
        email=args.operator_email,
        message=("Cleared" if marker is None else "Set") + " the hold classification corrected-from marker.",
        metadata={"target_org_slug": args.org_slug, "target_sorter_slug": args.sorter_slug,
                  "branch_id": int(site["branch_id"]), "previous": previous, "new": marker, "note": args.note},
    )


def _today(now: datetime | None = None) -> date:
    """Today in the product's zone: the zone whose days the holds report counts."""
    return (now or datetime.now(UTC)).astimezone(api_settings.product_timezone()).date()


def main(argv: list[str] | None = None, out: TextIO | None = None, now: datetime | None = None) -> int:
    out = out or sys.stdout
    args = _parser().parse_args(argv)

    if args.command == "review":
        url = args.database_url or os.environ.get("DATABASE_URL")
        if not url:
            print("REFUSED: needs a database URL (--database-url, or the DATABASE_URL environment variable).", file=out)
            return EXIT_REFUSED
        engine = create_engine(url, hide_parameters=True, future=True)
        try:
            with engine.begin() as conn:
                _review(conn, args.org_slug, out)
        finally:
            engine.dispose()
        return EXIT_OK

    if not _EMAIL.match(args.operator_email.strip()):
        print("REFUSED: --operator-email is not an e-mail address. Nothing was written.", file=out)
        return EXIT_REFUSED

    marker = None
    if args.command == "set":
        corrected_from = parse_corrected_from({CORRECTED_FROM_KEY: args.corrected_from})
        if corrected_from is None:
            print("REFUSED: --corrected-from is not a YYYY-MM-DD date. Nothing was written.", file=out)
            return EXIT_REFUSED
        today = _today(now)
        if corrected_from <= today:
            print(f"REFUSED: --corrected-from {corrected_from.isoformat()} is not after today ({today.isoformat()} in "
                  f"{api_settings.product_timezone().key}). It must be the first FULL local day of verified corrected "
                  "collection, so it is never today or earlier. Nothing was written.", file=out)
            return EXIT_REFUSED
        marker = marker_block(corrected_from)

    target = f"org_slug={args.org_slug!r} sorter_slug={args.sorter_slug!r}"
    if args.execute is None:
        if marker is None:
            print(f"DRY RUN: would remove the hold classification marker of {target}, audited. Nothing was written.",
                  file=out)
        else:
            print(f"DRY RUN: would store {json.dumps(marker)} for {target}, audited. Nothing was written.", file=out)
        print("Re-run with --execute to make the change.", file=out)
        return EXIT_OK

    url = os.environ.get("DATABASE_URL") if args.execute == "__use_env__" else args.execute
    if not url:
        print("REFUSED: --execute needs a database URL (an argument, or the DATABASE_URL environment variable).", file=out)
        return EXIT_REFUSED

    engine = create_engine(url, hide_parameters=True, future=True)
    try:
        with engine.begin() as conn:
            _change(conn, args, marker, out)
    except Refused as refusal:
        print(f"REFUSED: {refusal}. Nothing was written.", file=out)
        return EXIT_REFUSED
    finally:
        engine.dispose()

    if marker is None:
        print(f"CLEARED the hold classification marker of {target} (audited).", file=out)
    else:
        print(f"STORED {json.dumps(marker)} for {target} (audited).", file=out)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
