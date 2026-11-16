"""Reviews, sets or clears a sorter site's BIN LAYOUT: the `bin_layout` key of its branch's settings document
(services.bin_layout says what a layout is). The Live Today dashboard draws every bin of a sorter that has one, in its
place; a sorter without one is drawn from its check-ins alone.

    python scripts/set_sorter_bin_layout.py review [--org-slug SLUG] [--database-url URL]
    python scripts/set_sorter_bin_layout.py set    --org-slug SLUG --sorter-slug SLUG --confirm-branch-name NAME \\
                                                   --layout '{"end": "0", "columns": [["6", "5"], ["4", "3"], ["2", "1"]]}'
                                                   (or --layout-file layout.json: needed in Windows PowerShell 5.1)
    python scripts/set_sorter_bin_layout.py clear  --org-slug SLUG --sorter-slug SLUG --confirm-branch-name NAME

IDENTIFY FIRST. `review` only reads -- on PostgreSQL inside a READ ONLY transaction -- and lists each organization's
sorter sites as the customer API does (an active branch with an installation that is not retired): the organization's
slug, name and status, each site's slug, its branch's name, its installations' names and statuses, and its current
layout. The slugs and the branch name `set` and `clear` take are copied from that listing, never from tests or docs.

SAFE BY DEFAULT. Without --execute, `set` and `clear` touch no database: `set` validates the layout and prints it as it
would be stored. With --execute (a bare flag uses the DATABASE_URL environment variable, or pass a URL) the change is
made in ONE transaction that

  * finds exactly one sorter site for the two slugs, and refuses unless --confirm-branch-name is that site's branch
    name, exactly;
  * prints the layout stored before, as JSON -- keep it: `set` with it is the way back;
  * changes ONLY the `bin_layout` key, in place (jsonb_set / the `-` operator): the document is never read out and
    written back, so the efficiency settings and every other key are untouched;
  * reads the document again and rolls everything back unless the other keys are exactly as they were and the
    layout is exactly what was asked for.

A refusal writes nothing. Re-running `set` with the same layout stores the same value.

Never point --execute at production without an explicit go-ahead: production is changed by an operator, not by tests or
CI. Run this from an operator workstation, never from a city-owned machine: a city machine may not connect to the
database directly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, TextIO

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text

from src.services.bin_layout import (
    BinLayoutError,
    parse_bin_layout,
    serialize_bin_layout,
)

EXIT_OK = 0
EXIT_REFUSED = 2

# The sites the customer API lists (services.sorter_inventory_service), one row per installation, with the layout.
_REVIEW_SQL = text("""
    SELECT
        o.slug AS org_slug,
        o.name AS org_name,
        o.status AS org_status,
        b.slug AS sorter_slug,
        b.name AS branch_name,
        ci.name AS installation_name,
        ci.status AS installation_status,
        bs.settings_json -> 'bin_layout' AS bin_layout
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

# The one site the two slugs name, by the same rule. Locked: nothing else changes it while this transaction runs.
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
    VALUES (:branch_id, jsonb_build_object('bin_layout', CAST(:layout AS JSONB)))
    ON CONFLICT (branch_id) DO UPDATE
    SET settings_json = jsonb_set(branch_settings.settings_json, '{bin_layout}', CAST(:layout AS JSONB), true),
        updated_at = NOW()
""")

_CLEAR_SQL = text("""
    UPDATE branch_settings
    SET settings_json = settings_json - 'bin_layout',
        updated_at = NOW()
    WHERE branch_id = :branch_id
      AND jsonb_exists(settings_json, 'bin_layout')
""")


class Refused(Exception):
    """Nothing was written, and this says why."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Review, set or clear a sorter site's bin layout.")
    commands = parser.add_subparsers(dest="command", required=True)

    review = commands.add_parser("review", help="list sorter sites and their layouts (read-only)")
    review.add_argument("--org-slug", default=None, help="Only this organization. Every organization if left out.")
    review.add_argument("--database-url", default=None, help="Defaults to the DATABASE_URL environment variable.")

    write_common = argparse.ArgumentParser(add_help=False)
    write_common.add_argument("--org-slug", required=True)
    write_common.add_argument("--sorter-slug", required=True, help="The site's slug, as `review` lists it.")
    write_common.add_argument("--confirm-branch-name", required=True,
                              help="The site's branch name exactly as `review` lists it: the change is refused otherwise.")
    write_common.add_argument("--execute", nargs="?", const="__use_env__", default=None, metavar="DATABASE_URL",
                              help="Actually change the database. Without it nothing is written (a dry run).")

    set_cmd = commands.add_parser("set", parents=[write_common], help="store a layout for one sorter site")
    given = set_cmd.add_mutually_exclusive_group(required=True)
    given.add_argument("--layout", help="The layout as JSON (see services/bin_layout.py).")
    # Windows PowerShell 5.1 strips the double quotes inside an argument to a native program: give JSON there as a file.
    given.add_argument("--layout-file", type=Path, help="A file holding the layout as JSON.")
    commands.add_parser("clear", parents=[write_common], help="remove one sorter site's layout")
    return parser


