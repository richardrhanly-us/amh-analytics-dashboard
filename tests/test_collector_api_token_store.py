"""The Collector's API bearer token store (collector/api_token_store.py) and its `api-token set|check` command line.

Real DPAPI round trips run only on Windows (the production platform). The ACL verdicts, the fail-closed ordering and the
command line are platform-neutral. Every token here is SYNTHETIC.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from collector import api_token_store, v2_keys
from collector.api_token_store import ApiTokenStoreError, DpapiTokenStore

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI machine scope is Windows-only")

TOKEN = "CANARY-api-token-00000000000000000000001"
OTHER_TOKEN = "CANARY-api-token-00000000000000000000002"
CUSTOMER, BRANCH = 7, 3


def as_windows(monkeypatch):
    monkeypatch.setattr(api_token_store, "sys", types.SimpleNamespace(platform="win32", stdin=sys.stdin))


class TrustedAclStore(DpapiTokenStore):
    """The real DPAPI store with only its ACL verdict fixed to "protected". A test tmp directory inherits the user's ACL (so the real
    check rightly calls it exposed); the ACL logic has its own tests below, and the real refusal is exercised with DpapiTokenStore."""

    def acl_state(self):
        return "protected"


@pytest.fixture
def store(tmp_path):
    return TrustedAclStore(tmp_path / "secrets" / "api_token.dpapi")


def _blob_of(document: dict) -> bytes:
    return api_token_store.dpapi_protect(json.dumps(document).encode("ascii"))


# =====================================================================================================================
# DPAPI (Windows)
# =====================================================================================================================

@windows_only
def test_dpapi_round_trips_with_machine_scope_and_ui_forbidden():
    blob = api_token_store.dpapi_protect(b"synthetic payload")
    assert blob != b"synthetic payload" and b"synthetic" not in blob
    assert api_token_store.dpapi_unprotect(blob) == b"synthetic payload"
    assert api_token_store.CRYPTPROTECT_LOCAL_MACHINE == 0x4 and api_token_store.CRYPTPROTECT_UI_FORBIDDEN == 0x1


def test_the_entropy_is_its_own_and_never_the_v2_secrets():
    assert api_token_store._ENTROPY != v2_keys._ENTROPY
    assert b"API token" in api_token_store._ENTROPY


@windows_only
def test_a_v2_secret_blob_cannot_be_opened_as_an_api_token_and_vice_versa():
    v2_blob = v2_keys.dpapi_protect(b'{"format": 1}')
    with pytest.raises(ApiTokenStoreError) as caught:
        api_token_store.dpapi_unprotect(v2_blob)
    assert caught.value.code == "token_unreadable"

    token_blob = api_token_store.dpapi_protect(b'{"format": 1}')
    with pytest.raises(v2_keys.SecretStoreError):
        v2_keys.dpapi_unprotect(token_blob)


@windows_only
def test_dpapi_refuses_garbage():
    for garbage in (b"", b"not a dpapi blob", os.urandom(200)):
        with pytest.raises(ApiTokenStoreError) as caught:
            api_token_store.dpapi_unprotect(garbage)
        assert caught.value.code == "token_unreadable"


# =====================================================================================================================
# The store: save / load
# =====================================================================================================================

@windows_only
def test_save_then_load_returns_the_supplied_token_bound_to_the_tenant(store):
    store.save(CUSTOMER, BRANCH, TOKEN)
    assert store.load(CUSTOMER, BRANCH) == TOKEN


@windows_only
@pytest.mark.parametrize("token", ["a" * 20, "b" * 256, "Z9_-" * 10, "short-but-ok-2026-abc"])
def test_a_token_of_any_permitted_length_round_trips(store, token):
    store.save(CUSTOMER, BRANCH, token)
    assert store.load(CUSTOMER, BRANCH) == token


@windows_only
@pytest.mark.parametrize(("customer", "branch"), [(CUSTOMER + 1, BRANCH), (CUSTOMER, BRANCH + 1), (BRANCH, CUSTOMER)])
def test_a_token_for_another_customer_or_branch_fails_closed(store, customer, branch):
    store.save(CUSTOMER, BRANCH, TOKEN)
    with pytest.raises(ApiTokenStoreError) as caught:
        store.load(customer, branch)
    assert caught.value.code == "token_tenant_mismatch"
    assert TOKEN not in str(caught.value) and caught.value.__context__ is None


@windows_only
def test_save_atomically_replaces_an_existing_token_for_rotation(store):
    store.save(CUSTOMER, BRANCH, TOKEN)
    store.save(CUSTOMER, BRANCH, OTHER_TOKEN)

    assert store.load(CUSTOMER, BRANCH) == OTHER_TOKEN
    assert sorted(p.name for p in store.path.parent.iterdir()) == ["api_token.dpapi"]  # no temp file left behind


@windows_only
def test_only_ciphertext_ever_touches_the_disk_and_no_file_name_derives_from_the_token(store, monkeypatch):
    written: list[tuple[str, bytes]] = []
    real_replace = os.replace

    def spy(src, dst):
        written.append((Path(src).name, Path(src).read_bytes()))
        return real_replace(src, dst)

    monkeypatch.setattr(api_token_store.os, "replace", spy)
    store.save(CUSTOMER, BRANCH, TOKEN)

    ((temp_name, temp_bytes),) = written
    assert temp_name.startswith(".apitoken.") and TOKEN not in temp_name
    on_disk = store.path.read_bytes()
    for raw in (temp_bytes, on_disk):
        for needle in (TOKEN.encode(), b"token", b"customer_id", b"branch_id"):
            assert needle not in raw


@windows_only
def test_a_failed_write_removes_its_temp_file_and_keeps_the_previous_token(store, monkeypatch):
    store.save(CUSTOMER, BRANCH, TOKEN)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(api_token_store.os, "replace", boom)
    with pytest.raises(ApiTokenStoreError) as caught:
        store.save(CUSTOMER, BRANCH, OTHER_TOKEN)
    monkeypatch.undo()

    assert caught.value.code == "token_not_writable" and caught.value.__context__ is None
    assert sorted(p.name for p in store.path.parent.iterdir()) == ["api_token.dpapi"]
    assert store.load(CUSTOMER, BRANCH) == TOKEN


# --- a caller that cannot write the locked-down folder (e.g. NOT elevated) fails fast --------------------------------------
#
# Found on LIB-L26: after the folder is locked to SYSTEM + Administrators, a non-elevated `api-token set` could not create
# its temp file, and tempfile.mkstemp -- which on Windows reads PermissionError as a name collision and retries up to
# os.TMP_MAX (2**31) times -- hung until Ctrl+C.

def _deny_creation(monkeypatch):
    attempts: list[str] = []

    def denied(path, *args, **kwargs):
        attempts.append(str(path))
        raise PermissionError(13, "Access is denied", str(path))

    monkeypatch.setattr(api_token_store.os, "open", denied)
    return attempts


def test_permission_denied_creating_the_temp_file_fails_after_exactly_one_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(api_token_store, "dpapi_protect", lambda data: b"ciphertext")
    attempts = _deny_creation(monkeypatch)
    store = DpapiTokenStore(tmp_path / "secrets" / "api_token.dpapi")

    with pytest.raises(ApiTokenStoreError) as caught:
        store.save(CUSTOMER, BRANCH, TOKEN)

    assert caught.value.code == "token_not_writable"
    assert len(attempts) == 1  # no retry loop
    assert caught.value.__context__ is None and caught.value.__cause__ is None
    for text in (str(caught.value), repr(caught.value), *attempts):
        assert TOKEN not in text
    assert list(store.path.parent.iterdir()) == []  # nothing left behind


@windows_only
def test_an_existing_token_survives_a_denied_overwrite(store, monkeypatch):
    store.save(CUSTOMER, BRANCH, TOKEN)
    _deny_creation(monkeypatch)

    with pytest.raises(ApiTokenStoreError) as caught:
        store.save(CUSTOMER, BRANCH, OTHER_TOKEN)
    monkeypatch.undo()

    assert caught.value.code == "token_not_writable"
    assert store.load(CUSTOMER, BRANCH) == TOKEN
    assert sorted(p.name for p in store.path.parent.iterdir()) == ["api_token.dpapi"]


def test_an_interrupted_write_removes_its_temp_file_and_is_not_disguised(tmp_path, monkeypatch):
    # Ctrl+C mid-write: the partial (ciphertext) temp file is removed and the interrupt propagates as itself.
    monkeypatch.setattr(api_token_store, "dpapi_protect", lambda data: b"ciphertext")

    def interrupted(fd):
        raise KeyboardInterrupt

    monkeypatch.setattr(api_token_store.os, "fsync", interrupted)
    store = DpapiTokenStore(tmp_path / "secrets" / "api_token.dpapi")

    with pytest.raises(KeyboardInterrupt):
        store.save(CUSTOMER, BRANCH, TOKEN)

    assert list(store.path.parent.iterdir()) == []


def test_set_reports_a_denied_folder_promptly_with_a_fixed_code_and_never_the_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(api_token_store, "protect_directory", lambda *_a, **_k: None)
    monkeypatch.setattr(api_token_store, "acl_state_of", lambda *_a, **_k: "protected")
    monkeypatch.setattr(api_token_store, "dpapi_protect", lambda data: b"ciphertext")
    attempts = _deny_creation(monkeypatch)
    monkeypatch.setattr(sys, "stdin", _Stdin(TOKEN + "\r\n"))

    assert api_token_store.main(["set", "--config", str(_write_config(tmp_path))]) == 1

    out, err = capsys.readouterr()
    assert (out, err) == ("", "failed: token_not_writable\n")
    assert len(attempts) == 1


@windows_only
def test_a_real_write_denied_folder_fails_fast_instead_of_hanging(tmp_path):
    """No stubbing: a real folder that denies the current user file creation (as the locked-down secrets folder does to a
    non-elevated process). Run in a child process with a hard timeout, so a regression to the retry loop fails the test
    instead of hanging the suite."""
    import getpass

    folder = tmp_path / "secrets"
    folder.mkdir()
    user = getpass.getuser()
    denied = subprocess.run(["icacls", str(folder), "/deny", f"{user}:(WD,AD)"], capture_output=True, check=False)
    assert denied.returncode == 0, denied.stderr
    root = Path(api_token_store.__file__).resolve().parent.parent
    script = (
        "import sys\n"
        "from collector import api_token_store as s\n"
        "try:\n"
        f"    s.DpapiTokenStore(sys.argv[1]).save({CUSTOMER}, {BRANCH}, sys.argv[2])\n"
        "except s.ApiTokenStoreError as exc:\n"
        "    print('CODE=' + exc.code)\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", script, str(folder / "api_token.dpapi"), TOKEN],
            cwd=root, env={**os.environ, "PYTHONPATH": str(root)}, capture_output=True, text=True, timeout=60, check=False,
        )
    finally:
        subprocess.run(["icacls", str(folder), "/remove:d", user], capture_output=True, check=False)

    assert result.stdout.strip() == "CODE=token_not_writable", result.stdout + result.stderr
    assert TOKEN not in result.stdout + result.stderr
    assert list(folder.iterdir()) == []


def test_a_missing_file_is_token_missing(tmp_path):
    with pytest.raises(ApiTokenStoreError) as caught:
        DpapiTokenStore(tmp_path / "secrets" / "api_token.dpapi").load(CUSTOMER, BRANCH)
    assert caught.value.code == "token_missing"


def test_a_directory_where_the_file_should_be_is_unreadable_never_missing(tmp_path):
    path = tmp_path / "secrets" / "api_token.dpapi"
    path.mkdir(parents=True)
    with pytest.raises(ApiTokenStoreError) as caught:
        DpapiTokenStore(path).load(CUSTOMER, BRANCH)
    assert caught.value.code == "token_unreadable"


def test_a_path_that_cannot_even_be_checked_is_unreadable_never_missing(tmp_path, monkeypatch):
    def denied(path, *a, **k):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(api_token_store.os, "stat", denied)
    with pytest.raises(ApiTokenStoreError) as caught:
        DpapiTokenStore(tmp_path / "api_token.dpapi").load(CUSTOMER, BRANCH)
    assert caught.value.code == "token_unreadable"


@windows_only
def test_a_corrupt_file_fails_closed(store):
    store.save(CUSTOMER, BRANCH, TOKEN)
    blob = bytearray(store.path.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    store.path.write_bytes(bytes(blob))
    with pytest.raises(ApiTokenStoreError) as caught:
        store.load(CUSTOMER, BRANCH)
    assert caught.value.code == "token_unreadable"


@windows_only
@pytest.mark.parametrize(("document", "code"), [
    ({"format": 2, "customer_id": CUSTOMER, "branch_id": BRANCH, "token": TOKEN}, "token_unreadable"),
    ({"format": 1, "customer_id": CUSTOMER, "branch_id": BRANCH}, "token_unreadable"),
    ({"format": 1, "customer_id": CUSTOMER, "branch_id": BRANCH, "token": "has spaces in it 0000000"}, "token_unreadable"),
    ({"format": 1, "customer_id": CUSTOMER, "branch_id": BRANCH, "token": "short"}, "token_unreadable"),
    ({"format": 1, "customer_id": CUSTOMER, "branch_id": BRANCH, "token": 12345}, "token_unreadable"),
    ({"format": 1, "customer_id": True, "branch_id": BRANCH, "token": TOKEN}, "token_tenant_mismatch"),
    ({"format": 1, "customer_id": str(CUSTOMER), "branch_id": BRANCH, "token": TOKEN}, "token_tenant_mismatch"),
    ({"format": 1, "branch_id": BRANCH, "token": TOKEN}, "token_unreadable"),
])
def test_a_valid_blob_with_the_wrong_shape_is_rejected_without_echoing_it(store, document, code):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(_blob_of(document))
    with pytest.raises(ApiTokenStoreError) as caught:
        store.load(CUSTOMER, BRANCH)
    assert caught.value.code == code
    assert TOKEN not in str(caught.value) and caught.value.__context__ is None and caught.value.__cause__ is None


@windows_only
def test_a_payload_that_is_not_json_is_rejected_without_chaining_the_decrypted_text(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_bytes(api_token_store.dpapi_protect(b"not json " + TOKEN.encode()))
    with pytest.raises(ApiTokenStoreError) as caught:
        store.load(CUSTOMER, BRANCH)
    assert caught.value.code == "token_unreadable" and caught.value.__context__ is None


@pytest.mark.parametrize("token", [
    "", "   ", "short", "x" * 257, "has space 000000000000000", "line\r\nbreak0000000000000", "semi;colon000000000000000",
    "unicodé000000000000000000", None, 123,
])
def test_save_and_provision_refuse_anything_that_is_not_a_plausible_token(tmp_path, token):
    store = DpapiTokenStore(tmp_path / "secrets" / "api_token.dpapi")
    with pytest.raises(ApiTokenStoreError) as caught:
        store.save(CUSTOMER, BRANCH, token)
    assert caught.value.code == "token_invalid"
    with pytest.raises(ApiTokenStoreError):
        api_token_store.provision(store, CUSTOMER, BRANCH, token, protect_folder=False)
    assert not store.path.parent.exists()  # nothing written, not even the folder


@pytest.mark.parametrize(("customer", "branch"), [(0, 1), (1, 0), (-1, 1), (True, 1), ("1", 1), (None, 1)])
def test_save_refuses_an_invalid_tenant(tmp_path, customer, branch):
    with pytest.raises(ApiTokenStoreError) as caught:
        DpapiTokenStore(tmp_path / "api_token.dpapi").save(customer, branch, TOKEN)
    assert caught.value.code == "token_invalid"


# =====================================================================================================================
# ACL -- the ONLY boundary against an ordinary local user (machine-scope DPAPI alone is not)
# =====================================================================================================================

SDDL_FIXTURES = [
    ("D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", True),
    ("D:PAI(A;;FA;;;SY)(A;;FA;;;BA)", False),
    ("D:P(A;OICI;FA;;;S-1-5-18)(A;OICI;FA;;;S-1-5-32-544)", True),
    ("D:P(D;OICI;FA;;;BU)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", True),
    ("D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)", True),
    ("D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;WD)", True),
    ("D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;AU)", True),
    ("D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;S-1-5-21-1111111111-2222222222-3333333333-1001)", True),
    ("D:AI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", True),
    ("D:AI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", False),
    ("D:P", True), ("D:P(garbage)", True), ("", True),
]
EXPECTED_VERDICTS = ["protected", "protected", "protected", "protected", "exposed", "exposed", "exposed", "exposed", "exposed",
                     "protected", "unknown", "unknown", "unknown"]


@pytest.mark.parametrize(("fixture", "verdict"), list(zip(SDDL_FIXTURES, EXPECTED_VERDICTS, strict=True)))
def test_the_acl_verdict_is_exposed_unless_only_system_and_administrators_are_allowed(monkeypatch, fixture, verdict):
    sddl, require = fixture
    as_windows(monkeypatch)
    monkeypatch.setattr(api_token_store, "_dacl_of", lambda _path: sddl or None)
    assert api_token_store.acl_state_of("ignored", require_protected=require) == verdict


@pytest.mark.parametrize(("sddl", "require"), SDDL_FIXTURES)
def test_the_acl_rules_never_drift_from_the_v2_secrets(monkeypatch, sddl, require):
    # Two copies of the same rule on purpose (the v1 path may not import collector.v2_*); this keeps them identical.
    as_windows(monkeypatch)
    monkeypatch.setattr(v2_keys, "sys", types.SimpleNamespace(platform="win32"))
    monkeypatch.setattr(api_token_store, "_dacl_of", lambda _path: sddl or None)
    monkeypatch.setattr(v2_keys, "_dacl_of", lambda _path: sddl or None)
    assert api_token_store.acl_state_of("x", require_protected=require) == v2_keys.acl_state_of("x", require_protected=require)


def test_off_windows_the_acl_is_unknown_and_the_store_refuses_to_encrypt(tmp_path):
    if sys.platform == "win32":
        pytest.skip("off-Windows behavior")
    assert api_token_store.acl_state_of(tmp_path, require_protected=True) == "unknown"
    with pytest.raises(ApiTokenStoreError) as caught:
        DpapiTokenStore(tmp_path / "t").save(CUSTOMER, BRANCH, TOKEN)
    assert caught.value.code == "token_store_unavailable"


def test_the_store_verdict_combines_folder_and_file(monkeypatch, tmp_path):
    as_windows(monkeypatch)
    path = tmp_path / "t"
    path.write_bytes(b"x")
    store = DpapiTokenStore(path)
    replies: dict[bool, str] = {}
    monkeypatch.setattr(api_token_store, "acl_state_of", lambda p, *, require_protected: replies[require_protected])
    for folder, file, expected in (("protected", "protected", "protected"), ("exposed", "protected", "exposed"),
                                   ("protected", "exposed", "exposed"), ("unknown", "protected", "unknown"),
                                   ("unknown", "exposed", "exposed")):
        replies.update({True: folder, False: file})
        assert store.acl_state() == expected, (folder, file)


@pytest.mark.parametrize(("verdict", "code"), [("exposed", "token_exposed"), ("unknown", "token_acl_unverified")])
def test_load_checks_the_acl_before_reading_a_single_byte(monkeypatch, tmp_path, verdict, code):
    as_windows(monkeypatch)
    path = tmp_path / "api_token.dpapi"
    path.write_bytes(b"opaque blob")
    monkeypatch.setattr(DpapiTokenStore, "acl_state", lambda self: verdict)
    monkeypatch.setattr(api_token_store, "dpapi_unprotect", lambda _b: pytest.fail("decrypted although the ACL was not verified"))
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("read although the ACL was not verified"))
    with pytest.raises(ApiTokenStoreError) as caught:
        DpapiTokenStore(path).load(CUSTOMER, BRANCH)
    assert caught.value.code == code and caught.value.__context__ is None


@windows_only
def test_the_real_store_refuses_a_token_in_a_folder_that_inherits_the_users_acl(tmp_path):
    """No stubbing: an ordinary temp folder is readable by the current user, so the REAL ACL check calls it exposed."""
    plain = DpapiTokenStore(tmp_path / "secrets" / "api_token.dpapi")
    TrustedAclStore(plain.path).save(CUSTOMER, BRANCH, TOKEN)
    with pytest.raises(ApiTokenStoreError) as caught:
        plain.load(CUSTOMER, BRANCH)
    assert caught.value.code == "token_exposed"
    assert TrustedAclStore(plain.path).load(CUSTOMER, BRANCH) == TOKEN


def test_protect_directory_grants_only_system_and_administrators_by_sid_and_cuts_inheritance(monkeypatch, tmp_path):
    as_windows(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(api_token_store.subprocess, "run",
                        lambda args, **_k: calls.append(args) or types.SimpleNamespace(returncode=0))
    api_token_store.protect_directory(tmp_path)
    (args,) = calls
    assert args[:4] == ["icacls", str(tmp_path), "/inheritance:r", "/grant:r"]
    assert args[4:] == ["*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"]


def test_a_failed_icacls_is_a_fixed_code_error(monkeypatch, tmp_path):
    as_windows(monkeypatch)
    monkeypatch.setattr(api_token_store.subprocess, "run", lambda *_a, **_k: types.SimpleNamespace(returncode=5))
    with pytest.raises(ApiTokenStoreError) as caught:
        api_token_store.protect_directory(tmp_path)
    assert caught.value.code == "token_dir_not_protected"


# --- provision: lock, verify, write, verify ---------------------------------------------------------------------------------

def _fake_save(order):
    def save(self, customer_id, branch_id, token):
        order.append("save")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"ciphertext")
    return save


def test_provision_locks_and_verifies_the_folder_before_writing_then_verifies_the_result(monkeypatch, tmp_path):
    order: list[str] = []
    monkeypatch.setattr(api_token_store, "protect_directory", lambda *_a, **_k: order.append("protect"))
    monkeypatch.setattr(api_token_store, "acl_state_of", lambda *_a, **_k: order.append("verify-folder") or "protected")
    monkeypatch.setattr(DpapiTokenStore, "save", _fake_save(order))
    monkeypatch.setattr(DpapiTokenStore, "acl_state", lambda self: order.append("verify-result") or "protected")

    api_token_store.provision(DpapiTokenStore(tmp_path / "secrets" / "api_token.dpapi"), CUSTOMER, BRANCH, TOKEN)

    assert order == ["protect", "verify-folder", "save", "verify-result"]


@pytest.mark.parametrize(("verdict", "code"), [("exposed", "token_exposed"), ("unknown", "token_acl_unverified")])
def test_provision_writes_nothing_unless_the_folder_is_verified_protected(monkeypatch, tmp_path, verdict, code):
    monkeypatch.setattr(api_token_store, "protect_directory", lambda *_a, **_k: None)
    monkeypatch.setattr(api_token_store, "acl_state_of", lambda *_a, **_k: verdict)
    monkeypatch.setattr(DpapiTokenStore, "save", lambda *_a: pytest.fail("written into an unverified folder"))
    with pytest.raises(ApiTokenStoreError) as caught:
        api_token_store.provision(DpapiTokenStore(tmp_path / "secrets" / "api_token.dpapi"), CUSTOMER, BRANCH, TOKEN)
    assert caught.value.code == code


def test_provision_removes_a_token_it_cannot_verify_after_writing_it(monkeypatch, tmp_path):
    order: list[str] = []
    monkeypatch.setattr(api_token_store, "protect_directory", lambda *_a, **_k: None)
    monkeypatch.setattr(api_token_store, "acl_state_of", lambda *_a, **_k: "protected")
    monkeypatch.setattr(DpapiTokenStore, "save", _fake_save(order))
    monkeypatch.setattr(DpapiTokenStore, "acl_state", lambda self: "exposed")
    store = DpapiTokenStore(tmp_path / "secrets" / "api_token.dpapi")

    with pytest.raises(ApiTokenStoreError) as caught:
        api_token_store.provision(store, CUSTOMER, BRANCH, TOKEN)

    assert caught.value.code == "token_exposed"
    assert not store.path.exists()


# --- nothing token-shaped ever leaves the module ---------------------------------------------------------------------------

@windows_only
def test_the_token_never_appears_in_any_error_or_repr(store):
    store.save(CUSTOMER, BRANCH, TOKEN)
    texts = [repr(store), str(store.path)]
    for code in ("token_missing", "token_unreadable", "token_tenant_mismatch", "token_exposed"):
        texts += [str(ApiTokenStoreError(code)), repr(ApiTokenStoreError(code))]
    try:
        store.load(CUSTOMER + 1, BRANCH)
    except ApiTokenStoreError as exc:
        texts += [str(exc), repr(exc), str(exc.args)]
    for text in texts:
        assert TOKEN not in text


def test_the_module_never_writes_the_token_anywhere_but_the_dpapi_blob():
    source = Path(api_token_store.__file__).read_text(encoding="utf-8")
    code = source.split('"""', 2)[-1]
    assert "print(token" not in code and "logger" not in code and "logging" not in code
    assert "write_text" not in code
    assert not re.search(r"\benviron\b|getenv\s*\(|putenv\s*\(", code)  # never reads or writes the environment itself


