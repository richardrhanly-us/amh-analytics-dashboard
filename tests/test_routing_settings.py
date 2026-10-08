"""R8G: an organization's routing settings -- the pure model (services.routing_settings).

Nothing here touches a database or a request. What is tested is what a replacement must pass, how what is already
stored is read without ever being refused, and the form a block is stored in -- including the `key` that is written
beside each label for the dashboard's settings form and matched on by nothing.

The routes are in tests/test_customer_api_routing_settings.py; the statements that read and write the one key of the
settings document are in tests/test_routing_settings_postgres.py.
"""

from __future__ import annotations

import inspect
import json

import pytest

from services import routing_destination, routing_settings
from services.routing_destination import build_routing_config, destination_key
from services.routing_settings import (
    MAX_DESTINATIONS,
    ROUTING_SETTINGS_KEY,
    RoutingDestinationSetting,
    RoutingSettings,
    RoutingSettingsError,
    parse_stored_routing_settings,
    serialize_routing_settings,
    validate_routing_settings,
)


def _block(home="Main", *destinations):
    return {"home_branch_label": home, "destinations": [{"label": label, "enabled": enabled} for label, enabled in destinations]}


def _problems(block) -> list[tuple[str, str]]:
    with pytest.raises(RoutingSettingsError) as refused:
        validate_routing_settings(block)
    return [(problem.field, problem.code) for problem in refused.value.problems]


def _settings(home, *destinations) -> RoutingSettings:
    return RoutingSettings(home, tuple(RoutingDestinationSetting(label, enabled) for label, enabled in destinations))


# =====================================================================================================================
# What a replacement may be
# =====================================================================================================================

def test_a_block_is_accepted_with_its_labels_trimmed_and_its_order_and_flags_kept():
    accepted = validate_routing_settings(_block("  Main  ", ("  Westside ", True), ("Library Express", False), ("North Annex\t", True)))

    assert accepted == _settings("Main", ("Westside", True), ("Library Express", False), ("North Annex", True))
    assert ROUTING_SETTINGS_KEY == "transit"


@pytest.mark.parametrize("block", [
    _block("", ("Westside", True)),                         # no home label: a site is then labelled with its own name
    _block("   ", ("Westside", True)),
    _block("Main"),                                         # no destinations at all
    _block("", ),                                           # nothing configured
    _block("Main", ("Westside", False), ("Library Express", False)),   # every destination disabled
], ids=["blank-home", "whitespace-home", "no-destinations", "nothing", "all-disabled"])
def test_what_the_reports_already_have_a_meaning_for_is_allowed(block):
    accepted = validate_routing_settings(block)

    assert accepted.home_branch_label == ""  or accepted.home_branch_label == "Main"
    assert [d.label for d in accepted.destinations] == [d["label"] for d in block["destinations"]]


def test_a_home_label_left_out_is_a_blank_one():
    assert validate_routing_settings({"destinations": []}) == _settings("")


@pytest.mark.parametrize("label", [
    "Bücherei Süd", "Sucursal Ñandú (2ª planta)", "東図書館", "Annex #2 / Dock B", "O'Brien & Sons — Depot", "a" * 500,
    "x", "Branch 12", "  spaced   inside  ",
])
def test_a_label_may_hold_any_characters_and_be_any_length(label):
    accepted = validate_routing_settings(_block("Main", (label, True)))

    assert accepted.destinations == (RoutingDestinationSetting(label.strip(), True),)


def test_as_many_destinations_as_the_dashboards_form_ever_allowed_and_no_more():
    twenty = _block("Main", *((f"Branch {n}", True) for n in range(MAX_DESTINATIONS)))
    twenty_one = _block("Main", *((f"Branch {n}", True) for n in range(MAX_DESTINATIONS + 1)))

    assert MAX_DESTINATIONS == 20
    assert len(validate_routing_settings(twenty).destinations) == 20
    assert _problems(twenty_one) == [("destinations", "too_many")]


@pytest.mark.parametrize("label", ["", "   ", "\t\n"])
def test_a_destination_must_have_a_label(label):
    assert _problems(_block("Main", ("Westside", True), (label, True))) == [("destinations.1.label", "required")]


@pytest.mark.parametrize(("first", "second"), [
    ("Westside", "Westside"),
    ("Westside", "  WESTSIDE "),
    ("Library Express", "library_express"),          # the slug a collector stores for it
    ("Library Express", "LIBRARY   EXPRESS"),
    ("North Annex", "north-annex"),
    ("North Annex", "North  Annex!"),
])
def test_two_labels_that_are_one_destination_are_refused_at_the_second(first, second):
    assert destination_key(first) == destination_key(second)

    assert _problems(_block("Main", (first, True), ("Other Place", True), (second, False))) == [("destinations.2.label", "duplicate")]


