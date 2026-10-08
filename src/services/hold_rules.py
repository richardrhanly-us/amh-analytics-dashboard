"""The v1 hold classifier's rules: the `internal_routing` lists of patron account names and `|DA...|` markers, as
metrics.build_acs_item_summary compares them.

No Streamlit, no database, no request: the dashboard (services.settings_service, which re-exports these) and the
customer API's hold report (services.hold_report_service) build the same object from the same stored block, and
nothing else about the rules is decided here.

They decide v1 (pre-cutover) holds only. A Contract v2 hold arrives already classified by the collector, from the
collector's own local rules, and these lists do not reach it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# The settings key that holds the v1 hold classifier's lists of patron account names and `|DA...|` markers. Some of
# those names can be a person's own (a staff member's patron card), so the block is never part of the dashboard's
# generic settings: not as itself, and not inside the settings document that is handed on.
INTERNAL_ROUTING_KEY = "internal_routing"
V1_HOLD_LISTS = (
    "branch_services_names",
    "collection_services_names",
    "branch_services_da_patterns",
    "collection_services_da_patterns",
)


@dataclass(frozen=True)
class V1HoldRules:
    """What the v1 (pre-cutover) hold classifier, metrics.build_acs_item_summary, is given -- and the only place in
    the dashboard's settings these lists are kept.

    It is what tells the dashboard which holds are for the library's own accounts, so that the public-holds count
    leaves them out. A Contract v2 hold arrives already classified by the collector, from the collector's local
    rules: these lists do not affect it.

    Each entry is trimmed and upper-cased, as the classifier compares it; a blank entry is passed on as it is, and
    the classifier ignores it. Its repr names no entry: it says only how many there are."""

    branch_services_names: frozenset[str] = field(repr=False)
    collection_services_names: frozenset[str] = field(repr=False)
    branch_services_da_patterns: tuple[str, ...] = field(repr=False)
    collection_services_da_patterns: tuple[str, ...] = field(repr=False)

    def __repr__(self) -> str:
        names = len(self.branch_services_names) + len(self.collection_services_names)
        patterns = len(self.branch_services_da_patterns) + len(self.collection_services_da_patterns)
        return f"V1HoldRules(names={names}, patterns={patterns})"


def _listed(block: Mapping[str, Any], key: str) -> list[str]:
    values = block.get(key, [])
    return [str(value).strip().upper() for value in values] if isinstance(values, list) else []


def v1_hold_rules(internal_routing: object) -> V1HoldRules:
    """The v1 hold classifier's lists, from an `internal_routing` block (anything that is not one is no lists)."""
    block: Mapping[str, Any] = internal_routing if isinstance(internal_routing, Mapping) else {}
    return V1HoldRules(
        branch_services_names=frozenset(_listed(block, "branch_services_names")),
        collection_services_names=frozenset(_listed(block, "collection_services_names")),
        branch_services_da_patterns=tuple(_listed(block, "branch_services_da_patterns")),
        collection_services_da_patterns=tuple(_listed(block, "collection_services_da_patterns")),
    )
