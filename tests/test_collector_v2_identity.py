"""Contract v2 collector: keyed identity (collector/v2_identity.py).

Known-answer tests freeze the canonical forms and the keys derived from a fixed synthetic master; invariance tests prove that nothing raw can
influence an event_key; domain-separation tests prove one input under two purposes gives unrelated values.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from collector import v2_events as ev
from collector import v2_identity as ident

ROOT = Path(__file__).resolve().parent.parent
COLLECTOR = ROOT / "collector"
MASTER = bytes(range(32))
KEYS = ident.derive_subkeys(MASTER)
WHEN = datetime(2026, 3, 4, 15, 30, 5, tzinfo=UTC)
WHEN_TEXT = "2026-03-04T15:30:05Z"
ITEM = ident.item_key(KEYS, "ITEM-0001")
RULESET = "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d"


# --- RFC 5869 and the subkeys ---------------------------------------------------------------------------------------------------

def test_hkdf_expand_matches_the_rfc_5869_test_case_1():
    prk = bytes.fromhex("077709362c2e32df0ddc3f0dc47bba6390b6c73bb50f9c3122ec844ad7c2b3e5")
    info = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9")
    expected = bytes.fromhex("3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865")
    assert ident.hkdf_expand(prk, info, 42) == expected


def test_subkeys_are_the_documented_hkdf_of_the_master_and_match_frozen_values():
    frozen = {
        "event": "664df22073db24d5a7a26cb3d01ef484e2a9bbb3ddb285f3c4381da12dec8720",
        "item": "abd4af77cc014b8ddb921f63f76b6fa15b8868de817170b07403a26388359c80",
        "patron": "3571b6cbb873b36ea02e78dfa3338064c5160285347503404890f73fcc3ed4d1",
        "name": "87f13c4b38dc46fb45b35470b4ab355e79da2d2793a3908a52d0ee79ffcf07df",
        "ruleset": "6be8caafc8ca47edffa8bcb53ff9d4fa69f724be216c9db017304f40a64c2400",
    }
    for purpose, hex_value in frozen.items():
        independent = hmac.new(MASTER, b"sortview/v2/hmac-sha256-v1/" + purpose.encode() + b"\x01", hashlib.sha256).digest()
        assert getattr(KEYS, purpose) == independent == bytes.fromhex(hex_value)


def test_the_five_subkeys_are_pairwise_distinct_and_differ_from_the_master():
    values = [getattr(KEYS, purpose) for purpose in ident.PURPOSES]
    assert len(set(values)) == 5 and MASTER not in values


@pytest.mark.parametrize("bad", [b"", b"x" * 31, b"x" * 33, "a" * 32, None])
def test_the_master_must_be_exactly_32_bytes(bad):
    with pytest.raises(ValueError):
        ident.derive_subkeys(bad)


def test_subkeys_never_show_in_repr_or_str():
    for text in (repr(KEYS), str(KEYS), f"{KEYS}", f"{KEYS!r}"):
        assert text == "SubKeys(<hidden>)"
        assert MASTER.hex() not in text and KEYS.event.hex() not in text


# --- domain separation ----------------------------------------------------------------------------------------------------------

def test_one_input_under_each_purpose_gives_unrelated_values():
    same = "31234000123456"
    values = {
        "item": ident.item_key(KEYS, same),
        "patron": ident.patron_id_hmac(KEYS, same).hex(),
        "name": ident.name_hmac(KEYS, same).hex(),
        "event": ident.event_key(KEYS, same.encode()),
        "ruleset": ident.ruleset_fingerprint(KEYS, same.encode()).hex(),
    }
    assert len(set(values.values())) == 5


def test_a_cloud_item_key_can_never_be_looked_up_as_a_local_patron_key():
    assert bytes.fromhex(ident.item_key(KEYS, "SAME-STRING")) != ident.patron_id_hmac(KEYS, "SAME-STRING")


def test_different_masters_give_different_everything():
    other = ident.derive_subkeys(bytes(reversed(range(32))))
    assert ident.item_key(other, "ITEM-0001") != ITEM
    assert ident.patron_id_hmac(other, "X") != ident.patron_id_hmac(KEYS, "X")


def test_item_key_strips_whitespace_and_is_case_sensitive():
    assert ident.item_key(KEYS, "  ITEM-0001 \n") == ident.item_key(KEYS, "ITEM-0001")
    assert ident.item_key(KEYS, "item-0001") != ident.item_key(KEYS, "ITEM-0001")


def test_name_hmac_matches_the_dashboards_strip_and_upper_comparison():
    assert ident.name_hmac(KEYS, "  Some Name ") == ident.name_hmac(KEYS, "SOME NAME")


# --- known answers ---------------------------------------------------------------------------------------------------------------

def test_item_patron_and_name_known_answers():
    assert ident.item_key(KEYS, "ITEM-0001") == "55eb37a3ae32a24707dac7971b8ad938d819ade1d1f79b846be46d08cddee34d"
    assert ident.patron_id_hmac(KEYS, "ITEM-0001").hex() == "bc6d38048196f28af3db2f7290e4719486af59b254de80247547825ac6dd1cba"
    assert ident.name_hmac(KEYS, "some name").hex() == "32b2f37510bdf93295922a5ca8a10e41d41550c626c0c4fb3dd6bc3486d54189"


CANONICAL_CASES = [
    ("checkin", lambda: ident.canonical_checkin(event_time=WHEN_TEXT, item_key=ITEM, destination="westside", bin="3"),
     "sortview/v2/event/checkin/1\nevent_time=2026-03-04T15:30:05Z\nitem_key=" + ITEM + "\ndestination=westside\nbin=3",
     "ae19fd5f6ed86bc7a36fdfd7e3ce0eb8700a6f40c5550008890e0543cbccf074"),
    ("checkin without an item", lambda: ident.canonical_checkin(event_time=WHEN_TEXT, item_key=None, destination="unknown", bin="unknown"),
     "sortview/v2/event/checkin/1\nevent_time=2026-03-04T15:30:05Z\nitem_key=~\ndestination=unknown\nbin=unknown",
     "abd0f69e60fe77dd875a157e670f62b8aa7a7ee6c9451405ed3440c6d5bac59a"),
    ("reject", lambda: ident.canonical_reject(event_time=WHEN_TEXT, item_key=ITEM, error_class="item_not_found"),
     "sortview/v2/event/reject/1\nevent_time=2026-03-04T15:30:05Z\nitem_key=" + ITEM + "\nerror_class=item_not_found",
     "c70cc63ba22b43c9b2efa4803b92e105777fa78cb7a9d9be31d0877bd734e1c9"),
    ("reject without an item", lambda: ident.canonical_reject(event_time=WHEN_TEXT, item_key=None, error_class="unknown"),
     "sortview/v2/event/reject/1\nevent_time=2026-03-04T15:30:05Z\nitem_key=~\nerror_class=unknown",
     "6b63431781e23d9b0197729bbf034d4f4dd9b419e7c7c334deaa98eb9915e74a"),
    ("acs non-hold", lambda: ident.canonical_acs_item(event_time=WHEN_TEXT, item_key=ITEM, state="non_hold_101"),
     "sortview/v2/event/acs_item/1\nevent_time=2026-03-04T15:30:05Z\nitem_key=" + ITEM + "\nstate=non_hold_101",
     "3e4207af0df75727d1ec4958e0152da13c9a3cfe205ef96e170126d631a08bf4"),
    ("acs hold", lambda: ident.canonical_acs_item(event_time=WHEN_TEXT, item_key=ITEM, state="hold", destination="main", is_ill=False,
                                                  is_branch_services=True, is_collection_services=False),
     "sortview/v2/event/acs_item/1\nevent_time=2026-03-04T15:30:05Z\nitem_key=" + ITEM + "\nstate=hold\ndestination=main\nis_ill=0\n"
     "is_branch_services=1\nis_collection_services=0",
     "d601e73ab4658d7e74501e4b99e4c4f99d07da8b9d7196372f8d633a5bf31edc"),
    ("acs hold, ILL", lambda: ident.canonical_acs_item(event_time=WHEN_TEXT, item_key=ITEM, state="hold", destination="main", is_ill=True,
                                                       is_branch_services=False, is_collection_services=False),
     "sortview/v2/event/acs_item/1\nevent_time=2026-03-04T15:30:05Z\nitem_key=" + ITEM + "\nstate=hold\ndestination=main\nis_ill=1\n"
     "is_branch_services=0\nis_collection_services=0",
     "f36b0522f3c3381a72cae028212996d575c83908c907837a7a2389a1e700ddef"),
    ("acs hold, correction revision 1", lambda: ident.canonical_acs_item(
        event_time=WHEN_TEXT, item_key=ITEM, state="hold", destination="main", is_ill=True, is_branch_services=False,
        is_collection_services=False, revision=1),
     "sortview/v2/event/acs_item/1\nevent_time=2026-03-04T15:30:05Z\nitem_key=" + ITEM + "\nstate=hold\ndestination=main\nis_ill=1\n"
     "is_branch_services=0\nis_collection_services=0\nrevision=1",
     "b6f38edbe8274f38ea8bb2657c47089fdcb33b27550df8ada9410ed88a708564"),
    ("acs hold, correction revision 2", lambda: ident.canonical_acs_item(
        event_time=WHEN_TEXT, item_key=ITEM, state="hold", destination="main", is_ill=True, is_branch_services=False,
        is_collection_services=False, revision=2),
     "sortview/v2/event/acs_item/1\nevent_time=2026-03-04T15:30:05Z\nitem_key=" + ITEM + "\nstate=hold\ndestination=main\nis_ill=1\n"
     "is_branch_services=0\nis_collection_services=0\nrevision=2",
     "da21f86e6b992f58a32712c762c6578c334938ad14731d488c8dbb610d6bb0e0"),
]


@pytest.mark.parametrize(("label", "build", "canonical", "key"), CANONICAL_CASES, ids=[c[0] for c in CANONICAL_CASES])
def test_canonical_forms_and_event_keys_are_frozen(label, build, canonical, key):
    built = build()
    assert built.decode("ascii") == canonical, label
    assert ident.event_key(KEYS, built) == key
    # and an independent computation agrees
    assert hmac.new(KEYS.event, canonical.encode("ascii"), hashlib.sha256).hexdigest() == key


def test_the_built_events_carry_exactly_the_frozen_keys():
    assert ident.build_checkin(KEYS, event_time=WHEN, item_key=ITEM, destination="westside", bin="3").event_key == CANONICAL_CASES[0][3]
    assert ident.build_reject(KEYS, event_time=WHEN, item_key=None, error_class="unknown").event_key == CANONICAL_CASES[3][3]
    assert ident.build_acs_item(KEYS, event_time=WHEN, item_key=ITEM, state="non_hold_101").event_key == CANONICAL_CASES[4][3]
    hold = ident.build_acs_item(KEYS, event_time=WHEN, item_key=ITEM, state="hold", destination="main", is_ill=False,
                                is_branch_services=True, is_collection_services=False, ruleset_id=RULESET)
    assert hold.event_key == CANONICAL_CASES[5][3]


def test_the_absent_marker_is_not_a_legal_value_of_any_field():
    for pattern in (ev.HMAC_HEX_PATTERN, ev.DESTINATION_PATTERN, ev.BIN_PATTERN, ev.UUID4_PATTERN):
        assert re.fullmatch(pattern, ident.ABSENT) is None
    assert ident.ABSENT not in ev.ERROR_CLASSES + ev.ACS_ITEM_STATES


def test_a_line_break_can_never_enter_a_canonical_form():
    with pytest.raises(ValueError):
        ident.canonical_checkin(event_time=WHEN_TEXT, item_key=None, destination="a\nb", bin="1")


def test_every_field_that_is_part_of_the_canonical_form_changes_the_key():
    base = {"event_time": WHEN_TEXT, "item_key": ITEM, "destination": "westside", "bin": "3"}
    original = ident.event_key(KEYS, ident.canonical_checkin(**base))
    for name, value in (("event_time", "2026-03-04T15:30:06Z"), ("item_key", ident.item_key(KEYS, "ITEM-0002")),
                        ("destination", "main"), ("bin", "4")):
        changed = ident.event_key(KEYS, ident.canonical_checkin(**{**base, name: value}))
        assert changed != original, name
    hold = {"event_time": WHEN_TEXT, "item_key": ITEM, "state": "hold", "destination": "main", "is_ill": False, "is_branch_services": False,
            "is_collection_services": False}
    original = ident.event_key(KEYS, ident.canonical_acs_item(**hold))
    for name, value in (("destination", "westside"), ("is_ill", True), ("is_branch_services", True), ("is_collection_services", True),
                        ("state", "non_hold_101"), ("event_time", "2026-03-04T15:30:06Z"), ("revision", 1)):
        assert ident.event_key(KEYS, ident.canonical_acs_item(**{**hold, name: value})) != original, name


def test_the_same_safe_fields_always_give_the_same_key_across_processes_and_instances():
    again = ident.derive_subkeys(bytes(range(32)))
    one = ident.event_key(KEYS, ident.canonical_checkin(event_time=WHEN_TEXT, item_key=ITEM, destination="main", bin="1"))
    two = ident.event_key(again, ident.canonical_checkin(event_time=WHEN_TEXT, item_key=ITEM, destination="main", bin="1"))
    assert one == two


# --- the event_key cannot depend on anything the payload may not carry -------------------------------------------------------------

def test_canonical_builders_accept_only_v2_safe_fields():
    import inspect
    allowed = {
        "canonical_checkin": {"event_time", "item_key", "destination", "bin"},
        "canonical_reject": {"event_time", "item_key", "error_class"},
        "canonical_acs_item": {"event_time", "item_key", "state", "destination", "is_ill", "is_branch_services", "is_collection_services",
                               "revision"},
    }
    for name, fields in allowed.items():
        assert set(inspect.signature(getattr(ident, name)).parameters) == fields, name
    for name in ("build_checkin", "build_reject", "build_acs_item"):
        params = set(inspect.signature(getattr(ident, name)).parameters) - {"keys"}
        assert not params & {"raw", "line", "barcode", "patron", "patron_id", "name", "title", "call_number", "message", "text"}, name


def test_the_canonical_form_of_a_checkin_contains_no_barcode_only_its_hmac():
    barcode = "31234000123456"
    canonical = ident.canonical_checkin(event_time=WHEN_TEXT, item_key=ident.item_key(KEYS, barcode), destination="main", bin="1")
    assert barcode.encode() not in canonical


# --- no bare SHA-256, anywhere in the v2 collector ------------------------------------------------------------------------------------

V2_FILES = sorted(COLLECTOR.glob("v2_*.py"))


def test_there_are_v2_modules_to_scan():
    assert {p.name for p in V2_FILES} >= {"v2_identity.py", "v2_transform.py", "v2_uploader.py", "v2_run.py", "v2_keys.py"}


DIGESTS = {"sha1", "sha224", "sha256", "sha384", "sha512", "sha3_224", "sha3_256", "sha3_384", "sha3_512", "md5", "blake2b", "blake2s"}


@pytest.mark.parametrize("path", V2_FILES, ids=lambda p: p.name)
def test_no_v2_module_hashes_data_with_a_bare_digest(path):
    """`hashlib.sha256` may appear ONLY as the digestmod argument of hmac.new (an Attribute passed as a constructor), never CALLED."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {alias.asname or alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module == "hashlib"
                for alias in node.names}
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "hashlib":
            offenders.append(f"{path.name}:{node.lineno}:hashlib.{func.attr}")
        elif isinstance(func, ast.Name) and func.id in imported | DIGESTS:
            offenders.append(f"{path.name}:{node.lineno}:{func.id}")
    assert offenders == []


