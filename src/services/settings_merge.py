"""A site's effective settings, and the stored documents they are made from.

    settings_document(stored)              -> a stored settings_json as a dict
    merge_settings(organization, branch)   -> the organization's settings with the branch's merged over them

The one rule for effective settings: the dashboard's (services.tenant_service.get_effective_settings), the customer
API's (services.routing_config_service, services.hold_report_service) and a Collector's
(services.collector_rules.effective_settings) all use merge_settings.

Pure: standard library only. No database, no framework, no environment. Neither function changes what it is given.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def settings_document(value: object) -> dict[str, Any]:
    """A stored settings_json as a dict. PostgreSQL hands JSONB back parsed; a
    driver that hands back text is parsed here. Nothing stored, or anything
    that is not an object, is an empty document."""
    if isinstance(value, (str, bytes)):
        value = json.loads(value)
    return dict(value) if isinstance(value, Mapping) else {}


def merge_settings(organization: object, branch: object) -> dict[str, Any]:
    """The organization's settings with the branch's merged over them, key by key: where both hold an object at a key
    the two are merged the same way; anything else the branch holds replaces the organization's value outright. A
    document that is not an object counts as an empty one. Neither argument is changed."""
    result = dict(organization) if isinstance(organization, Mapping) else {}
    for key, value in (branch.items() if isinstance(branch, Mapping) else ()):
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = merge_settings(result[key], value)
        else:
            result[key] = value
    return result
