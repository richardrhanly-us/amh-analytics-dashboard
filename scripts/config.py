import json
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent / "agent_config.json"


def load_config():
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Missing config file: {CONFIG_FILE}")

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)