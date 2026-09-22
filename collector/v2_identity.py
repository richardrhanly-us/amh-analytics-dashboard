"""Contract v2: keyed identity -- domain-separated HMAC-SHA256 (docs/collector-v2.md).

EVERY identifier the collector derives is an HMAC-SHA256 under a key derived from the local master secret. There is no bare SHA-256
of an identifier anywhere in the v2 collector (a static test enforces it): a plain digest of a 14-digit barcode or card number can
be reversed by enumeration; a keyed one cannot without the secret, which never leaves this machine.

DOMAIN SEPARATION. One master secret, five purpose keys, each derived with HKDF-Expand (RFC 5869; the master is already uniform
random, so it is the PRK):

    K_event    the cloud event_key
    K_item     the cloud item_key            (HMAC of a raw item barcode)
    K_patron   the LOCAL patron-card identifier (never uploaded; the patron cache and the guard)
    K_name     the LOCAL patron-name identifier (never uploaded; local rule matching)
    K_ruleset  the LOCAL ruleset fingerprint  (never uploaded)

The same input under two purposes gives two unrelated values, so an item barcode and a patron card that happen to be the same string
cannot be linked, and a value derived for the cloud can never be looked up in a local cache.

CANONICAL EVENT FORMS. An event_key is the HMAC of a deterministic, versioned text built ONLY from the fields v2 allows in the payload.
It never includes a raw line, a barcode, a patron identifier or name, a title, a call number, raw SIP2 or reject text, so changing any of
those cannot change an event_key (a test proves it). The forms are `field=value` lines under a `kind/version` header, in a fixed order,
with `~` for an absent optional value (never a legal value of any field).

WHAT IS IN A HOLD'S IDENTITY: its `state`, `event_time`, `item_key`, `destination` and the three classification flags -- the classification
RESULT, so a corrected classification is a new event, never a conflict. The `ruleset_id` is provenance only: it is sent in the payload but is
NOT part of the event_key, so re-sending the same classification under a newer ruleset keeps the same identity. A CORRECTION of an earlier hold
(a patron profile that arrived later) carries a local `revision` counter greater than 0, which is added to the form ONLY then, so a
classification that returns to an earlier value (A -> B -> A) is still a new row and not a duplicate of the first. The counter is a plain
integer kept in the local hold ledger; it is not derived from any raw or personal data.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field

from .v2_events import AcsItemV2, CheckinV2, RejectV2, format_time

ALGORITHM_ID = "hmac-sha256-v1"
SUBKEY_INFO_PREFIX = b"sortview/v2/" + ALGORITHM_ID.encode("ascii") + b"/"
PURPOSES = ("event", "item", "patron", "name", "ruleset")
CANONICAL_VERSION = 1
ABSENT = "~"  # not in the alphabet of any hex key, UUID, slug or timestamp


def hmac_sha256(key: bytes, message: bytes) -> bytes:
    """HMAC-SHA256. `hashlib.sha256` is passed as a constructor, never called on data."""
    return hmac.new(key, message, hashlib.sha256).digest()


def hkdf_expand(prk: bytes, info: bytes, length: int = 32) -> bytes:
    """HKDF-Expand (RFC 5869, section 2.3) with HMAC-SHA256."""
    if length > 255 * 32 or length < 1:
        raise ValueError("invalid HKDF length")
    blocks: list[bytes] = []
    previous, counter = b"", 1
    while sum(len(block) for block in blocks) < length:
        previous = hmac_sha256(prk, previous + info + bytes([counter]))
        blocks.append(previous)
        counter += 1
    return b"".join(blocks)[:length]


@dataclass(frozen=True)
class SubKeys:
    """The purpose keys. `repr` hides them; there is no way to print one by accident."""

    event: bytes = field(repr=False)
    item: bytes = field(repr=False)
    patron: bytes = field(repr=False)
    name: bytes = field(repr=False)
    ruleset: bytes = field(repr=False)

    def __repr__(self) -> str:
        return "SubKeys(<hidden>)"

    __str__ = __repr__


def derive_subkeys(master: bytes) -> SubKeys:
    if not isinstance(master, bytes) or len(master) != 32:
        raise ValueError("the master secret must be exactly 32 bytes")
    return SubKeys(**{purpose: hkdf_expand(master, SUBKEY_INFO_PREFIX + purpose.encode("ascii"), 32) for purpose in PURPOSES})


def _ascii(text: str) -> bytes:
    return text.encode("utf-8")


def item_key(keys: SubKeys, barcode: str) -> str:
    """The cloud item_key: HMAC(K_item, the barcode as read, stripped). Hex, 64 characters."""
    return hmac_sha256(keys.item, _ascii(barcode.strip())).hex()


def patron_id_hmac(keys: SubKeys, patron_identifier: str) -> bytes:
    """LOCAL ONLY: HMAC(K_patron, the patron identifier, stripped). 32 bytes; never uploaded."""
    return hmac_sha256(keys.patron, _ascii(patron_identifier.strip()))


def name_hmac(keys: SubKeys, patron_name: str) -> bytes:
    """LOCAL ONLY: HMAC(K_name, the name stripped and upper-cased) -- the form the classifier compares. Never uploaded."""
    return hmac_sha256(keys.name, _ascii(patron_name.strip().upper()))


def ruleset_fingerprint(keys: SubKeys, canonical_rules: bytes) -> bytes:
    """LOCAL ONLY: a keyed fingerprint of the rules, used to notice that they changed. The UPLOADED ruleset_id is a random UUID and
    has no relationship to this value."""
    return hmac_sha256(keys.ruleset, canonical_rules)


# --- canonical forms (field names and order are part of the format; changing them is a new CANONICAL_VERSION) ----------------------

def _canonical(kind: str, fields: list[tuple[str, str]]) -> bytes:
    for _name, value in fields:
        if "\n" in value or "\r" in value:
            raise ValueError("canonical field contains a line break")
    header = f"sortview/v2/event/{kind}/{CANONICAL_VERSION}"
    return "\n".join([header, *(f"{name}={value}" for name, value in fields)]).encode("ascii")


def _flag(value: bool | None) -> str:
    return ABSENT if value is None else ("1" if value else "0")


def canonical_checkin(*, event_time: str, item_key: str | None, destination: str, bin: str) -> bytes:
    return _canonical("checkin", [("event_time", event_time), ("item_key", item_key or ABSENT),
                                  ("destination", destination), ("bin", bin)])


def canonical_reject(*, event_time: str, item_key: str | None, error_class: str) -> bytes:
    return _canonical("reject", [("event_time", event_time), ("item_key", item_key or ABSENT), ("error_class", error_class)])


def canonical_acs_item(*, event_time: str, item_key: str, state: str, destination: str | None = None,
                       is_ill: bool | None = None, is_branch_services: bool | None = None,
                       is_collection_services: bool | None = None, revision: int = 0) -> bytes:
    """A non-hold has only the first three fields; a hold adds its destination and three flags (NOT its ruleset_id: provenance is not identity).
    `revision` (a correction's local counter) is added only when it is greater than 0."""
    fields = [("event_time", event_time), ("item_key", item_key), ("state", state)]
    if state == "hold":
        fields += [("destination", destination or ABSENT), ("is_ill", _flag(is_ill)), ("is_branch_services", _flag(is_branch_services)),
                   ("is_collection_services", _flag(is_collection_services))]
        if revision > 0:
            fields.append(("revision", str(int(revision))))
    return _canonical("acs_item", fields)


def event_key(keys: SubKeys, canonical: bytes) -> str:
    """The cloud event_key: HMAC(K_event, the canonical form). Hex, 64 characters."""
    return hmac_sha256(keys.event, canonical).hex()


# --- building events (the one place a key is computed) ---------------------------------------------------------------------------

def build_checkin(keys: SubKeys, *, event_time, item_key: str | None, destination: str, bin: str) -> CheckinV2:
    canonical = canonical_checkin(event_time=format_time(event_time), item_key=item_key, destination=destination, bin=bin)
    return CheckinV2(event_key(keys, canonical), event_time, item_key, destination, bin)


def build_reject(keys: SubKeys, *, event_time, item_key: str | None, error_class: str) -> RejectV2:
    canonical = canonical_reject(event_time=format_time(event_time), item_key=item_key, error_class=error_class)
    return RejectV2(event_key(keys, canonical), event_time, error_class, item_key)


def build_acs_item(keys: SubKeys, *, event_time, item_key: str, state: str, destination: str | None = None,
                   is_ill: bool | None = None, is_branch_services: bool | None = None,
                   is_collection_services: bool | None = None, ruleset_id: str | None = None, revision: int = 0) -> AcsItemV2:
    """`ruleset_id` goes into the payload as provenance; it does not influence the event_key. `revision` > 0 marks a correction."""
    canonical = canonical_acs_item(event_time=format_time(event_time), item_key=item_key, state=state, destination=destination,
                                   is_ill=is_ill, is_branch_services=is_branch_services,
                                   is_collection_services=is_collection_services, revision=revision)
    return AcsItemV2(event_key(keys, canonical), event_time, item_key, state, destination, is_ill, is_branch_services,
                     is_collection_services, ruleset_id)
