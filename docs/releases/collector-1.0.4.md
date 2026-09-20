# SortView Collector 1.0.4 Release Baseline

## Source baseline

- Release: `1.0.4`
- Source branch at release validation: `main`
- Source commit: `9d6d0fa`
- Release readiness: PASS
- `collector.__version__`: `1.0.4`

## Canonical release artifact

Path used during release validation:

`C:\SortView\ReleaseBuilds\1.0.4\SortViewCollector-1.0.4.zip`

- Size: `34,319,755 bytes`
- SHA-256: `ABAD1ED73347BDB9178DB5E266C0A56D705EB050BA3A4BA4E8491A8D1B6EF08C`

This exact ZIP is the canonical SortView Collector 1.0.4 release artifact.

Do not rebuild or recompress this release and treat the result as the same artifact unless a new fingerprint and validation record are produced.

## Frozen runtime

Frozen executable:

`runtime\SortViewCollector.exe`

- Size: `10,503,049 bytes`
- SHA-256: `E0E5F1B431FE48E8E03F934FEE18E5D5D7F28168E725D07146B994EB7176114D`
- `version` output: `1.0.4`
- Exit code: `0`
- Frozen runtime file count: `728`

## Release bundle verification

The release builder produced a frozen bundle containing:

- Manifest entries: `737`
- Actual payload files: `737`
- Hash/size verification errors: `0`
- Extra/unlisted files: `0`
- `setup.ps1`: present
- Extracted runtime version: `1.0.4`
- Extracted runtime exit code: `0`

The final ZIP was extracted to an independent validation directory and verified against its manifest after compression.

## Clean-install validation

Validation machine:

`LAPTOP-FOUPPS00`

The machine was reset before testing:

- Existing Collector install: absent
- Existing Collector data root: absent
- Existing Scheduled Task: absent
- Existing machine API token: absent

The installation was performed from the canonical 1.0.4 ZIP using only the guided customer-facing entry point:

`setup.ps1`

The technician supplied only:

- the three source-file paths
- a one-time enrollment code

No customer ID, branch ID, installation ID, permanent API token, database credentials, Python installation, or repository checkout was supplied manually.

### Guided setup results

- Release manifest verification: PASS
- Frozen bundle detection: PASS
- Release version verification: PASS
- Existing-install safety check: PASS
- Source-file validation: PASS
- Hosted API connectivity: PASS
- One-time enrollment: PASS
- Machine-scope API token storage: PASS
- Enrollment recovery record creation/verification: PASS
- Automatic customer/branch/installation identity configuration: PASS
- Frozen Collector installation: PASS
- Interactive preflight: PASS
- SYSTEM-context preflight: PASS
- HTTPS-only/no direct database dependency check: PASS
- Bootstrap/start cursor seeding: PASS
- Scheduled Task registration: PASS
- Scheduled Task default state: Disabled

## Controlled runtime validation

The registered Scheduled Task was deliberately enabled for one controlled run.

Results:

- Task principal: `SYSTEM`
- `LastTaskResult`: `0`
- Collector status: `completed_no_new_rows`
- Checkins rows: `0`
- Rejects rows: `0`
- ACS rows: `0`
- Uploaded checkins: `0`
- Uploaded rejects: `0`
- Uploaded ACS: `0`
- Missing sources: none
- Rotated sources: none
- Truncated sources: none

The task was disabled again immediately after validation.

## Server-side validation state

Test installation used:

- SaaS organization ID: `2`
- Operational customer ID: `3`
- Branch ID: `2`
- Installation ID: `1`

Following validation cleanup:

- Installation status: `inactive`
- Collector version: `1.0.4`
- Test agent token ID `12`: inactive
- Consumed enrollment-code record retained as audit history

## Database migration

Collector enrollment required Alembic migration:

`b4e91d7a3c58_add_collector_enrollment_codes.py`

Validation confirmed:

- database Alembic revision: `b4e91d7a3c58 (head)`
- `public.collector_enrollment_codes` exists

## Release conclusion

SortView Collector 1.0.4 passed:

- release-readiness validation
- frozen-build verification
- release-manifest verification
- ZIP extraction verification
- one-code clean installation
- interactive and SYSTEM preflight
- bootstrap
- scheduled-task registration
- controlled SYSTEM execution
- post-test credential and installation cleanup

The canonical ZIP fingerprint above is the release baseline for Collector 1.0.4.