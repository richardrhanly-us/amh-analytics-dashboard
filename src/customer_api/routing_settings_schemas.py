"""Request and response models for the customer API's routing settings routes.

One shape both ways: the organization's routing block, under `routing`. A
destination is its label and whether it is enabled -- and nothing else. The
`key` stored beside each destination is not part of this contract in either
direction: a request that carries one is refused, like any other field these
models do not name, and no response has one.

No organization, customer, branch or user id has a field here, and nothing
else an organization's settings document holds.

The models only say what a body is made of. Every rule about a value -- a
label that is blank, two that are the same destination, one that means home,
too many destinations -- is services.routing_settings', and is answered as a
list of the fields at fault.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr

# The most entries a request's list is read to. Far past the number an organization may have: how many are
# allowed is the settings model's rule, answered as a problem, not this one's.
_LIST_READ_LIMIT = 200


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoutingDestination(_Model):
    """One place items are routed to. `label` is as the administrator writes
    it; it is what a check-in's destination is matched by."""

    label: StrictStr
    enabled: StrictBool


class Routing(_Model):
    """The organization's own routing settings. `home_branch_label` may be an
    empty string. `destinations` are in display order and may be empty."""

    home_branch_label: StrictStr
    destinations: list[RoutingDestination] = Field(max_length=_LIST_READ_LIMIT)


class RoutingSettingsRequest(_Model):
    """The body of a PUT: the whole block. It replaces what is stored."""

    routing: Routing


class RoutingSettingsResponse(_Model):
    routing: Routing
