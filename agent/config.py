import json
import os
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent / "agent_config.json"

REQUIRED_FILE_KEYS = [
    "customer_id",
    "branch_id",
    "api_url",
    "raw_checkins_file",
    "raw_rejects_file",
    "processed_checkins_file",
    "processed_rejects_file",
    "status_file",
    "raw_acs_file",
    "processed_acs_file",
]


def _clean(value):
    if value is None:
        return None

    value = str(value).strip()
    if value == "":
        return None

    return value


def load_config():
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Missing config file: {CONFIG_FILE}")

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)

    missing = [
        key for key in REQUIRED_FILE_KEYS
        if _clean(config.get(key)) is None
    ]

    if missing:
        raise ValueError(
            "Missing required config keys: " + ", ".join(missing)
        )

    api_token = _clean(os.getenv("SORTVIEW_API_TOKEN"))
    if api_token is None:
        raise ValueError(
            "Missing API token. Set SORTVIEW_API_TOKEN as an environment variable."
        )

    config["api_token"] = api_token
    config["customer_id"] = int(config["customer_id"])
    config["branch_id"] = int(config["branch_id"])
    config["api_url"] = str(config["api_url"]).rstrip("/")

    return config
