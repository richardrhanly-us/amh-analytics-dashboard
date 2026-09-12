"""Deterministic source-event identity (Continuous Ingestion Phase E).

Computes a stable identifier for one physical source record (one line in
Checkins.txt / Rejects.txt / ACS Log.txt), so the SAME record produces
the SAME id every time it is read -- whether that's a normal first read,
a reread after a crash, a resend after a lost ACK, or a retry of a
previously-accepted spool batch. This is TRANSPORT/SOURCE idempotency,
layered on top of (never replacing) the backend's existing semantic
deduplication -- see the Phase E audit report for the full distinction
and why both are needed during legacy/new-agent parallel validation.

CANONICAL IDENTITY INPUTS, in this exact fixed order:

  agent_id     -- this installation's durable LOCAL identity (see
                  agent.identity.load_or_create_agent_id). Not a raw
                  hardware/OS identifier, and not the backend bearer
                  token (a rotatable credential, not an installation
                  identity).
  source       -- the logical source name (one of
                  agent.state.SOURCE_NAMES: "checkins", "rejects", "acs").
  generation   -- agent.state.SourceState.generation for this source at
                  the time this record was read. Required specifically
                  because raw offsets are only comparable within one
                  generation -- see agent/state.py's module docstring.
  start_offset -- the offset of THIS record's first byte, from
                  agent.tailer.TailResult.line_offsets. Deliberately NOT
                  a batch's aggregate start/end offset -- a batch can
                  contain many records, and this must identify exactly
                  one of them.

No record/sub-index component. Verified against every parser in
agent/parser/ (checkins, rejects, acs): each produces AT MOST one
normalized row per input line (a line either becomes exactly one row or
is skipped entirely -- see parse_lines_with_offsets in each parser
module). start_offset therefore already uniquely identifies a record
within one (agent_id, source, generation) -- add a sub-index only if a
parser is ever changed to fan one line out into multiple rows.

Deliberately excluded, so the SAME input always produces the SAME id:
current timestamp, upload attempt count, spool filename randomness,
batch number, HTTP request id, process id. None of those are read by
this module, and none influence its output.

REPRESENTATION: a SHA-256 hex digest of the pipe-joined canonical string
above (e.g. "agent-id|checkins|12|8001234" -> sha256 -> 64 hex chars).
The digest -- not the raw components -- is what travels to the backend
and is stored in the database (see alembic revision 45ba2e7befbc and
main.py); the backend never needs to know raw st_dev/st_ino or anything
else tailer/discovery-internal. The digest is opaque and fixed-width
regardless of how long agent_id/source get in the future, and never
itself reveals the plaintext components.
"""

from __future__ import annotations

import hashlib

_FIELD_SEPARATOR = "|"

SOURCE_EVENT_ID_PATTERN = r"^[0-9a-f]{64}$"


class EventIdentityError(Exception):
    """Base class for every error this module raises deliberately."""


def canonical_identity_string(*, agent_id: str, source: str, generation: int, start_offset: int) -> str:
    """The exact pipe-joined string that gets hashed. Exposed separately
    from compute_source_event_id so tests (and, if ever needed,
    diagnostics) can assert on the pre-hash representation directly, not
    just its digest."""
    if not agent_id:
        raise EventIdentityError("agent_id must be a non-empty string")
    if not source:
        raise EventIdentityError("source must be a non-empty string")
    for field_name, value in (("agent_id", agent_id), ("source", source)):
        if _FIELD_SEPARATOR in value:
            raise EventIdentityError(
                f"{field_name} {value!r} must not contain {_FIELD_SEPARATOR!r} -- "
                "would make the canonical identity string ambiguous"
            )
    if generation < 0:
        raise EventIdentityError(f"generation must be non-negative, got {generation!r}")
    if start_offset < 0:
        raise EventIdentityError(f"start_offset must be non-negative, got {start_offset!r}")

    return _FIELD_SEPARATOR.join([agent_id, source, str(generation), str(start_offset)])


def compute_source_event_id(*, agent_id: str, source: str, generation: int, start_offset: int) -> str:
    """Same (agent_id, source, generation, start_offset) always returns
    the same 64-character lowercase hex digest -- a pure function of its
    inputs, with no dependency on wall-clock time, process state, or
    anything else that could vary between a first read and a retry."""
    canonical = canonical_identity_string(
        agent_id=agent_id, source=source, generation=generation, start_offset=start_offset
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def attach_source_event_id(
    record: dict, *, agent_id: str, source: str, generation: int, start_offset: int
) -> dict:
    """Returns a NEW dict (never mutates `record`) with 'source_event_id'
    set to compute_source_event_id(...)'s result.

    Intended call site: the future orchestrator (Phase F), immediately
    after producing one normalized event dict from a parsed row and
    BEFORE calling agent.spool.write_batch -- per the cursor invariant
    this whole redesign serves, the ID must exist before durable spool
    publication. A retry/resend must read the SAME id back from the
    already-spooled record (spool.py stores whatever dict it's given
    verbatim), never recompute a fresh one at upload time -- recomputing
    from identical inputs would happen to produce the same value anyway,
    but reading back what was actually spooled is the one true source of
    what was durably captured, and doesn't depend on the recomputation
    inputs staying available/consistent at upload time.
    """
    event_id = compute_source_event_id(
        agent_id=agent_id, source=source, generation=generation, start_offset=start_offset
    )
    return {**record, "source_event_id": event_id}
