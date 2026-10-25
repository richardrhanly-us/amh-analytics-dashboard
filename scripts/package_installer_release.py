"""Packages the SortView setup program (installer/SortView.Setup, the WPF wizard) with a canonical SortView Collector
release bundle into ONE distributable, installation-enabled GUI package.

    python scripts/package_installer_release.py --version 1.0.14 \
        --bundle-zip C:\\SortView\\ReleaseBuilds\\1.0.14\\SortViewCollector-1.0.14.zip \
        --output C:\\SortView\\ReleaseBuilds\\SortView-Setup-1.0.14

The repository's own installer stays a build that cannot install anything: installer/installation-gate.props stays
"false" and installer/bundle-pin.props stays empty, and this script never writes either. It builds the installer from a
TEMPORARY copy of the committed sources (`git archive HEAD installer`), in which -- and only in which -- the gate is set
to "true" and the pin to the bundle's MANIFEST.json SHA-256 and version.

The expected hashes are never read from the bundle or the command line: they come from the release record,
docs/releases/collector-<version>.md as committed at HEAD (its "Release ZIP SHA-256" and "Bundle `MANIFEST.json`
SHA-256" lines). --release-record-file reads a record that is NOT yet committed instead, and the package record says so.

Fails closed, and writes nothing to --output, unless every check passes:
  1. the ZIP's SHA-256 is the recorded one; its single folder SortViewCollector-<version> holds MANIFEST.json with the
     recorded SHA-256, product "SortView Collector" and this version, and exactly the files that manifest lists, each
     with its listed size and SHA-256;
  2. the committed gate and pin files are the defaults (false, empty) before the copy is changed;
  3. MSBuild builds SortView.Setup (Release) from the copy;
  4. the built program, read back by reflection, embeds the gate Enabled and the pin Configured with exactly the
     recorded hash and this version; its own BundleVerifier verifies the packaged bundle\\ against that pin; all four
     assemblies target .NETFramework,Version=v4.7.2;
  5. the packaged bundle\\ is byte-identical to the verified release bundle.
Output (created once, never overwritten):
  <output>\\SortView-Setup-<version>\\   SortView.Setup.exe, its three libraries, bundle\\ (the release bundle)
  <output>\\SortView-Setup-<version>.zip
  <output>\\PACKAGE-SHA256.txt           SHA-256, size and path of every file in the package folder
  <output>\\PACKAGE-RECORD.json          provenance, the read-back pin and gate, and the hashes
  <output>\\build-installer.log          the MSBuild output
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
PRODUCT = "SortView Collector"
TARGET_FRAMEWORK = ".NETFramework,Version=v4.7.2"
PROGRAM_FILES = ("SortView.Setup.exe", "SortView.Setup.Core.dll", "SortView.Setup.Wizard.dll", "SortView.Setup.Wpf.dll")
BUNDLE_FOLDER = "bundle"  # BundlePreparation.BundleFolderName: the program stages only the folder of this name beside it
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+.][0-9A-Za-z][0-9A-Za-z.+-]*)?$")  # SortView.Setup.csproj's own rule
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
RECORD_ZIP = re.compile(r"^- Release ZIP SHA-256: `([0-9a-f]{64})`\s*$", re.MULTILINE)
RECORD_MANIFEST = re.compile(r"^- Bundle `MANIFEST\.json` SHA-256: `([0-9a-f]{64})`\s*$", re.MULTILINE)
GATE_DEFAULT = "<SortViewInstallationEnabled>false</SortViewInstallationEnabled>"
PIN_DEFAULTS = ("<SortViewBundleManifestSha256></SortViewBundleManifestSha256>", "<SortViewBundleVersion></SortViewBundleVersion>")


class PackagingError(Exception):
    """Packaging refused: the message says why. Nothing was written to the output."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --- the release record ---------------------------------------------------------------------------------------------

