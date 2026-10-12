"""What a Contract v2 enrollment hands a Collector about its site, built from the site's own stored settings. Pure: no
database, no clock, no request, no Streamlit.

Three things, each read from a settings document and from nothing else:

    collector_timezone(organization_settings)
        organization_settings.settings_json["collector_timezone"]: the IANA zone the site's Tech Logic machine keeps its
        clock in (the Collector's `v2.timezone`). It must be there, non-blank and a zone ZoneInfo knows. THERE IS NO
        DEFAULT: a wrong zone moves every event silently, so a missing one is refused, never guessed. It configures the
        Collector only -- it is NOT the zone reports are drawn in (customer_api.settings.product_timezone), and nothing
        here changes that.

    require_rules_reviewed(organization_settings)
        organization_settings.settings_json["collector_rules"] = {"reviewed_at": <aware ISO time>,
        "reviewed_by_user_id": <positive integer>}: the record that a SortView administrator looked at this site's
        classification lists and confirmed them. It holds no name. It is what tells "reviewed, and empty" (a site with
        no service accounts) from "never looked at": an empty list alone can never mean reviewed, and a full one alone
        does not either.

    build_classification_rules(effective_settings)
        The site's local classification rules document, in the Collector's own schema
        (collector/v2_rules.py: schema_version, destinations, and the four internal_routing lists), from the site's
        EFFECTIVE settings: its organization's with its branch's merged over them (effective_settings below).

THE COLLECTOR IS THE AUTHORITY ON THE SCHEMA; THIS IS A COPY OF HOW IT SEEDS ONE. The API and the frozen Collector are
separate deployment units (see services.destination_mapping for the same arrangement), so this module reproduces
collector.v2_rules.seed_from_settings followed by collector.v2_rules.normalize_document rather than importing them.
tests/test_collector_rules.py holds the two together: for every settings document this module accepts, it returns
exactly what the Collector's own two functions return, and the Collector's validator accepts it unchanged.

It is STRICTER than the Collector's seeder in one way, on purpose: a list that is not a list, an entry that is not
text, or a `transit` / `internal_routing` block that is not an object is REFUSED here, where the seeder would skip it
or make something of it. A site's rules are never built from settings that do not say what they mean.

NOTHING IS ADDED. No library's names, destinations or defaults are in this module: a site with nothing configured gets
a document of empty lists. What a destination label MEANS -- the Collector's built-in home and destination names, and
how a configured destination's slug is chosen -- is deliberately reproduced as it is today and not decided here.

Errors carry a FIXED reason code and never a value: the lists hold patron account names, some of them a staff member's
own, and no message, log line or response may repeat one.

Standard library and services.ingest_v2_models only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .ingest_v2_models import DESTINATION_PATTERN

# The Collector's own (collector/v2_rules.py: SCHEMA_VERSION, _MAX_ENTRIES); held to it by a test.
RULES_SCHEMA_VERSION = 1
MAX_RULE_ENTRIES = 5000

TIMEZONE_KEY = "collector_timezone"
RULES_REVIEW_KEY = "collector_rules"

NAME_LISTS = (
    "branch_services_names",
    "collection_services_names",
    "branch_services_da_patterns",
    "collection_services_da_patterns",
)

# Fixed reason codes. They are for server logs and tests; none is ever sent to a client.
TIMEZONE_NOT_CONFIGURED = "timezone_not_configured"
TIMEZONE_INVALID = "timezone_invalid"
RULES_NOT_REVIEWED = "rules_not_reviewed"
RULES_INVALID = "rules_invalid"

_SLUG = re.compile(DESTINATION_PATTERN)
_NOT_SLUG_CHARACTERS = re.compile(r"[^a-z0-9]+")
_MAX_TIMEZONE_LENGTH = 64


class CollectorSettingsError(ValueError):
    """A site's settings cannot give a Collector what it needs. `reason` is one of the fixed codes above; the message
    is that code and nothing else, so no settings value can travel in it."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _document(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def effective_settings(organization_settings: object, branch_settings: object) -> dict[str, Any]:
    """The organization's settings with the branch's merged over them, key by key -- the same merge the dashboard's
    effective settings use (services.tenant_service._deep_merge_settings). Neither argument is changed."""
    result = _document(organization_settings)
    for key, value in _document(branch_settings).items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = effective_settings(result[key], value)
        else:
            result[key] = value
    return result


# --- time zone ---------------------------------------------------------------------------------------------------------

