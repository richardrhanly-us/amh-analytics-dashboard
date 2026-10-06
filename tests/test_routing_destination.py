"""F5.5: a sorter site's routing destinations -- the pure rules.

    destination_key(value)                       one key for a destination, however it was stored or configured
    build_routing_config(settings, site_name=)   home and the configured destinations of one site

Nothing here touches a database. The read of a site's settings is tested in
tests/test_customer_api_checkins_by_destination.py, through the real route.

Imported the "flat" way (services.routing_destination), the identity the API
process uses.
"""

from __future__ import annotations

import dataclasses
import inspect

import pytest

from services import destination_mapping, routing_destination
from services.routing_destination import (
    HOME_DESTINATION_KEY,
    UNKNOWN_DESTINATION_KEY,
    RoutingConfig,
    TransitDestination,
    build_routing_config,
    destination_key,
)


def _settings(home="Main", *destinations) -> dict:
    return {"transit": {"home_branch_label": home, "destinations": list(destinations)}}


def _destination(label, *, key="branch_1", enabled=True) -> dict:
    return {"key": key, "label": label, "enabled": enabled}


# =====================================================================================================================
# destination_key
# =====================================================================================================================

@pytest.mark.parametrize(("stored", "key"), [
    # A legacy label, the legacy agent's tidied form of it, and the current slug are one destination.
    ("Library Express", "library_express"),
    ("LIBRARY EXPRESS", "library_express"),
    ("  library express  ", "library_express"),
    ("library_express", "library_express"),
    ("Westside", "westside"),
    ("WESTSIDE", "westside"),
    ("westside", "westside"),
    ("No Agency Destination", "no_agency_destination"),
    ("no_agency_destination", "no_agency_destination"),
    # A label the collector's built-in rules do not know is its own text, in slug form.
    ("Northgate Annex", "northgate_annex"),
    ("NORTHGATE  ANNEX", "northgate_annex"),
    ("northgate_annex", "northgate_annex"),
    ("St. Mary's / East-Wing", "st_mary_s_east_wing"),
    ("Bin 7", "bin_7"),
])
def test_a_destination_has_one_key_whichever_way_it_was_stored(stored, key):
    assert destination_key(stored) == key


@pytest.mark.parametrize("stored", ["1", "LOCAL", "local", "Main", "MAIN", " main ", "main"])
def test_everything_a_sorter_calls_home_is_the_home_key(stored):
    assert destination_key(stored) == HOME_DESTINATION_KEY


@pytest.mark.parametrize("stored", [None, "", "   ", "---", "???", 7, 1.5, b"Westside", ["Westside"], {"label": "Westside"}])
def test_a_value_with_no_usable_text_is_unknown_and_never_raises(stored):
    assert destination_key(stored) == UNKNOWN_DESTINATION_KEY


def test_unknown_stays_unknown():
    assert destination_key("unknown") == UNKNOWN_DESTINATION_KEY
    assert destination_key("Unknown") == UNKNOWN_DESTINATION_KEY


def test_a_key_is_stable_under_being_keyed_again():
    for stored in ("Library Express", "WESTSIDE BRANCH", "Northgate Annex", "1", "No Agency Destination", "", "Bin 7"):
        key = destination_key(stored)

        assert destination_key(key) == key


def test_every_key_fits_the_contracts_destination_slug_shape():
    import re

    from services.ingest_v2_models import DESTINATION_PATTERN

    for stored in ("Library Express", "Westside", "Northgate Annex", "Main", "No Agency Destination", "A"):
        assert re.fullmatch(DESTINATION_PATTERN, destination_key(stored)), stored


def test_the_home_key_is_what_the_collector_contract_makes_of_a_sorters_home_labels():
    # services.destination_mapping mirrors the collector's built-in rules; this constant must be their home slug.
    for label in sorted(destination_mapping._HOME_ALIASES):
        assert destination_mapping.map_transit_label_to_v2_slug(label) == HOME_DESTINATION_KEY
    assert UNKNOWN_DESTINATION_KEY == destination_mapping.UNKNOWN


def test_a_key_is_the_slug_the_deployed_collector_would_have_stored():
    """Whatever the collector's own rules turn a sorter label into, the server
    turns the legacy text into the same thing -- so a destination does not
    change identity at a site's cutover."""
    from collector import v2_normalize

    @dataclasses.dataclass(frozen=True)
    class NoLocalRules:
        destinations: tuple = ()

    for raw in ("WESTSIDE BRANCH", "westside", "Library Express Annex", "library express", "1", "Local", "Main",
                "No Agency Destination"):
        assert destination_key(raw) == v2_normalize.normalize_destination(raw, rules=NoLocalRules()), raw


# =====================================================================================================================
# build_routing_config
# =====================================================================================================================

def test_the_configured_home_label_and_destinations_are_used_in_configured_order():
    config = build_routing_config(
        _settings("Main", _destination("Westside"), _destination("Library Express", key="branch_2")),
        site_name="Main Library",
    )

    assert config == RoutingConfig(
        home_label="Main",
        home_keys=frozenset({"main"}),
        transit=(TransitDestination("westside", "Westside"), TransitDestination("library_express", "Library Express")),
    )


def test_order_is_the_configured_order_not_alphabetical_and_not_by_key():
    config = build_routing_config(
        _settings("Main", _destination("Zebra Road", key="a"), _destination("Alpha Street", key="z"), _destination("Mid")),
        site_name="x",
    )

    assert [destination.label for destination in config.transit] == ["Zebra Road", "Alpha Street", "Mid"]


