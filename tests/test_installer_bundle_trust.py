"""Tests for the trusted bundle verifier, M2a: installer/SortView.Setup.Core/BundleVerifier.cs.

The verifier accepts a frozen Collector release bundle only when MANIFEST.json hashes to an expected SHA-256 supplied
from outside the bundle, and the bundle holds exactly the files it lists -- nothing more, nothing less, no link.

  1. STATIC checks (run everywhere): the verifier cannot start a process, run PowerShell, use the network or change the
     file system; it is built into Core with no extra shipped dependency; it claims no publisher authenticity.
  2. BEHAVIOR (Windows only): generated fixture bundles (fake bytes, never a real Collector) in pytest's temporary
     folders, each verified by the built SortView.Setup.Core.dll in one Windows PowerShell 5.1 process. Every fixture
     tree is snapshotted before and after: verification changes nothing.
"""

from __future__ import annotations

import ctypes
import getpass
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from test_installer_bridge import _MSBUILD, _POWERSHELL, needs_bridge

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_DIR = REPO_ROOT / "installer" / "SortView.Setup.Core"
CORE_PROJECT = CORE_DIR / "SortView.Setup.Core.csproj"
VERIFIER_CODE = CORE_DIR / "BundleVerifier.cs"

PRODUCT = "SortView Collector"
VERSION = "1.2.3"
FILES = {
    "setup.ps1": b"# fixture, not a script",
    "install.ps1": b"# fixture, not a script",
    "collector_config.example.json": b'{"api_url": "https://fixture.invalid"}',
    "tools/set-api-token.ps1": b"# fixture",
    "runtime/SortViewCollector.exe": b"fixture: not a program",
    "runtime/_internal/library.dll": b"fixture: not a library",
    "runtime/_internal/numpy/core/module.pyd": b"fixture: not an extension",
    "runtime/_internal/data[1].txt": b"a name with brackets",
    "runtime/_internal/Lorem ipsum.txt": b"a name with a space",
}


# =====================================================================================================================
# 1. STATIC checks
# =====================================================================================================================

def _code() -> str:
    text = VERIFIER_CODE.read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def test_the_verifier_cannot_run_anything_connect_or_change_the_file_system():
    code = _code()
    for forbidden in (
        "System.Diagnostics", "Process", "System.Management.Automation", "PowerShell", "Runspace", "System.Net", "WebClient",
        "Http", "Socket", "DllImport", "Assembly.Load", "Activator", "File.Write", "File.Create", "File.Delete", "File.Move",
        "File.Copy", "File.Replace", "File.SetAttributes", "Directory.Create", "Directory.Delete", "Directory.Move",
        "FileMode.Create", "FileMode.OpenOrCreate", "FileMode.Append", "FileMode.Truncate", "FileAccess.Write",
        "FileAccess.ReadWrite", ".Delete(", ".MoveTo(", ".CopyTo(", ".Attributes =", "setup.ps1",
    ):
        assert forbidden not in code, forbidden
    # Reading is all it does: every stream is opened for reading only.
    assert code.count("new FileStream(") == 2 and code.count("FileMode.Open, FileAccess.Read, FileShare.Read") == 2


def test_the_verifier_is_part_of_core_with_no_extra_shipped_dependency():
    project = CORE_PROJECT.read_text(encoding="utf-8")
    assert '<Compile Include="BundleVerifier.cs" />' in project
    # JavaScriptSerializer is part of .NET Framework (in the GAC): referenced, never copied into the build output.
    assert '<Reference Include="System.Web.Extensions" />' in project
    assert "<PackageReference" not in project and "HintPath>" + "System.Web" not in project


def test_the_verifier_reads_the_tree_before_the_manifest_and_hashes_the_manifest_before_parsing_it():
    code = _code()
    core = code[code.index("private static BundleVerification VerifyCore"):code.index("private static string NormalizeRoot")]
    walk, read, compare, parse = (core.index(s) for s in ("Walk(root, report)", "ReadAll(manifestFile", "ManifestHashMismatch",
                                                          "Parse(manifestBytes"))
    assert walk < read < compare < parse
    # Listed paths are validated before any is combined with the bundle folder (the only join is the walk's own).
    assert core.index("IsSafeRelativePath(entry.Path)") < core.index("tree.Files.TryGetValue(entry.Path")
    assert "Path.Combine" not in code