def test_the_module_imports_no_contract_v2_module():
    # collector/config.py (the v1 path) imports this module, and the v1 path must import no collector.v2_* module.
    source = Path(api_token_store.__file__).read_text(encoding="utf-8")
    assert not re.search(r"^\s*(from|import)\s+(\.|collector\.)v2_", source, re.MULTILINE)
    assert "from .v2_" not in source


# =====================================================================================================================
# Command line: `api-token set|check`
# =====================================================================================================================

def _write_config(tmp_path, **overrides) -> Path:
    doc = {
        "customer_id": CUSTOMER, "branch_id": BRANCH, "api_url": "https://example.invalid",
        "sources": [{"name": "checkins", "path": str(tmp_path / "Checkins.txt")}],
        "state_path": str(tmp_path / "data" / "state.json"),
        "status_path": str(tmp_path / "data" / "status.json"),
        "log_path": str(tmp_path / "logs" / "collector.log"),
    }
    doc.update(overrides)
    path = tmp_path / "config" / "collector_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


class _Stdin(io.StringIO):
    def __init__(self, text: str, tty: bool = False):
        super().__init__(text)
        self._tty = tty

    def isatty(self):
        return self._tty


@pytest.fixture
def trusted_acl(monkeypatch):
    """Real DPAPI and real files, with only the ACL steps neutralized (a test cannot lock itself out of its own tmp folder)."""
    monkeypatch.setattr(api_token_store, "protect_directory", lambda *_a, **_k: None)
    monkeypatch.setattr(api_token_store, "acl_state_of", lambda *_a, **_k: "protected")


