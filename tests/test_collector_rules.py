"""services.collector_rules: what a Contract v2 enrollment hands a Collector about its site -- the time zone, the
"rules were reviewed" record, and the classification rules document -- built from the site's own settings and nothing
else.

THE COLLECTOR IS THE AUTHORITY ON THE RULES SCHEMA. The API and the frozen Collector are separate deployment units, so
the server reproduces collector.v2_rules.seed_from_settings + normalize_document instead of importing them; the parity
tests here are what keep the two from drifting. Everything is pure: no database, no clock, no request.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from collector import v2_rules
from src.services import collector_rules as rules
from src.services.collector_rules import CollectorSettingsError

ROOT = Path(__file__).resolve().parent.parent
EMPTY_DOCUMENT = {
    "schema_version": 1,
    "destinations": [],
    "branch_services_names": [],
    "collection_services_names": [],
    "branch_services_da_patterns": [],
    "collection_services_da_patterns": [],
}

# A site that is not any real library: every name here is made up for this test.
SITE = {
    "library_name": "Example Library",
    "transit": {
        "home_branch_label": "Central",
        "destinations": [
            {"key": "north_annex", "label": "North Annex", "enabled": True},
            {"key": "", "label": "Bookmobile 7", "enabled": True},
            {"key": "closed", "label": "Closed Branch", "enabled": False},
            {"key": "9lives", "label": "Nine Lives"},
            {"key": "!!!", "label": "Unsluggable"},
            {"key": "blank", "label": "   "},
        ],
    },
    "internal_routing": {
        "branch_services_names": ["  canary (st)staffer ", "CANARY (ST)STAFFER", "Outreach Desk", ""],
        "collection_services_names": ["(zz) ts(zz)-cataloging"],
        "branch_services_da_patterns": ["DAcanary (st)staffer"],
        "collection_services_da_patterns": ["DA(zz) ts(zz)-cataloging", "  "],
    },
    "collector_timezone": "America/New_York",
}


def _reason(excinfo) -> str:
    return excinfo.value.reason


def _collector(settings) -> dict:
    """What the Collector's own two functions make of the same settings."""
    return v2_rules.normalize_document(v2_rules.seed_from_settings(settings))


# =====================================================================================================================
# the rules document
# =====================================================================================================================

def test_a_site_with_nothing_configured_gets_a_document_of_empty_lists_and_nothing_else():
    for settings in ({}, {"transit": {}, "internal_routing": {}}, {"transit": {"destinations": []}},
                     {"internal_routing": {name: [] for name in rules.NAME_LISTS}}):
        assert rules.build_classification_rules(settings) == EMPTY_DOCUMENT


def test_it_builds_the_collectors_schema_from_the_sites_own_settings():
    document = rules.build_classification_rules(SITE)

    assert list(document) == ["schema_version", "destinations", *rules.NAME_LISTS]
    assert document["schema_version"] == 1
    # In configured order; a disabled destination, a blank label and a key that cannot be a slug are left out, and a
    # destination with no key takes its slug from its label. (How a slug is chosen is the Collector's rule, reproduced.)
    assert document["destinations"] == [
        {"slug": "north_annex", "contains": "NORTH ANNEX"},
        {"slug": "bookmobile_7", "contains": "BOOKMOBILE 7"},
        {"slug": "d_9lives", "contains": "NINE LIVES"},
    ]
    # Stripped, upper-cased, de-duplicated, sorted; a blank entry is nothing.
    assert document["branch_services_names"] == ["CANARY (ST)STAFFER", "OUTREACH DESK"]
    assert document["collection_services_names"] == ["(ZZ) TS(ZZ)-CATALOGING"]
    assert document["branch_services_da_patterns"] == ["DACANARY (ST)STAFFER"]
    assert document["collection_services_da_patterns"] == ["DA(ZZ) TS(ZZ)-CATALOGING"]