def parse_release_record(text: str) -> tuple[str, str]:
    """(release ZIP SHA-256, bundle MANIFEST.json SHA-256): each must appear exactly once, 64 lowercase hex characters."""
    found = []
    for name, pattern in (("Release ZIP SHA-256", RECORD_ZIP), ("Bundle `MANIFEST.json` SHA-256", RECORD_MANIFEST)):
        matches = pattern.findall(text)
        if len(matches) != 1:
            raise PackagingError(f"the release record must hold exactly one '- {name}: `<64 lowercase hex>`' line (found {len(matches)})")
        found.append(matches[0])
    return found[0], found[1]


def read_release_record(version: str, record_file: Path | None) -> tuple[str, str]:
    """(record text, where it came from). The committed record at HEAD unless an explicit file is given."""
    if record_file is not None:
        if not record_file.is_file():
            raise PackagingError(f"release record file not found: {record_file}")
        return record_file.read_text(encoding="utf-8-sig"), f"file {record_file} (NOT read from a commit)"
    relative = f"docs/releases/collector-{version}.md"
    shown = subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"HEAD:{relative}"], capture_output=True, check=False)
    if shown.returncode != 0:
        raise PackagingError(f"{relative} is not committed at HEAD -- commit the release record first (or pass --release-record-file)")
    return shown.stdout.decode("utf-8-sig"), f"HEAD:{relative} ({head_commit()})"


def head_commit() -> str:
    return subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


# --- the release bundle ---------------------------------------------------------------------------------------------

def _safe_member(name: str) -> PurePosixPath:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts or ":" in path.parts[0]:
        raise PackagingError(f"unsafe path in the release ZIP: {name!r}")
    return path


def extract_release_zip(zip_path: Path, expected_zip_sha: str, version: str, into: Path) -> Path:
    """Checks the ZIP's hash, extracts it safely into `into`, and returns its single bundle folder."""
    if not zip_path.is_file():
        raise PackagingError(f"release ZIP not found: {zip_path}")
    actual = sha256_file(zip_path)
    if actual != expected_zip_sha:
        raise PackagingError(f"the release ZIP's SHA-256 is {actual}, not the recorded {expected_zip_sha}")
    folder = f"SortViewCollector-{version}"
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            path = _safe_member(member.filename)
            if path.parts[0] != folder:
                raise PackagingError(f"the release ZIP holds {member.filename!r} outside its single folder {folder}")
        archive.extractall(into)
    return into / folder


def verify_bundle(bundle: Path, expected_manifest_sha: str, version: str) -> dict:
    """The bundle's MANIFEST.json has the recorded hash, product and version; the folder holds exactly its files, each with
    its listed size and SHA-256 (the same contract as the installer's BundleVerifier). Returns the manifest."""
    manifest_path = bundle / "MANIFEST.json"
    if not manifest_path.is_file():
        raise PackagingError(f"{bundle} has no MANIFEST.json")
    actual = sha256_file(manifest_path)
    if actual != expected_manifest_sha:
        raise PackagingError(f"the bundle's MANIFEST.json SHA-256 is {actual}, not the recorded {expected_manifest_sha}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("product") != PRODUCT or manifest.get("version") != version:
        raise PackagingError(f"the bundle's manifest is {manifest.get('product')!r} {manifest.get('version')!r}, not {PRODUCT!r} {version!r}")
    listed: dict[str, dict] = {}
    for entry in manifest.get("files", []):
        path = str(_safe_member(entry["path"]))
        if path in listed:
            raise PackagingError(f"the manifest lists {path} twice")
        listed[path] = entry
    present = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()} - {"MANIFEST.json"}
    if present != set(listed):
        missing, extra = sorted(set(listed) - present), sorted(present - set(listed))
        raise PackagingError(f"the bundle does not match its manifest: missing {missing[:5]}, unlisted {extra[:5]}")
    for path, entry in listed.items():
        file = bundle / path
        if file.stat().st_size != entry["size_bytes"] or sha256_file(file) != entry["sha256"]:
            raise PackagingError(f"{path} does not match its manifest entry")
    return manifest


# --- the installer sources: a temporary copy with the gate on and the pin set ------------------------------------------