def test_the_verifier_claims_integrity_against_an_expected_hash_not_publisher_authenticity():
    text = VERIFIER_CODE.read_text(encoding="utf-8")
    prose = re.sub(r"\s+", " ", text.replace("///", " "))
    assert "says nothing about who published it" in prose
    assert "IsTrusted" not in text and "authentic" not in text.lower().replace("authenticity", "")


# =====================================================================================================================
# 2. BEHAVIOR -- fixture bundles
# =====================================================================================================================

FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_SYSTEM, FILE_ATTRIBUTE_REPARSE_POINT = 0x2, 0x4, 0x400


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest_bytes(entries: list[dict], *, product: str = PRODUCT, version: str = VERSION) -> bytes:
    # Exactly as collector/build_release.py writes it: sorted, indent=2, a trailing newline, UTF-8, LF.
    manifest = {"product": product, "version": version, "built_at": "2026-01-01T00:00:00.000000Z",
                "files": sorted(entries, key=lambda e: e["path"])}
    return (json.dumps(manifest, indent=2) + "\n").encode("utf-8")


def _entries(files: dict[str, bytes]) -> list[dict]:
    return [{"path": path, "sha256": _sha256(data), "size_bytes": len(data)} for path, data in files.items()]


def _write(root: Path, files: dict[str, bytes] | None = None, manifest: bytes | None = None) -> str:
    """Writes a bundle; returns the SHA-256 of its MANIFEST.json (the caller's independently supplied value)."""
    files = FILES if files is None else files
    root.mkdir(parents=True)
    for path, data in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    manifest = _manifest_bytes(_entries(files)) if manifest is None else manifest
    (root / "MANIFEST.json").write_bytes(manifest)
    return _sha256(manifest)


def _attributes(path: Path, add: int) -> None:
    current = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    assert ctypes.windll.kernel32.SetFileAttributesW(str(path), current | add), path