@pytest.fixture
def captured_provision(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(api_token_store, "provision",
                        lambda store, customer_id, branch_id, token, **_k: calls.append((store.path, customer_id, branch_id, token)))
    return calls


def test_set_reads_the_token_from_a_pipe_and_prints_one_fixed_line(tmp_path, monkeypatch, capsys, captured_provision):
    monkeypatch.setattr(sys, "stdin", _Stdin(TOKEN + "\r\n"))
    config = _write_config(tmp_path)

    assert api_token_store.main(["set", "--config", str(config)]) == 0

    out, err = capsys.readouterr()
    assert out == "api token stored and protected (value not shown)\n" and err == ""
    assert captured_provision == [(tmp_path / "secrets" / "api_token.dpapi", CUSTOMER, BRANCH, TOKEN)]


def test_set_reads_the_token_from_a_hidden_prompt_when_interactive(tmp_path, monkeypatch, capsys, captured_provision):
    monkeypatch.setattr(sys, "stdin", _Stdin("", tty=True))
    prompts: list[str] = []
    monkeypatch.setattr(api_token_store.getpass, "getpass", lambda prompt: prompts.append(prompt) or TOKEN)

    assert api_token_store.main(["set", "--config", str(_write_config(tmp_path))]) == 0

    assert prompts and "hidden" in prompts[0]
    assert captured_provision[0][3] == TOKEN
    assert TOKEN not in "".join(capsys.readouterr())


@pytest.mark.parametrize("flag", ["--token", "--api-token", "--value"])
def test_there_is_no_argument_that_could_carry_the_token(tmp_path, flag, capsys, captured_provision):
    with pytest.raises(SystemExit) as caught:
        api_token_store.main(["set", "--config", str(_write_config(tmp_path)), flag, TOKEN])
    assert caught.value.code == 2 and captured_provision == []


@pytest.mark.parametrize("piped", ["", "\r\n", "not a token\r\n", "x" * 5000, TOKEN + " extra\n"])
def test_set_refuses_an_implausible_token_without_echoing_it(tmp_path, monkeypatch, capsys, captured_provision, piped):
    monkeypatch.setattr(sys, "stdin", _Stdin(piped))

    assert api_token_store.main(["set", "--config", str(_write_config(tmp_path))]) == 1

    out, err = capsys.readouterr()
    assert err == "failed: token_invalid\n" and out == ""
    assert captured_provision == []


@windows_only
def test_set_then_check_then_rotate_through_the_command_line(tmp_path, monkeypatch, capsys, trusted_acl):
    config = str(_write_config(tmp_path))
    monkeypatch.setattr(sys, "stdin", _Stdin(TOKEN + "\n"))
    assert api_token_store.main(["set", "--config", config]) == 0
    assert api_token_store.main(["check", "--config", config]) == 0
    monkeypatch.setattr(sys, "stdin", _Stdin(OTHER_TOKEN + "\n"))
    assert api_token_store.main(["set", "--config", config]) == 0  # overwrite: rotation

    assert DpapiTokenStore(tmp_path / "secrets" / "api_token.dpapi").load(CUSTOMER, BRANCH) == OTHER_TOKEN
    out, err = capsys.readouterr()
    assert TOKEN not in out + err and OTHER_TOKEN not in out + err
    assert out.splitlines() == [
        "api token stored and protected (value not shown)",
        "api token present, bound to this customer_id/branch_id, and its ACL is verified protected",
        "api token stored and protected (value not shown)",
    ]


def test_check_reports_absent_as_exit_3_distinct_from_damaged(tmp_path, capsys, monkeypatch):
    config = str(_write_config(tmp_path))
    assert api_token_store.main(["check", "--config", config]) == 3
    assert capsys.readouterr().err == "failed: token_missing\n"

    def damaged(self, c, b):
        raise ApiTokenStoreError("token_unreadable")

    monkeypatch.setattr(DpapiTokenStore, "load", damaged)
    assert api_token_store.main(["check", "--config", config]) == 1
    assert capsys.readouterr().err == "failed: token_unreadable\n"


def test_check_never_prints_the_token(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(DpapiTokenStore, "load", lambda self, c, b: TOKEN)
    assert api_token_store.main(["check", "--config", str(_write_config(tmp_path))]) == 0
    out, err = capsys.readouterr()
    assert TOKEN not in out + err and str(len(TOKEN)) not in out + err


def test_set_needs_no_token_to_exist_first_even_with_a_placeholder_free_config(tmp_path, monkeypatch, captured_provision):
    # load_config() would refuse (no token yet); `set` only needs the tenant and the path.
    monkeypatch.setattr(sys, "stdin", _Stdin(TOKEN))
    assert api_token_store.main(["set", "--config", str(_write_config(tmp_path))]) == 0


def test_an_api_token_path_override_in_the_config_is_honored(tmp_path, monkeypatch, captured_provision):
    override = tmp_path / "elsewhere" / "token.dpapi"
    monkeypatch.setattr(sys, "stdin", _Stdin(TOKEN))
    assert api_token_store.main(["set", "--config", str(_write_config(tmp_path, api_token_path=str(override)))]) == 0
    assert captured_provision[0][0] == override


def test_explicit_tenant_arguments_target_exactly_where_the_installed_config_will_look(tmp_path, monkeypatch, captured_provision):
    # Guided setup stores the token BEFORE install.ps1 writes the config; the path must be the one the config then derives.
    data_root = tmp_path / "ProgramData" / "SortViewCollector"
    monkeypatch.setattr(sys, "stdin", _Stdin(TOKEN))
    argv = ["set", "--customer-id", str(CUSTOMER), "--branch-id", str(BRANCH), "--data-root", str(data_root)]
    assert api_token_store.main(argv) == 0

    from collector.config import load_token_settings
    installed = load_token_settings(_write_config(tmp_path, state_path=str(data_root / "data" / "state.json")))
    assert captured_provision == [(installed.token_path, installed.customer_id, installed.branch_id, TOKEN)]


@pytest.mark.parametrize("argv", [
    ["check"],
    ["check", "--customer-id", "1", "--branch-id", "1"],
    ["check", "--config", "c.json", "--customer-id", "1"],
    ["check", "--data-root", "x"],
])
def test_the_tenant_must_come_from_exactly_one_place(argv, capsys):
    assert api_token_store.main(argv) == 2
    assert "Usage error" in capsys.readouterr().err


def test_a_config_error_is_exit_2(tmp_path, capsys):
    assert api_token_store.main(["check", "--config", str(tmp_path / "missing.json")]) == 2
    assert "Configuration error" in capsys.readouterr().err


def test_the_command_line_as_a_real_process_rejects_a_token_argument_and_reads_stdin(tmp_path):
    root = Path(api_token_store.__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(root)}
    rejected = subprocess.run(
        [sys.executable, "-m", "collector.api_token_store", "set", "--config", str(_write_config(tmp_path)), "--token", TOKEN],
        cwd=root, env=env, capture_output=True, text=True, check=False, timeout=120,
    )
    assert rejected.returncode == 2 and TOKEN not in rejected.stdout

    piped = subprocess.run(
        [sys.executable, "-m", "collector.api_token_store", "set", "--config", str(_write_config(tmp_path))],
        input="bad token\n", cwd=root, env=env, capture_output=True, text=True, check=False, timeout=120,
    )
    assert piped.returncode == 1 and piped.stderr.strip() == "failed: token_invalid" and "bad token" not in piped.stdout
