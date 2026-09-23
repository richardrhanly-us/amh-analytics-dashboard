"""Maps a dashboard-configured transit destination label to the Contract v2 destination slug it would produce.

Government-readiness audit, Part 4 (ILL-by-branch parity). v1's ACS destination is raw text (the AMH message's CT tag,
stored verbatim in acs_events.destination); the dashboard buckets ILL counts by destination with a plain case-insensitive
exact-text match against each configured `transit_label` (src/metrics.py::build_acs_item_summary). v2's destination is
already normalized, COLLECTOR-SIDE, into a small closed slug set (collector/v2_normalize.py::normalize_destination):
"main", "westside", "library_express", "no_agency_destination", any label matched by the collector's LOCAL rules file's
custom `destinations` list, or "unknown".

The four built-in slugs below are copied from collector/v2_normalize.py's `_HOME_ALIASES`/`_BUILTIN` on purpose --
collector/v2_normalize.py's own docstring says "Only collector/v2_transform.py may import this," so this module
duplicates the built-in mapping rather than importing collector code into the dashboard/server deployment unit (they
are separate distribution units; the collector ships collector/*.py, the server ships src/*.py + main.py, and neither
should need the other's files to run). tests/test_destination_mapping_v2_parity.py fails if the two ever drift apart --
the same drift-protection pattern already used between src/services/ingest_v2_models.py and the v2 migrations' CHECK
patterns.

WHAT THIS DOES NOT COVER: a transit_label that only matches through the collector's local, per-branch custom rules file
(a `destinations` entry beyond these four built-ins) cannot be resolved from here -- the dashboard/server has no access to
that local file, by design (it never leaves the collector's machine). A configured label that isn't one of "Main"/"Home
branch", "Westside", or "Library Express" (or a "no agency destination"-style label) maps to "unknown", and any v2 ILL
event actually stored under a custom collector-side slug bucket-falls into ill_main rather than being silently
misattributed to the wrong branch. This is a disclosed limitation, not a mapping bug: closing it fully requires either
publishing the collector's resolved destination rules somewhere the server can read them, or moving to a shared
configuration source -- out of scope for this pilot-readiness round.

CHECKINS/REJECTS use a different (simpler) fix for the same underscore-vs-space mismatch: since a checkin/reject's
destination is only ever matched by raw TEXT (never looked up by slug against a fixed table like ACS's ill_by_branch
is), services/mixed_era_service.py rewrites a v2 checkin/reject's destination into v1's raw-text shape directly
("library_express" -> "LIBRARY EXPRESS") at the loader boundary, rather than duplicating a slug->label table here.
"""

from __future__ import annotations

_HOME_ALIASES = frozenset({"1", "LOCAL", "MAIN"})
_BUILTIN: tuple[tuple[str, str], ...] = (
    ("WESTSIDE", "westside"),
    ("LIBRARY EXPRESS", "library_express"),
    ("NO AGENCY DESTINATION", "no_agency_destination"),
)

UNKNOWN = "unknown"


def map_transit_label_to_v2_slug(label: object) -> str:
    """The v2 destination slug a raw label like a configured transit_label would normalize to, using ONLY the
    built-in mapping (no collector-local custom rules are visible here). Mirrors
    collector/v2_normalize.py::normalize_destination's built-in branch exactly."""
    text = label.strip() if isinstance(label, str) else ""
    upper = text.upper()
    if not upper:
        return UNKNOWN
    if upper in _HOME_ALIASES:
        return "main"
    for contains, slug in _BUILTIN:
        if contains in upper:
            return slug
    return UNKNOWN


def build_transit_label_to_v2_slug_map(transit_labels) -> dict[str, str]:
    """{transit_label: v2_slug} for every configured label the built-in mapping can resolve. A label that maps to
    UNKNOWN is deliberately left OUT of the returned dict (not mapped to "unknown" as a bucket): the caller should
    treat any v2 row whose slug does not appear among the returned values the same way ill_main already treats any
    v1 destination that doesn't match a configured transit_label -- as home-branch/main activity, never a
    silently-wrong branch attribution."""
    return {
        label: slug
        for label in transit_labels
        if (slug := map_transit_label_to_v2_slug(label)) != UNKNOWN
    }