def test_the_same_settings_always_give_the_same_document_whatever_order_the_lists_are_in():
    shuffled = copy.deepcopy(SITE)
    for name in rules.NAME_LISTS:
        shuffled["internal_routing"][name] = list(reversed(shuffled["internal_routing"][name]))
    shuffled["internal_routing"] = dict(reversed(list(shuffled["internal_routing"].items())))

    first = rules.build_classification_rules(SITE)

    assert rules.build_classification_rules(copy.deepcopy(SITE)) == first
    assert json.dumps(rules.build_classification_rules(shuffled)) == json.dumps(first)
    assert json.dumps(first, sort_keys=True) == json.dumps(rules.build_classification_rules(SITE), sort_keys=True)


def test_building_never_changes_the_settings_it_reads():
    before = copy.deepcopy(SITE)

    rules.build_classification_rules(SITE)
    rules.effective_settings(SITE, {"transit": {"home_branch_label": "Other"}})

    assert SITE == before


def test_no_librarys_names_or_defaults_are_in_the_module_or_in_an_empty_sites_document():
    source = (ROOT / "src" / "services" / "collector_rules.py").read_text(encoding="utf-8").lower()
    produced = json.dumps(rules.build_classification_rules({})).lower()
    shipped = json.loads((ROOT / "src" / "branch_settings.json").read_text(encoding="utf-8"))
    names = [name.lower() for key in rules.NAME_LISTS for name in shipped["internal_routing"][key]]
    assert names  # the repository's sample settings do hold names: that is what must not leak into another site

    for text in (source, produced):
        for forbidden in ("westside", "library express", "library_express", "new braunfels", "nbpl", "tlcfinal", *names):
            assert forbidden not in text, forbidden
    # It reads no file and imports nothing of the Collector's: the settings it is handed are all it knows.
    for forbidden in ("branch_settings.json", "import collector", "from collector", "open(", "import json", "import os"):
        assert forbidden not in source, forbidden


# --- parity with the Collector -----------------------------------------------------------------------------------------

PARITY_CASES = {
    "nothing": {},
    "the example site": SITE,
    "empty blocks": {"transit": {}, "internal_routing": {}},
    "empty lists": {"transit": {"destinations": []}, "internal_routing": {name: [] for name in rules.NAME_LISTS}},
    "the repository's sample settings": json.loads((ROOT / "src" / "branch_settings.json").read_text(encoding="utf-8")),
    "what Provision Library stores": {
        "transit": {"home_branch_label": "Main", "destinations": [
            {"key": "dest_1", "label": "East Branch", "enabled": True}, {"key": "dest_2", "label": "Depot", "enabled": True}]},
        "internal_routing": {name: [] for name in rules.NAME_LISTS},
    },
    "duplicate destinations are kept, as the Collector keeps them": {
        "transit": {"destinations": [{"key": "a", "label": "Alpha"}, {"key": "a", "label": "Alpha"}]}},
    "a long key is cut to 32 characters": {
        "transit": {"destinations": [{"key": "x" * 40, "label": "Long"}, {"key": "", "label": "A label " * 8}]}},
    "labels with punctuation and case": {
        "transit": {"destinations": [{"label": "St. Mary's — Annex #2"}, {"label": "  spaced   out  "}, {"label": "42nd Street"}]}},
    "names with odd spacing and case": {
        "internal_routing": {"branch_services_names": [" a ", "A", "b\t", "ß"], "collection_services_da_patterns": ["DAx", "dax"]}},
    "null blocks and lists": {"transit": None, "internal_routing": {"branch_services_names": None}},
    "other settings are ignored": {"efficiency": {"labor_rate": "20.00"}, "security": {"admin_enabled": True},
                                   "collector_timezone": "America/Denver", "collector_rules": {"reviewed_at": "x"}},
}


@pytest.mark.parametrize("settings", PARITY_CASES.values(), ids=PARITY_CASES.keys())
def test_the_server_builds_exactly_what_the_collector_would_seed_and_normalize(settings):
    built = rules.build_classification_rules(settings)

    assert built == _collector(settings)
    assert json.dumps(built) == json.dumps(_collector(settings))  # the same keys in the same order, too
    # And the Collector's validator accepts it as it is: normalizing again changes nothing.
    assert v2_rules.normalize_document(built) == built


