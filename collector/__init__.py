"""SortView Collector -- v1 commercial ingestion architecture.

Repoints SortView v1 away from the always-running continuous agent
(agent/main.py + agent/runtime/*, now frozen as experimental/future work --
see agent/README.md) toward the existing 15-minute scheduled pipeline
model, which is what is actually proven in production at NBPL. See:

  - docs/amh-production-cutover-runbook.md (continuous-agent runbook --
    superseded for v1 by the work in this package, not deleted)
  - The SortView Collector phased planning conversation (Phase 1-4a) for
    the full rationale, the live-AMH verification that grounds this
    package's design decisions, and the gap/risk audit this phase closes.

DELIBERATELY INDEPENDENT of agent/runtime/*, agent/tailer.py,
agent/state.py, agent/spool.py, agent/discovery.py, agent/identity.py,
and agent/event_identity.py -- this package imports NONE of them. The
continuous-agent architecture is frozen, not a dependency; a handful of
its PROVEN CONCEPTS (binary-mode true byte offsets, partial-line safety,
identity-based rotation detection, atomic file writes) are reimplemented
here as small, self-contained, independently-tested functions rather than
imported, so this package has zero coupling to code that may be removed
or left unmaintained later. Where a docstring says "mirrors" a continuous-
agent module, that means the CONCEPT was reused deliberately, per the
approved Phase 2 architecture -- not that any code was copied or imported.

Scope of what lives here (Phase 4a): incremental reading, state/status
persistence, config loading, and the upload client -- one bounded,
one-shot run: read new records, parse (via the existing, UNCHANGED
agent/parser/* modules -- parser behavior is explicitly out of scope for
this phase), upload, persist state, log status, exit. No spool, no
daemon, no supervisor, no long-running threads.
"""

from __future__ import annotations
