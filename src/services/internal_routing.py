"""An organization's Internal Routing lists: what a replacement must be, and the form it is stored in (A2 item 4).
Pure: no database, no clock, no request, no framework.

WHAT THEY ARE. The `internal_routing` block of an organization's settings document -- four lists the hold classifiers
read (services.hold_rules for v1, services.collector_rules for a Contract v2 enrollment's rules):

    branch_services_names             patron account names whose holds are the branch's own services
    collection_services_names         patron account names whose holds are collection services
    branch_services_da_patterns       destination markers, matched as `|<marker>|` in an ACS message
    collection_services_da_patterns   the same, for collection services

SOME ENTRIES CAN BE A PERSON'S OWN (a staff member's patron card). So no problem, error or message made here ever
holds an entry: a problem is a field, an index and a fixed code, and nothing else.

WHAT A REPLACEMENT IS. All four lists, each a list of text. Each entry is trimmed; a blank one is left out; the case is
kept as typed (every reader compares upper-cased); a later entry that is the same as an earlier one, whatever its case,
is left out; the order is kept. An empty list is allowed and means none. Refused: a list that is not a list, more than
MAX_ENTRIES entries, an entry that is not text, longer than MAX_ENTRY_LENGTH after trimming, with a control character,
or with `|` -- the separator of an ACS message, which no account name or marker can contain and which the classifiers
wrap a marker in.

READING WHAT IS STORED IS LENIENT (stored_internal_routing): what provisioning or the old customer settings page wrote
is returned to be seen and corrected, even where a replacement would be refused for it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# The key of an organization's settings document that these lists are, and the lists themselves, in display order.
INTERNAL_ROUTING_KEY = "internal_routing"
LISTS = (
    "branch_services_names",
    "collection_services_names",
    "branch_services_da_patterns",
    "collection_services_da_patterns",
)

# The Collector's own limit on a rules list (collector/v2_rules.py _MAX_ENTRIES; services.collector_rules
# MAX_RULE_ENTRIES): an enrollment refuses a site with more, so none is stored.
MAX_ENTRIES = 5000
MAX_ENTRY_LENGTH = 200

# Why a value was refused.
NOT_AN_OBJECT = "not_an_object"
UNKNOWN_FIELD = "unknown_field"
REQUIRED = "required"
NOT_A_LIST = "not_a_list"
TOO_MANY = "too_many"
NOT_TEXT = "not_text"
TOO_LONG = "too_long"
CONTROL_CHARACTER = "control_character"
SEPARATOR_CHARACTER = "separator_character"

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


@dataclass(frozen=True, slots=True)
class InternalRoutingProblem:
    """One refused field and a fixed code for why. `field` is a list's name, or "<list>.<n>" for its n-th submitted
    entry, counted from 0. Never the value."""

    field: str
    code: str


class InternalRoutingError(ValueError):
    """A replacement that cannot be stored. Its text is the fields and codes alone."""

    def __init__(self, problems: tuple[InternalRoutingProblem, ...]) -> None:
        super().__init__(", ".join(f"{problem.field}:{problem.code}" for problem in problems))
        self.problems = problems


def _normalized_list(name: str, values: object, problems: list[InternalRoutingProblem]) -> list[str]:
    if values is None:
        problems.append(InternalRoutingProblem(name, REQUIRED))
        return []
    if not isinstance(values, list):
        problems.append(InternalRoutingProblem(name, NOT_A_LIST))
        return []
    if len(values) > MAX_ENTRIES:
        problems.append(InternalRoutingProblem(name, TOO_MANY))
        return []

    kept: list[str] = []
    seen: set[str] = set()
    for index, value in enumerate(values):
        field = f"{name}.{index}"
        if not isinstance(value, str):
            problems.append(InternalRoutingProblem(field, NOT_TEXT))
            continue
        entry = value.strip()
        if not entry:
            continue
        if len(entry) > MAX_ENTRY_LENGTH:
            problems.append(InternalRoutingProblem(field, TOO_LONG))
        elif _CONTROL.search(entry):
            problems.append(InternalRoutingProblem(field, CONTROL_CHARACTER))
        elif "|" in entry:
            problems.append(InternalRoutingProblem(field, SEPARATOR_CHARACTER))
        elif entry.upper() not in seen:
            seen.add(entry.upper())
            kept.append(entry)
    return kept


def validate_internal_routing(block: object) -> dict[str, list[str]]:
    """The four lists a replacement asks for, normalized (see the module docstring) -- or InternalRoutingError naming
    every field at fault."""
    if not isinstance(block, Mapping):
        raise InternalRoutingError((InternalRoutingProblem("internal_routing", NOT_AN_OBJECT),))

    problems = [InternalRoutingProblem(str(name), UNKNOWN_FIELD) for name in block if name not in LISTS]
    lists = {name: _normalized_list(name, block.get(name), problems) for name in LISTS}
    if problems:
        raise InternalRoutingError(tuple(problems))
    return lists


def stored_internal_routing(stored: object) -> dict[str, list[str]]:
    """What is stored, to be seen, never refused: each list's entries as text, trimmed, blanks left out, in stored
    order. A block or list that is not one is empty."""
    block: Mapping[str, Any] = stored if isinstance(stored, Mapping) else {}
    lists: dict[str, list[str]] = {}
    for name in LISTS:
        values = block.get(name)
        entries = [str(value).strip() for value in values] if isinstance(values, list) else []
        lists[name] = [entry for entry in entries if entry]
    return lists


def entry_counts(lists: Mapping[str, list[str]]) -> dict[str, int]:
    """How many entries each list has: the one thing about them an audit record or a log line may say."""
    return {name: len(lists.get(name, [])) for name in LISTS}