def test_the_constants_the_server_copies_are_the_collectors_own():
    assert rules.RULES_SCHEMA_VERSION == v2_rules.SCHEMA_VERSION
    assert rules.MAX_RULE_ENTRIES == v2_rules._MAX_ENTRIES
    assert rules._SLUG.pattern == v2_rules._SLUG.pattern
    for key in ("", "North Annex", "9lives", "!!!", "x" * 40, " A-b_c ", "ünï", "d", "_", "1"):
        assert rules._slugify(key) == v2_rules._slugify(key), key
    # The four lists, by the names both sides use for them.
    assert set(rules.NAME_LISTS) == set(_collector({})) - {"schema_version", "destinations"}


def test_the_collector_can_compile_a_document_the_server_built():
    from collector.v2_identity import derive_subkeys

    compiled = v2_rules.compile_rules(rules.build_classification_rules(SITE), derive_subkeys(b"k" * 32))

    assert compiled.destinations == (("NORTH ANNEX", "north_annex"), ("BOOKMOBILE 7", "bookmobile_7"), ("NINE LIVES", "d_9lives"))
    assert len(compiled.branch_name_hmacs) == 2 and len(compiled.collection_name_hmacs) == 1
    assert compiled.branch_da_markers == ("|DACANARY (ST)STAFFER|",)


# --- what is refused ---------------------------------------------------------------------------------------------------

INVALID = {
    "not an object": ["transit"],
    "text": "settings",
    "nothing at all": None,
    "transit is not an object": {"transit": ["destinations"]},
    "transit is text": {"transit": "Main"},
    "internal_routing is not an object": {"internal_routing": ["names"]},
    "destinations is not a list": {"transit": {"destinations": {"key": "a", "label": "A"}}},
    "destinations is text": {"transit": {"destinations": "North Annex"}},
    "a destination is not an object": {"transit": {"destinations": ["North Annex"]}},
    "a name list is text": {"internal_routing": {"branch_services_names": "CANARY STAFFER"}},
    "a name list is an object": {"internal_routing": {"collection_services_names": {"a": "b"}}},
    "a name is a number": {"internal_routing": {"branch_services_names": ["A", 7]}},
    "a name is null": {"internal_routing": {"branch_services_da_patterns": [None]}},
    "a name is a list": {"internal_routing": {"collection_services_da_patterns": [["A"]]}},
    "too many names": {"internal_routing": {"branch_services_names": [f"N{i}" for i in range(rules.MAX_RULE_ENTRIES + 1)]}},
    "too many destinations": {"transit": {"destinations": [{"label": f"D{i}"} for i in range(rules.MAX_RULE_ENTRIES + 1)]}},
}


@pytest.mark.parametrize("settings", INVALID.values(), ids=INVALID.keys())
def test_settings_that_do_not_say_what_they_mean_are_refused(settings):
    with pytest.raises(CollectorSettingsError) as excinfo:
        rules.build_classification_rules(settings)

    assert _reason(excinfo) == rules.RULES_INVALID
    # The whole of what the error says is the fixed code: no value from the settings is in it.
    assert str(excinfo.value) == "rules_invalid" and excinfo.value.args == ("rules_invalid",)
    assert excinfo.value.__cause__ is None and excinfo.value.__context__ is None


def test_exactly_the_limit_is_accepted():
    settings = {"internal_routing": {"branch_services_names": [f"N{i}" for i in range(rules.MAX_RULE_ENTRIES)]}}

    assert len(rules.build_classification_rules(settings)["branch_services_names"]) == rules.MAX_RULE_ENTRIES
    assert rules.build_classification_rules(settings) == _collector(settings)


def test_a_refusal_never_repeats_a_name():
    settings = {"internal_routing": {"branch_services_names": ["CANARY (ST)STAFFER", 7]}}

    with pytest.raises(CollectorSettingsError) as excinfo:
        rules.build_classification_rules(settings)

    assert "CANARY" not in repr(excinfo.value) and "CANARY" not in str(excinfo.value)


# =====================================================================================================================
# effective settings
# =====================================================================================================================