def test_hmac_is_the_only_digest_use_and_uses_the_constructor_reference():
    source = (COLLECTOR / "v2_identity.py").read_text(encoding="utf-8")
    assert "hmac.new(key, message, hashlib.sha256)" in source
    assert not re.search(r"hashlib\.sha256\s*\(", source)
    for path in V2_FILES:
        assert not re.search(r"hashlib\.(sha\w*|md5|blake\w*)\s*\(", path.read_text(encoding="utf-8")), path.name


# --- the ruleset_id is provenance, not identity ----------------------------------------------------------------------------------------

OTHER_RULESET = "1b2c3d4e-5f6a-4b7c-8d8e-9f0a1b2c3d4e"
HOLD_FIELDS = {"event_time": WHEN, "item_key": ITEM, "state": "hold", "destination": "main", "is_ill": False, "is_branch_services": True,
               "is_collection_services": False}


def test_the_same_event_under_a_different_ruleset_id_has_the_same_event_key_but_carries_its_own_ruleset_id():
    one = ident.build_acs_item(KEYS, ruleset_id=RULESET, **HOLD_FIELDS)
    two = ident.build_acs_item(KEYS, ruleset_id=OTHER_RULESET, **HOLD_FIELDS)
    none = ident.build_acs_item(KEYS, ruleset_id=None, **HOLD_FIELDS)
    assert one.event_key == two.event_key == none.event_key == CANONICAL_CASES[5][3]
    assert one.payload()["ruleset_id"] == RULESET and two.payload()["ruleset_id"] == OTHER_RULESET   # provenance is kept in the payload
    assert "ruleset_id" not in none.payload()
    assert one.payload() != two.payload()


