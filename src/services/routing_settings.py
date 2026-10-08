"""An organization's routing settings: what they are, what may be stored, and
the form they are stored in. Pure: no database, no clock, no request.

WHAT THEY ARE. The `transit` block of an organization's settings document:

    home_branch_label    what the organization calls a sorter site's own shelves ("Main"). May be blank: a site
                         with no home label configured is labelled with its own name (services.routing_destination).
    destinations         the other places items are routed to, in display order. Each has a label, as an
                         administrator writes it, and whether it is enabled.

THESE ARE THE ORGANIZATION'S OWN SETTINGS, not what applies at a sorter site.
The reports read a site's EFFECTIVE routing: this block with the `transit` of
that site's branch settings merged over it (services.routing_config_service).
Nothing here reads, merges or writes a branch's block.

CHANGING THEM CHANGES WHAT PAST REPORTS SAY. A check-in stores the destination
it was sent to and nothing about how that is classified. Whether it counts as
home, as one of the configured destinations or as "other" is worked out when
a report is READ, from the settings as they are at that moment. So renaming a
destination, disabling it or removing it reclassifies every earlier check-in
the next time a report is asked for; no stored event is rewritten, and
nothing here keeps what the settings used to be.

A DESTINATION IS ITS LABEL. What a label matches is
services.routing_destination.destination_key(label) -- "Library Express",
"LIBRARY EXPRESS" and "library_express" are one destination -- and that is
the only identity a destination has. So two destinations may not share that
key, and none may have the key that means home: the reports would count such
a destination's check-ins under the earlier one, or as home, and show the
destination itself nowhere.

THE STORED `key`. Each stored destination also carries a `key`, beside its
label, because the dashboard's settings form has a field for one. Nothing has
ever matched on it. It is written here as destination_key(label), so that the
form shows something that agrees with the label; it is never taken from a
caller and never returned to one.

READING WHAT IS STORED IS LENIENT; ACCEPTING A REPLACEMENT IS NOT. The
dashboard's form has stored blocks that a replacement here would be refused
for: two destinations with one label, a destination that means home, one with
no `enabled`, a blank row. parse_stored_routing_settings never fails on
those -- it returns what is there, in order, with a blank row left out and a
missing `enabled` read as enabled, as the reports read it.
validate_routing_settings is what a replacement must pass, and says which
field is at fault and why, never the value that was sent.

Standard library and services.routing_destination only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from services.routing_destination import (
    HOME_DESTINATION_KEY,
    UNKNOWN_DESTINATION_KEY,
    destination_key,
)

# The key of an organization's settings document that these settings are.
ROUTING_SETTINGS_KEY = "transit"

# The most destinations an organization may have: the dashboard's settings form has always stopped at this many.
MAX_DESTINATIONS = 20

# Why a value was refused.
NOT_TEXT = "not_text"
NOT_A_LIST = "not_a_list"
NOT_AN_OBJECT = "not_an_object"
NOT_A_BOOLEAN = "not_a_boolean"
REQUIRED = "required"
TOO_MANY = "too_many"
DUPLICATE = "duplicate"
MEANS_HOME = "means_home"
UNKNOWN_FIELD = "unknown_field"

_BLOCK_FIELDS = ("home_branch_label", "destinations")
_DESTINATION_FIELDS = ("label", "enabled")


@dataclass(frozen=True, slots=True)
class RoutingDestinationSetting:
    """One destination: its label, as written, and whether it is enabled."""

    label: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class RoutingSettings:
    """An organization's routing settings. `home_branch_label` may be an empty
    string; `destinations` are in display order and may be none."""

    home_branch_label: str
    destinations: tuple[RoutingDestinationSetting, ...]


@dataclass(frozen=True, slots=True)
class RoutingSettingProblem:
    """One field that was refused and a stable word for why. `field` is
    "home_branch_label", "destinations", or "destinations.<n>.label" /
    "destinations.<n>.enabled" for the n-th destination, counted from 0.
    Never the value."""

    field: str
    code: str


class RoutingSettingsError(Exception):
    """A replacement that cannot be stored. Carries every problem found, and
    no value: its text is the fields and codes alone."""

    def __init__(self, problems: tuple[RoutingSettingProblem, ...]) -> None:
        super().__init__(", ".join(f"{problem.field}:{problem.code}" for problem in problems))
        self.problems = problems


def _home_keys(home_branch_label: str) -> set[str]:
    """The destination keys that mean "stays here" for an organization with
    this home label: the collector contract's home, and whatever the label
    itself says. (A site with no label configured is also its own name; that
    is the site's, and cannot be known here.)"""
    return {HOME_DESTINATION_KEY, destination_key(home_branch_label)} - {UNKNOWN_DESTINATION_KEY}


def validate_routing_settings(block: object) -> RoutingSettings:
    """The settings a replacement asks for, with every label trimmed -- or
    RoutingSettingsError naming every field at fault.

    Refused: anything of the wrong kind, a field that is not one of the
    block's, more than MAX_DESTINATIONS destinations, a destination with no
    label, one whose label is the same destination as an earlier one's, and
    one whose label means home. Allowed: a blank home label, no destinations,
    destinations that are all disabled, and any characters in a label."""
    if not isinstance(block, Mapping):
        raise RoutingSettingsError((RoutingSettingProblem("routing", NOT_AN_OBJECT),))

    problems: list[RoutingSettingProblem] = [
        RoutingSettingProblem(str(name), UNKNOWN_FIELD) for name in block if name not in _BLOCK_FIELDS
    ]

    home = block.get("home_branch_label", "")
    if not isinstance(home, str):
        problems.append(RoutingSettingProblem("home_branch_label", NOT_TEXT))
        home = ""
    home = home.strip()

    entries = block.get("destinations")
    if not isinstance(entries, list):
        problems.append(RoutingSettingProblem("destinations", REQUIRED if entries is None else NOT_A_LIST))
        entries = []
    elif len(entries) > MAX_DESTINATIONS:
        problems.append(RoutingSettingProblem("destinations", TOO_MANY))

    home_keys = _home_keys(home)
    taken: set[str] = set()
    destinations: list[RoutingDestinationSetting] = []
    for index, entry in enumerate(entries):
        field = f"destinations.{index}"
        if not isinstance(entry, Mapping):
            problems.append(RoutingSettingProblem(field, NOT_AN_OBJECT))
            continue
        problems += [RoutingSettingProblem(f"{field}.{name}", UNKNOWN_FIELD) for name in entry if name not in _DESTINATION_FIELDS]

        enabled = entry.get("enabled")
        if not isinstance(enabled, bool):
            problems.append(RoutingSettingProblem(f"{field}.enabled", REQUIRED if enabled is None else NOT_A_BOOLEAN))
            enabled = True

        label = entry.get("label")
        if not isinstance(label, str):
            problems.append(RoutingSettingProblem(f"{field}.label", REQUIRED if label is None else NOT_TEXT))
            continue
        label = label.strip()
        if not label:
            problems.append(RoutingSettingProblem(f"{field}.label", REQUIRED))
            continue

        key = destination_key(label)
        if key in home_keys:
            problems.append(RoutingSettingProblem(f"{field}.label", MEANS_HOME))
        elif key in taken:
            problems.append(RoutingSettingProblem(f"{field}.label", DUPLICATE))
        taken.add(key)
        destinations.append(RoutingDestinationSetting(label=label, enabled=enabled))

    if problems:
        raise RoutingSettingsError(tuple(problems))
    return RoutingSettings(home_branch_label=home, destinations=tuple(destinations))


def parse_stored_routing_settings(stored: object) -> RoutingSettings:
    """What is stored, read as the reports read it and never refused.

    `stored` is the `transit` block itself (None, or anything that is not an
    object, is no settings at all). The home label is its text, trimmed, or
    empty. A destination is every entry that is an object with a label of
    some text, in stored order, enabled unless it says otherwise; anything
    else -- a blank row, an entry that is not an object -- is left out, as
    the reports leave it out. Destinations that share a label, or mean home,
    are returned as they are: they are what is stored. The stored `key` is
    not read."""
    block: Mapping[str, Any] = stored if isinstance(stored, Mapping) else {}

    home = block.get("home_branch_label")
    entries = block.get("destinations")

    destinations: list[RoutingDestinationSetting] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, Mapping):
            continue
        label = entry.get("label")
        label = label.strip() if isinstance(label, str) else ""
        if label:
            destinations.append(RoutingDestinationSetting(label=label, enabled=bool(entry.get("enabled", True))))

    return RoutingSettings(
        home_branch_label=home.strip() if isinstance(home, str) else "",
        destinations=tuple(destinations),
    )


def serialize_routing_settings(settings: RoutingSettings) -> dict[str, Any]:
    """The block as it is stored: the whole of it, every time. Each
    destination is written with the `key` its label gives it
    (destination_key), which is for the dashboard's settings form to show and
    for nothing to match on."""
    return {
        "home_branch_label": settings.home_branch_label,
        "destinations": [
            {"key": destination_key(destination.label), "label": destination.label, "enabled": destination.enabled}
            for destination in settings.destinations
        ],
    }