def export_installer_sources(into: Path) -> Path:
    """`git archive HEAD installer` -- the committed sources only, never the working tree's -- extracted into `into`."""
    archived = subprocess.run(["git", "-C", str(REPO_ROOT), "archive", "--format=tar", "HEAD", "installer"], capture_output=True, check=False)
    if archived.returncode != 0:
        raise PackagingError("git archive HEAD installer failed: " + archived.stderr.decode(errors="replace")[-300:])
    with tarfile.open(fileobj=io.BytesIO(archived.stdout)) as archive:
        archive.extractall(into, filter="data")
    return into / "installer"


def enable_copy(installer_copy: Path, manifest_sha: str, version: str) -> None:
    """Sets the gate and the pin in the COPY. Refuses unless the copy's files are exactly the committed defaults."""
    if not SHA256_PATTERN.match(manifest_sha) or not VERSION_PATTERN.match(version) or len(version) > 64:
        raise PackagingError("the pin must be 64 lowercase hex characters and the version a release version")
    gate_path, pin_path = installer_copy / "installation-gate.props", installer_copy / "bundle-pin.props"
    gate, pin = gate_path.read_text(encoding="utf-8"), pin_path.read_text(encoding="utf-8")
    if gate.count(GATE_DEFAULT) != 1 or any(pin.count(default) != 1 for default in PIN_DEFAULTS):
        raise PackagingError("the committed installation gate and pin are not the defaults (false, empty): refusing to build from them")
    gate_path.write_text(gate.replace(GATE_DEFAULT, "<SortViewInstallationEnabled>true</SortViewInstallationEnabled>"), encoding="utf-8")
    pin = pin.replace(PIN_DEFAULTS[0], f"<SortViewBundleManifestSha256>{manifest_sha}</SortViewBundleManifestSha256>")
    pin = pin.replace(PIN_DEFAULTS[1], f"<SortViewBundleVersion>{version}</SortViewBundleVersion>")
    pin_path.write_text(pin, encoding="utf-8")


def find_msbuild(explicit: str | None) -> str:
    """--msbuild, then SORTVIEW_MSBUILD, then vswhere (the same order as the installer tests)."""
    for candidate in (explicit, os.environ.get("SORTVIEW_MSBUILD")):
        if candidate:
            if not Path(candidate).is_file():
                raise PackagingError(f"MSBuild not found at {candidate}")
            return candidate
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if vswhere.is_file():
        found = subprocess.run([str(vswhere), "-latest", "-products", "*", "-requires", "Microsoft.Component.MSBuild",
                                "-find", r"MSBuild\**\Bin\MSBuild.exe"], capture_output=True, text=True, check=False)
        candidates = [line.strip() for line in found.stdout.splitlines() if line.strip()]
        if candidates:
            return candidates[0]
    raise PackagingError("MSBuild not found: install the Visual Studio Build Tools, or pass --msbuild / set SORTVIEW_MSBUILD")


def build_installer(installer_copy: Path, msbuild: str, framework_root: Path | None, log: Path) -> Path:
    """MSBuild Release of the copy's SortView.Setup. `framework_root` (the .NET Framework 4.7.2 reference assemblies, e.g.
    the Microsoft.NETFramework.ReferenceAssemblies.net472 package's build folder) is passed to MSBuild for this process only."""
    env = dict(os.environ)
    if framework_root is not None:
        if not (framework_root / ".NETFramework" / "v4.7.2").is_dir():
            raise PackagingError(f"{framework_root} holds no .NETFramework\\v4.7.2 reference assemblies")
        env["TargetFrameworkRootPath"] = str(framework_root)
    project = installer_copy / "SortView.Setup" / "SortView.Setup.csproj"
    with log.open("w", encoding="utf-8") as handle:
        built = subprocess.run([msbuild, str(project), "/t:Rebuild", "/p:Configuration=Release", "/nologo", "/v:normal"],
                               stdout=handle, stderr=subprocess.STDOUT, env=env, check=False)
    if built.returncode != 0:
        raise PackagingError(f"the installer build failed (MSBuild exit {built.returncode}): see {log}")
    output = installer_copy / "SortView.Setup" / "bin" / "Release"
    produced = sorted(p.name for p in output.iterdir() if p.is_file() and p.suffix.lower() != ".pdb")
    if produced != sorted(PROGRAM_FILES):
        raise PackagingError(f"the installer build produced {produced}, not exactly {sorted(PROGRAM_FILES)}")
    return output