def test_the_canonical_form_of_a_hold_never_mentions_a_ruleset():
    canonical = ident.canonical_acs_item(**{k: (ident.format_time(v) if k == "event_time" else v) for k, v in HOLD_FIELDS.items()})
    assert b"ruleset" not in canonical and RULESET.encode() not in canonical
    with pytest.raises(TypeError):
        ident.canonical_acs_item(ruleset_id=RULESET, **{k: (ident.format_time(v) if k == "event_time" else v) for k, v in HOLD_FIELDS.items()})


@pytest.mark.parametrize("change", [{"destination": "westside"}, {"is_ill": True}, {"is_branch_services": False},
                                    {"is_collection_services": True}, {"state": "non_hold_101"}, {"item_key": ident.item_key(KEYS, "OTHER")}])
def test_a_changed_classification_result_changes_the_event_key(change):
    base = ident.build_acs_item(KEYS, ruleset_id=RULESET, **HOLD_FIELDS)
    fields = {**HOLD_FIELDS, **change}
    if change.get("state") == "non_hold_101":
        fields = {"event_time": WHEN, "item_key": ITEM, "state": "non_hold_101"}
    other = ident.build_acs_item(KEYS, ruleset_id=RULESET if fields["state"] == "hold" else None, **fields)  # a non-hold has no ruleset
    assert other.event_key != base.event_key


