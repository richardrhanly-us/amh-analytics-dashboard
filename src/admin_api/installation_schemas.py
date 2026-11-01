"""Request and response models for a library's Collector installations in the administration API (A2 item 5).

Closed (extra="forbid") both ways. An installation's id is shown to platform administrators as a support reference;
nothing else that identifies a machine, a credential or a tenant is a field here: no hostname, no Collector version, no
organization, branch, operational, key or code-row id, no code or token hash. The raw enrollment code is a field of
EnrollmentCodeResponse alone and the raw recovery token of RecoveryTokenResponse alone -- the answer to the request that
issued it, once. A token's row id appears only as the handle for revoking a legacy unbound token.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictStr

AddProblem = Literal["library_cancelled", "no_active_primary_branch", "collector_settings_not_ready"]
InstallationStatus = Literal["provisioning", "active", "inactive", "retired"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstallationsLibrary(_Model):
    slug: str
    name: str
    status: str


class PrimaryBranchSummary(_Model):
    slug: str
    name: str
    status: str


class InstallationBranch(_Model):
    slug: str
    name: str
    is_primary: bool
    status: str


class Installation(_Model):
    """One installation. `pending_code_expires_at`: when its unused enrollment code expires (null if it has none);
    `last_code_redeemed_at`: when a code of its was last redeemed (null if never)."""

    id: int
    name: str
    branch: InstallationBranch
    status: InstallationStatus
    required_contract: Literal[1, 2]
    installed_at: datetime | None
    last_seen_at: datetime | None
    created_at: datetime
    pending_code_expires_at: datetime | None
    last_code_redeemed_at: datetime | None


class InstallationsResponse(_Model):
    """Every installation of the library, whatever its status or its branch's. `add_problems` is what stops an
    installation being added now; `collector_settings_not_ready` concerns contract 2 alone."""

    library: InstallationsLibrary
    primary_branch: PrimaryBranchSummary | None
    add_problems: list[AddProblem]
    installations: list[Installation]


class AddInstallationRequest(_Model):
    """No branch: an installation is added at the library's active primary branch."""

    name: StrictStr
    required_contract: Literal[1, 2]


class RenameInstallationRequest(_Model):
    name: StrictStr


class InstallationStatusRequest(_Model):
    """Retired is final; active is reached only by a heartbeat."""

    status: Literal["inactive", "provisioning", "retired"]


class InstallationRef(_Model):
    id: int
    name: str


class EnrollmentCodeResponse(_Model):
    """The one answer that carries a raw enrollment code. It is not stored and cannot be shown again."""

    installation: InstallationRef
    required_contract: Literal[1, 2]
    enrollment_code: str
    expires_at: datetime
    ttl_minutes: int
    revoked_previous_count: int


class RecoveryTokenResponse(_Model):
    """The one answer that carries a raw recovery token. It is not stored and cannot be shown again."""

    installation: InstallationRef
    agent_token: str
    revoked_previous_count: int


class UnboundToken(_Model):
    """A legacy unbound agent token, by its row id: never its hash. `description` is what an operator wrote when issuing it."""

    id: int
    description: str | None
    branch_name: str | None
    created_at: datetime
    last_used_at: datetime | None


class UnboundTokensResponse(_Model):
    tokens: list[UnboundToken]


class RevokedUnboundToken(_Model):
    id: int
