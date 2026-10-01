"""The Collector's API bearer token, stored as a Windows DPAPI blob -- the Collector's ONLY token source.

A Machine-scope environment variable (how the token was kept before 1.0.11) is readable by every local user and copied
into every process an interactive user starts, so the Collector no longer reads one at all. This module keeps the token
in ONE file, `<root>\\secrets\\api_token.dpapi` (the same folder as the v2 secret), and is the only code that writes or
reads it:

  * DPAPI MACHINE scope (`CRYPTPROTECT_LOCAL_MACHINE`), UI forbidden, with this module's OWN fixed entropy -- distinct
    from collector/v2_keys.py's, so neither blob can be mistaken for the other. Machine scope is required: the blob is
    written by an elevated administrator (install/update/rotation) and read both by the SYSTEM Scheduled Task and by the
    administrator's own interactive preflight. A copy of the file is useless on another machine.
  * Machine scope alone would let ANY local process decrypt the blob; the folder ACL (Administrators + SYSTEM only,
    inheritance cut) is what keeps an ordinary user from reading it. The ACL is CHECKED before a single byte is read, and
    after every write; anything that is not verifiably protected fails closed. This is NOT protection against a local
    Administrator or SYSTEM -- both can read the file and decrypt it, by design.
  * The payload is bound to the config's customer_id + branch_id, so a file for another tenant fails closed.
  * Unlike the v2 secret, the token is SUPPLIED (issued by the server) and may be OVERWRITTEN -- atomically -- because
    rotation is required.

Deliberately independent of collector/v2_keys.py: the v1 path (collector/config.py) must import no `collector.v2_*`
module, and that module's semantics (generate, key_id-bound, never overwrite, exactly 32 bytes) are wrong for a bearer
token. Errors carry fixed codes only -- never the token, a decrypted payload, or a path's contents.

Command line (also `SortViewCollector.exe api-token ...`): `api-token set|check --config <path>`. `set` reads the token
from STDIN only (a hidden prompt when interactive, a pipe when automated); there is no argument that could carry it.
"""

from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import os
import re
import secrets
import stat
import subprocess  # nosec B404 - runs icacls with fixed arguments and a path we own
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

CRYPTPROTECT_UI_FORBIDDEN = 0x1
CRYPTPROTECT_LOCAL_MACHINE = 0x4
_ENTROPY = b"SortView Collector API token / DPAPI entropy / format 1"  # application binding, not a secret
BLOB_FORMAT = 1
FILE_NAME = "api_token.dpapi"

# The shape of a server-issued token (secrets.token_urlsafe(32) today); the same rule setup-collector.ps1 applies to an
# enrollment response. Also keeps anything that could break an HTTP header out of the Authorization value.
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_\-]{20,256}")
_MAX_STDIN_CHARS = 4096

SYSTEM_SID, ADMINISTRATORS_SID = "S-1-5-18", "S-1-5-32-544"
_ALLOWED_SIDS = {"SY", "BA", SYSTEM_SID, ADMINISTRATORS_SID}


class ApiTokenStoreError(Exception):
    """Fixed codes only: token_missing, token_unreadable, token_tenant_mismatch, token_invalid, token_store_unavailable,
    token_exposed (a known-insecure ACL), token_acl_unverified (an ACL that could not be read or verified),
    token_dir_not_protected, token_not_writable (this process may not write the secrets folder -- e.g. not elevated)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def require_protected(acl_state: str) -> None:
    """Accepts ONLY a verified-protected ACL; `exposed` and anything unverifiable fail closed with a fixed code."""
    if acl_state == "protected":
        return
    raise ApiTokenStoreError("token_exposed" if acl_state == "exposed" else "token_acl_unverified")


def validate_token(token: object) -> str:
    if not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None:
        raise ApiTokenStoreError("token_invalid")
    return token


def _validate_tenant(customer_id: object, branch_id: object) -> None:
    for value in (customer_id, branch_id):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ApiTokenStoreError("token_invalid")


def default_path(state_path: str | Path) -> Path:
    """`<root>\\secrets\\api_token.dpapi`, root derived exactly as collector/v2_config.py derives the v2 secret's."""
    return Path(state_path).parent.parent / "secrets" / FILE_NAME


# --- DPAPI ----------------------------------------------------------------------------------------------------------------------

def _require_windows() -> None:
    if sys.platform != "win32":
        raise ApiTokenStoreError("token_store_unavailable")


