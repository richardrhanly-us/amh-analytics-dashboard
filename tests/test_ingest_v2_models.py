"""Privacy Contract v2 request models (src/services/ingest_v2_models.py, docs/contract-v2-design.md).

What these prove: every v2 model is a strict, typed allowlist with no way to carry a tenant id, a patron/card value, a raw
barcode, a title, raw SIP2 or free text; that the formats and bounds are exactly the approved ones; and that a
validation failure never contains a submitted value.

Every value is SYNTHETIC. Timestamps are built from the real clock (the models reject an instant a day ahead of now).
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from datetime import UTC, datetime, timedelta
from typing import get_args

import pytest
from pydantic import ValidationError

from src.services import ingest_v2_models as models
from src.services import ingest_v2_service as service
from src.services.ingest_v2_models import (
    AcsHoldEvent,
    CheckinEvent,
    RejectEvent,
    StatusV2Request,
    UploadV2Request,
)

KEY_ID = "3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e"
RULESET_ID = "0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d"


def hmac_like(n: int) -> str:
    return hashlib.sha256(f"synthetic-{n}".encode()).hexdigest()


def when(**delta) -> str:
    return (datetime.now(UTC) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def checkin(n=1, **overrides):
    event = {"event_key": hmac_like(n), "event_time": when(minutes=5), "item_key": hmac_like(n + 1000),
             "destination": "westside", "bin": "3"}
    event.update(overrides)
    return event


def reject(n=1, **overrides):
    event = {"event_key": hmac_like(n), "event_time": when(minutes=5), "error_class": "item_not_found"}
    event.update(overrides)
    return event


def acs_hold(n=1, **overrides):
    event = {"event_key": hmac_like(n), "event_time": when(minutes=5), "item_key": hmac_like(n + 1000),
             "destination": "library_express", "is_ill": False, "is_branch_services": False,
             "is_collection_services": True}
    event.update(overrides)
    return event


def upload(**overrides):
    body = {"contract_version": 2, "key_id": KEY_ID, "checkins": [checkin()]}
    body.update(overrides)
    return body


def status(**overrides):
    body = {"contract_version": 2, "key_id": KEY_ID, "status": "healthy"}
    body.update(overrides)
    return body


def rejected(model, payload) -> list[str]:
    """The error types of a failed validation, and nothing else."""
    with pytest.raises(ValidationError) as caught:
        model.model_validate(payload)
    return [error["type"] for error in caught.value.errors()]


# --- valid shapes ----------------------------------------------------------------------------------------------------

def test_the_three_event_shapes_and_the_envelope_are_accepted():
    request = UploadV2Request.model_validate(upload(checkins=[checkin()], rejects=[reject(2)], acs_holds=[acs_hold(3)]))

    assert (len(request.checkins), len(request.rejects), len(request.acs_holds)) == (1, 1, 1)
    assert request.contract_version == 2 and request.key_id == KEY_ID


def test_optional_fields_may_be_omitted_and_default_to_none():
    checkin_event = CheckinEvent.model_validate({k: v for k, v in checkin().items() if k != "item_key"})
    reject_event = RejectEvent.model_validate(reject())
    acs_event = AcsHoldEvent.model_validate(acs_hold())

    assert checkin_event.item_key is None and reject_event.item_key is None and acs_event.ruleset_id is None


def test_an_acs_hold_may_carry_an_opaque_random_ruleset_id():
    assert AcsHoldEvent.model_validate(acs_hold(ruleset_id=RULESET_ID)).ruleset_id == RULESET_ID


def test_the_lists_default_to_empty_so_a_request_may_carry_one_kind():
    request = UploadV2Request.model_validate({"contract_version": 2, "key_id": KEY_ID, "rejects": [reject()]})

    assert request.checkins == [] and request.acs_holds == []


# --- extra="forbid": nothing outside the allowlist is ever accepted --------------------------------------------------

PROHIBITED = ["customer_id", "branch_id", "patron_id", "patron_card", "patron_name", "card_number", "barcode", "title",
              "raw_message", "message", "error_message", "source_event_id", "source_file", "last_error", "note",
              "collection_code", "call_number", "shelf_code", "flag_1", "is_problem", "extra"]


@pytest.mark.parametrize("field", PROHIBITED)
def test_every_prohibited_field_is_rejected_on_the_envelope(field):
    assert rejected(UploadV2Request, upload(**{field: "CANARY-VALUE"})) == ["extra_forbidden"]


@pytest.mark.parametrize("field", PROHIBITED)
@pytest.mark.parametrize("builder,list_name", [(checkin, "checkins"), (reject, "rejects"), (acs_hold, "acs_holds")])
def test_every_prohibited_field_is_rejected_inside_every_event(field, builder, list_name):
    lists = {"checkins": [], "rejects": [], "acs_holds": [], list_name: [builder(**{field: "CANARY-VALUE"})]}
    assert rejected(UploadV2Request, upload(**lists)) == ["extra_forbidden"]


@pytest.mark.parametrize("field", ["customer_id", "branch_id", "installation_id", "collector_version", "last_error",
                                   "message", "destination_breakdown", "checkins_rows", "hostname"])
def test_the_heartbeat_rejects_every_field_outside_its_allowlist(field):
    value = {} if field == "destination_breakdown" else "CANARY-VALUE"
    assert rejected(StatusV2Request, status(**{field: value})) == ["extra_forbidden"]


def _all_v2_models():
    found, todo = [], [models._V2Model]
    while todo:
        cls = todo.pop()
        found.append(cls)
        todo.extend(cls.__subclasses__())
    return found


def test_every_v2_model_is_strict_frozen_and_forbids_extra_fields():
    found = _all_v2_models()

    assert {m.__name__ for m in found} == {"_V2Model", "CheckinEvent", "RejectEvent", "AcsHoldEvent", "UploadV2Request",
                                          "StatusV2Request"}
    for model in found:
        assert model.model_config.get("extra") == "forbid", model
        assert model.model_config.get("strict") is True, model
        assert model.model_config.get("frozen") is True, model


def test_no_v2_model_declares_a_tenant_or_a_free_text_field():
    forbidden = {"customer_id", "branch_id", "message", "last_error", "error_message", "title", "barcode", "patron_id",
                 "raw_message", "source_event_id", "source_file"}
    for model in _all_v2_models():
        assert forbidden.isdisjoint(model.model_fields), model


def test_the_persisted_columns_of_each_event_are_exactly_its_model_fields():
    # The explicit insert-column tuples and the models cannot drift apart: a new model field must be added to the tuple
    # (and the migration) on purpose, or this fails.
    assert set(CheckinEvent.model_fields) == set(service.CHECKIN_COLUMNS)
    assert set(RejectEvent.model_fields) == set(service.REJECT_COLUMNS)
    assert set(AcsHoldEvent.model_fields) == set(service.ACS_HOLD_COLUMNS)


def test_the_row_builders_produce_exactly_the_declared_columns():
    assert set(service.checkin_row(CheckinEvent.model_validate(checkin()))) == set(service.CHECKIN_COLUMNS)
    assert set(service.reject_row(RejectEvent.model_validate(reject()))) == set(service.REJECT_COLUMNS)
    assert set(service.acs_hold_row(AcsHoldEvent.model_validate(acs_hold()))) == set(service.ACS_HOLD_COLUMNS)


def test_the_persistence_code_never_calls_model_dump():
    tree = ast.parse(inspect.getsource(service))
    methods = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}  # `.model_dump`, `.dict`, ... (not the builtin dict)

    assert methods.isdisjoint({"model_dump", "model_dump_json", "dict", "json", "__dict__", "model_extra", "model_fields_set"})


# --- strict types ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("version", ["2", 2.0, 2.5, True, 1, 3, None, "v2", [2], {"v": 2}])
def test_contract_version_must_be_the_integer_two(version):
    assert rejected(UploadV2Request, upload(contract_version=version))


def test_contract_version_is_required():
    body = upload()
    del body["contract_version"]
    assert rejected(UploadV2Request, body) == ["missing"]


@pytest.mark.parametrize("value", ["true", "false", 1, 0, "yes", None, "True"])
def test_the_acs_flags_are_strict_booleans(value):
    for flag in ("is_ill", "is_branch_services", "is_collection_services"):
        assert rejected(AcsHoldEvent, acs_hold(**{flag: value}))


def test_the_acs_flags_are_required_and_independent():
    for flag in ("is_ill", "is_branch_services", "is_collection_services"):
        event = acs_hold()
        del event[flag]
        assert rejected(AcsHoldEvent, event) == ["missing"]
    both = AcsHoldEvent.model_validate(acs_hold(is_ill=True, is_collection_services=True))
    assert both.is_ill and both.is_collection_services


# --- timestamps: offset-aware ISO-8601 only ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "2026-09-21T10:00:00",            # naive
    "2026-09-21T10:00:00.123456",     # naive with a fraction
    "2026-09-21",                     # a date
    "2026-09-21 10:00:00+00:00",      # a space, not the ISO 'T'
    "20260921T100000Z",               # basic format
    1758448800, 1758448800.5,         # epoch numbers
    True, None, "", "now", "yesterday",
    "2026-09-21T10:00:00+0000",       # offset without a colon
    "2026-09-21T10:00:00Z\n",         # trailing newline
    "2026-09-21T25:00:00Z", "2026-13-01T10:00:00Z", "2026-09-21T10:00:00+25:00",
])
def test_a_timestamp_that_is_not_iso_8601_with_an_offset_is_rejected(value):
    for model, build in ((CheckinEvent, checkin), (RejectEvent, reject), (AcsHoldEvent, acs_hold)):
        assert rejected(model, build(event_time=value))[0] in {"timestamp_format", "timestamp_range", "missing"}


def test_a_datetime_object_is_rejected_too_only_the_string_form_is_the_contract():
    assert rejected(CheckinEvent, checkin(event_time=datetime.now(UTC)))[0] == "timestamp_format"
    assert rejected(CheckinEvent, checkin(event_time=datetime.now(UTC).replace(tzinfo=None)))[0] == "timestamp_format"  # naive object


@pytest.mark.parametrize("suffix", ["Z", "+00:00", "-05:00", "+05:30"])
def test_an_offset_aware_timestamp_is_accepted_and_normalized_to_utc(suffix):
    local = (datetime.now(UTC) - timedelta(hours=1)).replace(microsecond=0)
    offset = timedelta(0) if suffix == "Z" else timedelta(hours=int(suffix[:3]), minutes=int(suffix[0] + suffix[4:]))
    text = (local + offset).strftime("%Y-%m-%dT%H:%M:%S") + suffix

    event = CheckinEvent.model_validate(checkin(event_time=text))

    assert event.event_time.tzinfo is not None and event.event_time.utcoffset() == timedelta(0)
    assert event.event_time == local


def test_a_fractional_second_is_accepted():
    assert CheckinEvent.model_validate(checkin(event_time=when(minutes=1).replace("Z", ".250000Z"))).event_time.microsecond == 250000


def test_an_instant_outside_the_plausible_window_is_rejected():
    assert rejected(CheckinEvent, checkin(event_time="1999-12-31T23:59:59Z")) == ["timestamp_range"]
    future = (datetime.now(UTC) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert rejected(CheckinEvent, checkin(event_time=future)) == ["timestamp_range"]
    just_ahead = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert CheckinEvent.model_validate(checkin(event_time=just_ahead))  # clock skew is tolerated


# --- identifiers -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "", "not-a-uuid", KEY_ID.upper(), KEY_ID.replace("-", ""), KEY_ID + "\n", "0" * 36,
    "3f2b8c1e-4d5a-1b6c-8d7e-9f0a1b2c3d4e",   # version 1, not 4
    "3f2b8c1e-4d5a-4b6c-cd7e-9f0a1b2c3d4e",   # a variant a UUIDv4 does not have
    "key-2026-Q3", "hmac-sha256-v1", hmac_like(1),
    None, 12345,
])
def test_key_id_must_be_a_lowercase_uuidv4(value):
    assert rejected(UploadV2Request, upload(key_id=value))


@pytest.mark.parametrize("value", [
    "", "abc", hmac_like(1).upper(), hmac_like(1)[:-1], hmac_like(1) + "0", hmac_like(1) + "\n", "g" * 64,
    "CANARY-PATRON-CARD-2300000000003", "B-1", None, 5,
])
def test_event_key_and_item_key_must_be_64_lowercase_hex(value):
    assert rejected(CheckinEvent, checkin(event_key=value))
    if value is not None:  # item_key may be null on a checkin; anything else must be 64 lowercase hex
        assert rejected(CheckinEvent, checkin(item_key=value))
    assert rejected(RejectEvent, reject(event_key=value))
    assert rejected(AcsHoldEvent, acs_hold(event_key=value))
    assert rejected(AcsHoldEvent, acs_hold(item_key=value))  # required on a hold


def test_item_key_may_be_null_on_a_checkin_and_a_reject_but_not_on_a_hold():
    assert CheckinEvent.model_validate(checkin(item_key=None)).item_key is None
    assert RejectEvent.model_validate(reject(item_key=None)).item_key is None
    assert rejected(AcsHoldEvent, acs_hold(item_key=None))


@pytest.mark.parametrize("value", [RULESET_ID.upper(), "ruleset-2026-q3", "v3", hmac_like(9), "", RULESET_ID + "\n",
                                   "3f2b8c1e-4d5a-1b6c-8d7e-9f0a1b2c3d4e", 7])
def test_ruleset_id_must_be_an_opaque_uuid_never_readable_text_or_a_hash(value):
    assert rejected(AcsHoldEvent, acs_hold(ruleset_id=value))


# --- destination and bin: normalized slugs, never raw AMH labels, never empty ------------------------------------------

@pytest.mark.parametrize("value", ["main", "westside", "library_express", "no_agency_destination", "unknown", "a", "d2",
                                   "x" * 32])
def test_a_normalized_destination_slug_is_accepted(value):
    assert CheckinEvent.model_validate(checkin(destination=value)).destination == value
    assert AcsHoldEvent.model_validate(acs_hold(destination=value)).destination == value


@pytest.mark.parametrize("value", [
    "Westside", "Library Express", "Main", "MAIN", "No Agency Destination",         # raw / display labels
    "DA(AH) TS(AH)-CATALOGING", "(AH) TS(AH)-CATALOGING", "COURTNEY (ST)MEISSNER",  # staff and department names
    "library-express", "library.express", "west side", "west/side", "west|side", "1main", "_main",
    "", " ", "main ", " main", "main\n", "x" * 33, "westé", "вест", None, 4, ["main"],
])
def test_a_raw_label_an_empty_value_or_anything_but_a_lowercase_slug_is_rejected(value):
    assert rejected(CheckinEvent, checkin(destination=value))
    assert rejected(AcsHoldEvent, acs_hold(destination=value))


def test_destination_and_bin_are_required_on_a_checkin():
    for field in ("destination", "bin"):
        event = checkin()
        del event[field]
        assert rejected(CheckinEvent, event) == ["missing"]


@pytest.mark.parametrize("value", ["0", "1", "12", "exception", "unknown", "b_2", "x" * 16])
def test_a_normalized_bin_code_is_accepted(value):
    assert CheckinEvent.model_validate(checkin(bin=value)).bin == value


@pytest.mark.parametrize("value", ["", " ", "Bin 1", "BIN1", "A", "-1", "_1", "1 ", "x" * 17, "1.5", "1\n", None, 3, "b|2"])
def test_a_bin_that_is_empty_long_or_not_a_lowercase_code_is_rejected(value):
    assert rejected(CheckinEvent, checkin(bin=value))


# --- error_class: a closed enum, no free-text fallback ---------------------------------------------------------------

def test_the_error_class_enum_is_exactly_the_approved_set():
    assert models.ERROR_CLASSES == ("item_not_found", "ils_acs_failure", "rfid_collision", "configuration_error",
                                    "routing_error", "communication_error", "other", "unknown")


@pytest.mark.parametrize("value", models.ERROR_CLASSES)
def test_every_approved_error_class_is_accepted(value):
    assert RejectEvent.model_validate(reject(error_class=value)).error_class == value


@pytest.mark.parametrize("value", [
    "Item Not Found", "ILS / ACS Failure", "Other", "item not found", "call_number_config_error", "error", "",
    "Item not found: barcode CANARY-BARCODE-3001", "item_not_found\n", "ITEM_NOT_FOUND", None, 3, {"a": 1},
])
def test_any_other_error_class_including_free_text_is_rejected(value):
    assert rejected(RejectEvent, reject(error_class=value))


def test_a_reject_needs_an_error_class():
    event = reject()
    del event["error_class"]
    assert rejected(RejectEvent, event) == ["missing"]


# --- bounds: at most 1000 events in TOTAL ---------------------------------------------------------------------------

def _batch(checkins, rejects, acs_holds):
    return upload(checkins=[checkin(i) for i in range(checkins)], rejects=[reject(5000 + i) for i in range(rejects)],
                  acs_holds=[acs_hold(9000 + i) for i in range(acs_holds)])


@pytest.mark.parametrize("sizes", [(1000, 0, 0), (0, 1000, 0), (0, 0, 1000), (400, 300, 300), (334, 333, 333)])
def test_exactly_one_thousand_events_in_total_are_accepted(sizes):
    request = UploadV2Request.model_validate(_batch(*sizes))

    assert len(request.checkins) + len(request.rejects) + len(request.acs_holds) == 1000


@pytest.mark.parametrize("sizes", [(334, 333, 334), (500, 500, 1), (1, 1000, 0), (0, 0, 1001)])
def test_more_than_one_thousand_events_in_total_are_rejected(sizes):
    types = rejected(UploadV2Request, _batch(*sizes))

    assert types and set(types) <= {"too_many_events", "too_long"}


def test_a_single_list_over_its_own_bound_is_rejected_with_the_list_error():
    assert rejected(UploadV2Request, _batch(1001, 0, 0)) == ["too_long"]


def test_the_total_limit_is_reported_with_a_custom_error_not_a_message_containing_the_input():
    types = rejected(UploadV2Request, _batch(500, 500, 1))

    assert types == ["too_many_events"]


# --- heartbeat allowlist ---------------------------------------------------------------------------------------------

def test_a_full_heartbeat_is_accepted():
    request = StatusV2Request.model_validate(status(
        status="degraded", last_error_class="retryable_infra", pending_outbox_count=12, quarantined_count=0,
        oldest_pending_event_at=when(hours=3), last_success_at=when(minutes=20), watcher_last_active_at=when(seconds=30)))

    assert request.status == "degraded" and request.pending_outbox_count == 12 and request.quarantined_count == 0
    assert request.last_success_at is not None and request.last_success_at.tzinfo is not None


def test_a_minimal_heartbeat_needs_only_the_envelope_and_a_status():
    request = StatusV2Request.model_validate(status())

    assert request.last_error_class is None and request.pending_outbox_count is None and request.last_success_at is None


@pytest.mark.parametrize("value", ["healthy", "degraded", "error"])
def test_every_approved_status_is_accepted(value):
    assert StatusV2Request.model_validate(status(status=value)).status == value


@pytest.mark.parametrize("value", ["ok", "Healthy", "ERROR", "errors", "failed", "unhealthy", "auth_failure", "retryable_infra",
                                   "everything is fine", "unhealthy: CANARY-DETAIL", "", None, 1, True])
def test_a_free_text_or_unknown_status_is_rejected(value):
    assert rejected(StatusV2Request, status(status=value))


@pytest.mark.parametrize("value", [
    "Traceback (most recent call last): CANARY", "authentication/authorization failure 403: CANARY-BODY",
    "timeout: HTTPSConnectionPool(host='CANARY')", "RuntimeError", "unknown", "", 500, True, {"code": 500},
])
def test_last_error_class_is_a_closed_enum_never_an_exception_or_response_text(value):
    assert rejected(StatusV2Request, status(last_error_class=value))


@pytest.mark.parametrize("value", models.LAST_ERROR_CLASSES)
def test_every_approved_last_error_class_is_accepted(value):
    assert StatusV2Request.model_validate(status(last_error_class=value)).last_error_class == value


@pytest.mark.parametrize("field", ["pending_outbox_count", "quarantined_count"])
@pytest.mark.parametrize("value", [-1, 10_000_001, 1.5, "5", True, [1], {"n": 1}])
def test_the_counters_are_bounded_strict_integers(field, value):
    assert rejected(StatusV2Request, status(**{field: value}))


@pytest.mark.parametrize("field", ["oldest_pending_event_at", "last_success_at", "watcher_last_active_at"])
@pytest.mark.parametrize("value", ["2026-09-21T10:00:00", 1758448800, "yesterday", True])
def test_the_heartbeat_timestamps_must_carry_an_offset_too(field, value):
    assert rejected(StatusV2Request, status(**{field: value}))


def test_there_is_no_open_dictionary_and_no_free_text_string_in_the_heartbeat():
    for name, field in StatusV2Request.model_fields.items():
        annotation = str(field.annotation)
        assert "dict" not in annotation.lower(), name
    # every string field is pattern-constrained (an identifier); an unconstrained `str` would be free text
    free_text = [n for n, f in StatusV2Request.model_fields.items()
                 if f.annotation in (str, str | None) and not any(getattr(m, "pattern", None) for m in f.metadata)]
    assert free_text == []


# --- a failure never contains the submitted value --------------------------------------------------------------------

def test_the_validation_error_types_are_fixed_and_carry_no_submitted_value_in_their_names():
    canary = "CANARY-PATRON-CARD-2300000000003"
    for model, payload in ((UploadV2Request, upload(checkins=[checkin(destination=canary)])),
                           (StatusV2Request, status(status=canary)),
                           (UploadV2Request, upload(**{canary: "x"}))):
        with pytest.raises(ValidationError) as caught:
            model.model_validate(payload)
        # the raw pydantic error does echo its `input` -- that is why the API never forwards it (see the API tests)
        assert all(set(e) >= {"type", "loc", "msg"} for e in caught.value.errors())
        assert all(e["type"].replace("_", "").isalnum() for e in caught.value.errors())


def test_the_models_round_trip_through_json_the_way_a_request_arrives():
    body = json.dumps(upload(rejects=[reject(2)], acs_holds=[acs_hold(3)]))

    request = UploadV2Request.model_validate(json.loads(body))

    assert request.rejects[0].error_class == "item_not_found"


# --- the two heartbeat enums are frozen exactly ------------------------------------------------------------------------

def test_the_overall_status_is_exactly_healthy_degraded_error():
    assert models.HEALTH_STATUSES == ("healthy", "degraded", "error")
    assert get_args(models.HealthStatus) == models.HEALTH_STATUSES  # the tuple and the type the API validates cannot drift


def test_last_error_class_is_exactly_these_six_values():
    assert models.LAST_ERROR_CLASSES == ("retryable_infra", "auth_failure", "permanent_rejection", "source_unavailable",
                                         "configuration_error", "other")
    assert get_args(models.LastErrorClass) == models.LAST_ERROR_CLASSES


def test_the_reject_error_class_enum_and_its_type_agree():
    assert get_args(models.ErrorClass) == models.ERROR_CLASSES


def test_auth_failure_is_an_error_class_never_an_overall_status():
    assert "auth_failure" in models.LAST_ERROR_CLASSES and "auth_failure" not in models.HEALTH_STATUSES
    assert set(models.HEALTH_STATUSES).isdisjoint(models.LAST_ERROR_CLASSES)  # a state and a kind of failure never share a name
    assert rejected(StatusV2Request, status(status="auth_failure"))
    request = StatusV2Request.model_validate(status(status="error", last_error_class="auth_failure"))
    assert (request.status, request.last_error_class) == ("error", "auth_failure")


# --- no field can carry a secret, and nothing that issues a key handles one -------------------------------------------------

SECRET_FIELDS = ["hmac_secret", "hmac_key", "hmac", "secret", "secret_key", "key_material", "signing_key", "private_key", "salt",
                 "seed", "password", "token", "api_key", "algorithm", "key"]


@pytest.mark.parametrize("field", SECRET_FIELDS)
def test_no_request_model_has_a_field_that_could_carry_a_secret(field):
    assert rejected(UploadV2Request, upload(**{field: "CANARY-HMAC-SECRET"})) == ["extra_forbidden"]
    assert rejected(UploadV2Request, upload(checkins=[checkin(**{field: "CANARY-HMAC-SECRET"})])) == ["extra_forbidden"]
    assert rejected(UploadV2Request, upload(checkins=[], rejects=[reject(**{field: "CANARY-HMAC-SECRET"})])) == ["extra_forbidden"]
    assert rejected(UploadV2Request, upload(checkins=[], acs_holds=[acs_hold(**{field: "CANARY-HMAC-SECRET"})])) == ["extra_forbidden"]
    assert rejected(StatusV2Request, status(**{field: "CANARY-HMAC-SECRET"})) == ["extra_forbidden"]


def test_a_key_id_cannot_smuggle_a_secret_it_must_be_a_uuidv4():
    for smuggled in (hmac_like(5), hmac_like(5)[:32], "CANARY-HMAC-SECRET-3201", "c2VjcmV0LWtleS1tYXRlcmlhbC1oZXJl"):
        assert rejected(UploadV2Request, upload(key_id=smuggled))
        assert rejected(StatusV2Request, status(key_id=smuggled))


def _imports_and_attributes(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    return modules, attributes


@pytest.mark.parametrize("relative", ["src/services/ingest_v2_models.py", "src/services/ingest_v2_service.py",
                                      "scripts/issue_ingest_key.py"])
def test_the_code_that_issues_and_stores_keys_has_no_cryptography_and_no_key_generation(relative):
    from pathlib import Path

    modules, attributes = _imports_and_attributes(Path(__file__).resolve().parent.parent / relative)

    assert modules.isdisjoint({"hmac", "secrets", "hashlib", "cryptography", "Crypto", "nacl", "jwt", "base64", "binascii"}), modules
    assert attributes.isdisjoint({"urandom", "token_bytes", "token_hex", "token_urlsafe", "randbytes", "getrandbits", "SystemRandom"})


def test_the_only_thing_ever_generated_is_a_random_uuid_label():
    from pathlib import Path

    source = (Path(__file__).resolve().parent.parent / "src/services/ingest_v2_service.py").read_text(encoding="utf-8")

    assert source.count("uuid.uuid4()") == 1  # the key_id, and nothing else, is generated
