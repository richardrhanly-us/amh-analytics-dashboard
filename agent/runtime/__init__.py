"""Canonical continuous agent runtime (Continuous Ingestion Phase F).

Wires the Phase B-E building blocks (parser, discovery, tailer, state,
spool, identity, event_identity) into one working process: a Collector
that reads/parses/spools source data, an Uploader that drains the spool
to the backend, a Heartbeat that reports health, a Housekeeping loop for
lightweight local maintenance, and a Supervisor that runs all four.

Deliberately has no SQLite dependency anywhere in this package -- see
tests/test_runtime_no_sqlite.py. The experimental SQLite path this
package superseded (agent/outbox.py, agent/outbox_uploader.py,
agent/watcher.py, agent/maintenance.py, agent/heartbeat.py) has been
removed from the repository -- once this package's own tests proved
equivalent coverage for every reliability requirement those modules
existed to satisfy (crash/restart safety, retry/backoff, poison-event
isolation, disk-pressure-safe housekeeping, health reporting), the old
implementation was deleted rather than kept in parallel. See agent/README.md
for the full removed-architecture -> canonical-replacement mapping. This
is now the one canonical continuous-agent path (`python -m agent.main`),
not yet the live AMH-machine production path -- see agent/README.md's
"Remaining pre-production gates."
"""
