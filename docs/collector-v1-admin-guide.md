# SortView Collector v1 -- Admin Guide

Operator/IT documentation for the `collector/` package (Phase 4c). This
is **not** the continuous agent (`agent/main.py` + `agent/runtime/*`,
frozen as experimental/future work) and **not** the legacy pipeline
(`C:\SortViewAgent`, still authoritative for NBPL production today) --
see `docs/amh-production-cutover-runbook.md` for that older material,
which this document does not replace or supersede for NBPL specifically.

## What this is

A short-lived, one-shot Windows program that a Scheduled Task runs every
15 minutes: read new records from the configured Tech Logic files, parse,
upload over HTTPS, persist state, write a status report, exit. No
persistent process, no daemon, no spool, no live-feed requirement.

## Status of this build -- read before installing anything

**The production Tech Logic parser is not wired in yet.** The collector
fails closed by design: every run exits with code `2` and a message
naming which source has no parser configured, and no data is ever
uploaded. This is intentional (see `collector/run.py`'s module
docstring's `PARSER SEAM` section) -- everything below (install,
preflight, task registration) is safe and correct to do now, and proves
the install/scheduling/connectivity layer works, but this is **not yet
ready for real production data collection**. That happens in a separate,
later phase.

**Also not yet done, regardless of the above:** real SYSTEM-context
validation on an actual Windows machine, a real reboot test, and real
proxy/TLS validation on a municipal network. This document tells you how
to do all three -- doing them is a required step before any production
cutover, not optional polish. Nothing in this repository's test suite
proves any of these three; they are explicitly out of reach for a unit
test (see `collector/preflight.py`'s and `collector/state.py`'s own
module docstrings).

---

## Fresh install

From an elevated PowerShell session, in a checkout of this repository:

```powershell
cd collector\deploy
.\install-collector.ps1 -CustomerId <id> -BranchId <id> -ApiUrl "https://sortview-app-2p336.ondigitalocean.app"
```