def _json(value: object) -> object:
    """A selected JSON value: PostgreSQL hands it back parsed, SQLite as text."""
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _layout_state(stored: object) -> str:
    if stored is None:
        return "none"
    try:
        return json.dumps(serialize_bin_layout(parse_bin_layout(_json(stored))))
    except (BinLayoutError, ValueError) as error:
        return f"INVALID ({error.code if isinstance(error, BinLayoutError) else 'unreadable'}) -- ignored by the API"


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
            print(f"\norganization  slug={row['org_slug']!r}  name={row['org_name']!r}  status={row['org_status']!r}",
                  file=out)
        if row["sorter_slug"] != site:
            site = row["sorter_slug"]
            print(f"  sorter site  slug={row['sorter_slug']!r}  branch name={row['branch_name']!r}", file=out)
            print(f"    bin layout: {_layout_state(row['bin_layout'])}", file=out)
        print(f"    installation: {row['installation_name']!r} ({row['installation_status']})", file=out)


def _without_layout(document: object) -> dict[str, Any]:
    stored = _json(document) if document is not None else {}
    return {key: value for key, value in (stored or {}).items() if key != "bin_layout"}


def _change(conn: Any, args: argparse.Namespace, layout: dict[str, Any] | None, out: TextIO) -> None:
    sites = conn.execute(_SITE_SQL, {"org_slug": args.org_slug, "sorter_slug": args.sorter_slug}).mappings().all()
    if len(sites) != 1:
        raise Refused(f"no sorter site org_slug={args.org_slug!r} sorter_slug={args.sorter_slug!r} (run `review`)")
    (site,) = sites
    if site["branch_name"] != args.confirm_branch_name:
        raise Refused(f"--confirm-branch-name does not match: the site's branch name is {site['branch_name']!r}")

    before = conn.execute(_DOCUMENT_SQL, {"branch_id": site["branch_id"]}).scalar_one_or_none()
    previous = (_json(before) or {}).get("bin_layout") if before is not None else None
    print(f"Layout stored before: {json.dumps(previous)}", file=out)

    if layout is None:
        conn.execute(_CLEAR_SQL, {"branch_id": site["branch_id"]})
    else:
        conn.execute(_SET_SQL, {"branch_id": site["branch_id"], "layout": json.dumps(layout)})

    after = conn.execute(_DOCUMENT_SQL, {"branch_id": site["branch_id"]}).scalar_one_or_none()
    stored = (_json(after) or {}).get("bin_layout") if after is not None else None
    if _without_layout(after) != _without_layout(before) or stored != layout:
        raise Refused("the stored document is not as expected after the change; rolled back")


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
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

    layout = None
    if args.command == "set":
        try:
            given = args.layout if args.layout_file is None else args.layout_file.read_text(encoding="utf-8-sig")
            layout = serialize_bin_layout(parse_bin_layout(json.loads(given)))
        except OSError:
            print("REFUSED: --layout-file could not be read. Nothing was written.", file=out)
            return EXIT_REFUSED
        except ValueError as error:
            code = error.code if isinstance(error, BinLayoutError) else "not JSON"
            print(f"REFUSED: --layout is not a valid bin layout ({code}). Nothing was written.", file=out)
            return EXIT_REFUSED

    target = f"org_slug={args.org_slug!r} sorter_slug={args.sorter_slug!r}"
    if args.execute is None:
        if layout is None:
            print(f"DRY RUN: would remove the bin layout of {target}. Nothing was written.", file=out)
        else:
            print(f"DRY RUN: would store this bin layout for {target}: {json.dumps(layout)}. Nothing was written.",
                  file=out)
        print("Re-run with --execute to make the change.", file=out)
        return EXIT_OK

    url = os.environ.get("DATABASE_URL") if args.execute == "__use_env__" else args.execute
    if not url:
        print("REFUSED: --execute needs a database URL (an argument, or the DATABASE_URL environment variable).", file=out)
        return EXIT_REFUSED

    engine = create_engine(url, hide_parameters=True, future=True)
    try:
        with engine.begin() as conn:
            _change(conn, args, layout, out)
    except Refused as refusal:
        print(f"REFUSED: {refusal}. Nothing was written.", file=out)
        return EXIT_REFUSED
    finally:
        engine.dispose()

    if layout is None:
        print(f"CLEARED the bin layout of {target}.", file=out)
    else:
        print(f"STORED the bin layout of {target}: {json.dumps(layout)}", file=out)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
