"""Contract v2: the local HMAC master secret (docs/collector-v2.md).

ONE 32-byte secret, generated on this machine, is bound to ONE server-issued `key_id` (a non-secret UUID). It is
  * generated locally (`secrets.token_bytes`) and never sent, logged, printed, put in config, an environment variable or the repo;
  * stored only as a Windows DPAPI blob, MACHINE scope (`CRYPTPROTECT_LOCAL_MACHINE`), so the Scheduled Task's SYSTEM account can
    open it and a copy of the file is useless on another machine; with a fixed application entropy;
  * kept in a folder whose ACL is Administrators + SYSTEM only (inheritance cut). Machine-scope DPAPI alone would let ANY local
    process decrypt; the ACL is what closes that. The ACL is CHECKED, and only a verified-protected ACL is accepted: a known-insecure ACL
    (`secret_exposed`) and an ACL that cannot be read or verified (`secret_acl_unverified`) BOTH fail closed, when loading the secret, when
    initializing it, and at the start of every run;
  * never written to the server. There is no escrow and no recovery: losing it means a NEW key_id from the server (and, by design,
    a break in item continuity).

The blob binds the secret to its key_id. A blob for another key_id is `secret_key_mismatch` and the collector FAILS CLOSED.

Only ever imported for its behavior: it never returns the secret except to the caller that asked, and it has no `__repr__` that could
show one. Errors carry fixed codes, never values.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import re
import secrets
import subprocess  # nosec B404 - runs icacls with fixed arguments and a path we own
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .v2_events import validate_key_id
from .v2_identity import ALGORITHM_ID
from .v2_safe_errors import CollectorV2Error

CRYPTPROTECT_UI_FORBIDDEN = 0x1
CRYPTPROTECT_LOCAL_MACHINE = 0x4
_ENTROPY = b"SortView Collector v2 / DPAPI entropy / format 1"  # application binding, not a secret
BLOB_FORMAT = 1
SECRET_BYTES = 32

SYSTEM_SID, ADMINISTRATORS_SID = "S-1-5-18", "S-1-5-32-544"
_ALLOWED_SIDS = {"SY", "BA", SYSTEM_SID, ADMINISTRATORS_SID}


class SecretStoreError(CollectorV2Error):
    """Fixed codes: secret_missing, secret_unreadable, secret_key_mismatch, secret_store_unavailable, secret_exists,
    secret_exposed (a known-insecure ACL), secret_acl_unverified (an ACL that could not be read or verified), secret_dir_not_protected."""


def require_protected(acl_state: str) -> None:
    """Accepts ONLY a verified-protected ACL. `exposed` and anything that is not verifiably protected (`unknown`, an unreadable ACL, an
    unexpected value) fail closed with a fixed code and no detail."""
    if acl_state == "protected":
        return
    raise SecretStoreError("secret_exposed" if acl_state == "exposed" else "secret_acl_unverified")


class SecretStore(Protocol):
    def exists(self) -> bool: ...
    def load(self, expected_key_id: str) -> bytes: ...
    def create(self, key_id: str) -> None: ...
    def acl_state(self) -> str: ...  # "protected" | "exposed" | "unknown"


# --- DPAPI ----------------------------------------------------------------------------------------------------------------------

def _require_windows() -> None:
    if sys.platform != "win32":
        raise SecretStoreError("secret_store_unavailable")


def _dpapi(function_name: str, data: bytes) -> bytes:
    """CryptProtectData / CryptUnprotectData, machine scope, with the fixed entropy. Raises SecretStoreError on any failure."""
    _require_windows()
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def to_blob(raw: bytes):
        buffer = ctypes.create_string_buffer(raw, len(raw))
        return DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    function = getattr(crypt32, function_name)
    blob_pointer = ctypes.POINTER(DataBlob)
    if function_name == "CryptProtectData":
        function.argtypes = [blob_pointer, wintypes.LPCWSTR, blob_pointer, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer]
    else:
        function.argtypes = [blob_pointer, ctypes.c_void_p, blob_pointer, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, blob_pointer]
    function.restype = wintypes.BOOL

    source, source_buffer = to_blob(data)
    entropy, entropy_buffer = to_blob(_ENTROPY)
    result = DataBlob()
    flags = CRYPTPROTECT_UI_FORBIDDEN | CRYPTPROTECT_LOCAL_MACHINE
    description = "SortView Collector v2" if function_name == "CryptProtectData" else None
    ok = function(ctypes.byref(source), description, ctypes.byref(entropy), None, None, flags, ctypes.byref(result))
    del source_buffer, entropy_buffer
    if not ok:
        raise SecretStoreError("secret_unreadable" if function_name == "CryptUnprotectData" else "secret_store_unavailable")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


def dpapi_protect(data: bytes) -> bytes:
    return _dpapi("CryptProtectData", data)


def dpapi_unprotect(blob: bytes) -> bytes:
    return _dpapi("CryptUnprotectData", blob)


# --- ACL (Administrators + SYSTEM only) -----------------------------------------------------------------------------------------

def protect_directory(path: str | Path, *, extra_principals: tuple[str, ...] = ()) -> None:
    """Restricts a folder to SYSTEM and Administrators, well-known SIDs (language independent), inheritance cut. `extra_principals`
    exists for TESTS only, so a standard-token test can still clean up; production passes none."""
    _require_windows()
    grants = [f"*{SYSTEM_SID}:(OI)(CI)F", f"*{ADMINISTRATORS_SID}:(OI)(CI)F", *(f"{name}:(OI)(CI)F" for name in extra_principals)]
    result = subprocess.run(  # nosec B603 B607 - fixed executable name and arguments; path is ours
        ["icacls", str(path), "/inheritance:r", "/grant:r", *grants], capture_output=True, check=False)
    if result.returncode != 0:
        raise SecretStoreError("secret_dir_not_protected")


def _dacl_of(path: Path) -> str | None:
    """The SDDL DACL line of a path (from `icacls /save`, which writes UTF-16-LE without a BOM), or None."""
    fd, tmp = tempfile.mkstemp(prefix="acl_", suffix=".sddl")
    os.close(fd)
    try:
        result = subprocess.run(["icacls", str(path), "/save", tmp], capture_output=True, check=False)  # nosec B603 B607 - fixed executable name and arguments; the path is ours
        if result.returncode != 0:
            return None
        raw = Path(tmp).read_bytes()
        text = raw.decode("utf-16-le", errors="replace") if b"\x00" in raw[:8] else raw.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if line.startswith("D:"):
                return line.strip()
        return None
    except OSError:
        return None
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def acl_state_of(path: str | Path, *, require_protected: bool) -> str:
    """"protected" if every ALLOW entry is SYSTEM or Administrators (and, for a folder, inheritance is cut); "exposed" if anyone else
    can reach it; "unknown" if the ACL could not be read (never guessed)."""
    if sys.platform != "win32":
        return "unknown"
    dacl = _dacl_of(Path(path))
    if dacl is None:
        return "unknown"
    head = dacl[2:].split("(", 1)[0]  # the DACL flags, e.g. "P" (protected: no inheritance from the parent)
    if require_protected and "P" not in head:
        return "exposed"
    entries = re.findall(r"\(([^)]*)\)", dacl)
    if not entries:
        return "unknown"
    for entry in entries:
        fields = entry.split(";")  # type ; flags ; rights ; object ; inherit-object ; trustee
        if len(fields) < 6:
            return "unknown"
        if fields[0].startswith("A") and fields[5] not in _ALLOWED_SIDS:
            return "exposed"  # an ALLOW entry for anyone but SYSTEM / Administrators
    return "protected"


class DpapiSecretStore:
    """The production store: a DPAPI machine-scope blob in an Administrators+SYSTEM-only folder."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.is_file()

    def acl_state(self) -> str:
        folder = acl_state_of(self.path.parent, require_protected=True)
        file_state = acl_state_of(self.path, require_protected=False) if self.exists() else "protected"
        if "exposed" in (folder, file_state):
            return "exposed"
        return "unknown" if "unknown" in (folder, file_state) else "protected"

    def create(self, key_id: str) -> None:
        """Generates the secret, protects it, writes it atomically. Refuses to overwrite: there is no rotation-in-place."""
        validate_key_id(key_id)
        if self.exists():
            raise SecretStoreError("secret_exists")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({
            "format": BLOB_FORMAT, "algorithm": ALGORITHM_ID, "key_id": key_id,
            "created": datetime.now(UTC).strftime("%Y-%m-%d"),
            "secret": base64.b64encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii"),
        }).encode("ascii")
        blob = dpapi_protect(payload)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".v2key.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def load(self, expected_key_id: str) -> bytes:
        """The secret, only if the blob is intact and bound to `expected_key_id`. Fails closed otherwise."""
        validate_key_id(expected_key_id)
        if not self.exists():
            raise SecretStoreError("secret_missing")
        require_protected(self.acl_state())  # before a single byte of the secret is read: an unverifiable ACL is not a reason to proceed
        blob = b""
        unreadable = False
        try:
            blob = self.path.read_bytes()
        except OSError:
            unreadable = True
        if unreadable:
            raise SecretStoreError("secret_unreadable")
        payload = dpapi_unprotect(blob)  # tampering, another machine, another entropy: SecretStoreError("secret_unreadable")

        # Every failure below is reported OUTSIDE the except block: the decrypted payload (and the JSONDecodeError that quotes it in
        # `.doc`) must not survive as an exception's __context__.
        bound_key, secret = "", b""
        malformed = False
        try:
            document = json.loads(payload)
            bound_key = document["key_id"]
            secret = base64.b64decode(document["secret"], validate=True)
            if document.get("format") != BLOB_FORMAT or len(secret) != SECRET_BYTES or not isinstance(bound_key, str):
                malformed = True
        except (ValueError, KeyError, TypeError, AttributeError):
            malformed = True
        del payload
        if malformed:
            raise SecretStoreError("secret_unreadable")
        if bound_key != expected_key_id:
            raise SecretStoreError("secret_key_mismatch")
        return secret