Defaults: install root `C:\SortView\Collector` (code + venv), data root
`C:\ProgramData\SortViewCollector` (`config\`, `data\`, `logs\`). Override
either with `-InstallRoot`/`-DataRoot`. Safe to re-run: if either already
exists, it refuses to proceed without `-Force` (see the script's own
`.DESCRIPTION`).

If you don't pass `-CustomerId`/`-BranchId`, it copies the example config
template instead -- edit `<DataRoot>\config\collector_config.json` by
hand before continuing, especially the three `sources` paths if this site
doesn't use the standard Tech Logic locations.

## Config

`<DataRoot>\config\collector_config.json` -- see
`collector/deploy/collector_config.example.json` for every field and
what it means. Never contains a secret. `sources` makes source **paths**
configurable per site; it does not make the collector work with a
different AMH vendor's file format -- the parser (not yet wired in this
build) is still Tech Logic-specific.

## Token setup

The collector reads `SORTVIEW_API_TOKEN` from the environment only --
never from the config file, never from Git, never logged. Set it as a
Machine-scope Windows environment variable (so the unattended SYSTEM-run
task can see it) using the existing, reused-as-is script from the
continuous-agent tooling:

```powershell
<repo>\agent\deploy\set-sortview-api-token.ps1
```

Prompts for the token as a SecureString (never displayed). Provision a
**separate** token for the collector, distinct from any legacy or
continuous-agent token, via `scripts/create_agent_token.py` (dry-run by
default -- see that script's own docstring) so it can be revoked
independently.

Setting it only affects **new** processes -- if the collector or an
already-open shell is already running, it won't see a just-set value
until restarted.

## Running preflight

Interactively first (proves config/paths/network from your own account,
not the production identity):

```powershell
<InstallRoot>\.venv\Scripts\python.exe -m collector.preflight --config <ConfigPath>
```

Fourteen independent checks, each printed with `[PASS]`/`[FAIL]` and a
detail message; overall exit code is `0` only if every check passed. A
passing interactive run does **not** prove SYSTEM can do the same things
-- proxy configuration, file permissions, and environment variable
visibility can all differ by security principal. See the next section.

## Running preflight as SYSTEM (required, not optional)

```powershell
cd collector\deploy
.\run-preflight-as-system.ps1 -InstallRoot <InstallRoot> -ConfigPath <ConfigPath>
```

Registers a **temporary**, clearly-named, one-time Scheduled Task running
as SYSTEM, starts it immediately, waits for it to finish, reads back its
result file, prints a labeled `SYSTEM CONTEXT VALIDATION: PASS`/`FAIL`
summary, and unregisters the temporary task afterward -- always, whether
it passed, failed, or timed out. Never touches the real production task
or `C:\SortViewAgent`.

**Do not register or start the real production task until this passes.**
A check that fails here and passed interactively almost always means a
SYSTEM-specific permission or proxy issue -- see Troubleshooting below.

## Task registration

```powershell
cd collector\deploy
.\register-collector-task.ps1 -InstallRoot <InstallRoot> -ConfigPath <ConfigPath>
```

Registers "SortView Collector" (15-minute cadence, `IgnoreNew` overlap
policy, restart-on-failure 3 attempts/5 minutes apart, 1-hour execution
limit, SYSTEM by default) -- does **not** start it. Settings are
generated by `python -m collector.task_settings` (the single, unit-tested
source of truth for these values -- see that module) and registered via
`schtasks /create /xml`. Idempotent: re-running without `-Force` reports
the existing task and changes nothing.

```powershell
Start-ScheduledTask -TaskName "SortView Collector"
```

Remember: every run will exit `2` (parser not configured) until that
later phase is complete. This is expected.

## Checking task status

```powershell
Get-ScheduledTask -TaskName "SortView Collector" | Get-ScheduledTaskInfo
```

`LastTaskResult` `0` = success; `2` = config/parser-not-wired (expected
for now); `1` = a genuine run failure -- check the log.

**Recovery from a failed run (`1` or `2`):** the collector never persists
state on a nonzero exit, so the next normal 15-minute scheduled run just
re-reads and re-sends the same uncommitted work -- nothing needs to be
manually retried. Do not expect the registered `RestartOnFailure` setting
(3 attempts, 5 minutes apart) to do this sooner: Phase 4d Section H
live-tested it in isolation on LIB-L26 and found it does not activate for
a process that exits cleanly with a nonzero code, for either exit `1` or
`2`. The setting is left configured (it matches legacy production and is
harmless), but recovery timing in practice is "next 15-minute cycle," not
"within 5 minutes."

## Manual run

```powershell
<InstallRoot>\.venv\Scripts\python.exe -m collector.run --config <ConfigPath>
```

Same thing the Scheduled Task does, run directly for troubleshooting.
Exit code and `<DataRoot>\data\status.json` reflect the outcome exactly
as a scheduled run would.

## Reading logs/status

- `<DataRoot>\logs\collector.log` -- one bounded, rotating log file (5MB
  × 3 backups), not three separate unbounded files like the legacy
  pipeline.
- `<DataRoot>\data\status.json` -- the same report POSTed to
  `/upload-pipeline-status`.
- Support summary (version, install root, config path, task name, Python
  version, last status) without digging through files by hand:

```powershell
<InstallRoot>\.venv\Scripts\python.exe -m collector.support_info --config <ConfigPath>
```

## Update

```powershell
cd collector\deploy
.\update-collector.ps1 -InstallRoot <InstallRoot> -ConfigPath <ConfigPath>
```

Stops the task if running, backs up the current code (and the entire
prior runtime if `requirements.txt` changed, rather than just the code),
replaces the runtime, runs preflight against the **new** runtime, and
only restarts the task if that preflight passes. Config/state/status/logs
under `<DataRoot>` are never touched. If preflight fails post-update, the
task is **not** restarted and exact manual rollback commands are printed
-- nothing is auto-reverted. Re-run `run-preflight-as-system.ps1`
afterward too, especially if dependencies changed.

## Rollback

Following a failed update, the script above prints the exact commands.
In general: the prior runtime is preserved at `<InstallRoot>.backup-<timestamp>`
(code-only) or `<InstallRoot>.backup-<timestamp>-full` (code + venv, when
dependencies changed) -- copy or move it back into place, then
`Start-ScheduledTask` manually once you're satisfied.

## Uninstall

```powershell
cd collector\deploy
.\uninstall-collector.ps1
```

Unregisters the task and removes the application runtime
(`<InstallRoot>`, including its venv). **Config/state/status/logs under
`<DataRoot>` are preserved by default** -- pass `-PurgeData` (with
confirmation) to also delete those. The Machine-scope
`SORTVIEW_API_TOKEN` environment variable is never removed automatically
-- the script tells you whether it's still set and the exact command to
remove it yourself.

## Reboot validation (required onsite, not provable by unit tests)

1. Confirm the task is registered and `SYSTEM` is its principal:
   `(Get-ScheduledTask -TaskName "SortView Collector").Principal.UserId`
2. Reboot the machine. Do **not** log in as any user afterward.
3. Wait past the next 15-minute boundary.
4. From a separate admin session (or after logging in, checking history
   only), confirm: `Get-ScheduledTaskInfo` shows a recent `LastRunTime`,
   and `<DataRoot>\data\status.json`'s `last_attempt` timestamp is after
   the reboot.

If the task never ran, the most likely cause is the principal not
actually being SYSTEM, or a permissions/policy restriction on this
specific machine -- this is exactly the class of fact this document
cannot predict for you; it can only tell you how to check it for real.

## Proxy/TLS troubleshooting

`collector/preflight.py` never assumes SYSTEM's proxy/TLS behavior
matches an interactive account's, and never auto-configures a proxy or
modifies the Windows trust store. If SYSTEM-context preflight's
`dns_resolution` or `https_tls_connection` check fails while the
interactive run passed:

- **DNS/connection failure**: this network likely requires an explicit
  proxy that only your interactive account's profile has configured
  (WinINET, per-user). SYSTEM does not share that. Configure a
  machine-wide proxy for SYSTEM's use, e.g. via `netsh winhttp set proxy
  <proxy>:<port>` (run once, as admin, on this machine) or by setting
  `HTTP_PROXY`/`HTTPS_PROXY` as Machine-scope environment variables --
  whichever matches this site's actual network policy. This is a
  site-specific IT decision this document cannot make for you.
- **TLS certificate validation failure**: this network likely does
  TLS-inspecting proxying with a custom root CA. That CA must be
  installed in the Windows trust store (Local Machine, not just Current
  User, so SYSTEM can see it) -- typically via Group Policy or manually
  through `certlm.msc` on this machine. Confirm with your network/security
  team before installing any certificate.

## Source-file permission troubleshooting

If `source_paths_exist` passes but `source_paths_readable` fails under
SYSTEM specifically: the Tech Logic install directory's ACLs likely don't
grant SYSTEM read access (uncommon, but possible if it was locked down to
a specific interactive account only). Grant SYSTEM read access to the
Tech Logic output directory via `icacls`, coordinating with whoever
manages that software, rather than working around it in the collector.

---

## City IT quick reference

| Question | Answer |
|---|---|
| Outbound destination/port? | One host, port 443 (HTTPS/TLS): the SortView API. Nothing else. |
| Inbound ports? | None. The collector never listens for or accepts inbound connections. |
| Direct database access? | Never. `collector/preflight.py` explicitly verifies no database driver is even installed in the collector's own virtual environment and that `DATABASE_URL` is not set. |
| Source files read? | Only the three configured Tech Logic files (`Checkins.txt`, `Rejects.txt`, `ACS Log.txt` by default), read-only. |
| Files written? | Only inside its own install/data directories: `state.json`, `status.json`, its own rotating log file, and (once parser wiring is complete) cleaned CSV copies under `data\processed\`. |
| Run account? | SYSTEM by default -- no password to manage or expire, no dependency on any user staying logged in. |
| Reboot behavior? | The Scheduled Task is registered to run whether anyone is logged in or not, and resumes automatically after reboot with no login required (`StartWhenAvailable`) -- see Reboot validation above for how to verify this on a specific machine. |
| Secret storage? | A single Machine-scope Windows environment variable (`SORTVIEW_API_TOKEN`), set once via a provided script. Never in a file, never in Git, never logged. |
| Uninstall footprint? | Unregisters its one Scheduled Task and removes its own install directory (including its self-contained Python virtual environment). Never touches any other software, any other scheduled task, or the base Python installation. Config/state/logs are preserved by default and must be explicitly requested to also remove. |
