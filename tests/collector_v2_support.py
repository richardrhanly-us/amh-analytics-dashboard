"""Shared helpers for the Contract v2 collector tests (not a test module).

Everything here is SYNTHETIC. Every raw value is a recognizable CANARY string so a test can search every byte the collector wrote for it.
Timestamps that reach a real server are built from the real clock; the transformation tests inject their own clock.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from collector.v2_config import V2Config, load_v2_config
from collector.v2_identity import derive_subkeys
from collector.v2_keys import SecretStoreError
from collector.v2_patrons import PatronCache
from collector.v2_rules import compile_rules
from collector.v2_transform import TransformContext

KEY_ID = "3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e"
OTHER_KEY_ID = "5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d"
MASTER = bytes(range(32))
ZONE = "America/Chicago"
API_URL = "http://testserver"
TOKEN = "CANARY-V2-COLLECTOR-TOKEN-4001"

# --- raw canaries: values that must never be found in anything the collector writes --------------------------------------------
BARCODE_HOLD = "CANARY-BARCODE-HOLD"
BARCODE_ILL = "CANARY-BARCODE-ILL"
BARCODE_PROG = "CANARY-BARCODE-PROG"
BARCODE_COLL = "CANARY-BARCODE-COLL"
BARCODE_PATTERN = "CANARY-BARCODE-DA"
BARCODE_NON_HOLD = "CANARY-BARCODE-NONHOLD"
BARCODE_OTHER10 = "CANARY-BARCODE-OTHER10"
BARCODE_CI = "CANARY-BARCODE-CHECKIN"
BARCODE_REJ = "CANARY-BARCODE-REJECT"
BARCODE_LONG = "31234000123456"                     # 14 digits: an item barcode that merely LOOKS like a card number
PATRON_ADULT, PATRON_ILL, PATRON_PROG, PATRON_COLL = "CANARY-PATRON-A", "CANARY-PATRON-B", "CANARY-PATRON-C", "CANARY-PATRON-D"
PATRON_CARD = "CANARY-PATRON-CARD-99"
NAME_ADULT, NAME_ILL = "CANARY-NAME ONE", "CANARY-NAME TWO"
NAME_PROG, NAME_COLL = "CANARY PROGRAMMING ACCOUNT", "CANARY CATALOGING ACCOUNT"
PATTERN_COLL = "DACANARY DEPARTMENT PATTERN"
TITLE = "CANARY-TITLE"
ADDRESS, MAIL = "CANARY-ADDR-1 MAIN ST", "CANARY-MAIL@example.invalid"
CI_TITLE, CI_CALLNO, CI_COLLECTION, CI_SHELF = "CANARY-CI-TITLE", "CANARY-CI-CALLNO", "CANARY-CI-COLLECTION", "CANARY-CI-SHELF"
CI_MESSAGE, CI_FLAG = "CANARY-CI-MESSAGE", "CANARY-CI-FLAG"
REJECT_TEXT = "CANARY-RAW-REJECT item not found CANARY-RAW-DETAIL"
UNMAPPED_DESTINATION = "CANARY-UNMAPPED-DEST"

RAW_CANARIES = (
    BARCODE_HOLD, BARCODE_ILL, BARCODE_PROG, BARCODE_COLL, BARCODE_PATTERN, BARCODE_NON_HOLD, BARCODE_OTHER10, BARCODE_CI, BARCODE_REJ,
    BARCODE_LONG, PATRON_ADULT, PATRON_ILL, PATRON_PROG, PATRON_COLL, PATRON_CARD, NAME_ADULT, NAME_ILL, NAME_PROG, NAME_COLL,
    PATTERN_COLL, TITLE, ADDRESS, MAIL, CI_TITLE, CI_CALLNO, CI_COLLECTION, CI_SHELF, CI_MESSAGE, CI_FLAG, REJECT_TEXT,
    "CANARY-RAW", "CANARY-", UNMAPPED_DESTINATION,
)

RULES_DOC = {
    "schema_version": 1,
    "destinations": [],
    "branch_services_names": [NAME_PROG],
    "collection_services_names": [NAME_COLL],
    "branch_services_da_patterns": [],
    "collection_services_da_patterns": [PATTERN_COLL],
}


# --- Tech Logic line builders ---------------------------------------------------------------------------------------------------

def _stamp(local: datetime) -> tuple[str, str]:
    return local.strftime("%m/%d/%Y"), local.strftime("%I:%M:%S %p")


def acs_line(local: datetime, message: str) -> str:
    date, time = _stamp(local)
    return f"{date}\x02{time}\x02{message}\n"


def patron_message(patron_id: str, name: str, patron_type: str) -> str:
    return f"64 |AA{patron_id}|AE{name}|PT{patron_type}|BD{ADDRESS}|BE{MAIL}"


def item_message(barcode: str, patron_id: str | None, destination: str | None, prefix: str = "101YNY", extra: str = "",
                 title: str = TITLE) -> str:
    return f"{prefix}|AB{barcode}|AJ{title} {barcode}|AA{patron_id or ''}|CT{destination or ''}{extra}"


def checkin_line(local: datetime, barcode: str, destination: str = "1", bin_code: str = "3", *, title: str = CI_TITLE,
                 call_number: str = CI_CALLNO, collection: str = CI_COLLECTION, shelf: str = CI_SHELF,
                 message: str = CI_MESSAGE, flag: str = CI_FLAG) -> str:
    date, time = _stamp(local)
    return f"{title}|{barcode}|{collection}|{call_number}|{shelf}|{destination}|FALSE|{message}|{bin_code}|{flag}|||{date}|{time}\n"


def reject_line(local: datetime, barcode: str, error_message: str = REJECT_TEXT) -> str:
    date, time = _stamp(local)
    return f"{barcode}|{error_message}|{date}|{time}\n"


def naive(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    """A NAIVE local wall-clock time, which is what Tech Logic writes."""
    return datetime(year, month, day, hour, minute, second)  # noqa: DTZ001 -- naive on purpose: Tech Logic timestamps have no zone


def local_naive(instant: datetime, zone: str = ZONE) -> datetime:
    return instant.astimezone(ZoneInfo(zone)).replace(tzinfo=None, microsecond=0)


def minutes_ago(minutes: float, *, now: datetime | None = None, zone: str = ZONE) -> datetime:
    """A naive local Tech Logic time for `minutes` before now (the real clock unless `now` is given)."""
    return local_naive((now or datetime.now(UTC)) - timedelta(minutes=minutes), zone)


def full_corpus(base: datetime) -> dict[str, list[str]]:
    """Lines for every source. `base` is a naive local time; each record is one second apart so every event has its own event_key."""
    tick = iter(range(1000))

    def at() -> datetime:
        return base + timedelta(seconds=next(tick))

    acs = [
        acs_line(at(), patron_message(PATRON_ADULT, NAME_ADULT, "ADULT")),
        acs_line(at(), patron_message(PATRON_ILL, NAME_ILL, "ILL")),
        acs_line(at(), patron_message(PATRON_PROG, NAME_PROG, "STAFF")),
        acs_line(at(), patron_message(PATRON_COLL, NAME_COLL, "STAFF")),
        acs_line(at(), patron_message(PATRON_CARD, "CANARY-NAME CARD", "ADULT")),
        acs_line(at(), item_message(BARCODE_HOLD, PATRON_ADULT, "Main")),
        acs_line(at(), item_message(BARCODE_ILL, PATRON_ILL, "Westside")),
        acs_line(at(), item_message(BARCODE_PROG, PATRON_PROG, "Main")),
        acs_line(at(), item_message(BARCODE_COLL, PATRON_COLL, "Main")),
        acs_line(at(), item_message(BARCODE_PATTERN, None, "Main", extra=f"|{PATTERN_COLL}|")),
        acs_line(at(), item_message(BARCODE_NON_HOLD, PATRON_ADULT, "Main", prefix="101NNY")),
        acs_line(at(), item_message(BARCODE_OTHER10, PATRON_ADULT, "Main", prefix="100NUN")),
        acs_line(at(), item_message(PATRON_CARD, None, "Main")),          # a patron card scanned as an "item"
    ]
    checkins = [
        checkin_line(at(), BARCODE_CI, "Westside", "3"),
        checkin_line(at(), BARCODE_LONG, UNMAPPED_DESTINATION, "abc"),
        checkin_line(at(), PATRON_CARD, "1", "1"),                       # patron card at check-in: dropped
        checkin_line(at(), "", "1", "2"),                                # no barcode: kept, item_key absent
    ]
    rejects = [reject_line(at(), BARCODE_REJ), reject_line(at(), PATRON_CARD, "some acs failure")]
    return {"acs": acs, "checkins": checkins, "rejects": rejects}


def write_lines(path: Path, lines: list[str], *, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab" if append else "wb") as handle:
        handle.write("".join(lines).encode("utf-8"))


# --- fakes ------------------------------------------------------------------------------------------------------------------------

class FakeStore:
    """A SecretStore for the platform-neutral tests (the DPAPI store has its own Windows-only tests)."""

    def __init__(self, master: bytes = MASTER, bound_key_id: str = KEY_ID, *, present: bool = True, acl: str = "protected"):
        self.master, self.bound_key_id, self.present, self.acl = master, bound_key_id, present, acl
        self.loads = 0  # how many times the secret was actually loaded

    def exists(self) -> bool:
        return self.present

    def load(self, expected_key_id: str) -> bytes:
        self.loads += 1
        if not self.present:
            raise SecretStoreError("secret_missing")
        if expected_key_id != self.bound_key_id:
            raise SecretStoreError("secret_key_mismatch")
        return self.master

    def create(self, key_id: str) -> None:  # pragma: no cover - not used
        raise NotImplementedError

    def acl_state(self) -> str:
        return self.acl


@dataclass
class Reply:
    status_code: int
    body: Any = None

    def json(self) -> Any:
        if self.body is None:
            raise ValueError("no body")
        return self.body


@dataclass
class ScriptedSession:
    """Answers POSTs from a script (a list of Reply / exception / callable) and records what it was sent."""

    script: list[Any] = field(default_factory=list)          # replies for /v2/upload, in order
    default: Any = None                                       # the upload reply once the script is exhausted (None = success)
    status_reply: Any = None                                  # the /v2/status reply (None = success)
    calls: list[tuple[str, Any]] = field(default_factory=list)
    headers: list[Any] = field(default_factory=list)

    def post(self, url: str, json: Any = None, headers: Any = None, timeout: Any = None) -> Any:
        self.calls.append((url, json))
        self.headers.append(headers)
        if url.endswith("/v2/status"):
            item = self.status_reply
        else:
            item = self.script.pop(0) if self.script else self.default
        if callable(item):
            item = item(url, json)
        if isinstance(item, BaseException):
            raise item
        if item is None:
            return Reply(200, {"status": "success"})
        return item

    def uploads(self) -> list[dict]:
        return [body for url, body in self.calls if url.endswith("/v2/upload")]

    def statuses(self) -> list[dict]:
        return [body for url, body in self.calls if url.endswith("/v2/status")]


def success_reply(body: dict | None = None) -> Reply:
    return Reply(200, {"status": "success", **(body or {})})


# --- the configuration and context --------------------------------------------------------------------------------------------

def write_config(root: Path, *, contract_mode: str | None = "v2", v2_extra: dict | None = None, sources: bool = True,
                 timezone: str = ZONE) -> Path:
    """A collector config JSON (no secret in it) under `root`, with v2 settings pointing at `root`."""
    root.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = {
        "customer_id": 10, "branch_id": 1, "api_url": API_URL,
        "state_path": str(root / "state" / "state.json"), "status_path": str(root / "state" / "status.json"),
        "log_path": str(root / "logs" / "collector.log"),
        "sources": [{"name": name, "path": str(root / "tech" / f"{name}.txt")} for name in ("acs", "checkins", "rejects")] if sources else [],
        "v2": {"key_id": KEY_ID, "timezone": timezone, "request_interval_seconds": 0, **(v2_extra or {})},
    }
    if contract_mode is not None:
        document["contract_mode"] = contract_mode
    if not sources:
        document["sources"] = [{"name": "acs", "path": str(root / "tech" / "acs.txt")}]
    path = root / "config.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def write_rules(v2: V2Config, document: dict | None = None) -> None:
    v2.rules_path.parent.mkdir(parents=True, exist_ok=True)
    v2.rules_path.write_text(json.dumps(document if document is not None else RULES_DOC), encoding="utf-8")


def load_v2(config_path: Path) -> V2Config:
    v2 = load_v2_config(config_path, require=True)
    assert v2 is not None
    return v2


def make_context(tmp_path: Path, *, now: datetime | None = None, rules_doc: dict | None = None, master: bytes = MASTER,
                 zone: str = ZONE) -> tuple[TransformContext, PatronCache]:
    now = now or datetime.now(UTC)
    keys = derive_subkeys(master)
    rules = compile_rules(rules_doc if rules_doc is not None else RULES_DOC, keys)
    cache = PatronCache(tmp_path / "patrons.db", today=now.date())
    ctx = TransformContext(keys=keys, rules=rules, ruleset_id="0a1b2c3d-4e5f-4a6b-9c7d-8e9f0a1b2c3d", cache=cache,
                           zone=ZoneInfo(zone), now=now)
    return ctx, cache


def run_logger(log_path: Path) -> logging.Logger:
    """A logger writing to a file under the test root, so a byte sweep of the root covers everything the run logs."""
    logger = logging.getLogger(f"collector-v2-test-{log_path}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.addHandler(logging.FileHandler(log_path, encoding="utf-8"))
    return logger


# --- byte sweeps ---------------------------------------------------------------------------------------------------------------------

def every_byte_under(root: Path) -> bytes:
    """The concatenated bytes of EVERY file the collector writes under `root` (state, status, cache database, quarantine, logs ...). Only the
    Tech Logic INPUT files (`tech/`) and the operator-authored rules file (`config/`) are excluded: they are inputs, not collector output."""
    return b"".join(path.read_bytes() for path in sorted(root.rglob("*")) if path.is_file() and not {"tech", "config"} & set(path.relative_to(root).parts))


def canary_variants(values: tuple[str, ...] = RAW_CANARIES) -> list[bytes]:
    """Each canary as UTF-8 bytes and as UTF-16-LE bytes (a text encoding a stray writer might use)."""
    out: list[bytes] = []
    for value in values:
        out.extend((value.encode("utf-8"), value.encode("utf-16-le")))
    return out


def find_leaks(blob: bytes, values: tuple[str, ...] = RAW_CANARIES) -> list[str]:
    """The canaries present in `blob`, compared case-insensitively (a lower-cased leak is still a leak)."""
    lowered = blob.lower()
    return [value for value in values if value.lower().encode("utf-8") in lowered or value.lower().encode("utf-16-le") in lowered]
