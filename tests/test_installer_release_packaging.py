"""Tests for scripts/package_installer_release.py: the distributable, installation-enabled setup program package.

  1. UNIT (run everywhere): the release record is the only source of the expected hashes; the release ZIP and the bundle
     are checked against them and against the bundle's manifest; only a COPY of the installer sources is changed, and
     only from the committed defaults; every refusal happens before anything is written.
  2. BUILD (Windows, MSBuild): a synthetic fixture bundle packaged end to end -- the built program embeds the gate
     Enabled and exactly the recorded pin, its own BundleVerifier accepts the packaged bundle, the inventory and the ZIP
     match the package, and the repository's gate, pin and build output are untouched.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from test_installer_bridge import needs_bridge
from test_installer_preparation import _bundle

from scripts import package_installer_release as packager

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_FILE = REPO_ROOT / "installer" / "installation-gate.props"
PIN_FILE = REPO_ROOT / "installer" / "bundle-pin.props"
REPO_SETUP_EXE = REPO_ROOT / "installer" / "SortView.Setup" / "bin" / "Release" / "SortView.Setup.exe"
VERSION = "0.0.1-g1test"  # a synthetic fixture release -- never a real one


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record(zip_sha: str, manifest_sha: str) -> str:
    return (f"# SortView Collector {VERSION}\n\n## Canonical release artifact\n\n- Release ZIP SHA-256: `{zip_sha}`\n"
            f"- Bundle `MANIFEST.json` SHA-256: `{manifest_sha}`\n")


@pytest.fixture
def release(tmp_path) -> dict:
    """A synthetic release: SortViewCollector-<VERSION>\\ (verified-bundle shape), its ZIP, and a record of both hashes."""
    folder = tmp_path / "build" / f"SortViewCollector-{VERSION}"
    manifest_sha = _bundle(folder, VERSION)
    zip_path = tmp_path / f"SortViewCollector-{VERSION}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(folder.parent).as_posix())
    zip_sha = _sha(zip_path.read_bytes())
    record = tmp_path / "record.md"
    record.write_text(_record(zip_sha, manifest_sha), encoding="utf-8")
    return {"folder": folder, "zip": zip_path, "zip_sha": zip_sha, "manifest_sha": manifest_sha, "record": record}


# =====================================================================================================================
# 1. UNIT
# =====================================================================================================================

def test_the_release_record_gives_exactly_one_zip_and_one_manifest_hash():
    assert packager.parse_release_record(_record("a" * 64, "b" * 64)) == ("a" * 64, "b" * 64)


@pytest.mark.parametrize("text", [
    "",                                                                                  # neither
    _record("a" * 64, "b" * 64) + "- Release ZIP SHA-256: `" + "c" * 64 + "`\n",          # two ZIP hashes
    _record("A" * 64, "b" * 64),                                                          # not lowercase
    _record("a" * 63, "b" * 64),                                                          # too short
    _record("a" * 64, "b" * 64).replace("Bundle `MANIFEST.json`", "Bundle MANIFEST.json"),  # not the recorded form
])
def test_a_missing_ambiguous_or_malformed_record_is_refused(text):
    with pytest.raises(packager.PackagingError, match="release record"):
        packager.parse_release_record(text)


def test_the_committed_record_is_read_from_head_and_names_the_commit():
    from collector import __version__

    text, source = packager.read_release_record(__version__, None)
    committed = subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"HEAD:docs/releases/collector-{__version__}.md"],
                               capture_output=True, check=True).stdout.decode("utf-8-sig")
    assert text == committed and source.startswith(f"HEAD:docs/releases/collector-{__version__}.md (")


def test_a_record_that_is_not_committed_is_refused_unless_given_as_a_file(tmp_path):
    with pytest.raises(packager.PackagingError, match="not committed at HEAD"):
        packager.read_release_record("0.0.0-never-released", None)
    record = tmp_path / "record.md"
    record.write_text("x", encoding="utf-8")
    assert packager.read_release_record("0.0.0-never-released", record)[1].endswith("(NOT read from a commit)")


def test_a_release_zip_with_another_hash_is_refused(release, tmp_path):
    with pytest.raises(packager.PackagingError, match="release ZIP's SHA-256"):
        packager.extract_release_zip(release["zip"], "0" * 64, VERSION, tmp_path / "x")


@pytest.mark.parametrize("member", ["elsewhere/file.txt", "../escape.txt", "C:/absolute.txt"])
def test_a_release_zip_with_anything_outside_its_one_folder_is_refused(tmp_path, member):
    zip_path = tmp_path / "bad.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr(f"SortViewCollector-{VERSION}/MANIFEST.json", "{}")
        archive.writestr(member, "x")
    with pytest.raises(packager.PackagingError, match="outside its single folder|unsafe path"):
        packager.extract_release_zip(zip_path, _sha(zip_path.read_bytes()), VERSION, tmp_path / "x")


def test_the_verified_bundle_is_accepted(release):
    assert packager.verify_bundle(release["folder"], release["manifest_sha"], VERSION)["version"] == VERSION


@pytest.mark.parametrize("change, message", [
    ("manifest_sha", "MANIFEST.json SHA-256"),
    ("version", "not 'SortView Collector'"),
    ("tamper", "does not match its manifest entry"),
    ("extra", "unlisted"),
    ("missing", "missing"),
])
def test_a_bundle_that_differs_from_the_record_or_its_manifest_is_refused(release, change, message):
    folder, manifest_sha, version = release["folder"], release["manifest_sha"], VERSION
    listed = json.loads((folder / "MANIFEST.json").read_text(encoding="utf-8"))["files"][0]["path"]
    if change == "manifest_sha":
        manifest_sha = "0" * 64
    elif change == "version":
        version = "0.0.2-other"
    elif change == "tamper":
        (folder / listed).write_bytes((folder / listed).read_bytes() + b"!")
    elif change == "extra":
        (folder / "unlisted.txt").write_bytes(b"x")
    else:
        (folder / listed).unlink()
    with pytest.raises(packager.PackagingError, match=message):
        packager.verify_bundle(folder, manifest_sha, version)


def test_the_committed_sources_are_exported_with_the_default_gate_and_pin(tmp_path):
    copy = packager.export_installer_sources(tmp_path)
    committed = subprocess.run(["git", "-C", str(REPO_ROOT), "show", "HEAD:installer/installation-gate.props"], capture_output=True, check=True).stdout
    # git archive applies the checkout line-ending conversion; git show prints the stored blob: compare the content.
    assert (copy / "installation-gate.props").read_bytes().replace(b"\r\n", b"\n") == committed.replace(b"\r\n", b"\n")
    assert packager.GATE_DEFAULT in (copy / "installation-gate.props").read_text(encoding="utf-8")
    assert all(d in (copy / "bundle-pin.props").read_text(encoding="utf-8") for d in packager.PIN_DEFAULTS)
    assert not any(part in ("bin", "obj") for p in copy.rglob("*") for part in p.relative_to(copy).parts)  # committed files only


def test_only_the_copy_gets_the_gate_on_and_the_pin_set(tmp_path):
    before = (GATE_FILE.read_bytes(), PIN_FILE.read_bytes())
    copy = packager.export_installer_sources(tmp_path)
    packager.enable_copy(copy, "c" * 64, VERSION)
    gate, pin = (copy / "installation-gate.props").read_text(encoding="utf-8"), (copy / "bundle-pin.props").read_text(encoding="utf-8")
    assert "<SortViewInstallationEnabled>true</SortViewInstallationEnabled>" in gate and packager.GATE_DEFAULT not in gate
    assert f"<SortViewBundleManifestSha256>{'c' * 64}</SortViewBundleManifestSha256>" in pin
    assert f"<SortViewBundleVersion>{VERSION}</SortViewBundleVersion>" in pin
    assert (GATE_FILE.read_bytes(), PIN_FILE.read_bytes()) == before  # the repository's own files are never written
    with pytest.raises(packager.PackagingError, match="not the defaults"):
        packager.enable_copy(copy, "c" * 64, VERSION)  # a copy that is already enabled is not a committed default


@pytest.mark.parametrize("sha, version", [("C" * 64, VERSION), ("c" * 63, VERSION), ("c" * 64, "latest"), ("c" * 64, "1.0")])
def test_a_malformed_pin_or_version_is_refused(tmp_path, sha, version):
    copy = packager.export_installer_sources(tmp_path)
    with pytest.raises(packager.PackagingError, match="pin must be"):
        packager.enable_copy(copy, sha, version)


def test_the_script_never_names_the_repository_gate_or_pin_files():
    source = (REPO_ROOT / "scripts" / "package_installer_release.py").read_text(encoding="utf-8")
    assert "installation-gate.props\"" in source and "bundle-pin.props\"" in source  # only ever joined to the copy
    assert 'REPO_ROOT / "installer"' not in source and "REPO_ROOT / 'installer'" not in source


def test_an_existing_output_is_refused_before_anything_else(release, tmp_path):
    output = tmp_path / "exists"
    output.mkdir()
    with pytest.raises(packager.PackagingError, match="already exists"):
        packager.package(VERSION, release["zip"], output, record_file=release["record"])
    assert list(output.iterdir()) == []


def test_a_record_that_does_not_match_the_zip_writes_nothing(release, tmp_path):
    record = tmp_path / "wrong.md"
    record.write_text(_record("0" * 64, release["manifest_sha"]), encoding="utf-8")
    output = tmp_path / "out"
    with pytest.raises(packager.PackagingError, match="release ZIP's SHA-256"):
        packager.package(VERSION, release["zip"], output, record_file=record, msbuild=packager.sys.executable)
    assert not output.exists()


# =====================================================================================================================
# 2. BUILD
# =====================================================================================================================

@pytest.fixture(scope="module")
def packaged(tmp_path_factory) -> dict:
    tmp = tmp_path_factory.mktemp("g1")
    folder = tmp / "build" / f"SortViewCollector-{VERSION}"
    manifest_sha = _bundle(folder, VERSION)
    zip_path = tmp / f"SortViewCollector-{VERSION}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(folder.parent).as_posix())
    record = tmp / "record.md"
    record.write_text(_record(_sha(zip_path.read_bytes()), manifest_sha), encoding="utf-8")
    repo_before = {p: (p.read_bytes() if p.exists() else None, p.stat().st_mtime_ns if p.exists() else None)
                   for p in (GATE_FILE, PIN_FILE, REPO_SETUP_EXE)}
    output = tmp / "package"
    result = packager.package(VERSION, zip_path, output, record_file=record)
    return {"result": result, "output": output, "folder": folder, "manifest_sha": manifest_sha, "repo_before": repo_before}


@needs_bridge
def test_the_package_embeds_the_gate_enabled_and_exactly_the_recorded_pin(packaged):
    embedded = packaged["result"]["embedded"]
    assert (embedded["gate_state"], embedded["pin_state"]) == ("Enabled", "Configured")
    assert (embedded["pin_sha256"], embedded["pin_version"]) == (packaged["manifest_sha"], VERSION)
    assert embedded["frameworks"] == [packager.TARGET_FRAMEWORK] * 4


@needs_bridge
def test_the_programs_own_verifier_accepts_the_packaged_bundle(packaged):
    embedded = packaged["result"]["embedded"]
    assert embedded["bundle_verified"] is True and embedded["bundle_problems"] == []
    assert embedded["bundle_files"] == len(json.loads((packaged["folder"] / "MANIFEST.json").read_text(encoding="utf-8"))["files"])


@needs_bridge
def test_the_package_holds_the_program_its_libraries_and_the_unchanged_bundle(packaged):
    folder = packaged["output"] / f"SortView-Setup-{VERSION}"
    assert sorted(p.name for p in folder.iterdir()) == sorted([*packager.PROGRAM_FILES, "bundle"])
    released = {p.relative_to(packaged["folder"]).as_posix(): p.read_bytes() for p in packaged["folder"].rglob("*") if p.is_file()}
    shipped = {p.relative_to(folder / "bundle").as_posix(): p.read_bytes() for p in (folder / "bundle").rglob("*") if p.is_file()}
    assert shipped == released
    assert sorted(p.name for p in packaged["output"].iterdir()) == sorted(
        [f"SortView-Setup-{VERSION}", f"SortView-Setup-{VERSION}.zip", "PACKAGE-SHA256.txt", "PACKAGE-RECORD.json", "build-installer.log"])


@needs_bridge
def test_the_inventory_and_the_zip_match_the_package_exactly(packaged):
    output, folder = packaged["output"], packaged["output"] / f"SortView-Setup-{VERSION}"
    files = {p.relative_to(folder).as_posix(): p for p in folder.rglob("*") if p.is_file()}
    rows = [line.split(None, 2) for line in (output / "PACKAGE-SHA256.txt").read_text(encoding="utf-8").splitlines()]
    assert {path: (sha, int(size)) for sha, size, path in rows} == {k: (_sha(p.read_bytes()), p.stat().st_size) for k, p in files.items()}
    with zipfile.ZipFile(output / f"SortView-Setup-{VERSION}.zip") as archive:
        zipped = {name.split("/", 1)[1]: archive.read(name) for name in archive.namelist()}
    assert zipped == {k: p.read_bytes() for k, p in files.items()}
    record = json.loads((output / "PACKAGE-RECORD.json").read_text(encoding="utf-8"))
    assert record["zip_sha256"] == _sha((output / f"SortView-Setup-{VERSION}.zip").read_bytes())
    assert record["setup_exe_sha256"] == _sha((folder / "SortView.Setup.exe").read_bytes())
    assert record["release_record"].endswith("(NOT read from a commit)")


@needs_bridge
def test_packaging_leaves_the_repository_gate_pin_and_build_output_untouched(packaged):
    for path, (data, mtime) in packaged["repo_before"].items():
        assert (path.read_bytes() if path.exists() else None, path.stat().st_mtime_ns if path.exists() else None) == (data, mtime), path
    assert packager.GATE_DEFAULT in GATE_FILE.read_text(encoding="utf-8")
    assert all(d in PIN_FILE.read_text(encoding="utf-8") for d in packager.PIN_DEFAULTS)
