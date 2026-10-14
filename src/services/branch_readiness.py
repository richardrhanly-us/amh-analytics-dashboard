"""Whether the dashboard may open a branch: the readiness decision, from values already read.

    branch_readiness(row) -> BranchReadiness(code, onboarding)

row is the organization/branch row services.readiness_service reads (None when there is no such organization). Of
it, only branch_slug, branch_status, customer_id (the organization's operational id), branch_id (the branch's
operational id) and onboarding_status are read. The checks, in order:

    no row                              -> "org_not_found"
    no branch_slug                      -> "branch_not_found"
    branch_status other than "active"   -> "branch_inactive"
    either operational id NULL          -> "missing_operational_mapping"   (0 is an id)
    otherwise                           -> the onboarding_status itself, NULL or empty counting as "pending"

Only an onboarding_status of exactly "ready" is ready. `onboarding` tells the last case from the four before it, so
a stored status that happens to spell one of their codes is still read as a status.

This is the DASHBOARD's rule. The customer API's tenant resolution (services.tenant_resolution_service) does not
read onboarding_status; that difference is deliberate and is not decided here.

    normalize_onboarding_status(stored) -> "ready" | "pending"

How a branch's stored onboarding_status is SHOWN to the customer API: exactly "ready" is "ready", and anything else
-- NULL, empty, "paused", a value of another case, one never heard of -- is "pending". Presentation only: it reads
nothing, writes nothing and grants or refuses nothing.

Pure: no database, no framework, no environment, no messages. What each code says to a person is
services.readiness_service's.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

READY = "ready"

OnboardingStatus = Literal["pending", "ready"]


def normalize_onboarding_status(value: object) -> OnboardingStatus:
    return "ready" if value == READY else "pending"


@dataclass(frozen=True, slots=True)
class BranchReadiness:
    code: str
    onboarding: bool  # True: code is the branch's onboarding status. False: a setup problem came first.

    @property
    def is_ready(self) -> bool:
        return self.onboarding and self.code == READY


def branch_readiness(row: Mapping[str, object] | None) -> BranchReadiness:
    if row is None:
        return BranchReadiness("org_not_found", onboarding=False)
    if row["branch_slug"] is None:
        return BranchReadiness("branch_not_found", onboarding=False)
    if row["branch_status"] != "active":
        return BranchReadiness("branch_inactive", onboarding=False)
    if row["customer_id"] is None or row["branch_id"] is None:
        return BranchReadiness("missing_operational_mapping", onboarding=False)
    return BranchReadiness(str(row["onboarding_status"] or "pending"), onboarding=True)