def _dpapi(function_name: str, data: bytes) -> bytes:
    """CryptProtectData / CryptUnprotectData, machine scope, with this module's entropy. Raises ApiTokenStoreError on any
    failure."""
    _require_windows()
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def to_blob(raw: bytes):
        buffer = ctypes.create_string_buffer(raw, len(raw))
        return DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise ApiTokenStoreError("token_store_unavailable")

    crypt32 = win_dll("crypt32", use_last_error=True)
    kernel32 = win_dll("kernel32", use_last_error=True)
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
    description = "SortView Collector API token" if function_name == "CryptProtectData" else None
    ok = function(ctypes.byref(source), description, ctypes.byref(entropy), None, None, flags, ctypes.byref(result))
    del source_buffer, entropy_buffer
    if not ok:
        raise ApiTokenStoreError("token_unreadable" if function_name == "CryptUnprotectData" else "token_store_unavailable")
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
    """Restricts a folder to SYSTEM and Administrators, well-known SIDs (language independent), inheritance cut.
    `extra_principals` exists for TESTS only, so a standard-token test can still clean up; production passes none."""
    _require_windows()
    grants = [f"*{SYSTEM_SID}:(OI)(CI)F", f"*{ADMINISTRATORS_SID}:(OI)(CI)F", *(f"{name}:(OI)(CI)F" for name in extra_principals)]
    result = subprocess.run(  # nosec B603 B607 - fixed executable name and arguments; path is ours
        ["icacls", str(path), "/inheritance:r", "/grant:r", *grants], capture_output=True, check=False)
    if result.returncode != 0:
        raise ApiTokenStoreError("token_dir_not_protected")


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
    """"protected" if every ALLOW entry is SYSTEM or Administrators (and, for a folder, inheritance is cut); "exposed" if
    anyone else can reach it; "unknown" if the ACL could not be read (never guessed)."""
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


# --- the store --------------------------------------------------------------------------------------------------------------------

