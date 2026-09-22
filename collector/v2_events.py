"""Contract v2: the privacy-safe event types (docs/collector-v2.md).

These frozen dataclasses are the ONLY thing that may leave the transformation layer (collector/v2_transform.py). The uploader,
the quarantine, the state files and the heartbeat see nothing else, so raw Tech Logic data has no type to travel in.

Each event validates itself on construction against the SAME formats the server enforces (the patterns below are compared with
src/services/ingest_v2_models.py by a test): a fixed-format HMAC key, a lower-case slug, a closed enum, an offset-aware UTC time.
A failed check raises `UnsafeEventError` with a FIXED message that never contains the offending value.

`payload()` builds the request dict field by field, by name. Nothing is dumped generically, and a non-hold ACS item has no field
for a destination or a flag, so none can be sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# --- the server's formats (mirrored; tests compare them with the server models) -----------------------------------------------

UUID4_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
HMAC_HEX_PATTERN = r"^[0-9a-f]{64}$"
DESTINATION_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
BIN_PATTERN = r"^[a-z0-9][a-z0-9_]{0,15}$"

ERROR_CLASSES = (
    "item_not_found", "ils_acs_failure", "rfid_collision", "configuration_error",
    "routing_error", "communication_error", "other", "unknown",
)
ACS_ITEM_STATES = ("hold", "non_hold_101", "other_code10")
NON_HOLD_STATES = ("non_hold_101", "other_code10")

HEALTH_STATUSES = ("healthy", "degraded", "error")
LAST_ERROR_CLASSES = (
    "retryable_infra", "auth_failure", "permanent_rejection", "source_unavailable", "configuration_error", "other",
)

# The three payload lists, in the order the server names them.
KIND_CHECKINS, KIND_REJECTS, KIND_ACS_ITEMS = "checkins", "rejects", "acs_items"
KINDS = (KIND_CHECKINS, KIND_REJECTS, KIND_ACS_ITEMS)

_UUID4 = re.compile(UUID4_PATTERN)
_HMAC_HEX = re.compile(HMAC_HEX_PATTERN)
_DESTINATION = re.compile(DESTINATION_PATTERN)
_BIN = re.compile(BIN_PATTERN)


class UnsafeEventError(ValueError):
    """An event field is not in its privacy-safe format. The message names the field, never the value."""


def _check(name: str, ok: bool) -> None:
    if not ok:
        raise UnsafeEventError(f"unsafe or malformed field: {name}")


def _hex_key(name: str, value: object) -> None:
    _check(name, isinstance(value, str) and _HMAC_HEX.fullmatch(value) is not None)


def _utc_time(value: object) -> None:
    _check("event_time", isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
           and value.utcoffset().total_seconds() == 0)  # type: ignore[union-attr]


def format_time(instant: datetime) -> str:
    """The wire (and canonical) spelling: `YYYY-MM-DDTHH:MM:SSZ`, whole seconds, UTC."""
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class CheckinV2:
    event_key: str
    event_time: datetime
    item_key: str | None
    destination: str
    bin: str

    kind = KIND_CHECKINS

    def __post_init__(self) -> None:
        _hex_key("event_key", self.event_key)
        _utc_time(self.event_time)
        if self.item_key is not None:
            _hex_key("item_key", self.item_key)
        _check("destination", isinstance(self.destination, str) and _DESTINATION.fullmatch(self.destination) is not None)
        _check("bin", isinstance(self.bin, str) and _BIN.fullmatch(self.bin) is not None)

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"event_key": self.event_key, "event_time": format_time(self.event_time),
                                "destination": self.destination, "bin": self.bin}
        if self.item_key is not None:
            body["item_key"] = self.item_key
        return body


@dataclass(frozen=True)
class RejectV2:
    event_key: str
    event_time: datetime
    error_class: str
    item_key: str | None

    kind = KIND_REJECTS

    def __post_init__(self) -> None:
        _hex_key("event_key", self.event_key)
        _utc_time(self.event_time)
        _check("error_class", self.error_class in ERROR_CLASSES)
        if self.item_key is not None:
            _hex_key("item_key", self.item_key)

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"event_key": self.event_key, "event_time": format_time(self.event_time),
                                "error_class": self.error_class}
        if self.item_key is not None:
            body["item_key"] = self.item_key
        return body


@dataclass(frozen=True)
class AcsItemV2:
    """One ACS item record (message code 10) by its derived `state`. A hold carries its derived classification; a non-hold carries
    only what retraction needs, and its other fields are None -- never a placeholder."""

    event_key: str
    event_time: datetime
    item_key: str
    state: str
    destination: str | None = None
    is_ill: bool | None = None
    is_branch_services: bool | None = None
    is_collection_services: bool | None = None
    ruleset_id: str | None = None

    kind = KIND_ACS_ITEMS

    def __post_init__(self) -> None:
        _hex_key("event_key", self.event_key)
        _utc_time(self.event_time)
        _hex_key("item_key", self.item_key)
        _check("state", self.state in ACS_ITEM_STATES)
        hold_fields = (self.destination, self.is_ill, self.is_branch_services, self.is_collection_services)
        if self.state == "hold":
            _check("destination", isinstance(self.destination, str) and _DESTINATION.fullmatch(self.destination) is not None)
            _check("flags", all(isinstance(flag, bool) for flag in hold_fields[1:]))
            if self.ruleset_id is not None:
                _check("ruleset_id", isinstance(self.ruleset_id, str) and _UUID4.fullmatch(self.ruleset_id) is not None)
        else:
            _check("non_hold_shape", all(value is None for value in hold_fields) and self.ruleset_id is None)

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"state": self.state, "event_key": self.event_key,
                                "event_time": format_time(self.event_time), "item_key": self.item_key}
        if self.state == "hold":
            body.update(destination=self.destination, is_ill=self.is_ill, is_branch_services=self.is_branch_services,
                        is_collection_services=self.is_collection_services)
            if self.ruleset_id is not None:
                body["ruleset_id"] = self.ruleset_id
        return body


SafeEvent = CheckinV2 | RejectV2 | AcsItemV2


@dataclass(frozen=True)
class Cursor:
    """A position in one source file: its (st_dev, st_ino) identity and a byte offset. Integers only -- safe to persist and pass anywhere."""

    identity: tuple[int, int] | None
    offset: int


def validate_key_id(value: object) -> str:
    """A server-issued key_id: a lower-case UUIDv4. The error never echoes the value."""
    _check("key_id", isinstance(value, str) and _UUID4.fullmatch(value) is not None)
    return str(value)
