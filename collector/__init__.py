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

Scope of what lives here:
  - Phase 4a: incremental reading, state/status persistence, config
    loading, and the upload client -- one bounded, one-shot run: read
    new records, parse (via the existing, UNCHANGED agent/parser/*
    modules -- parser behavior is explicitly out of scope for this
    phase), upload, persist state, log status, exit. No spool, no
    daemon, no supervisor, no long-running threads.
  - Phase 4c: install/preflight/Scheduled Task productization
    (collector/preflight.py, collector/task_settings.py,
    collector/support_info.py, collector/deploy/*.ps1) -- makes the
    Phase 4a runtime installable, verifiable under the real Scheduled
    Task identity, and operable by a library IT administrator.
  - Parser-parity phase (PR #25): wired the production Tech Logic parser
    in via collector/parsers.py, a thin adapter over the unchanged
    agent/parser/{checkins,rejects,acs}.py modules (imported, not
    copied) -- an ordinary registered-task run now parses and uploads
    real data for those three sources. The fail-closed gate from Phase
    4a (ParserNotConfiguredError) is unchanged and unbypassed; it now
    only fires for a source name outside those three, not as the
    routine condition it used to be. Deployment packaging repair (this
    same phase) extended collector/deploy/*.ps1 to also ship the narrow
    canonical-parser runtime slice of agent/ this adapter needs -- see
    collector/deploy_manifest.py, the single source of truth for exactly
    which files that is.
"""

from __future__ import annotations

# Simple semver -- the SINGLE AUTHORITATIVE Collector version. It is what the
# RUNNING Collector reports on every heartbeat
# (collector/uploader.py::post_status), in `support-info`, and via the
# config-free `SortViewCollector.exe version` command, and what the backend
# records as collector_installations.collector_version. Nothing else keeps a
# copy of it, and no build rewrites it: it is bumped here by hand.
#
# Release builds ASSERT against it instead of trusting their own input:
#   - collector/build_release.py: --version is only an assertion and must
#     equal this value exactly (source and frozen bundles alike); a frozen
#     bundle's SortViewCollector.exe is also run with `version` and must report
#     it before anything is packaged.
#   - collector/freeze/build_frozen.ps1: the freshly built executable's
#     `version` output must equal this value, or the build fails.
# 1.0.4 is the release validated and currently running in production at
# NBPL (see docs/releases/collector-1.0.4.md); 1.0.5 adds local,
# privacy-safe per-run audit logging (collector/run_audit.py) while
# preserving the existing Collector runtime, protocol and upload
# behavior unchanged -- new observability, not a new contract.
# 1.0.6 adds the Contract v2 runtime and privacy-safe mixed-era production
# support. Contract v2 remains opt-in and disabled until explicitly configured
# for a validated pilot installation.
# 1.0.7 adds the identity-collision-diag onsite diagnostic (explains
# identical_identity_events on real Tech Logic data), reachable both as
# `python -m collector.identity_collision_diag` and as the frozen
# SortViewCollector.exe's own `identity-collision-diag` subcommand -- see
# collector/identity_collision_diag.py and collector/freeze/dispatcher.py.
# It reuses the existing v2 dry-run config, transform logic and safety
# contract unchanged (throwaway key, no persistent secret, no network call,
# no state/cursor write); the identity algorithm itself is unchanged.
# 1.0.8 replaces the pilot's flat identical_identity_events == 0 acceptance
# rule with a bounded, source/category-aware gate (identity_collision_diag.py's
# _evaluate_gate, surfaced as its printed identity_collision_gate=pass|fail
# and consumed by collector/deploy/prepare_v2_pilot.ps1's Step 6b): permits
# only the ACS hold-read-twice-unchanged case and a small, spread-out,
# low-rate cluster of barcode-less ils_acs_failure/rfid_collision reject
# duplicates -- real NBPL AMH data (91 such pairs of 1024 rejects) motivated
# this over the flat rule, which those same real events would otherwise have
# failed. identical_identity_events is still always printed, never hidden;
# the identity algorithm, event_key generation, transformation semantics,
# the uploader, the privacy boundary and dedup behavior are all unchanged.
# 1.0.9 adds frozen-runtime support for local v2 secret provisioning --
# `SortViewCollector.exe v2-key init|check` (collector/freeze/dispatcher.py's
# `v2-key` subcommand, pure argv forwarding to collector/v2_keys.py's
# existing main(), never a copy of its key-generation or DPAPI/ACL logic) --
# and a new release-packaged tool, tools\configure_v2.ps1
# (collector/deploy/configure_v2.ps1), which converts an existing production
# collector_config.json from contract_mode "v1" to "v2" in place: fail-closed
# throughout, requires the Scheduled Task disabled, validates a candidate
# copy with the real collector config loader BEFORE ever touching the
# production file, takes a UTC-timestamped backup, and replaces the
# production file atomically only once validation has already passed. Key
# creation, server feature-flag activation, cutover recording, and task
# enabling remain separate, explicit, later operator steps -- none of them
# automatic and none performed by this release's own tooling. No change to
# cryptography, key format, DPAPI scope, ACL rules, the identity algorithm,
# the uploader, the privacy boundary, or dedup behavior.
__version__ = "1.0.9"