class DpapiTokenStore:
    """The production store: one DPAPI machine-scope blob in an Administrators+SYSTEM-only folder."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def __repr__(self) -> str:
        return f"DpapiTokenStore({str(self.path)!r})"

    def exists(self) -> bool:
        """True for a regular file. A path that cannot even be checked (e.g. access denied) is an error, never "absent"."""
        try:
            return self.path.is_file()
        except OSError:
            raise ApiTokenStoreError("token_unreadable") from None

    def _require_present(self) -> None:
        """token_missing ONLY when nothing is at the path; anything else that is not a readable regular file is
        token_unreadable -- so "absent" (store one) is never confused with "damaged or unreachable" (investigate). Both fail
        closed in collector/config.py; the distinction is diagnostic only."""
        try:
            mode = os.stat(self.path).st_mode
        except (FileNotFoundError, NotADirectoryError):
            raise ApiTokenStoreError("token_missing") from None
        except OSError:
            raise ApiTokenStoreError("token_unreadable") from None
        if not stat.S_ISREG(mode):
            raise ApiTokenStoreError("token_unreadable")

    def acl_state(self) -> str:
        folder = acl_state_of(self.path.parent, require_protected=True)
        file_state = acl_state_of(self.path, require_protected=False) if self.exists() else "protected"
        if "exposed" in (folder, file_state):
            return "exposed"
        return "unknown" if "unknown" in (folder, file_state) else "protected"

    def save(self, customer_id: int, branch_id: int, token: str) -> None:
        """Encrypts and writes the token, atomically REPLACING any existing file (rotation). Only the DPAPI blob ever
        touches the disk: the temp file holds ciphertext, and neither its name nor the final name derives from the token."""
        _validate_tenant(customer_id, branch_id)
        validate_token(token)
        payload = json.dumps({
            "format": BLOB_FORMAT, "customer_id": customer_id, "branch_id": branch_id, "token": token,
            "updated": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }).encode("ascii")
        blob = dpapi_protect(payload)
        del payload
        # ONE exclusive create, never tempfile.mkstemp: on Windows mkstemp treats PermissionError as a name collision and
        # retries up to os.TMP_MAX (2**31) times -- a process that cannot write the locked-down folder (e.g. not elevated)
        # would hang instead of failing. Any filesystem failure here is the fixed code token_not_writable; the existing
        # token file is only ever replaced by the final os.replace, so a failed attempt leaves it intact.
        tmp = self.path.parent / f".apitoken.{secrets.token_hex(8)}.tmp"
        created = False
        not_writable = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            created = True
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException as exc:
            if created:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            if not isinstance(exc, OSError):
                raise  # e.g. Ctrl+C: cleaned up above, and never disguised as a store error
            not_writable = True
        if not_writable:  # raised OUTSIDE the except block: nothing is chained onto the fixed code
            raise ApiTokenStoreError("token_not_writable")

    def load(self, customer_id: int, branch_id: int) -> str:
        """The token, only if the ACL is verified, the blob decrypts, the payload is well-formed and it is bound to this
        customer_id + branch_id. Fails closed with a fixed code otherwise."""
        self._require_present()  # first: with nothing there, the tenant is irrelevant and the answer is token_missing
        _validate_tenant(customer_id, branch_id)
        require_protected(self.acl_state())  # before a single byte is read
        blob = b""
        unreadable = False
        try:
            blob = self.path.read_bytes()
        except OSError:
            unreadable = True
        if unreadable:
            raise ApiTokenStoreError("token_unreadable")
        payload = dpapi_unprotect(blob)  # tampering, another machine, another entropy: token_unreadable

        # Every failure below is raised OUTSIDE the except block, so the decrypted payload (which a JSONDecodeError quotes)
        # never survives as an exception's __context__.
        token: object = None
        bound: tuple[object, object] = (None, None)
        malformed = False
        try:
            document = json.loads(payload)
            token = document["token"]
            bound = (document["customer_id"], document["branch_id"])
            if document.get("format") != BLOB_FORMAT:
                malformed = True
        except (ValueError, KeyError, TypeError, AttributeError):
            malformed = True
        del payload
        if malformed or not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None:
            raise ApiTokenStoreError("token_unreadable")
        if any(isinstance(v, bool) or not isinstance(v, int) for v in bound) or bound != (customer_id, branch_id):
            raise ApiTokenStoreError("token_tenant_mismatch")
        return token


def provision(store: DpapiTokenStore, customer_id: int, branch_id: int, token: str, *, protect_folder: bool = True) -> None:
    """Lock the folder, verify it, write (or atomically replace) the token, then verify again. Nothing is written unless the
    folder is verified protected, and a file that cannot be verified afterwards is removed. Prints nothing."""
    validate_token(token)
    _validate_tenant(customer_id, branch_id)
    if protect_folder:
        store.path.parent.mkdir(parents=True, exist_ok=True)
        protect_directory(store.path.parent)
        require_protected(acl_state_of(store.path.parent, require_protected=True))
    store.save(customer_id, branch_id, token)
    if protect_folder:
        try:
            require_protected(store.acl_state())
        except ApiTokenStoreError:
            store.path.unlink(missing_ok=True)  # never leave a token behind that we could not verify is protected
            raise


# --- command line -------------------------------------------------------------------------------------------------------------

def _read_token_from_stdin() -> str:
    """A hidden prompt when a person is typing, otherwise one line from a pipe. Never echoed; surrounding whitespace (a
    pipe's trailing CRLF) is dropped, anything else must match TOKEN_PATTERN."""
    if sys.stdin is not None and sys.stdin.isatty():
        raw = getpass.getpass("Paste the SortView Collector API token (input hidden): ")
    else:
        raw = sys.stdin.read(_MAX_STDIN_CHARS + 1) if sys.stdin is not None else ""
    if len(raw) > _MAX_STDIN_CHARS:
        raise ApiTokenStoreError("token_invalid")
    return validate_token(raw.strip())


EXIT_OK, EXIT_FAILED, EXIT_CONFIG, EXIT_ABSENT = 0, 1, 2, 3


def main(argv: list[str] | None = None) -> int:
    """`api-token set|check (--config <path> | --customer-id N --branch-id N --data-root <dir>)`.

    The tenant/path comes from the config -- or, for guided setup, which stores the token BEFORE the config is installed,
    from the same three values install.ps1 will write (the file is then `<data-root>\\secrets\\api_token.dpapi`, exactly
    where the config will look). Output is one fixed line; the token (and anything derived from it) is never shown.
    Exit codes: 0 ok, 1 present but unusable / could not be stored, 2 configuration or usage error, 3 (`check` only) no
    token file at all -- so a caller can tell "absent" (store a token) from "damaged" (investigate). Neither is usable:
    there is no fallback source."""
    parser = argparse.ArgumentParser(description="SortView Collector -- API token storage (token read from stdin only)")
    parser.add_argument("command", choices=("set", "check"))
    parser.add_argument("--config")
    parser.add_argument("--customer-id", type=int)
    parser.add_argument("--branch-id", type=int)
    parser.add_argument("--data-root")
    args = parser.parse_args(argv)

    from .config import ConfigError, TokenSettings, load_token_settings

    explicit = (args.customer_id, args.branch_id, args.data_root)
    if (args.config is None) == all(value is None for value in explicit) or (args.config is None and None in explicit):
        print("Usage error: pass either --config, or all of --customer-id, --branch-id and --data-root", file=sys.stderr)
        return EXIT_CONFIG
    try:
        if args.config is not None:
            settings = load_token_settings(args.config)
        else:
            settings = TokenSettings(args.customer_id, args.branch_id, Path(args.data_root) / "secrets" / FILE_NAME)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    store = DpapiTokenStore(settings.token_path)
    try:
        if args.command == "set":
            provision(store, settings.customer_id, settings.branch_id, _read_token_from_stdin())
            print("api token stored and protected (value not shown)")
        else:
            store.load(settings.customer_id, settings.branch_id)
            print("api token present, bound to this customer_id/branch_id, and its ACL is verified protected")
        return EXIT_OK
    except ApiTokenStoreError as exc:
        print(f"failed: {exc.code}", file=sys.stderr)
        return EXIT_ABSENT if args.command == "check" and exc.code == "token_missing" else EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
