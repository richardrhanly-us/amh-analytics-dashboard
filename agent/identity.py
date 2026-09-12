"""Durable agent installation identity (Continuous Ingestion Phase E).

Phase E's deterministic source-event identity (agent/event_identity.py)
needs to be scoped to a specific installed agent, not just to a logical
source + generation + offset -- because agent/state.py's generation is
LOCAL, PERSISTED state, not a globally unique historical record. A
reinstall, a lost/corrupted state directory, or a replaced machine could
all eventually reproduce the same (source, generation, offset) triple
that a PRIOR installation already produced. Scoping the event ID to an
installation identity that itself changes across exactly those events
keeps the two namespaces disjoint.

This is deliberately NOT a server-issued or centrally-enrolled identity.
There is no request to the backend, no registration flow, no lookup
against a customer/branch-scoped "agents" table -- that is remote
enrollment/installer infrastructure, explicitly out of scope for this
phase (Phase F/G territory: verifying an install actually belongs to a
given customer/branch, revoking or rotating an install's identity,
recognizing a machine replacement as a deliberate migration rather than a
reinstall). Implementing that correctly needs backend-side identity
issuance and lifecycle decisions this phase has no mandate to make.

What IS implemented here is the minimum clean abstraction Phase E
actually needs: a small, opaque, LOCALLY-generated identifier -- a UUID4
minted once on first run and persisted next to the agent's other durable
state (see agent/state.py's DEFAULT_STATE_PATH for the sibling
convention). Loaded back unchanged on every subsequent run. A genuine
reinstall or state-loss event (the identity file is gone, exactly like
the state file being gone) naturally mints a NEW identifier -- which is
exactly the property Phase E needs: it makes whatever event IDs this
"new" installation produces disjoint from the prior installation's,
even if source-generation/offset numbers end up being reused from zero.

The backend never needs to validate, look up, or attach meaning to this
value beyond treating it as an opaque scoping component inside a hashed
source_event_id (see agent/event_identity.py) -- it is never sent to the
backend on its own, and there is no server-side "agents" table this
needs to match today.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import uuid
from pathlib import Path

DEFAULT_IDENTITY_PATH = Path("data") / "agent_identity.json"


class AgentIdentityError(Exception):
    """Base class for every error this module raises deliberately."""


class CorruptAgentIdentityError(AgentIdentityError):
    """The identity file exists but cannot be safely read (bad JSON,
    wrong shape, empty/non-string id). Never silently regenerated -- a
    fresh, different agent_id would silently change every future event
    ID's namespace with no operator visibility, which is a much worse
    outcome than refusing to start. Recovery is an explicit operator
    decision (inspect and fix, or deliberately delete the file to mint a
    new identity, understanding that is equivalent to a reinstall for
    event-ID purposes), not something this module decides on its own.
    """


def _atomic_write_text(path: Path, text: str) -> None:
    """Same proven shape as agent/state.py's save_state and
    agent/spool.py's _atomic_write_bytes -- duplicated here rather than
    imported, keeping this module's dependency footprint at zero, same
    rationale as spool.py's own copy."""
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())

        os.replace(str(tmp_path), str(path))

        if os.name != "nt":
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(str(tmp_path))
        raise


def _load_agent_id(path: Path) -> str:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorruptAgentIdentityError(f"agent identity file exists but could not be read: {exc}") from exc

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise CorruptAgentIdentityError(f"agent identity file is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise CorruptAgentIdentityError(f"agent identity document must be an object, got {raw!r}")

    agent_id = raw.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        raise CorruptAgentIdentityError(f"'agent_id' must be a non-empty string, got {agent_id!r}")

    return agent_id


def load_or_create_agent_id(path: str | Path = DEFAULT_IDENTITY_PATH) -> str:
    """Returns this installation's durable local identity, creating and
    persisting a new one (a fresh uuid4) the first time this is ever
    called against `path`. Every subsequent call against the same path
    -- including across process/machine restarts -- returns the exact
    same string, read back from disk, never regenerated.

    Raises CorruptAgentIdentityError rather than silently minting a
    replacement if the file exists but is unreadable/malformed -- see
    that exception's docstring for why.
    """
    p = Path(path)
    if p.exists():
        return _load_agent_id(p)

    agent_id = str(uuid.uuid4())
    _atomic_write_text(p, json.dumps({"agent_id": agent_id}, indent=2))
    return agent_id
