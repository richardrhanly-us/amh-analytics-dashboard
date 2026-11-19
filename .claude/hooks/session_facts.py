"""Claude Code SessionStart hook for SortView: puts the governing rules in front of every session.

Prints the manifest's "Last reviewed" line and its "Production deployment facts" section, read fresh from
docs/PROJECT_MANIFEST.md, so a session starts from the file and not from memory. What it prints becomes session
context. If the section cannot be found it says so loudly instead of printing nothing.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HEADING = "## Production deployment facts"


def facts(manifest: Path) -> str:
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError:
        return f"WARNING: {manifest} could not be read. Treat production state as UNKNOWN and stop before any " \
               "production-affecting action."
    reviewed = next((line for line in lines if line.startswith("Last reviewed:")), "Last reviewed: (not found)")
    try:
        start = lines.index(HEADING)
    except ValueError:
        return f"WARNING: '{HEADING}' is missing from {manifest}. Treat production state as UNKNOWN and stop before " \
               "any production-affecting action."
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    section = "\n".join(line for line in lines[start:end] if line.strip() != "---").strip()
    return f"{reviewed.split(';')[0]}\n\n{section}"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):  # a Windows console defaults to cp1252, which cannot print the manifest
        sys.stdout.reconfigure(encoding="utf-8")
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
    print("SortView governing rules (CLAUDE.md, 'The manifest governs'): docs/PROJECT_MANIFEST.md and CLAUDE.md are hard "
          "rules. Stop and ask before anything that could potentially violate them. A merge to main deploys the "
          "backend. Production-affecting actions need a pre-action compliance report and explicit operator approval. "
          "The facts below are dated observations: re-verify the ones an action depends on.\n")
    print(facts(root / "docs" / "PROJECT_MANIFEST.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
