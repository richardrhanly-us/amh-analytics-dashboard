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

## Release conclusion

SortView Collector 1.0.13 passed:

- release-readiness validation
- frozen-build verification
- release-manifest verification
- ZIP extraction verification
- extracted-runtime version verification

Production installation validation is intentionally not recorded yet; it will be completed after deployment to the AMH.

The canonical ZIP fingerprint above is the release baseline for Collector 1.0.13.