def test_a_branchs_settings_are_merged_over_its_organizations_key_by_key():
    organization = {"transit": {"home_branch_label": "Main", "destinations": [{"label": "Org Wide"}]},
                    "internal_routing": {"branch_services_names": ["ORG NAME"], "collection_services_names": ["KEPT"]},
                    "collector_timezone": "America/Chicago"}
    branch = {"transit": {"destinations": [{"label": "Branch Only"}]},
              "internal_routing": {"branch_services_names": ["BRANCH NAME"]}, "branch_name": "East"}

    effective = rules.effective_settings(organization, branch)

    assert effective["transit"] == {"home_branch_label": "Main", "destinations": [{"label": "Branch Only"}]}
    assert effective["internal_routing"] == {"branch_services_names": ["BRANCH NAME"], "collection_services_names": ["KEPT"]}
    assert effective["collector_timezone"] == "America/Chicago" and effective["branch_name"] == "East"
    assert rules.build_classification_rules(effective)["branch_services_names"] == ["BRANCH NAME"]
    # The dashboard's own merge gives the same document.
    from src.services.settings_merge import merge_settings

    assert effective == merge_settings(organization, branch)


@pytest.mark.parametrize("missing", [None, "", [], 7])
def test_settings_that_are_not_stored_are_an_empty_document(missing):
    assert rules.effective_settings(missing, missing) == {}
    assert rules.effective_settings({"a": 1}, missing) == {"a": 1}
    assert rules.effective_settings(missing, {"b": 2}) == {"b": 2}


# =====================================================================================================================
# the Collector's time zone
# =====================================================================================================================

@pytest.mark.parametrize("name", ["America/New_York", "America/Chicago", "America/Los_Angeles", "Pacific/Honolulu", "UTC",
                                  "Europe/London"])
def test_a_configured_iana_zone_is_returned_as_it_is(name):
    assert rules.collector_timezone({"collector_timezone": name}) == name
    assert rules.collector_timezone({"collector_timezone": f"  {name} "}) == name


@pytest.mark.parametrize("settings", [{}, {"collector_timezone": None}, {"collector_timezone": ""},
                                      {"collector_timezone": "   "}, None, "America/Chicago", []],
                         ids=["absent", "null", "empty", "blank", "no settings", "settings are text", "settings are a list"])
def test_a_zone_that_was_never_configured_is_refused_and_never_defaulted(settings):
    with pytest.raises(CollectorSettingsError) as excinfo:
        rules.collector_timezone(settings)

    assert _reason(excinfo) == rules.TIMEZONE_NOT_CONFIGURED


@pytest.mark.parametrize("value", ["Central", "CST6", "America/Not_A_City", "GMT-6", "../etc/passwd",
                                   "America/Chicago;x", "A" * 65, 5, True, ["America/Chicago"], {"zone": "UTC"}, -6.0])
def test_a_value_that_is_not_a_known_iana_zone_is_refused(value):
    with pytest.raises(CollectorSettingsError) as excinfo:
        rules.collector_timezone({"collector_timezone": value})

    assert _reason(excinfo) == rules.TIMEZONE_INVALID
    assert str(excinfo.value) == "timezone_invalid"  # never the value


def test_the_collector_accepts_a_zone_the_server_accepts(tmp_path):
    # collector.v2_config applies the same test to `v2.timezone`: a name ZoneInfo knows.
    from collector.v2_config import load_v2_config

    config = tmp_path / "collector_config.json"
    config.write_text(json.dumps({
        "state_path": str(tmp_path / "data" / "state.json"),
        "v2": {"key_id": "3f2b8c1e-4a5d-4e6f-8a9b-0c1d2e3f4a5b", "timezone": rules.collector_timezone(SITE)},
    }), encoding="utf-8")

    assert load_v2_config(config, require=True).timezone == "America/New_York"


def test_the_zone_is_the_collectors_and_is_read_by_nothing_that_draws_a_report():
    # Stage A1 hands it to a Collector at enrollment and does nothing else with it. Stage A3b adds the one other
    # place it is named: services.tenant_service, where Super Admin reads and records a site's Collector settings
    # (get_collector_site_settings / set_collector_site_settings). Neither draws a report.
    readers = sorted(
        str(path.relative_to(ROOT)).replace("\\", "/")
        for path in (ROOT / "src").rglob("*.py")
        if "collector_timezone" in path.read_text(encoding="utf-8")
    )

    assert readers == [
        "src/services/collector_enrollment_service.py",
        "src/services/collector_rules.py",
        "src/services/tenant_service.py",
    ]