@pytest.mark.parametrize(("home", "label"), [
    ("Main", "Main"),
    ("Main", "MAIN"),
    ("Main", "Local"),                  # the sorter's own words for "stays here"
    ("Main", "1"),
    ("Central Library", "Main"),        # the contract's home, whatever the organization calls its own
    ("Central Library", "central library"),
    ("Central Library", "Central-Library"),
    ("", "Main"),
])
def test_a_destination_that_means_home_is_refused(home, label):
    assert destination_key(label) in {"main", destination_key(home)}

    assert _problems(_block(home, ("Westside", True), (label, True))) == [("destinations.1.label", "means_home")]


def test_a_blank_home_label_makes_nothing_else_mean_home():
    # "unknown" is no destination's identity: a blank home label does not turn it into home.
    accepted = validate_routing_settings(_block("", ("Unknown", True)))

    assert [d.label for d in accepted.destinations] == ["Unknown"]


def test_every_problem_is_reported_at_once_and_none_carries_a_value():
    block = {
        "home_branch_label": 7,
        "destinations": [
            {"label": "Westside", "enabled": True},
            {"label": "CANARY-secret-label", "enabled": "yes", "key": "CANARY-supplied-key"},
            "CANARY-not-an-object",
            {"label": "   ", "enabled": True},
            {"label": "westside", "enabled": True},
            {"enabled": True},
            {"label": 5},
        ],
        "extra": "CANARY-extra",
    }

    with pytest.raises(RoutingSettingsError) as refused:
        validate_routing_settings(block)

    assert [(p.field, p.code) for p in refused.value.problems] == [
        ("extra", "unknown_field"),
        ("home_branch_label", "not_text"),
        ("destinations.1.key", "unknown_field"),
        ("destinations.1.enabled", "not_a_boolean"),
        ("destinations.2", "not_an_object"),
        ("destinations.3.label", "required"),
        ("destinations.4.label", "duplicate"),
        ("destinations.5.label", "required"),
        ("destinations.6.enabled", "required"),
        ("destinations.6.label", "not_text"),
    ]
    assert "CANARY" not in str(refused.value) and "CANARY" not in repr(refused.value.problems)


@pytest.mark.parametrize(("block", "problem"), [
    (None, ("routing", "not_an_object")),
    ([], ("routing", "not_an_object")),
    ("Main", ("routing", "not_an_object")),
    ({"home_branch_label": "Main"}, ("destinations", "required")),
    ({"home_branch_label": "Main", "destinations": None}, ("destinations", "required")),
    ({"home_branch_label": "Main", "destinations": {"0": {}}}, ("destinations", "not_a_list")),
    ({"home_branch_label": "Main", "destinations": "Westside"}, ("destinations", "not_a_list")),
    ({"home_branch_label": None, "destinations": []}, ("home_branch_label", "not_text")),
    ({"home_branch_label": "Main", "destinations": [{"label": "Westside", "enabled": 1}]}, ("destinations.0.enabled", "not_a_boolean")),
    ({"home_branch_label": "Main", "destinations": [{"label": "Westside"}]}, ("destinations.0.enabled", "required")),
])
def test_a_block_of_the_wrong_shape_is_refused(block, problem):
    assert _problems(block) == [problem]


# =====================================================================================================================
# The form a block is stored in, and the key written beside each label
# =====================================================================================================================

def test_the_stored_block_is_whole_and_each_destination_carries_the_key_its_label_gives_it():
    stored = serialize_routing_settings(_settings("Main", ("Westside", True), ("Library Express", False), ("North Annex #2", True)))

    assert stored == {
        "home_branch_label": "Main",
        "destinations": [
            {"key": "westside", "label": "Westside", "enabled": True},
            {"key": "library_express", "label": "Library Express", "enabled": False},
            {"key": "north_annex_2", "label": "North Annex #2", "enabled": True},
        ],
    }
    for destination in stored["destinations"]:
        assert destination["key"] == destination_key(destination["label"])
    assert json.loads(json.dumps(stored)) == stored
    # Nothing configured is still a block, stored whole: an explicit "no destinations".
    assert serialize_routing_settings(_settings("")) == {"home_branch_label": "", "destinations": []}


