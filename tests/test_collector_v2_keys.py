"""Contract v2 collector: the local master secret (collector/v2_keys.py).

DPAPI round-trip and tamper tests run only on Windows (the production platform). The ACL parser, the fail-closed behavior and the refusal to run
on other platforms are platform-neutral.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import types
from pathlib import Path

import pytest
from collector_v2_support import KEY_ID, OTHER_KEY_ID, FakeStore

from collector import v2_keys
from collector.v2_keys import DpapiSecretStore, SecretStoreError

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI machine scope is Windows-only")


def as_windows(monkeypatch):
    monkeypatch.setattr(v2_keys, "sys", types.SimpleNamespace(platform="win32"))


# =====================================================================================================================
# DPAPI (Windows)
# =====================================================================================================================

@windows_only
def test_dpapi_round_trips_and_uses_machine_scope_with_fixed_entropy():
    blob = v2_keys.dpapi_protect(b"synthetic secret payload")
    assert blob != b"synthetic secret payload" and b"synthetic" not in blob
    assert v2_keys.dpapi_unprotect(blob) == b"synthetic secret payload"
    assert v2_keys.CRYPTPROTECT_LOCAL_MACHINE == 0x4 and v2_keys.CRYPTPROTECT_UI_FORBIDDEN == 0x1


@windows_only
def test_a_blob_made_without_the_collector_entropy_cannot_be_opened(tmp_path):
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    data = b"payload made WITHOUT the collector's entropy"
    buffer = ctypes.create_string_buffer(data, len(data))
    source, out = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    assert crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None, v2_keys.CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(out))
    foreign = ctypes.string_at(out.pbData, out.cbData)
    with pytest.raises(SecretStoreError) as caught:
        v2_keys.dpapi_unprotect(foreign)
    assert caught.value.code == "secret_unreadable"


@windows_only
def test_dpapi_refuses_garbage():
    for garbage in (b"", b"not a dpapi blob", os.urandom(200)):
        with pytest.raises(SecretStoreError) as caught:
            v2_keys.dpapi_unprotect(garbage)
        assert caught.value.code == "secret_unreadable"


class TrustedAclStore(DpapiSecretStore):
    """The real DPAPI store with only its ACL verdict fixed to "protected". A test tmp directory inherits the user's ACL (so the real check
    rightly calls it exposed); the ACL logic has its own tests below, and the real refusal is exercised with `DpapiSecretStore` directly."""

    def acl_state(self):
        return "protected"


@pytest.fixture
def store(tmp_path):
    return TrustedAclStore(tmp_path / "secrets" / "v2_key.dpapi")


@windows_only
def test_create_then_load_returns_a_32_byte_secret_bound_to_the_key_id(store):
    store.create(KEY_ID)
    secret = store.load(KEY_ID)
    assert isinstance(secret, bytes) and len(secret) == 32 and secret != bytes(32)
    assert store.load(KEY_ID) == secret                   # stable across loads


@windows_only
def test_two_installs_get_different_secrets(tmp_path):
    a, b = TrustedAclStore(tmp_path / "a" / "k"), TrustedAclStore(tmp_path / "b" / "k")
    a.create(KEY_ID)
    b.create(KEY_ID)
    assert a.load(KEY_ID) != b.load(KEY_ID)


@windows_only
def test_the_file_on_disk_holds_no_cleartext_secret_key_id_or_field_name(store):
    store.create(KEY_ID)
    secret = store.load(KEY_ID)
    blob = store.path.read_bytes()
    for needle in (secret, secret.hex().encode(), base64.b64encode(secret), KEY_ID.encode(), KEY_ID.encode("utf-16-le"), b"secret",
                   b"key_id", b"hmac-sha256", KEY_ID.replace("-", "").encode()):
        assert needle not in blob, needle
    with pytest.raises(ValueError):                       # the blob is not JSON either
        json.loads(blob)


@windows_only
def test_a_key_id_mismatch_fails_closed(store):
    store.create(KEY_ID)
    with pytest.raises(SecretStoreError) as caught:
        store.load(OTHER_KEY_ID)
    assert caught.value.code == "secret_key_mismatch"


@windows_only
def test_a_tampered_blob_never_yields_a_different_secret_and_fails_closed_everywhere_that_matters(store):
    """Flip every byte of the blob in turn. A flip must either fail closed (`secret_unreadable`) or return the IDENTICAL secret -- never a
    different one. The only bytes DPAPI tolerates are the 16-byte provider GUID in the header (it carries no secret material); everything else
    (ciphertext, salt, HMAC, description) is integrity-protected."""
    store.create(KEY_ID)
    original = store.path.read_bytes()
    genuine = store.load(KEY_ID)
    tolerated = []
    for position in range(len(original)):
        tampered = bytearray(original)
        tampered[position] ^= 0x01
        store.path.write_bytes(bytes(tampered))
        try:
            returned = store.load(KEY_ID)
        except SecretStoreError as error:
            assert error.code == "secret_unreadable", position
            continue
        assert returned == genuine, position                # never a different secret
        tolerated.append(position)
    assert len(tolerated) <= 16 and all(p < 24 for p in tolerated), tolerated
    for cut in (0, 1, len(original) // 2, len(original) - 1):
        store.path.write_bytes(original[:cut])
        with pytest.raises(SecretStoreError):
            store.load(KEY_ID)
    store.path.write_bytes(original + b"trailing bytes")   # DPAPI ignores bytes after a complete blob: harmless, never another secret
    try:
        assert store.load(KEY_ID) == genuine
    except SecretStoreError as error:
        assert error.code == "secret_unreadable"
    store.path.write_bytes(original)
    assert store.load(KEY_ID) == genuine                   # the intact blob still opens


@windows_only
def test_a_valid_dpapi_blob_with_the_wrong_shape_is_rejected_without_echoing_it(store):
    for payload in (b"not json CANARY-SECRET-SHAPE", json.dumps({"format": 1}).encode(), json.dumps([1, 2]).encode(),
                    json.dumps({"format": 2, "key_id": KEY_ID, "secret": base64.b64encode(bytes(32)).decode()}).encode(),
                    json.dumps({"format": 1, "key_id": KEY_ID, "secret": base64.b64encode(bytes(31)).decode()}).encode(),
                    json.dumps({"format": 1, "key_id": KEY_ID, "secret": "!!!not base64!!!"}).encode(),
                    json.dumps({"format": 1, "key_id": 5, "secret": base64.b64encode(bytes(32)).decode()}).encode()):
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_bytes(v2_keys.dpapi_protect(payload))
        with pytest.raises(SecretStoreError) as caught:
            store.load(KEY_ID)
        assert caught.value.code == "secret_unreadable"
        assert "CANARY" not in str(caught.value) + repr(caught.value)
        assert caught.value.__cause__ is None and caught.value.__context__ is None       # nothing of the payload can be reached


@windows_only
def test_create_never_overwrites_an_existing_secret(store):
    store.create(KEY_ID)
    before = store.path.read_bytes()
    with pytest.raises(SecretStoreError) as caught:
        store.create(KEY_ID)
    assert caught.value.code == "secret_exists" and store.path.read_bytes() == before


@windows_only
def test_create_rejects_a_malformed_key_id_and_writes_nothing(store):
    from collector.v2_events import UnsafeEventError
    with pytest.raises(UnsafeEventError):
        store.create("not-a-key-id")
    assert not store.path.exists()


@windows_only
def test_a_missing_secret_fails_closed(store):
    assert not store.exists()
    with pytest.raises(SecretStoreError) as caught:
        store.load(KEY_ID)
    assert caught.value.code == "secret_missing"


@windows_only
def test_no_temp_file_is_left_behind_by_create(store):
    store.create(KEY_ID)
    assert [p.name for p in store.path.parent.iterdir()] == ["v2_key.dpapi"]


@windows_only
def test_the_secret_never_appears_in_any_error_or_repr(store):
    store.create(KEY_ID)
    secret = store.load(KEY_ID)
    for text in (repr(store), str(store.path), str(SecretStoreError("secret_missing")), repr(SecretStoreError("secret_unreadable"))):
        assert secret.hex() not in text and base64.b64encode(secret).decode() not in text


# =====================================================================================================================
# ACL (parsed from SDDL, so the verdicts are testable without changing real permissions)
# =====================================================================================================================

PROTECTED_DIR = "D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
PROTECTED_FILE = "D:PAI(A;;FA;;;SY)(A;;FA;;;BA)"
LONG_SIDS = "D:P(A;OICI;FA;;;S-1-5-18)(A;OICI;FA;;;S-1-5-32-544)"
USERS_ALLOWED = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)"
EVERYONE = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;WD)"
AUTHENTICATED = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;AU)"
A_SPECIFIC_USER = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;S-1-5-21-1111111111-2222222222-3333333333-1001)"
INHERITING = "D:AI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
WITH_DENY = "D:P(D;OICI;FA;;;BU)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"


@pytest.mark.parametrize(("sddl", "require_protected", "verdict"), [
    (PROTECTED_DIR, True, "protected"), (PROTECTED_FILE, False, "protected"), (LONG_SIDS, True, "protected"),
    (WITH_DENY, True, "protected"),
    (USERS_ALLOWED, True, "exposed"), (EVERYONE, True, "exposed"), (AUTHENTICATED, True, "exposed"), (A_SPECIFIC_USER, True, "exposed"),
    (USERS_ALLOWED, False, "exposed"),
    (INHERITING, True, "exposed"),                         # a folder must have inheritance cut
    (INHERITING, False, "protected"),                      # a file inherits its folder's cut ACL: only the entries matter
    ("D:P", True, "unknown"), ("D:P(garbage)", True, "unknown"), ("", True, "unknown"),
])
def test_the_acl_verdict_is_exposed_unless_only_system_and_administrators_are_allowed(monkeypatch, sddl, require_protected, verdict):
    as_windows(monkeypatch)
    monkeypatch.setattr(v2_keys, "_dacl_of", lambda _path: sddl or None)
    assert v2_keys.acl_state_of("ignored", require_protected=require_protected) == verdict


def test_an_unreadable_acl_is_unknown_never_guessed_protected(monkeypatch):
    as_windows(monkeypatch)
    monkeypatch.setattr(v2_keys, "_dacl_of", lambda _path: None)
    assert v2_keys.acl_state_of("x", require_protected=True) == "unknown"


def test_off_windows_the_acl_is_unknown():
    if sys.platform != "win32":
        assert v2_keys.acl_state_of("x", require_protected=True) == "unknown"


def test_the_store_verdict_combines_folder_and_file(monkeypatch, tmp_path):
    as_windows(monkeypatch)
    path = tmp_path / "k"
    path.write_bytes(b"x")
    store = DpapiSecretStore(path)
    replies = {}
    monkeypatch.setattr(v2_keys, "acl_state_of", lambda p, *, require_protected: replies[require_protected])
    for folder, file, expected in (("protected", "protected", "protected"), ("exposed", "protected", "exposed"),
                                   ("protected", "exposed", "exposed"), ("unknown", "protected", "unknown"),
                                   ("unknown", "exposed", "exposed")):
        replies.update({True: folder, False: file})
        assert store.acl_state() == expected, (folder, file)


@windows_only
def test_the_real_icacls_round_trip_detects_an_extra_principal(tmp_path):
    """Real icacls: a folder locked to SYSTEM+Administrators PLUS the current user (so the test can clean up) is reported EXPOSED,
    and the un-locked default folder (inherited ACL) is EXPOSED too."""
    import getpass
    folder = tmp_path / "locked"
    folder.mkdir()
    assert v2_keys.acl_state_of(folder, require_protected=True) == "exposed"           # inherits the parent's ACL
    v2_keys.protect_directory(folder, extra_principals=(getpass.getuser(),))
    assert v2_keys.acl_state_of(folder, require_protected=True) == "exposed"           # cut, but the current user still has access
    (folder / "f.bin").write_bytes(b"x")
    assert v2_keys.acl_state_of(folder / "f.bin", require_protected=False) == "exposed"


@windows_only
def test_the_real_dacl_reader_returns_an_sddl_line(tmp_path):
    dacl = v2_keys._dacl_of(tmp_path)
    assert dacl is not None and dacl.startswith("D:") and re.search(r"\(A;", dacl)


@pytest.mark.parametrize(("verdict", "code"), [("exposed", "secret_exposed"), ("unknown", "secret_acl_unverified"),
                                               ("garbage", "secret_acl_unverified"), (None, "secret_acl_unverified")])
def test_initialise_fails_closed_unless_the_folder_is_verified_protected_and_writes_nothing(monkeypatch, tmp_path, verdict, code):
    as_windows(monkeypatch)
    store = DpapiSecretStore(tmp_path / "s" / "k")
    monkeypatch.setattr(v2_keys, "protect_directory", lambda *_a, **_k: None)
    monkeypatch.setattr(v2_keys, "acl_state_of", lambda *_a, **_k: verdict)
    created = []
    monkeypatch.setattr(DpapiSecretStore, "create", lambda self, key_id: created.append(key_id))
    with pytest.raises(SecretStoreError) as caught:
        v2_keys.initialise(store, KEY_ID)
    assert caught.value.code == code and str(caught.value) == code and created == []
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_initialise_locks_verifies_creates_then_verifies_the_result(monkeypatch, tmp_path):
    as_windows(monkeypatch)
    store = DpapiSecretStore(tmp_path / "s" / "k")
    order = []
    monkeypatch.setattr(v2_keys, "protect_directory", lambda *_a, **_k: order.append("protect"))
    monkeypatch.setattr(v2_keys, "acl_state_of", lambda *_a, **_k: order.append("verify") or "protected")
    monkeypatch.setattr(DpapiSecretStore, "create", lambda self, key_id: order.append("create"))
    v2_keys.initialise(store, KEY_ID)
    assert order == ["protect", "verify", "create", "verify"]                       # the folder, the create, then the created store


def test_protect_directory_grants_only_system_and_administrators_by_sid(monkeypatch, tmp_path):
    as_windows(monkeypatch)
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(v2_keys.subprocess, "run", fake_run)
    v2_keys.protect_directory(tmp_path)
    (argv,) = calls
    assert argv[0] == "icacls" and "/inheritance:r" in argv
    grants = [a for a in argv if a.startswith("*S-")]
    assert grants == ["*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"]
    granted = " ".join(argv[2:])                            # the path (argv[1]) may legitimately contain "Users"
    assert "Users" not in granted and "Everyone" not in granted and "Authenticated" not in granted


def test_a_failed_icacls_is_a_fixed_code_error(monkeypatch, tmp_path):
    as_windows(monkeypatch)
    monkeypatch.setattr(v2_keys.subprocess, "run", lambda *_a, **_k: types.SimpleNamespace(returncode=5))
    with pytest.raises(SecretStoreError) as caught:
        v2_keys.protect_directory(tmp_path)
    assert caught.value.code == "secret_dir_not_protected"


@pytest.mark.skipif(sys.platform == "win32", reason="off-Windows behaviour")
def test_off_windows_the_store_refuses_to_operate(tmp_path):
    store = DpapiSecretStore(tmp_path / "k")
    with pytest.raises(SecretStoreError) as caught:
        v2_keys.dpapi_protect(b"x")
    assert caught.value.code == "secret_store_unavailable"
    with pytest.raises(SecretStoreError):
        store.create(KEY_ID)


def test_the_command_line_prints_only_fixed_lines_and_never_the_secret(monkeypatch, tmp_path, capsys):
    from collector_v2_support import write_config
    config = write_config(tmp_path / "root")
    fake = FakeStore()
    monkeypatch.setattr(v2_keys, "DpapiSecretStore", lambda _path: fake)
    monkeypatch.setattr(v2_keys, "initialise", lambda _store, _key_id: None)
    assert v2_keys.main(["init", "--config", str(config)]) == 0
    assert v2_keys.main(["check", "--config", str(config)]) == 0
    output = capsys.readouterr()
    assert fake.master.hex() not in output.out + output.err and str(fake.master) not in output.out + output.err


def test_the_command_line_reports_a_fixed_code_on_failure(monkeypatch, tmp_path, capsys):
    from collector_v2_support import write_config
    config = write_config(tmp_path / "root")
    monkeypatch.setattr(v2_keys, "DpapiSecretStore", lambda _path: FakeStore(bound_key_id=OTHER_KEY_ID))
    assert v2_keys.main(["check", "--config", str(config)]) == 1
    assert "secret_key_mismatch" in capsys.readouterr().err


def test_the_secret_bytes_length_and_blob_format_are_frozen():
    assert v2_keys.SECRET_BYTES == 32 and v2_keys.BLOB_FORMAT == 1
    assert v2_keys.SYSTEM_SID == "S-1-5-18" and v2_keys.ADMINISTRATORS_SID == "S-1-5-32-544"


def test_this_module_never_writes_the_secret_anywhere_but_the_dpapi_blob():
    source = Path(v2_keys.__file__).read_text(encoding="utf-8")
    assert "os.environ" not in source and "print(secret" not in source and "logging" not in source


# =====================================================================================================================
# Fail closed unless the ACL is VERIFIED protected: run, initialize, load
# =====================================================================================================================

@pytest.mark.parametrize(("verdict", "code"), [("exposed", "secret_exposed"), ("unknown", "secret_acl_unverified"),
                                               ("garbage", "secret_acl_unverified"), ("", "secret_acl_unverified"),
                                               (None, "secret_acl_unverified"), ("PROTECTED", "secret_acl_unverified")])
def test_require_protected_accepts_only_the_exact_protected_verdict(verdict, code):
    with pytest.raises(SecretStoreError) as caught:
        v2_keys.require_protected(verdict)
    assert caught.value.code == code and str(caught.value) == code
    assert caught.value.summary == "" and caught.value.__cause__ is None and caught.value.__context__ is None


def test_require_protected_accepts_protected():
    assert v2_keys.require_protected("protected") is None


@pytest.mark.parametrize(("verdict", "code"), [("exposed", "secret_exposed"), ("unknown", "secret_acl_unverified")])
def test_loading_a_persistent_secret_checks_the_acl_before_reading_a_single_byte_of_it(monkeypatch, tmp_path, verdict, code):
    as_windows(monkeypatch)
    path = tmp_path / "v2_key.dpapi"
    path.write_bytes(b"opaque blob")
    store = DpapiSecretStore(path)
    monkeypatch.setattr(DpapiSecretStore, "acl_state", lambda self: verdict)
    monkeypatch.setattr(v2_keys, "dpapi_unprotect", lambda _blob: pytest.fail("the secret was decrypted although the ACL was not verified"))
    monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("the secret file was read although the ACL was not verified"))
    with pytest.raises(SecretStoreError) as caught:
        store.load(KEY_ID)
    assert caught.value.code == code and caught.value.__context__ is None


@windows_only
def test_the_real_store_refuses_a_secret_in_a_folder_that_inherits_the_users_acl(tmp_path):
    """No stubbing: a normal temp folder is readable by the current user, so the REAL ACL check reports it exposed and load fails closed."""
    plain = DpapiSecretStore(tmp_path / "secrets" / "v2_key.dpapi")
    TrustedAclStore(plain.path).create(KEY_ID)
    with pytest.raises(SecretStoreError) as caught:
        plain.load(KEY_ID)
    assert caught.value.code == "secret_exposed"
    assert len(TrustedAclStore(plain.path).load(KEY_ID)) == 32                # the same file opens once the ACL verdict is trusted


@windows_only
def test_an_unreadable_acl_on_windows_is_an_unverified_refusal_not_a_pass(monkeypatch, tmp_path):
    store = TrustedAclStore(tmp_path / "secrets" / "v2_key.dpapi")
    store.create(KEY_ID)
    unverified = DpapiSecretStore(store.path)
    monkeypatch.setattr(v2_keys, "_dacl_of", lambda _path: None)                # icacls could not be read
    with pytest.raises(SecretStoreError) as caught:
        unverified.load(KEY_ID)
    assert caught.value.code == "secret_acl_unverified"


def test_initialise_removes_a_secret_it_cannot_verify_after_creating_it(monkeypatch, tmp_path):
    as_windows(monkeypatch)
    store = DpapiSecretStore(tmp_path / "s" / "k")
    monkeypatch.setattr(v2_keys, "protect_directory", lambda *_a, **_k: None)
    verdicts = iter(["protected", "unknown"])
    monkeypatch.setattr(v2_keys, "acl_state_of", lambda *_a, **_k: next(verdicts, "unknown"))
    monkeypatch.setattr(DpapiSecretStore, "create", lambda self, key_id: self.path.write_bytes(b"just created"))
    with pytest.raises(SecretStoreError) as caught:
        v2_keys.initialise(store, KEY_ID)
    assert caught.value.code == "secret_acl_unverified" and not store.path.exists()


def test_the_check_command_reports_a_fixed_code_when_the_acl_cannot_be_verified(monkeypatch, tmp_path, capsys):
    from collector_v2_support import write_config
    config = write_config(tmp_path / "root")
    class Guarded(FakeStore):
        def load(self, expected_key_id):
            v2_keys.require_protected(self.acl_state())
            return super().load(expected_key_id)

    monkeypatch.setattr(v2_keys, "DpapiSecretStore", lambda _path: Guarded(acl="unknown"))
    assert v2_keys.main(["check", "--config", str(config)]) == 1
    error = capsys.readouterr().err
    assert "secret_acl_unverified" in error
