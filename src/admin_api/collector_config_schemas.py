"""Request and response models for a library's collector configuration in the administration API (A2 item 4).

Closed (extra="forbid") both ways: only these fields can reach the admin app, and a request carrying any other is
refused. No organization, branch, installation, key or user id is a field here, and no settings document is returned
whole. The Internal Routing lists are returned only by the internal-routing route, to platform administrators.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from admin_api.diagnostics_schemas import LibraryRef

EnrollmentProblem = Literal["timezone_not_configured", "timezone_invalid", "rules_not_reviewed"]

# How many entries a request's list is read to: past the number a list may have, so that "too many" is the settings
# rule's own answer (services.internal_routing.MAX_ENTRIES), not this model's.
_LIST_READ_LIMIT = 10_000


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InternalRoutingCounts(_Model):
    branch_services_names: int
    collection_services_names: int
    branch_services_da_patterns: int
    collection_services_da_patterns: int


class CollectorSettingsResponse(_Model):
    """What a Contract v2 enrollment reads from the library, and the library's own routing for context.

    `collector_timezone` is what is stored (null if nothing is). `rules_reviewed_at` is when the routing configuration
    was last recorded as reviewed (null if never, or if the record is not a valid one). `home_branch_label` and
    `transit_destination_labels` (the enabled ones, in order) are the library's own settings, edited by the library:
    they are shown here, never written. `enrollment_problems` is what would stop a Contract v2 enrollment now."""

    library: LibraryRef
    collector_timezone: str | None
    rules_reviewed_at: datetime | None
    home_branch_label: str
    transit_destination_labels: list[str]
    internal_routing_counts: InternalRoutingCounts
    enrollment_problems: list[EnrollmentProblem]


class CollectorSettingsRequest(_Model):
    """A PUT: the time zone, and the confirmation that the routing configuration was reviewed -- which must be true."""

    collector_timezone: StrictStr
    rules_reviewed: Literal[True]


class InternalRoutingResponse(_Model):
    library: LibraryRef
    branch_services_names: list[str]
    collection_services_names: list[str]
    branch_services_da_patterns: list[str]
    collection_services_da_patterns: list[str]


class InternalRoutingRequest(_Model):
    """A PUT: all four lists, replacing what is stored. Each entry's rules are services.internal_routing's, answered
    as problems; a list's entries are read as anything here so that an entry that is not text is one of them."""

    branch_services_names: list[Any] = Field(max_length=_LIST_READ_LIMIT)
    collection_services_names: list[Any] = Field(max_length=_LIST_READ_LIMIT)
    branch_services_da_patterns: list[Any] = Field(max_length=_LIST_READ_LIMIT)
    collection_services_da_patterns: list[Any] = Field(max_length=_LIST_READ_LIMIT)