# =====================================================================================================================
# the review record
# =====================================================================================================================

REVIEWED_AT = datetime(2026, 10, 11, 15, 4, 5, tzinfo=UTC)


def test_the_marker_records_who_and_when_and_holds_no_name():
    marker = rules.rules_review_marker(reviewed_at=REVIEWED_AT, reviewed_by_user_id=7)

    assert marker == {"reviewed_at": "2026-10-11T15:04:05+00:00", "reviewed_by_user_id": 7}
    rules.require_rules_reviewed({"collector_rules": marker})
    # Any aware instant is one; it is stored with its own offset.
    eastern = datetime(2026, 10, 11, 11, 4, 5, tzinfo=timezone(timedelta(hours=-4)))
    rules.require_rules_reviewed({"collector_rules": rules.rules_review_marker(reviewed_at=eastern, reviewed_by_user_id=1)})


@pytest.mark.parametrize("reviewed_at, reviewer", [(REVIEWED_AT.replace(tzinfo=None), 7), ("2026-10-11T15:04:05+00:00", 7),
                                                   (None, 7), (REVIEWED_AT, 0), (REVIEWED_AT, -1), (REVIEWED_AT, True),
                                                   (REVIEWED_AT, "7"), (REVIEWED_AT, None)])
def test_a_marker_cannot_be_made_without_an_aware_time_and_a_real_reviewer(reviewed_at, reviewer):
    with pytest.raises(ValueError):
        rules.rules_review_marker(reviewed_at=reviewed_at, reviewed_by_user_id=reviewer)


def test_reviewed_and_empty_is_ready():
    settings = {"collector_rules": rules.rules_review_marker(reviewed_at=REVIEWED_AT, reviewed_by_user_id=7),
                "internal_routing": {name: [] for name in rules.NAME_LISTS}}

    rules.require_rules_reviewed(settings)
    assert rules.build_classification_rules(settings) == EMPTY_DOCUMENT


def test_full_lists_that_nobody_reviewed_are_not_ready():
    # Readiness is the record, never the contents: what Provision Library has always stored says nothing about review.
    for settings in (SITE, {"internal_routing": SITE["internal_routing"]}, PARITY_CASES["what Provision Library stores"], {}):
        with pytest.raises(CollectorSettingsError) as excinfo:
            rules.require_rules_reviewed(settings)
        assert _reason(excinfo) == rules.RULES_NOT_REVIEWED


@pytest.mark.parametrize("marker", [
    None, {}, [], "reviewed", True, 1,
    {"reviewed_at": "2026-10-11T15:04:05+00:00"},
    {"reviewed_by_user_id": 7},
    {"reviewed_at": "2026-10-11T15:04:05", "reviewed_by_user_id": 7},
    {"reviewed_at": "2026-10-11", "reviewed_by_user_id": 7},
    {"reviewed_at": "yesterday", "reviewed_by_user_id": 7},
    {"reviewed_at": "", "reviewed_by_user_id": 7},
    {"reviewed_at": None, "reviewed_by_user_id": 7},
    {"reviewed_at": 1760195045, "reviewed_by_user_id": 7},
    {"reviewed_at": "2026-10-11T15:04:05+00:00", "reviewed_by_user_id": 0},
    {"reviewed_at": "2026-10-11T15:04:05+00:00", "reviewed_by_user_id": -3},
    {"reviewed_at": "2026-10-11T15:04:05+00:00", "reviewed_by_user_id": "7"},
    {"reviewed_at": "2026-10-11T15:04:05+00:00", "reviewed_by_user_id": True},
    {"reviewed_at": "2026-10-11T15:04:05+00:00", "reviewed_by_user_id": None},
    {"reviewed": True},
    {"configured": True},
])
def test_anything_short_of_a_valid_record_is_not_reviewed(marker):
    with pytest.raises(CollectorSettingsError) as excinfo:
        rules.require_rules_reviewed({"collector_rules": marker, "internal_routing": SITE["internal_routing"]})

    assert _reason(excinfo) == rules.RULES_NOT_REVIEWED
    assert str(excinfo.value) == "rules_not_reviewed"