# --- read back what the built program embeds ---------------------------------------------------------------------------

_READ_BACK = r"""
param([string]$Folder)
$ErrorActionPreference = 'Stop'
$core = [Reflection.Assembly]::LoadFrom((Join-Path $Folder 'SortView.Setup.Core.dll'))
$exe = [Reflection.Assembly]::LoadFrom((Join-Path $Folder 'SortView.Setup.exe'))
$pin = $exe.GetType('SortView.Setup.TrustedBundlePin', $true).GetMethod('ForThisInstaller').Invoke($null, @())
$gate = $exe.GetType('SortView.Setup.InstallationGate', $true).GetMethod('ForThisInstaller').Invoke($null, @())
$verified = [SortView.Setup.Core.BundleVerifier]::Verify([string](Join-Path $Folder 'bundle'), [string]$pin.ManifestSha256, [string]$pin.Version)
$frameworks = @(foreach ($name in 'SortView.Setup.exe', 'SortView.Setup.Core.dll', 'SortView.Setup.Wizard.dll', 'SortView.Setup.Wpf.dll') {
    @([Reflection.Assembly]::LoadFrom((Join-Path $Folder $name)).GetCustomAttributes($true) |
      Where-Object { $_.GetType().Name -eq 'TargetFrameworkAttribute' } | ForEach-Object { $_.FrameworkName }) -join ';' })
ConvertTo-Json -Compress -InputObject ([ordered]@{
    pin_state = "$($pin.State)"; pin_sha256 = $pin.ManifestSha256; pin_version = $pin.Version; gate_state = "$($gate.State)"
    bundle_verified = $verified.IsVerified; bundle_version = $verified.Version; bundle_files = $verified.FileCount
    bundle_problems = @($verified.Problems | ForEach-Object { "$_" }); frameworks = $frameworks })
"""


def read_back(package: Path, workdir: Path) -> dict:
    """In a separate Windows PowerShell process (the assemblies are loaded there, never here)."""
    script = workdir / "read_back.ps1"
    script.write_text(_READ_BACK, encoding="utf-8")
    powershell = shutil.which("powershell") or "powershell.exe"
    done = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Folder", str(package)],
                          capture_output=True, text=True, check=False, timeout=600)
    if done.returncode != 0:
        raise PackagingError("reading the built program back failed: " + (done.stderr or done.stdout)[-500:])
    return json.loads(done.stdout.strip().splitlines()[-1])


def check_read_back(read: dict, manifest_sha: str, version: str, file_count: int) -> None:
    expected = {"pin_state": "Configured", "pin_sha256": manifest_sha, "pin_version": version, "gate_state": "Enabled",
                "bundle_verified": True, "bundle_version": version, "bundle_files": file_count}
    wrong = {k: read.get(k) for k, v in expected.items() if read.get(k) != v}
    if wrong or read.get("frameworks") != [TARGET_FRAMEWORK] * len(PROGRAM_FILES):
        raise PackagingError(f"the built program is not the expected release build: {wrong or read.get('frameworks')} "
                             f"(problems: {read.get('bundle_problems')})")


# --- packaging ---------------------------------------------------------------------------------------------------------

def inventory(folder: Path) -> list[str]:
    return [f"{sha256_file(p)}  {p.stat().st_size:>10}  {p.relative_to(folder).as_posix()}"
            for p in sorted(folder.rglob("*")) if p.is_file()]


def write_zip(folder: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, (PurePosixPath(folder.name) / path.relative_to(folder).as_posix()).as_posix())