def test_a_changed_classification_is_a_different_event_even_under_the_same_ruleset_id():
    a = ident.build_acs_item(KEYS, ruleset_id=RULESET, **HOLD_FIELDS)
    b = ident.build_acs_item(KEYS, ruleset_id=RULESET, **{**HOLD_FIELDS, "is_ill": True})
    assert a.event_key != b.event_key and a.payload()["ruleset_id"] == b.payload()["ruleset_id"]


# --- the correction revision -------------------------------------------------------------------------------------------------------------

def test_revision_zero_is_the_plain_form_and_only_a_positive_revision_is_added():
    plain = ident.build_acs_item(KEYS, **HOLD_FIELDS)
    zero = ident.build_acs_item(KEYS, revision=0, **HOLD_FIELDS)
    one = ident.build_acs_item(KEYS, revision=1, **HOLD_FIELDS)
    two = ident.build_acs_item(KEYS, revision=2, **HOLD_FIELDS)
    assert plain.event_key == zero.event_key and len({plain.event_key, one.event_key, two.event_key}) == 3


def test_a_classification_that_returns_to_an_earlier_value_is_still_a_new_row():
    """A -> B -> A: the third event has the first one's flags but a higher revision, so its key differs and the server stores it (a plain
    resend of the first key would be a duplicate and the item would stay stuck on B)."""
    a = ident.build_acs_item(KEYS, revision=0, **HOLD_FIELDS)
    b = ident.build_acs_item(KEYS, revision=1, **{**HOLD_FIELDS, "is_ill": True})
    a_again = ident.build_acs_item(KEYS, revision=2, **HOLD_FIELDS)
    assert len({a.event_key, b.event_key, a_again.event_key}) == 3


def test_a_revision_never_applies_to_a_non_hold():
    non_hold = {"event_time": WHEN, "item_key": ITEM, "state": "non_hold_101"}
    assert ident.build_acs_item(KEYS, revision=5, **non_hold).event_key == ident.build_acs_item(KEYS, **non_hold).event_key