def test_the_key_is_never_taken_from_a_caller_and_never_given_back():
    assert _problems({"home_branch_label": "Main", "destinations": [{"label": "Westside", "enabled": True, "key": "anything"}]}) == [
        ("destinations.0.key", "unknown_field"),
    ]
    assert [field.name for field in RoutingDestinationSetting.__dataclass_fields__.values()] == ["label", "enabled"]
    # A stored key that disagrees with its label is not read: the label is what the destination is.
    read = parse_stored_routing_settings({"destinations": [{"key": "something_else", "label": "Westside", "enabled": True}]})
    assert read.destinations == (RoutingDestinationSetting("Westside", True),)
    assert serialize_routing_settings(read)["destinations"][0]["key"] == "westside"


def test_what_is_accepted_and_stored_is_read_back_the_same_and_means_the_same_to_the_reports():
    accepted = validate_routing_settings(_block(" Main ", ("Westside", True), ("Library Express", False), ("North Annex", True)))
    stored = serialize_routing_settings(accepted)

    assert parse_stored_routing_settings(stored) == accepted
    # The reports' own reading of that stored block: the enabled destinations, by the same keys.
    config = build_routing_config({ROUTING_SETTINGS_KEY: stored}, site_name="Central Branch")
    assert config.home_label == "Main"
    assert [(d.key, d.label) for d in config.transit] == [("westside", "Westside"), ("north_annex", "North Annex")]


# =====================================================================================================================
# What is already stored is never refused
# =====================================================================================================================

def test_a_block_the_dashboards_form_stored_is_read_as_the_reports_read_it():
    stored = {
        "home_branch_label": "  Main ",
        "destinations": [
            {"key": "branch_1", "label": "Westside", "enabled": True},      # a key that says nothing of its label
            {"key": "westside", "label": " Westside ", "enabled": False},    # the same destination again
            {"key": "main", "label": "Main", "enabled": True},               # one that means home
            {"key": "library_express", "label": "Library Express"},          # no `enabled`: enabled
            {"key": "branch_5", "label": "   ", "enabled": True},            # a blank row
            {"key": "branch_6", "label": "", "enabled": True},
            {"label": "North Annex", "enabled": 0},                          # no key, a falsy flag
        ],
    }

    assert parse_stored_routing_settings(stored) == _settings(
        "Main", ("Westside", True), ("Westside", False), ("Main", True), ("Library Express", True), ("North Annex", False),
    )
    # ... though a replacement saying the same thing would be refused, until it is put right.
    assert ("destinations.1.label", "duplicate") in _problems(_block("Main", ("Westside", True), ("Westside", False)))


@pytest.mark.parametrize("stored", [
    None, {}, [], "transit", 7, {"home_branch_label": None}, {"destinations": None}, {"destinations": "Westside"},
    {"destinations": {"0": {"label": "Westside"}}}, {"home_branch_label": ["Main"], "destinations": [None, 3, "x", [], {}]},
    {"destinations": [{"label": None}, {"label": 9}, {"enabled": True}]},
])
def test_nothing_stored_or_something_malformed_is_no_settings_and_never_an_error(stored):
    assert parse_stored_routing_settings(stored) == _settings("")


def test_only_the_two_fields_of_the_block_are_read_whatever_else_is_beside_them():
    read = parse_stored_routing_settings({
        "home_branch_label": "Main",
        "destinations": [{"key": "westside", "label": "Westside", "enabled": True, "id": 41, "secret": "CANARY"}],
        "admin_password_hash": "CANARY-hash",
        "id": 9,
    })

    assert read == _settings("Main", ("Westside", True))
    assert "CANARY" not in repr(read) and "41" not in repr(read)


# =====================================================================================================================
# The shape of the module
# =====================================================================================================================

def test_the_module_is_pure_and_says_that_a_change_reclassifies_what_was_already_recorded():
    source = inspect.getsource(routing_settings)
    code = source.split('"""', 2)[2]

    for forbidden in ("sqlalchemy", "get_engine", "fastapi", "streamlit", "datetime", "import os", "branch_settings", "internal_routing"):
        assert forbidden not in code, forbidden
    assert "CHANGING THEM CHANGES WHAT PAST REPORTS SAY." in source
    assert "no stored event is rewritten" in source
    assert "THESE ARE THE ORGANIZATION'S OWN SETTINGS, not what applies at a sorter site." in source
    # One rule for what a label matches, and it is the reports' own.
    assert routing_settings.destination_key is routing_destination.destination_key