def package(version: str, bundle_zip: Path, output: Path, *, record_file: Path | None = None, msbuild: str | None = None,
            framework_root: Path | None = None) -> dict:
    if not VERSION_PATTERN.match(version):
        raise PackagingError(f"{version!r} is not a release version")
    if output.exists():
        raise PackagingError(f"{output} already exists: a package is created once, never overwritten")
    record_text, record_source = read_release_record(version, record_file)
    zip_sha, manifest_sha = parse_release_record(record_text)
    msbuild_path = find_msbuild(msbuild)
    if framework_root is None and os.environ.get("TargetFrameworkRootPath"):
        framework_root = Path(os.environ["TargetFrameworkRootPath"])
    work = Path(tempfile.mkdtemp(prefix="sortview-setup-package-"))
    try:
        bundle = extract_release_zip(bundle_zip, zip_sha, version, work / "release")
        manifest = verify_bundle(bundle, manifest_sha, version)
        installer_copy = export_installer_sources(work / "source")
        enable_copy(installer_copy, manifest_sha, version)
        staging = work / "out"
        staging.mkdir()
        built = build_installer(installer_copy, msbuild_path, framework_root, staging / "build-installer.log")

        name = f"SortView-Setup-{version}"
        folder = staging / name
        folder.mkdir()
        for file in PROGRAM_FILES:
            shutil.copyfile(built / file, folder / file)
        shutil.copytree(bundle, folder / BUNDLE_FOLDER)
        packaged = {p.relative_to(folder / BUNDLE_FOLDER).as_posix(): sha256_file(p) for p in (folder / BUNDLE_FOLDER).rglob("*") if p.is_file()}
        released = {p.relative_to(bundle).as_posix(): sha256_file(p) for p in bundle.rglob("*") if p.is_file()}
        if packaged != released:
            raise PackagingError("the packaged bundle\\ is not byte-identical to the verified release bundle")
        read = read_back(folder, work)
        check_read_back(read, manifest_sha, version, len(manifest["files"]))

        lines = inventory(folder)
        (staging / "PACKAGE-SHA256.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        write_zip(folder, staging / f"{name}.zip")
        record = {
            "package": name, "version": version, "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "installer_source": f"git archive HEAD installer ({head_commit()})",
            "release_record": record_source,
            "release_zip": str(bundle_zip), "release_zip_sha256": zip_sha, "bundle_manifest_sha256": manifest_sha,
            "bundle_files": len(manifest["files"]), "embedded": read,
            "setup_exe_sha256": sha256_file(folder / "SortView.Setup.exe"),
            "package_files": len(lines), "package_sha256_txt_sha256": sha256_file(staging / "PACKAGE-SHA256.txt"),
            "zip": f"{name}.zip", "zip_sha256": sha256_file(staging / f"{name}.zip"),
            "msbuild": msbuild_path, "reference_assemblies": str(framework_root) if framework_root else "installed targeting pack",
            "repository_defaults": "installer/installation-gate.props (false) and installer/bundle-pin.props (empty) were not changed",
        }
        (staging / "PACKAGE-RECORD.json").write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(output))
        return record
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Package the SortView setup program with a canonical Collector release bundle.")
    parser.add_argument("--version", required=True, help="the Collector release version, e.g. 1.0.14")
    parser.add_argument("--bundle-zip", required=True, type=Path, help="the canonical SortViewCollector-<version>.zip")
    parser.add_argument("--output", required=True, type=Path, help="a folder that does not exist yet")
    parser.add_argument("--release-record-file", type=Path, help="read the release record from this file instead of HEAD")
    parser.add_argument("--msbuild", help="MSBuild.exe (default: SORTVIEW_MSBUILD, then vswhere)")
    parser.add_argument("--framework-root", type=Path, help="the .NET Framework reference assemblies root (TargetFrameworkRootPath)")
    args = parser.parse_args(argv)
    try:
        record = package(args.version, args.bundle_zip, args.output, record_file=args.release_record_file, msbuild=args.msbuild,
                         framework_root=args.framework_root)
    except PackagingError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({k: record[k] for k in ("package", "bundle_manifest_sha256", "setup_exe_sha256", "zip_sha256", "package_files",
                                             "release_record")}, indent=1))
    print(f"Package: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