def initialise(store: SecretStore, key_id: str, *, protect_folder: bool = True) -> None:
    """One-time setup: create the folder, lock it down, verify it, then create the secret. Nothing is written if the folder cannot be
    protected. Prints nothing."""
    if isinstance(store, DpapiSecretStore) and protect_folder:
        store.path.parent.mkdir(parents=True, exist_ok=True)
        protect_directory(store.path.parent)
        require_protected(acl_state_of(store.path.parent, require_protected=True))  # nothing is written unless the folder is VERIFIED locked down
    store.create(key_id)
    if isinstance(store, DpapiSecretStore) and protect_folder:
        try:
            require_protected(store.acl_state())
        except SecretStoreError:
            store.path.unlink(missing_ok=True)  # never leave a secret behind that we could not verify is protected
            raise


# --- command line (core operator tool; installer integration comes later) -------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """`python -m collector.v2_keys init|check --config <path>`. Output is one fixed line; the secret is never shown."""
    parser = argparse.ArgumentParser(description="SortView Collector v2 -- local secret setup")
    parser.add_argument("command", choices=("init", "check"))
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)

    from .config import ConfigError
    from .v2_config import load_v2_config

    try:
        v2 = load_v2_config(args.config, require=True)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if v2 is None:  # unreachable with require=True; keeps the type honest without an assert
        return 2
    store = DpapiSecretStore(v2.secret_path)
    try:
        if args.command == "init":
            initialise(store, v2.key_id)
            print("secret created and protected (value not shown)")
        else:
            store.load(v2.key_id)  # verifies the ACL first, then the blob and its key_id binding: it raises on anything but "protected"
            print("secret present, bound to the configured key_id, and its ACL is verified protected")
        return 0
    except CollectorV2Error as exc:
        print(f"failed: {exc.code}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
