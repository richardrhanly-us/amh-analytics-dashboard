# SortView Production Baseline — 2026-09-18

## Purpose

This document records the known-good production baseline for SortView Collector v1 after migration from the source + virtual-environment runtime to the frozen standalone executable.

This baseline was validated end-to-end on September 18, 2026.

---

## Production Host

- Hostname: `NewBraunfelsSorterPC`
- Production role: Tech Logic UltraSort ingestion host
- Collector task: `SortView Collector`

---

## Production Runtime

Install root:

```text
C:\SortView\Collector
```

Live executable:

```text
C:\SortView\Collector\SortViewCollector.exe
```

SHA-256:

```text
2A9466BC65F1D3E22797B583401EC3F054B47C2861FA9E21ED0162C4659DFE12
```

Frozen runtime contents observed after cutover:

```text
C:\SortView\Collector\SortViewCollector.exe
C:\SortView\Collector\_internal\
C:\SortView\Collector\logs\
```

The live runtime does not contain:

```text
.venv\
collector\
agent\
```

The `logs\` directory under the install root contains parser-specific logs:

```text
parser.acs.log
parser.checkins.log
parser.rejects.log
```

The authoritative Collector application log remains:

```text
C:\ProgramData\SortViewCollector\logs\collector.log
```

---

## Production Data Root

```text
C:\ProgramData\SortViewCollector
```

Configuration:

```text
C:\ProgramData\SortViewCollector\config\collector_config.json
```

State:

```text
C:\ProgramData\SortViewCollector\data\state.json
```

Status:

```text
C:\ProgramData\SortViewCollector\data\status.json
```

Collector log:

```text
C:\ProgramData\SortViewCollector\logs\collector.log
```

The production API token is not stored in the configuration file. It is provided through the machine-scope `SORTVIEW_API_TOKEN` environment variable.

---

## Production Configuration

```text
customer_id = 1
branch_id   = 1
api_url     = https://sortview-app-2p336.ondigitalocean.app
```

Configured Tech Logic source files:

```text
checkins = C:\TLCFinalDlls\Checkins.txt
rejects  = C:\TLCFinalDlls\Rejects.txt
acs      = C:\TLCFinalDlls\ACS Log.txt
```

---

## Scheduled Task

Task:

```text
SortView Collector
```

Task action:

```text
Execute:
C:\SortView\Collector\SortViewCollector.exe

Arguments:
run --config "C:\ProgramData\SortViewCollector\config\collector_config.json"

Working directory:
C:\SortView\Collector
```

Principal:

```text
UserId    = SYSTEM
LogonType = ServiceAccount
RunLevel  = Highest
```

Scheduling and reliability settings:

```text
Cadence              = every 15 minutes
MultipleInstances    = IgnoreNew
ExecutionTimeLimit   = PT1H
RestartCount         = 3
RestartInterval      = PT5M
StartWhenAvailable   = True
```

Known-good task state after cutover:

```text
SortView Collector        = Enabled
SortView Canonical Agent  = Disabled
sortview-scheduler        = Disabled
```

Only `SortView Collector` is part of the active production ingestion path.

---

## Migration and Rollback Assets

Migration backup:

```text
C:\SortView\MigrationBackup-20260918-083302
```

This contains:

```text
Collector-source-runtime\
SortView-Collector-task.xml
DataSnapshot\
```

Staged frozen release:

```text
C:\SortView\Staging\SortViewCollector-1.0.0
```

Transferred release ZIP:

```text
C:\SortView\Incoming\SortViewCollector-1.0.0.zip
```

ZIP SHA-256:

```text
672AE42F29D25828D6238F8AAD3364633024C795487450670158A116C97E395C
```

Do not delete these rollback assets until the frozen runtime has demonstrated sufficient production stability.

---

## Preflight Validation

Interactive frozen preflight passed all checks:

```text
config_loads
source_paths_exist
source_paths_readable
state_dir_writable
status_dir_writable
log_dir_writable
api_token_visible
dns_resolution
https_tls_connection
api_authentication
token_scope_matches
no_direct_database_dependency
collector_runtime_imports
```

SYSTEM-context frozen preflight also passed all checks.

---

## Controlled Frozen Runtime Validation

A one-time temporary SYSTEM task was used to validate the frozen runtime before enabling the production schedule.

Result:

```text
LastTaskResult = 0
status         = completed
```

The controlled run processed:

```text
checkins = 20
rejects  = 0
acs      = 131
```

Production state advanced normally from the preserved existing offsets. No bootstrap was performed during the migration.

---

## First Scheduled Frozen Production Run

The first recurring scheduled frozen production run completed successfully.

Task result:

```text
LastRunTime        = 2026-09-18 08:57:57 local
LastTaskResult     = 0
NumberOfMissedRuns = 0
```

Status:

```text
status        = completed
checkins_rows = 6
rejects_rows  = 0
acs_rows      = 22
```

The source identities remained unchanged and offsets advanced normally.

---

## End-to-End Production Validation

The next scheduled production run at approximately 09:12 local completed successfully.

Task result:

```text
LastRunTime        = 2026-09-18 09:12:12 local
LastTaskResult     = 0
NumberOfMissedRuns = 0
```

Collector status:

```text
status        = completed
checkins_rows = 30
rejects_rows  = 3
acs_rows      = 98
```

State after that run:

```text
checkins offset = 2124124
rejects offset  = 34773
acs offset      = 9208008
```

The Streamlit production dashboard independently reflected the same run:

```text
New Checkins This Run = 30
New Rejects This Run  = 3
```

Dashboard totals changed from:

```text
Checkins: 66 -> 96
Rejects:   1 -> 4
Transit:   7 -> 10
Westside:  7 -> 10
```

This confirms the normal production path is functioning end-to-end:

```text
Tech Logic source files
    ->
SortViewCollector.exe
    ->
Windows Scheduled Task as SYSTEM
    ->
HTTPS API
    ->
Production database
    ->
Streamlit dashboard
```

---

## Known UI / Observability Follow-up Items

The following items were observed after the successful cutover and do not indicate ingestion failure:

1. `Last Pipeline Attempt` and `Last Successful Upload Run` appear to display UTC timestamps without conversion to local time.
2. The dashboard still reports `Continuous agent reporting healthy` even though Collector v1 is now the production ingestion path.
3. `Uploaded Checkins This Run` and `Uploaded Rejects This Run` display `0` while the dashboard correctly receives new records. The meaning or wiring of these fields should be reviewed.

These should be addressed separately from the production Collector migration.

---

## Known-Good Baseline Conclusion

As of September 18, 2026, SortView production ingestion is running successfully on the frozen standalone Collector v1 runtime.

The source + virtual-environment runtime is no longer part of the live production path.

The active production path has been validated at all of the following layers:

- executable integrity
- configuration
- source-file access
- SYSTEM execution
- API authentication
- API connectivity
- state continuity
- scheduled execution
- database ingestion
- Streamlit dashboard visibility

This document should be updated whenever the production runtime, executable hash, task definition, API endpoint, source paths, or deployment architecture changes.