def _junction(link: Path, target: Path) -> None:
    completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _is_reparse(path: Path) -> bool:
    return bool(os.lstat(path).st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _snapshot(root: Path) -> dict:
    """Every entry under root -- size, modification time, attributes, and a file's SHA-256 -- never through a link."""
    if not os.path.lexists(root):
        return {}
    snap = {}
    top = os.lstat(root)
    snap["."] = (top.st_mtime_ns, top.st_file_attributes)
    if _is_reparse(root) or not root.is_dir():
        return snap
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        for name in list(dirnames):
            path = Path(dirpath) / name
            st = os.lstat(path)
            snap[str(path.relative_to(root))] = ("dir", st.st_mtime_ns, st.st_file_attributes)
            if st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT:
                dirnames.remove(name)
        for name in filenames:
            path = Path(dirpath) / name
            st = os.lstat(path)
            data = b"" if st.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT else path.read_bytes()
            snap[str(path.relative_to(root))] = (st.st_size, st.st_mtime_ns, st.st_file_attributes, _sha256(data))
    return snap


def _icacls(path: Path, *args: str) -> None:
    completed = subprocess.run(["icacls", str(path), *args], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr


UNSAFE_PATHS = {
    "parent_traversal": "../escape.dll",
    "inner_traversal": "runtime/../../escape.dll",
    "dot_segment": "./dot.dll",
    "rooted": "/rooted.dll",
    "drive_letter": "C:/drive.dll",
    "drive_relative": "C:drive.dll",
    "unc": "//server/share/x.dll",
    "backslash": "runtime\\back.dll",
    "wildcard_star": "runtime/*.dll",
    "wildcard_question": "runtime/a?.dll",
    "alternate_stream": "runtime/_internal/library.dll:payload",
    "reserved_con": "runtime/CON",
    "reserved_with_extension": "runtime/con.txt",
    "reserved_lpt": "runtime/LPT1.dll",
    "reserved_nul_json": "runtime/NUL.json",
    "trailing_dot": "runtime/trailing.",
    "trailing_space": "runtime/trailing ",
    "empty_segment": "runtime//double.dll",
    "trailing_slash": "runtime/",
    "empty": "",
    "angle_brackets": "runtime/a<b>.dll",
    "control_character": "runtime/tab\t.dll",
    "the_manifest_itself": "MANIFEST.json",
}

MALFORMED_MANIFESTS = {
    "not_json": b"this is not json\n",
    "invalid_utf8": b'{"product": "\xff\xfe"}',
    "array_root": b"[]\n",
    "missing_files": json.dumps({"product": PRODUCT, "version": VERSION, "built_at": "x"}).encode(),
    "files_not_a_list": json.dumps({"product": PRODUCT, "version": VERSION, "built_at": "x", "files": "nope"}).encode(),
    "files_an_object": json.dumps({"product": PRODUCT, "version": VERSION, "built_at": "x", "files": {"a": 1}}).encode(),
    "extra_top_level_key": json.dumps({"product": PRODUCT, "version": VERSION, "built_at": "x", "files": [], "x": 1}).encode(),
    "product_not_a_string": json.dumps({"product": 7, "version": VERSION, "built_at": "x", "files": []}).encode(),
    "entry_missing_sha256": json.dumps({"product": PRODUCT, "version": VERSION, "built_at": "x",
                                        "files": [{"path": "setup.ps1", "size_bytes": 1}]}).encode(),
    "entry_extra_key": json.dumps({"product": PRODUCT, "version": VERSION, "built_at": "x",
                                   "files": [{"path": "setup.ps1", "sha256": "0" * 64, "size_bytes": 1, "x": 1}]}).encode(),
    "sha256_not_hex": json.dumps({"product": PRODUCT, "version": VERSION, "built_at": "x",
                                  "files": [{"path": "setup.ps1", "sha256": "z" * 64, "size_bytes": 1}]}).encode(),
    "size_negative": json.dumps({"product": PRODUCT, "version": VERSION, "built_at": "x",
                                 "files": [{"path": "setup.ps1", "sha256": "0" * 64, "size_bytes": -1}]}).encode(),
    "size_a_string": json.dumps({"product": PRODUCT, "version": VERSION, "built_at": "x",
                                 "files": [{"path": "setup.ps1", "sha256": "0" * 64, "size_bytes": "1"}]}).encode(),
    "size_a_fraction": json.dumps({"product": PRODUCT, "version": VERSION, "built_at": "x",
                                   "files": [{"path": "setup.ps1", "sha256": "0" * 64, "size_bytes": 1.5}]}).encode(),
    "deeply_nested": (b'{"product": "SortView Collector", "version": "1.2.3", "built_at": "x", "files": [' + b"[" * 40 + b"]" * 40
                      + b"]}"),
}


def _build_cases(base: Path) -> tuple[dict, dict]:
    """Returns (cases, notes): each case is {root, hash, version}; notes records what needs cleaning up or skipping."""
    cases: dict[str, dict] = {}
    notes: dict = {"denied": [], "skipped": []}

    def add(name: str, root: Path, expected_hash: str, version: str | None = None) -> None:
        cases[name] = {"root": str(root), "hash": expected_hash, "version": version}

    def bundle(name: str, **kwargs) -> tuple[Path, str]:
        root = base / name
        sha = _write(root, **kwargs)
        add(name, root, sha)
        return root, sha

    bundle("valid")
    root, sha = bundle("valid_with_expected_version")
    cases["valid_with_expected_version"]["version"] = VERSION
    root, sha = bundle("version_mismatch")
    cases["version_mismatch"]["version"] = "9.9.9"
    root, sha = bundle("expected_version_invalid")
    cases["expected_version_invalid"]["version"] = "1.2.3; rm"
    root, sha = bundle("wrong_expected_hash")
    cases["wrong_expected_hash"]["hash"] = _sha256(b"some other manifest")
    root, sha = bundle("expected_hash_not_hex")
    cases["expected_hash_not_hex"]["hash"] = "not-a-sha256"
    root, sha = bundle("expected_hash_missing")
    cases["expected_hash_missing"]["hash"] = ""

    root, sha = bundle("modified_manifest")
    (root / "MANIFEST.json").write_bytes((root / "MANIFEST.json").read_bytes() + b" ")
    root, sha = bundle("modified_manifest_and_extra_file")  # the inventory is never compared against an untrusted manifest
    (root / "MANIFEST.json").write_bytes((root / "MANIFEST.json").read_bytes().replace(b"1.2.3", b"1.2.4"))
    (root / "planted.dll").write_bytes(b"planted")

    root, sha = bundle("modified_content_same_size")
    (root / "runtime/_internal/library.dll").write_bytes(b"FIXTURE: NOT A LIBRARY")
    root, sha = bundle("modified_size")
    (root / "runtime/_internal/library.dll").write_bytes(b"fixture: not a library, longer now")
    root, sha = bundle("missing_file")
    (root / "runtime/_internal/library.dll").unlink()

    root, sha = bundle("extra_visible_file")
    (root / "runtime/_internal/planted.dll").write_bytes(b"planted")
    root, sha = bundle("extra_hidden_file")
    (root / "runtime/_internal/planted.dll").write_bytes(b"planted")
    _attributes(root / "runtime/_internal/planted.dll", FILE_ATTRIBUTE_HIDDEN)
    root, sha = bundle("extra_system_file")
    (root / "runtime/_internal/planted.dll").write_bytes(b"planted")
    _attributes(root / "runtime/_internal/planted.dll", FILE_ATTRIBUTE_SYSTEM)
    root, sha = bundle("file_in_extra_hidden_folder")
    (root / "runtime/_internal/sub").mkdir()
    (root / "runtime/_internal/sub/planted.dll").write_bytes(b"planted")
    _attributes(root / "runtime/_internal/sub", FILE_ATTRIBUTE_HIDDEN)
    root, sha = bundle("extra_empty_folder")
    (root / "runtime/_internal/empty").mkdir()
    root, sha = bundle("extra_root_file")
    (root / "notes.txt").write_bytes(b"planted")
    root, sha = bundle("manifest_missing")
    (root / "MANIFEST.json").unlink()
    root, sha = bundle("manifest_wrong_case")
    (root / "MANIFEST.json").rename(root / "manifest.json")

    # Listed paths: duplicates, collisions, a listed file that is a folder on disk.
    entries = _entries(FILES)
    root = base / "duplicate_path"
    add("duplicate_path", root, _write(root, manifest=_manifest_bytes(entries + [entries[0]])))
    lib = next(e for e in entries if e["path"] == "runtime/_internal/library.dll")
    root = base / "case_collision"
    add("case_collision", root, _write(root, manifest=_manifest_bytes(entries + [dict(lib, path="runtime/_internal/LIBRARY.dll")])))
    root = base / "file_and_folder_collision"
    add("file_and_folder_collision", root, _write(root, manifest=_manifest_bytes(entries + [dict(lib, path="runtime/_internal")])))
    root = base / "listed_file_is_a_folder"
    files = dict(FILES)
    add("listed_file_is_a_folder", root, _write(root, files=files, manifest=_manifest_bytes(entries + [dict(lib, path="runtime/folder")])))
    (root / "runtime/folder").mkdir()

    for name, bad in UNSAFE_PATHS.items():
        root = base / f"unsafe_{name}"
        add(f"unsafe_{name}", root, _write(root, manifest=_manifest_bytes(entries + [{"path": bad, "sha256": "0" * 64, "size_bytes": 0}])))

    for name, data in MALFORMED_MANIFESTS.items():
        root = base / f"malformed_{name}"
        add(f"malformed_{name}", root, _write(root, manifest=data))
    root = base / "wrong_product"
    add("wrong_product", root, _write(root, manifest=_manifest_bytes(entries, product="Something Else")))
    root = base / "invalid_manifest_version"
    add("invalid_manifest_version", root, _write(root, manifest=_manifest_bytes(entries, version="one.two")))

    # The bundle folder itself.
    add("root_missing", base / "no-such-bundle", _sha256(b"x"))
    (base / "root_is_a_file.txt").write_bytes(b"not a folder")
    add("root_is_a_file", base / "root_is_a_file.txt", _sha256(b"x"))
    add("root_relative", Path("relative/bundle"), _sha256(b"x"))
    add("root_is_a_drive", Path(str(base)[:3]), _sha256(b"x"))

    # Reparse points: none is ever entered or read through.
    outside = base / "outside-the-bundle"
    outside.mkdir()
    (outside / "from-outside.dll").write_bytes(b"from outside")
    real = base / "real-bundle"
    sha = _write(real)
    _junction(base / "root_junction", real)
    add("root_junction", base / "root_junction", sha)
    root, sha = bundle("interior_junction")
    _junction(root / "runtime/_internal/linked", outside)
    root, sha = bundle("dangling_junction")  # entering it would be a path error, not a clean refusal
    _junction(root / "runtime/_internal/linked", base / "does-not-exist")
    root = base / "listed_through_junction"
    add("listed_through_junction", root, _write(root, manifest=_manifest_bytes(
        entries + [{"path": "runtime/_internal/linked/from-outside.dll", "sha256": _sha256(b"from outside"), "size_bytes": 12}])))
    _junction(root / "runtime/_internal/linked", outside)
    root, sha = bundle("junction_and_wrong_hash")  # the tree is checked before the manifest is even read
    _junction(root / "runtime/_internal/linked", outside)
    cases["junction_and_wrong_hash"]["hash"] = _sha256(b"wrong")

    def manifest_link(r: Path) -> None:
        (r / "MANIFEST.json").replace(r / "real-manifest.json")
        os.symlink(r / "real-manifest.json", r / "MANIFEST.json")

    for name, make_link in (("file_symlink", lambda r: os.symlink(outside / "from-outside.dll", r / "runtime/_internal/link.dll")),
                            ("manifest_symlink", manifest_link)):
        root, sha = bundle(name)
        try:
            make_link(root)
        except OSError:
            notes["skipped"].append(name)
            del cases[name]

    # Read failures: a listed file, and a folder, this user is denied reading (fixture ACLs only, restored afterwards).
    user = getpass.getuser()
    root, sha = bundle("unreadable_file")
    notes["denied"].append((root / "runtime/_internal/library.dll", user))
    root, sha = bundle("unreadable_folder")
    notes["denied"].append((root / "runtime/_internal/numpy", user))
    return cases, notes


_VERIFY_SCRIPT = r"""
param([string]$CorePath, [string]$CasesPath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
Add-Type -Path $CorePath
$cases = Get-Content -LiteralPath $CasesPath -Raw | ConvertFrom-Json
$out = [ordered]@{}
foreach ($case in $cases.PSObject.Properties) {
    $c = $case.Value
    # [NullString]::Value: a real null. PowerShell turns a plain $null into "" for a .NET string parameter.
    $result = [SortView.Setup.Core.BundleVerifier]::Verify([string]$c.root, [string]$c.hash, $(if ($null -eq $c.version) { [NullString]::Value } else { [string]$c.version }))
    $out[$case.Name] = [ordered]@{
        verified = $result.IsVerified; version = $result.Version; file_count = $result.FileCount; truncated = $result.ProblemsTruncated
        problems = @($result.Problems | ForEach-Object { "$($_.Code)|$($_.Path)" }) }
}
ConvertTo-Json -InputObject $out -Depth 5 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


@pytest.fixture(scope="module")
def core_dll() -> Path:
    completed = subprocess.run([_MSBUILD, str(CORE_PROJECT), "/p:Configuration=Release", "/nologo", "/v:minimal"],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return CORE_DIR / "bin" / "Release" / "SortView.Setup.Core.dll"


@pytest.fixture(scope="module")
def results(tmp_path_factory, core_dll) -> dict:
    base = tmp_path_factory.mktemp("bundles")
    cases, notes = _build_cases(base)
    # Only fixture folders are snapshotted (never a case whose "bundle" is outside them, such as a drive root).
    before = {name: _snapshot(Path(case["root"])) for name, case in cases.items()
              if Path(case["root"]).is_absolute() and Path(case["root"]).is_relative_to(base)}
    for path, user in notes["denied"]:
        # (RD) only: reading a file's data, listing a folder. Its own permissions stay readable, so the entry can be
        # removed afterwards (a generic (R) deny would also lock out that removal).
        _icacls(path, "/deny", f"{user}:(RD)")
    try:
        (base / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
        (base / "verify.ps1").write_text(_VERIFY_SCRIPT, encoding="utf-8")
        completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                    str(base / "verify.ps1"), "-CorePath", str(core_dll), "-CasesPath", str(base / "cases.json"),
                                    "-OutPath", str(base / "out.json")], capture_output=True, text=True, timeout=300, check=False)
        assert (base / "out.json").exists(), completed.stdout + completed.stderr
        out = json.loads((base / "out.json").read_text(encoding="utf-8-sig"))
    finally:
        for path, user in notes["denied"]:
            _icacls(path, "/remove:d", user)
    after = {name: _snapshot(Path(cases[name]["root"])) for name in before}
    for dirpath, dirnames, _ in os.walk(base, topdown=True, followlinks=False):  # junctions removed as links only
        for name in list(dirnames):
            if _is_reparse(Path(dirpath) / name):
                os.rmdir(Path(dirpath) / name)
                dirnames.remove(name)
    out["_unchanged"] = {name: before[name] == after[name] for name in before}
    out["_skipped"] = notes["skipped"]
    return out


def _codes(result: dict) -> set[str]:
    return {p.split("|", 1)[0] for p in result["problems"]}


@needs_bridge
@pytest.mark.parametrize("case", ["valid", "valid_with_expected_version"])
def test_an_exact_bundle_is_verified_with_its_independently_supplied_hash(results, case):
    result = results[case]
    assert result["verified"] is True and result["problems"] == [], result
    assert result["version"] == VERSION and result["file_count"] == len(FILES)


EXPECTED = {
    "version_mismatch": {"VersionMismatch"},
    "expected_version_invalid": {"VersionInvalid"},
    "wrong_expected_hash": {"ManifestHashMismatch"},
    "expected_hash_not_hex": {"ExpectedHashInvalid"},
    "expected_hash_missing": {"ExpectedHashInvalid"},
    "modified_manifest": {"ManifestHashMismatch"},
    "modified_manifest_and_extra_file": {"ManifestHashMismatch"},
    "modified_content_same_size": {"HashMismatch"},
    "modified_size": {"SizeMismatch"},
    "missing_file": {"FileMissing"},
    "extra_visible_file": {"UnexpectedFile"},
    "extra_hidden_file": {"UnexpectedFile"},
    "extra_system_file": {"UnexpectedFile"},
    "file_in_extra_hidden_folder": {"UnexpectedFile", "UnexpectedDirectory"},
    "extra_empty_folder": {"UnexpectedDirectory"},
    "extra_root_file": {"UnexpectedFile"},
    "manifest_missing": {"ManifestMissing"},
    "manifest_wrong_case": {"ManifestMissing"},
    "duplicate_path": {"DuplicatePath"},
    "case_collision": {"PathCollision"},
    "file_and_folder_collision": {"PathCollision"},
    "listed_file_is_a_folder": {"NotAFile", "UnexpectedDirectory"},
    "wrong_product": {"ProductMismatch"},
    "invalid_manifest_version": {"VersionInvalid"},
    "root_missing": {"BundleRootMissing"},
    "root_is_a_file": {"BundleRootInvalid"},
    "root_relative": {"BundleRootInvalid"},
    "root_is_a_drive": {"BundleRootInvalid"},
    "root_junction": {"ReparsePoint"},
    "interior_junction": {"ReparsePoint"},
    "dangling_junction": {"ReparsePoint"},
    "listed_through_junction": {"ReparsePoint"},
    "junction_and_wrong_hash": {"ReparsePoint"},
    "file_symlink": {"ReparsePoint"},
    "manifest_symlink": {"ReparsePoint"},
    "unreadable_file": {"AccessDenied"},
    "unreadable_folder": {"AccessDenied"},
    **{f"unsafe_{name}": {"UnsafePath"} for name in UNSAFE_PATHS},
    **{f"malformed_{name}": {"ManifestMalformed"} for name in MALFORMED_MANIFESTS},
}


@needs_bridge
@pytest.mark.parametrize("case", sorted(EXPECTED))
def test_every_unexpected_malformed_or_unsafe_bundle_is_refused(results, case):
    if case in results["_skipped"]:
        pytest.skip("this session may not create symbolic links (no privilege or Developer Mode)")
    result = results[case]
    assert result["verified"] is False, result
    # Exactly the expected kinds of problem -- so each refusal is for the stated reason, not a side effect.
    assert _codes(result) == EXPECTED[case], result


@needs_bridge
def test_problems_name_paths_never_contents(results):
    assert results["extra_hidden_file"]["problems"] == ["UnexpectedFile|runtime/_internal/planted.dll"]
    assert results["interior_junction"]["problems"] == ["ReparsePoint|runtime/_internal/linked"]
    assert results["root_junction"]["problems"] == ["ReparsePoint|"]
    assert results["modified_content_same_size"]["problems"] == ["HashMismatch|runtime/_internal/library.dll"]
    assert results["unreadable_file"]["problems"] == ["AccessDenied|runtime/_internal/library.dll"]
    assert results["unreadable_folder"]["problems"] == ["AccessDenied|runtime/_internal/numpy"]
    for result in (v for k, v in results.items() if not k.startswith("_")):
        for problem in result["problems"]:
            assert "fixture:" not in problem and "planted" not in problem.replace("planted.dll", ""), problem


@needs_bridge
def test_nothing_is_reported_before_the_manifest_is_trusted(results):
    # A wrong manifest hash stops verification before the inventory is compared, and no version is reported.
    for case in ("wrong_expected_hash", "modified_manifest", "modified_manifest_and_extra_file"):
        assert results[case]["version"] is None and results[case]["file_count"] == 0, case


@needs_bridge
def test_verification_changes_nothing(results):
    changed = [name for name, unchanged in results["_unchanged"].items() if not unchanged]
    assert changed == []
    assert len(results["_unchanged"]) >= 60