def test_a_destination_is_identified_by_its_label_never_by_the_key_typed_beside_it():
    config = build_routing_config(_settings("Main", _destination("Northgate Annex", key="branch_1")), site_name="x")

    assert config.transit == (TransitDestination("northgate_annex", "Northgate Annex"),)


def test_the_label_is_kept_as_written_apart_from_surrounding_space():
    config = build_routing_config(_settings("Main", _destination("  LIBRARY express ")), site_name="x")

    assert config.transit == (TransitDestination("library_express", "LIBRARY express"),)


def test_a_disabled_destination_is_left_out_and_one_with_no_flag_is_enabled():
    no_flag = {"key": "k", "label": "Riverside"}
    config = build_routing_config(
        _settings("Main", _destination("Westside", enabled=False), no_flag, _destination("Hilltop", enabled=True)),
        site_name="x",
    )

    assert [destination.label for destination in config.transit] == ["Riverside", "Hilltop"]


def test_a_destination_that_repeats_an_earlier_one_is_left_out_so_nothing_is_counted_twice():
    config = build_routing_config(
        _settings("Main", _destination("Westside"), _destination("WESTSIDE"), _destination("westside  ")),
        site_name="x",
    )

    assert config.transit == (TransitDestination("westside", "Westside"),)


def test_a_destination_that_means_home_is_left_out():
    config = build_routing_config(
        _settings("Central", _destination("Main"), _destination("central"), _destination("Local"), _destination("Uptown")),
        site_name="x",
    )

    assert config.home_keys == frozenset({"main", "central"})
    assert config.transit == (TransitDestination("uptown", "Uptown"),)


def test_a_destination_with_no_usable_label_is_left_out():
    config = build_routing_config(
        _settings("Main", _destination(""), _destination("   "), _destination("???"), _destination(None),
                  _destination("unknown"), _destination(12), _destination("Uptown")),
        site_name="x",
    )

    assert config.transit == (TransitDestination("uptown", "Uptown"),)


def test_a_site_whose_home_has_another_name_is_still_its_own_home():
    config = build_routing_config(_settings("Westside", _destination("Main Library")), site_name="x")

    # What the sorter itself calls home ("1", "LOCAL", "MAIN") and what the site calls home are both home.
    assert config.home_label == "Westside"
    assert config.home_keys == frozenset({"main", "westside"})
    assert config.transit == (TransitDestination("main_library", "Main Library"),)


@pytest.mark.parametrize("home", [None, "", "   ", 5])
def test_a_site_with_no_home_label_is_labelled_with_its_own_name(home):
    config = build_routing_config(_settings(home, _destination("Uptown")), site_name="  Hilltop Library ")

    assert config.home_label == "Hilltop Library"
    assert config.home_keys == frozenset({"main", "hilltop_library"})


@pytest.mark.parametrize("settings", [
    None, {}, [], "transit", {"transit": None}, {"transit": []}, {"transit": {}}, {"transit": {"destinations": None}},
    {"transit": {"destinations": "Westside"}}, {"transit": {"destinations": {"label": "Westside"}}},
    {"transit": {"destinations": [None, "Westside", 3, ["Westside"]]}},
])
def test_settings_with_no_usable_transit_block_give_a_site_with_no_destinations(settings):
    config = build_routing_config(settings, site_name="Hilltop")

    assert config == RoutingConfig(home_label="Hilltop", home_keys=frozenset({"main", "hilltop"}), transit=())


def test_no_two_destinations_share_a_key_and_none_is_a_home_key():
    config = build_routing_config(
        _settings("Main", *[_destination(label) for label in
                            ("A", "a", "B", "Main", "b ", "C-1", "c 1", "LOCAL", "D", "", "1")]),
        site_name="x",
    )
    keys = [destination.key for destination in config.transit]

    assert keys == ["a", "b", "c_1", "d"]
    assert len(set(keys)) == len(keys)
    assert not set(keys) & config.home_keys


def test_many_destinations_are_all_kept():
    labels = [f"Stop {number}" for number in range(1, 41)]
    config = build_routing_config(_settings("Main", *[_destination(label) for label in labels]), site_name="x")

    assert [destination.label for destination in config.transit] == labels


def test_the_settings_are_not_changed():
    settings = _settings("Main", _destination("Westside"), _destination("WESTSIDE"))
    before = repr(settings)

    build_routing_config(settings, site_name="x")

    assert repr(settings) == before


# =====================================================================================================================
# What the module is and is not
# =====================================================================================================================

def test_the_module_names_no_library_and_no_destination_of_its_own():
    code = "\n".join(
        line for line in inspect.getsource(routing_destination).split('"""')[2].splitlines()
        if not line.lstrip().startswith("#")
    )

    for name in ("westside", "library express", "library_express", "braunfels", "nbpl", "no agency"):
        assert name not in code.lower(), name


def test_the_module_depends_only_on_the_standard_library_and_the_destination_mapping():
    imports = [line.strip() for line in inspect.getsource(routing_destination).splitlines()
               if line.startswith(("import ", "from "))]

    assert imports == [
        "from __future__ import annotations",
        "import re",
        "from collections.abc import Mapping",
        "from dataclasses import dataclass",
        "from typing import Any",
        "from services.destination_mapping import UNKNOWN, map_transit_label_to_v2_slug",
    ]