def collector_timezone(organization_settings: object) -> str:
    """The site's configured IANA time zone name. Raises CollectorSettingsError if it is missing, blank, not text, or
    not a zone this server knows."""
    value = _document(organization_settings).get(TIMEZONE_KEY)
    if value is None:
        raise CollectorSettingsError(TIMEZONE_NOT_CONFIGURED)
    if not isinstance(value, str):
        raise CollectorSettingsError(TIMEZONE_INVALID)
    name = value.strip()
    if not name:
        raise CollectorSettingsError(TIMEZONE_NOT_CONFIGURED)

    known = len(name) <= _MAX_TIMEZONE_LENGTH
    if known:
        try:
            ZoneInfo(name)
        except Exception:  # an unknown key, a malformed one, or no tz database: all the same answer
            known = False
    if not known:
        raise CollectorSettingsError(TIMEZONE_INVALID)
    return name


# --- the readiness marker ----------------------------------------------------------------------------------------------

def rules_review_marker(*, reviewed_at: datetime, reviewed_by_user_id: int) -> dict[str, Any]:
    """The `collector_rules` block that records a review: who, and when (an aware instant). No name is in it."""
    if not isinstance(reviewed_at, datetime) or reviewed_at.tzinfo is None:
        raise ValueError("reviewed_at must be an offset-aware datetime")
    if isinstance(reviewed_by_user_id, bool) or not isinstance(reviewed_by_user_id, int) or reviewed_by_user_id < 1:
        raise ValueError("reviewed_by_user_id must be a positive integer")
    return {"reviewed_at": reviewed_at.isoformat(), "reviewed_by_user_id": reviewed_by_user_id}


def require_rules_reviewed(organization_settings: object) -> None:
    """Raises CollectorSettingsError(RULES_NOT_REVIEWED) unless the settings carry a valid review record. What the
    lists contain is not looked at: empty lists that were reviewed pass, and full ones that were not do not."""
    marker = _document(organization_settings).get(RULES_REVIEW_KEY)
    reviewed_at = marker.get("reviewed_at") if isinstance(marker, Mapping) else None
    reviewer = marker.get("reviewed_by_user_id") if isinstance(marker, Mapping) else None

    aware = False
    if isinstance(reviewed_at, str):
        try:
            aware = datetime.fromisoformat(reviewed_at).tzinfo is not None
        except ValueError:
            aware = False
    by_someone = isinstance(reviewer, int) and not isinstance(reviewer, bool) and reviewer >= 1
    if not (aware and by_someone):
        raise CollectorSettingsError(RULES_NOT_REVIEWED)


# --- the rules document ------------------------------------------------------------------------------------------------

def _block(settings: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """A settings block that is absent is empty; one that is there and is not an object is refused."""
    value = settings.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CollectorSettingsError(RULES_INVALID)
    return value


def _entries(block: Mapping[str, Any], key: str) -> list[Any]:
    value = block.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_RULE_ENTRIES:
        raise CollectorSettingsError(RULES_INVALID)
    return value


def _slugify(key: str) -> str | None:
    """collector.v2_rules._slugify: lower_snake_case, at most 32 characters, starting with a letter."""
    slug = _NOT_SLUG_CHARACTERS.sub("_", key.strip().lower()).strip("_")[:32]
    if slug and not slug[0].isalpha():
        slug = ("d_" + slug)[:32]
    return slug if _SLUG.fullmatch(slug or "") else None


def _names(block: Mapping[str, Any], key: str) -> list[str]:
    values = _entries(block, key)
    if not all(isinstance(value, str) for value in values):
        raise CollectorSettingsError(RULES_INVALID)
    return sorted({value.strip().upper() for value in values if value.strip()})


def build_classification_rules(settings: object) -> dict[str, Any]:
    """The site's classification rules document from its effective settings, normalized exactly as the Collector
    normalizes one: names stripped, upper-cased, de-duplicated and sorted; destinations in configured order. The same
    settings always give the same document.

    A destination is kept as the Collector's seeder keeps it: enabled (absent means enabled), with a label, and a
    `key` or label that can be made into a slug. One that cannot is left out, and its label is then `unknown` to the
    Collector -- the safe answer.

    Raises CollectorSettingsError(RULES_INVALID) if the settings are not an object, or a block or list in them is not
    what it should be."""
    if not isinstance(settings, Mapping):
        raise CollectorSettingsError(RULES_INVALID)
    transit = _block(settings, "transit")
    internal = _block(settings, "internal_routing")

    destinations: list[dict[str, str]] = []
    for entry in _entries(transit, "destinations"):
        if not isinstance(entry, Mapping):
            raise CollectorSettingsError(RULES_INVALID)
        if not bool(entry.get("enabled", True)):
            continue
        label = str(entry.get("label", "")).strip()
        slug = _slugify(str(entry.get("key", "")) or label)
        if label and slug:
            destinations.append({"slug": slug, "contains": label.upper()})

    return {
        "schema_version": RULES_SCHEMA_VERSION,
        "destinations": destinations,
        **{key: _names(internal, key) for key in NAME_LISTS},
    }
