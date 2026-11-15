"""C6a R4: make one batch of synthetic Tech Logic records for a qualification VM.

Runs on the HOST, in this repository's virtual environment. It never runs on the VM and never contacts a server.

The records are the repository's synthetic `full_corpus()` (tests/collector_v2_support.py): CANARY values only, never
customer data. Each record is one second after the batch's start time, so every batch has new event keys. The start
time is a few minutes ago in the library's Collector time zone, because Tech Logic writes naive local times.

The bytes are generated here, in Python, and copied to the VM as files. The VM only appends them
(Add-SyntheticBatch.ps1). Nothing is typed into PowerShell, so the 2026-11-12 harness defect (PowerShell treating
`$s` and `$S` as one variable, which dropped every 0x02 separator) cannot recur.

USAGE
    .venv\\Scripts\\python scripts\\c6a_r4\\make_synthetic_batch.py --label A1 --out <folder> [--zone America/Chicago]

It writes <folder>\\<label>\\ with acs.part, checkins.part, rejects.part and batch.json (sizes, SHA-256 and the expected
server counts). It refuses to overwrite an existing batch folder.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]

# Batch part -> the Tech Logic file it is appended to (the standard names under C:\TLCFinalDlls).
TARGETS = {"acs": "ACS Log.txt", "checkins": "Checkins.txt", "rejects": "Rejects.txt"}

# What one batch became on the server in the earlier qualifications (G3 on 2026-10-25: 7 ACS, 3 check-ins, 1 reject;
# G2 on 2026-11-12: ACS 5 hold, 1 non_hold_101, 1 other_code10). The patron-card records are dropped by the Collector.
# A different count is a finding to explain, never a number to adjust.
EXPECTED_EVENTS = {
    "acs_item_events": {"hold": 5, "non_hold_101": 1, "other_code10": 1},
    "checkin_events": 3,
    "reject_events": 1,
}

_LABEL = re.compile(r"^[A-Za-z0-9-]{1,40}$")


def _corpus_module() -> Any:
    """The shared synthetic fixture. It imports the Collector package, so the repository root must be importable."""
    for path in (REPO_ROOT, REPO_ROOT / "tests"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    return importlib.import_module("collector_v2_support")


def build_batch(minutes_ago: float, zone: str) -> tuple[datetime, dict[str, bytes]]:
    """The batch's naive local start time and the bytes of each part."""
    support = _corpus_module()
    base = support.minutes_ago(minutes_ago, zone=zone)
    corpus = support.full_corpus(base)
    return base, {name: "".join(corpus[name]).encode("utf-8") for name in TARGETS}


def check_parts(parts: dict[str, bytes]) -> list[str]:
    """Problems that would make the batch unusable. Every line must end with a line break, and every ACS line
    must have its two 0x02 separators (the parser skips a line without them)."""
    problems = []
    for name, data in parts.items():
        if not data or not data.endswith(b"\n"):
            problems.append(f"{name}: empty, or does not end with a line break")
    for number, line in enumerate(parts.get("acs", b"").splitlines(), start=1):
        separators = line.count(b"\x02")
        if separators != 2:
            problems.append(f"acs line {number}: {separators} separators (0x02), expected 2")
    return problems


def write_batch(out: Path, label: str, zone: str, minutes_ago: float) -> Path:
    if not _LABEL.match(label):
        raise ValueError("the label must be 1-40 letters, digits or hyphens")
    folder = out / label
    if folder.exists():
        raise FileExistsError(f"{folder} already exists; every batch gets a new label")
    base, parts = build_batch(minutes_ago, zone)
    problems = check_parts(parts)
    if problems:
        raise ValueError("; ".join(problems))

    folder.mkdir(parents=True)
    files = {}
    for name, data in parts.items():
        part = f"{name}.part"
        (folder / part).write_bytes(data)
        files[name] = {
            "part": part,
            "target": TARGETS[name],
            "bytes": len(data),
            "lines": data.count(b"\n"),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    manifest = {
        "label": label,
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "zone": zone,
        "first_record_local": base.isoformat(sep=" "),
        "files": files,
        "expected_events": EXPECTED_EVENTS,
    }
    (folder / "batch.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return folder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--label", required=True, help="a new name for this batch, e.g. A1 or B2")
    parser.add_argument("--out", required=True, type=Path, help="the folder that holds the batches")
    parser.add_argument("--zone", default="America/Chicago", help="the library's Collector time zone")
    parser.add_argument("--minutes-ago", type=float, default=2.0, help="how long ago the first record was written")
    args = parser.parse_args(argv)
    try:
        folder = write_batch(args.out, args.label, args.zone, args.minutes_ago)
    except (ValueError, FileExistsError) as problem:
        print(f"Not written: {problem}", file=sys.stderr)
        return 1
    manifest = json.loads((folder / "batch.json").read_text(encoding="utf-8"))
    print(f"Batch {args.label} written to {folder}")
    for name, entry in manifest["files"].items():
        print(f"  {name:9} {entry['lines']:3} lines {entry['bytes']:6} bytes -> {entry['target']}")
    print(f"  first record (local, {args.zone}): {manifest['first_record_local']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
