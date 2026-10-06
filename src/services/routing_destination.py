"""A sorter site's routing destinations: what counts as home, what counts as
one of the site's configured destinations, and the one key a destination has
whichever way it was stored. Pure: no database, no clock, no request.

WHAT A BRANCH IS HERE. A customer request names an organization and a branch.
Operationally that branch is a SORTER SITE: the scope a collector uploads
under. The other places its items are routed to are DESTINATIONS -- values on
its check-in rows, configured by label in the site's settings -- and are not
sites, branches or sorters of their own.

THE CONFIGURATION is the `transit` block of the site's effective settings
(read by services.routing_config_service):

    transit.home_branch_label      what the site calls its own shelves ("Main")
    transit.destinations           [{"key", "label", "enabled"}, ...], in display order

Nothing here names a library or a destination: every label comes from those
settings.

ONE KEY SPACE FOR BOTH ERAS. A check-in's destination is stored two ways:

    checkins.destination         (legacy)   text -- the sorter's label, or the legacy agent's tidied form of it
    checkin_events.destination   (current)  a lower_snake_case slug the collector derived from that label

destination_key turns either one, and a configured label, into the SAME key:
the slug the collector would have produced for that text
(services.destination_mapping, which mirrors the collector's built-in rules
and is held to them by a test), or, where the collector's built-in rules do
not know the text, the text itself in slug form. So "Library Express",
"LIBRARY EXPRESS" and "library_express" are one destination, whichever table
a row is in, and a configured label is matched by what it says -- never by
the free-form `key` an administrator typed beside it, which nothing has ever
matched on.

HOME is the collector contract's home slug (HOME_DESTINATION_KEY: what "1",
"LOCAL" and "MAIN" on a sorter become) together with the key of the site's
own configured home label, so a site that calls its shelves something else is
still its own home.

KNOWN LIMIT, inherited from services.destination_mapping: a destination a
collector matches only through its LOCAL rules file is stored under whatever
slug that file names. If that slug is not what the configured label says, the
server cannot connect the two, and those check-ins are counted as "other" --
never under the wrong destination, and never dropped.

Standard library and services.destination_mapping only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from services.destination_mapping import UNKNOWN, map_transit_label_to_v2_slug

# The slug a sorter's own "home" labels become under the collector contract
# (collector/v2_normalize.py; tests/test_routing_destination.py holds this
# to services.destination_mapping). It is the contract's word for "stays
# here", whatever a site calls itself.
HOME_DESTINATION_KEY = "main"

# A destination with no usable text. Never home and never a configured
# destination: rows under it are counted as "other".
UNKNOWN_DESTINATION_KEY = UNKNOWN

_NOT_SLUG_CHARACTERS = re.compile(r"[^a-z0-9]+")


def destination_key(value: object) -> str:
    """The one key for a destination, from a stored legacy label, a stored
    current slug or a configured label alike. Anything that is not text, and
    text with nothing in it a slug can hold, is UNKNOWN_DESTINATION_KEY."""
    if not isinstance(value, str):
        return UNKNOWN_DESTINATION_KEY

    # What the collector's built-in rules make of this text, if they know it.
    built_in = map_transit_label_to_v2_slug(value)
    if built_in != UNKNOWN:
        return built_in

    # Otherwise the text itself, in slug form. A current slug is unchanged by this.
    return _NOT_SLUG_CHARACTERS.sub("_", value.strip().lower()).strip("_") or UNKNOWN_DESTINATION_KEY


@dataclass(frozen=True, slots=True)
class TransitDestination:
    """One configured destination. `key` is its destination_key -- stable for
    as long as the label says the same thing -- and `label` is the configured
    label, as an administrator wrote it."""

    key: str
    label: str


@dataclass(frozen=True, slots=True)
class RoutingConfig:
    """A sorter site's routing configuration. `home_keys` are the destination
    keys that mean "stays at this site". `transit` is the site's enabled
    destinations in configured order; no two share a key and none is a home
    key."""

    home_label: str
    home_keys: frozenset[str]
    transit: tuple[TransitDestination, ...]


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _label(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def build_routing_config(settings: object, *, site_name: str) -> RoutingConfig:
    """The routing configuration in a site's effective settings.

    The home label is the configured one; a site with none configured is
    labelled with its own name (`site_name`), since home is, by definition,
    that site.

    A configured destination is used when it is enabled (absent means
    enabled, as in the dashboard) and has a label. One whose label has no
    usable text, means home, or repeats an earlier destination is left out:
    its check-ins are then counted where they belong -- home, the earlier
    destination, or "other" -- and never twice.
    """
    transit_settings = _mapping(_mapping(settings).get("transit"))

    home_label = _label(transit_settings.get("home_branch_label")) or _label(site_name)
    home_keys = {HOME_DESTINATION_KEY, destination_key(home_label)} - {UNKNOWN_DESTINATION_KEY}

    configured = transit_settings.get("destinations")
    transit: list[TransitDestination] = []
    taken = set(home_keys)
    for entry in configured if isinstance(configured, list) else []:
        entry = _mapping(entry)
        label = _label(entry.get("label"))
        if not label or not bool(entry.get("enabled", True)):
            continue
        key = destination_key(label)
        if key == UNKNOWN_DESTINATION_KEY or key in taken:
            continue
        taken.add(key)
        transit.append(TransitDestination(key=key, label=label))

    return RoutingConfig(home_label=home_label, home_keys=frozenset(home_keys), transit=tuple(transit))
