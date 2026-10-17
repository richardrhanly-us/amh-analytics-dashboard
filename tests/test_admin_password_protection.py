"""No tracked settings file carries an admin password.

The legacy Admin Settings lock (an extra "admin password" on the retired customer Streamlit app) was once kept in
PLAINTEXT in settings JSON, including a tracked settings file. The lock and its code are gone; this keeps any tracked
settings JSON under src/ (src/branch_settings.json today) from carrying an admin password or a `security` block again.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_tracked_settings_file_carries_an_admin_password():
    offenders = []
    for base in (ROOT / "src",):
        for path in base.rglob("*.json"):
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
            if "admin_password" in text or (isinstance(data, dict) and "security" in data):
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []
