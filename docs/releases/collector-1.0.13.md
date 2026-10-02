# SortView Collector 1.0.13 Release Baseline

## Source baseline

- Release: `1.0.13`
- Source branch at release validation: `release/collector-1.0.13`
- Source commit: `ab01074`
- Release readiness: PASS
- `collector.__version__`: `1.0.13`

## Canonical release artifact

Path used during release validation:

`C:\SortView\ReleaseBuilds\1.0.13\SortViewCollector-1.0.13.zip`

- Size: `34,964,710 bytes`
- SHA-256: `EB389BF6EE9C1E274CBE8B157BB6BFBBF9F240B0BBBD59629D398A8536590450`

This exact ZIP is the canonical SortView Collector 1.0.13 release artifact.

Do not rebuild or recompress this release and treat the result as the same artifact unless a new fingerprint and validation record are produced.

## Frozen runtime

Frozen executable:

`runtime\SortViewCollector.exe`

- Size: `10,693,655 bytes`
- SHA-256: `BFCBBFE7D982CE2654C2CC9A069DCF1ABFDD00076A41C5A782149A74BAC66882`
- `version` output: `1.0.13`
- Exit code: `0`
- Frozen runtime file count: `1334`

## Release bundle verification

The release builder produced a frozen bundle containing:

- Manifest entries: `1350`
- Actual payload files: `1350`
- Hash verification errors: `0`
- Extra/unlisted files: `0`
- `setup.ps1`: present
- Extracted runtime version: `1.0.13`
- Extracted runtime exit code: `0`

The final ZIP was extracted to an independent validation directory and verified against its manifest after compression.

## Production installation validation

Validation machine:

`NewBraunfelsSorterPC`

Production update results:

- Existing frozen runtime updated in place from the canonical 1.0.13 ZIP
- Release manifest verification: PASS
- Prior runtime backup created at `C:\SortView\Collector.backup-20261002-081805`
- New frozen runtime preflight: PASS
- SYSTEM-context preflight: PASS
- DPAPI API token source: PASS
- Hosted API connectivity and authentication: PASS
- Tenant scope: `customer_id=1`, `branch_id=1`
- Installation ID: `2`
- HTTPS-only / no direct database dependency: PASS
- Installed manifest verification: `1334` files, no unexpected release-managed files
- Installed runtime `version` output: `1.0.13`
- Scheduled Task state after update: `Ready`
- First normal scheduled run: `10/2/2026 8:27:27 AM`
- First normal scheduled run result: `0`
- Next scheduled run: `10/2/2026 8:42:42 AM`

Dashboard validation after the first normal 1.0.13 run:

- Last Collector Run: populated
- Next Scheduled Run: populated
- Latest Run Duration: `12.49 seconds`
- Collector Schedule: `Healthy`
- Latest Result: `Contract v2 collector reporting healthy`
- Status Code: `healthy`

## Release conclusion

SortView Collector 1.0.13 passed:

- release-readiness validation
- frozen-build verification
- release-manifest verification
- ZIP extraction verification
- extracted-runtime version verification

Production installation validation completed successfully on the production AMH.

The canonical ZIP fingerprint above is the release baseline for Collector 1.0.13.
