"""Contract v2: the LOCAL classification rules (docs/collector-v2.md).

The dashboard used to classify ACS holds in the cloud from rules kept in its settings -- lists of staff and service-account NAMES and of
`|DA...|` destination patterns. Under v2 the classification happens here, so the rules live in a local file that never leaves the
machine (`rules_path`). It has this shape:

    {
      "schema_version": 1,
      "destinations": [ {"slug": "west_annex", "contains": "WEST ANNEX"} ],      optional extra routing labels -> v2 slugs
      "branch_services_names": [...],        patron/account names that mean "branch services" (the dashboard's "programming")
      "collection_services_names": [...],
      "branch_services_da_patterns": [...],  `|<pattern>|` markers in a message
      "collection_services_da_patterns": [...]
    }

Names are compared exactly as the dashboard compares them (stripped, upper-cased). At load time each name is reduced to a keyed HMAC, so the
in-memory rules and the patron cache compare HMACs, not names; the file itself is the only place a name is spelled out.

`ruleset_id` (what a hold event reports) is an OPAQUE RANDOM UUID minted when a rules file with a new keyed fingerprint is first seen (see
collector/v2_patrons.py). It has no relationship to the rules' content.

`seed_from_settings` builds a rules document from the dashboard's branch settings (the `transit.destinations` and `internal_routing.*`
keys of src/branch_settings.json / the admin settings page). The tool that calls it writes a LOCAL file and prints only counts.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .v2_events import DESTINATION_PATTERN
from .v2_identity import SubKeys, name_hmac, ruleset_fingerprint
from .v2_safe_errors import CollectorV2Error

SCHEMA_VERSION = 1
_MAX_ENTRIES = 5000
_SLUG = re.compile(DESTINATION_PATTERN)


class RulesError(CollectorV2Error):
    """Fixed codes: rules_missing, rules_invalid."""


@dataclass(frozen=True)
class Rules:
    """Compiled rules. Names are HMACs; only the `|DA...|` patterns and routing labels are kept as text (they are not patron data)."""

    destinations: tuple[tuple[str, str], ...]   # (upper-case "contains" text, slug), in file order
    branch_name_hmacs: frozenset[bytes]
    collection_name_hmacs: frozenset[bytes]
    branch_da_markers: tuple[str, ...]          # "|PATTERN|", upper-cased
    collection_da_markers: tuple[str, ...]
    fingerprint: bytes                          # keyed; LOCAL ONLY


def _names(document: dict[str, Any], key: str) -> list[str]:
    values = document.get(key, [])
    if not isinstance(values, list) or len(values) > _MAX_ENTRIES or not all(isinstance(v, str) for v in values):
        raise RulesError("rules_invalid")
    return sorted({v.strip().upper() for v in values if v.strip()})


def normalize_document(document: Any) -> dict[str, Any]:
    """Validates a rules document and returns its normalized form (sorted, upper-cased, de-duplicated)."""
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise RulesError("rules_invalid")
    destinations = document.get("destinations", [])
    if not isinstance(destinations, list) or len(destinations) > _MAX_ENTRIES:
        raise RulesError("rules_invalid")
    cleaned: list[dict[str, str]] = []
    for entry in destinations:
        if (not isinstance(entry, dict) or not isinstance(entry.get("slug"), str) or not isinstance(entry.get("contains"), str)
                or _SLUG.fullmatch(entry["slug"]) is None or not entry["contains"].strip()):
            raise RulesError("rules_invalid")
        cleaned.append({"slug": entry["slug"], "contains": entry["contains"].strip().upper()})
    return {
        "schema_version": SCHEMA_VERSION,
        "destinations": cleaned,
        "branch_services_names": _names(document, "branch_services_names"),
        "collection_services_names": _names(document, "collection_services_names"),
        "branch_services_da_patterns": _names(document, "branch_services_da_patterns"),
        "collection_services_da_patterns": _names(document, "collection_services_da_patterns"),
    }


def compile_rules(document: Any, keys: SubKeys) -> Rules:
    normalized = normalize_document(document)
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return Rules(
        destinations=tuple((entry["contains"], entry["slug"]) for entry in normalized["destinations"]),
        branch_name_hmacs=frozenset(name_hmac(keys, n) for n in normalized["branch_services_names"]),
        collection_name_hmacs=frozenset(name_hmac(keys, n) for n in normalized["collection_services_names"]),
        branch_da_markers=tuple(f"|{p}|" for p in normalized["branch_services_da_patterns"]),
        collection_da_markers=tuple(f"|{p}|" for p in normalized["collection_services_da_patterns"]),
        fingerprint=ruleset_fingerprint(keys, canonical),
    )


def load_rules(path: str | Path, keys: SubKeys) -> Rules:
    """Reads and compiles the local rules file. A missing or malformed file is an error, never an empty ruleset: silently classifying
    every service-account hold as a public hold would be wrong data with no warning."""
    file = Path(path)
    if not file.is_file():
        raise RulesError("rules_missing")
    document: Any = None
    unreadable = False
    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # JSONDecodeError holds the whole document; it must not survive as an exception context
        unreadable = True
    if unreadable:
        raise RulesError("rules_invalid")
    return compile_rules(document, keys)


def _slugify(key: str) -> str | None:
    slug = re.sub(r"[^a-z0-9]+", "_", key.strip().lower()).strip("_")[:32]
    if slug and not slug[0].isalpha():
        slug = ("d_" + slug)[:32]
    return slug if _SLUG.fullmatch(slug or "") else None


def seed_from_settings(settings: Any) -> dict[str, Any]:
    """A rules document from the dashboard's branch settings (`transit.destinations`, `internal_routing.*`). Only enabled transit
    destinations with a usable key/label are kept; a label that cannot become a valid slug is skipped (its raw label then maps to
    `unknown`, the safe fallback)."""
    if not isinstance(settings, dict):
        raise RulesError("rules_invalid")
    transit = settings.get("transit", {}) if isinstance(settings.get("transit", {}), dict) else {}
    internal = settings.get("internal_routing", {}) if isinstance(settings.get("internal_routing", {}), dict) else {}

    destinations: list[dict[str, str]] = []
    for entry in transit.get("destinations", []) or []:
        if not isinstance(entry, dict) or not bool(entry.get("enabled", True)):
            continue
        label = str(entry.get("label", "")).strip()
        slug = _slugify(str(entry.get("key", "")) or label)
        if label and slug:
            destinations.append({"slug": slug, "contains": label.upper()})

    def listed(key: str) -> list[str]:
        values = internal.get(key, []) or []
        return sorted({str(v).strip().upper() for v in values if str(v).strip()})

    return {
        "schema_version": SCHEMA_VERSION,
        "destinations": destinations,
        "branch_services_names": listed("branch_services_names"),
        "collection_services_names": listed("collection_services_names"),
        "branch_services_da_patterns": listed("branch_services_da_patterns"),
        "collection_services_da_patterns": listed("collection_services_da_patterns"),
    }


def main(argv: list[str] | None = None) -> int:
    """`python -m collector.v2_rules seed --settings <branch_settings.json> --out <rules.json>`: writes a LOCAL rules file (it holds the
    names, so it belongs on this machine only) and prints only counts. Refuses to overwrite an existing file."""
    parser = argparse.ArgumentParser(description="SortView Collector v2 -- seed the local classification rules")
    parser.add_argument("command", choices=("seed",))
    parser.add_argument("--settings", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)

    out = Path(args.out)
    if out.exists():
        print("refused: the rules file already exists", file=sys.stderr)
        return 2
    try:
        document = seed_from_settings(json.loads(Path(args.settings).read_text(encoding="utf-8")))
    except (OSError, ValueError, CollectorV2Error):
        print("failed: the settings file could not be read", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2), encoding="utf-8")
    print(f"rules written: destinations={len(document['destinations'])} "
          f"branch_services_names={len(document['branch_services_names'])} "
          f"collection_services_names={len(document['collection_services_names'])} "
          f"da_patterns={len(document['branch_services_da_patterns']) + len(document['collection_services_da_patterns'])}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
