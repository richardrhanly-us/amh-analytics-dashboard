"""Tests for the trusted bundle pin and the preparation controller, M2c-1:
installer/bundle-pin.props, installer/SortView.Setup/TrustedBundlePin.cs and BundlePreparation.cs.

  1. STATIC (run everywhere): the pin is empty by default and only bundle-pin.props sets it; the build refuses overrides
     and malformed values and embeds the pin as AssemblyMetadata; the pin is read only from the program's own assembly;
     the production factory cannot be redirected; nothing here removes earlier runs, runs anything or starts setup.
  2. BUILD (Windows): the default build embeds an empty pin; a /p: override is refused; an environment variable is
     ignored; a pinned build (of a COPY of the installer sources, never the real pin file) embeds exactly its pin, and a
     malformed or half-set pin fails the build.
  3. BEHAVIOR (Windows, unelevated): the controller, built through its internal test seam with fixture bundles, a pin and a
     disposable staging policy in pytest's temporary folders. Nothing is ever created under Program Files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_installer_bridge import _MSBUILD, _POWERSHELL, needs_bridge

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "installer"
SETUP_DIR = INSTALLER / "SortView.Setup"
SETUP_PROJECT = SETUP_DIR / "SortView.Setup.csproj"
PIN_FILE = INSTALLER / "bundle-pin.props"
PIN_CODE = SETUP_DIR / "TrustedBundlePin.cs"
PREPARATION_CODE = SETUP_DIR / "BundlePreparation.cs"
VERIFIER_CODE = INSTALLER / "SortView.Setup.Core" / "BundleVerifier.cs"
MSBUILD_NS = "{http://schemas.microsoft.com/developer/msbuild/2003}"

PRODUCT, VERSION = "SortView Collector", "1.2.3"
FILES = {
    "setup.ps1": b"# fixture, not a script",
    "install.ps1": b"# fixture, not a script",
    "collector_config.example.json": b'{"api_url": "https://fixture.invalid"}',
    "runtime/SortViewCollector.exe": b"fixture: not a program",
    "runtime/_internal/library.dll": b"fixture: not a library",
    "runtime/_internal/zz-last.dll": b"the last file copied",
}
SYSTEM, ADMINISTRATORS = "S-1-5-18", "S-1-5-32-544"
TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
PIN_SHA = "a" * 64


def _code(path: Path) -> str:
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


# =====================================================================================================================
# 1. STATIC
# =====================================================================================================================

def test_the_pin_is_empty_by_default_and_only_its_file_sets_it():
    import xml.etree.ElementTree as ET

    root = ET.parse(PIN_FILE).getroot()
    values = {child.tag.replace(MSBUILD_NS, ""): (child.text or "") for group in root for child in group}
    assert values == {"SortViewBundleManifestSha256": "", "SortViewBundleVersion": ""}
    # Nothing else in the installer names the pin properties (no second place to set them).
    for path in INSTALLER.rglob("*"):
        if (path.is_file() and path.suffix in (".props", ".targets", ".csproj", ".cs") and "obj" not in path.parts
                and "bin" not in path.parts and path not in (PIN_FILE, SETUP_PROJECT)):
            assert "SortViewBundleManifestSha256" not in path.read_text(encoding="utf-8"), path


def test_the_build_refuses_overrides_and_malformed_pins_and_embeds_the_pin():
    project = re.sub(r"\s+", " ", _code(SETUP_PROJECT))
    assert '<Import Project="..\\bundle-pin.props" />' in project
    assert '<Target Name="SortViewBundlePin" BeforeTargets="CoreCompile">' in project
    # The effective values must equal the file's own text: anything from /p: (or a property set elsewhere) is refused.
    assert project.count('XmlInputPath="$(MSBuildThisFileDirectory)..\\bundle-pin.props"') == 2
    assert "'$(SortViewBundleManifestSha256)' != '$(_SortViewPinFileSha256)'" in project
    assert "'$(SortViewBundleVersion)' != '$(_SortViewPinFileVersion)'" in project
    assert project.count("<Error ") == 5
    assert "'^[0-9a-f]{64}$'" in project and "$(SortViewBundleVersion.Length) &gt; 64" in project
    assert "<_Parameter1>SortView.BundleManifestSha256</_Parameter1> <_Parameter2>pin:$(SortViewBundleManifestSha256)</_Parameter2>" in project
    assert "<_Parameter1>SortView.BundleVersion</_Parameter1> <_Parameter2>pin:$(SortViewBundleVersion)</_Parameter2>" in project
    assert '<Compile Include="TrustedBundlePin.cs" />' in project and '<Compile Include="BundlePreparation.cs" />' in project


def test_the_pin_is_read_only_from_the_programs_own_assembly():
    code = _code(PIN_CODE)
    assert "public static TrustedBundlePin ForThisInstaller() => Read(typeof(TrustedBundlePin).Assembly);" in code
    assert "internal TrustedBundlePin(string manifestSha256, string version)" in code and "public TrustedBundlePin(" not in code
    for forbidden in ("Environment.", "Registry", "GetCommandLineArgs", "File.", "Directory.", "ConfigurationManager", "AppSettings",
                      "BundleVerifier", "MANIFEST"):
        assert forbidden not in code, forbidden
    # The same version rule as BundleVerifier's.
    pattern = re.search(r'VersionPattern = new Regex\(@"([^"]+)"', code).group(1)
    assert pattern in VERIFIER_CODE.read_text(encoding="utf-8")


def test_the_production_factory_cannot_be_redirected():
    code = re.sub(r"\s+", " ", _code(PREPARATION_CODE))
    assert ("public static BundlePreparation CreateForThisInstaller() => new BundlePreparation(Path.Combine(ProgramFolder(), "
            "BundleFolderName), TrustedBundlePin.ForThisInstaller(), StagingPolicy.Production, IsElevatedAdministrator);") in code
    assert "Path.GetDirectoryName(typeof(BundlePreparation).Assembly.Location)" in code
    assert "internal BundlePreparation(string sourceRoot, TrustedBundlePin pin, StagingPolicy policy, Func<bool> isElevated)" in code
    assert "public BundlePreparation(" not in code
    # Staged only against the PIN (never a value from the bundle), after the pin, elevation and bundle checks.
    work = code[code.index("private PreparationResult Work("):]
    order = [work.index(s) for s in ("PinNotConfigured", "isElevated()", "BundleFolderExists()", "BundleStager.ClassifyRuns(policy, token)",
                                      "BundleStager.Stage(sourceRoot, pin.ManifestSha256, pin.Version, policy, token, progress)")]
    assert order == sorted(order)


def test_nothing_in_the_program_removes_runs_runs_anything_or_takes_outside_values():
    sources = {path.name: _code(path) for path in SETUP_DIR.glob("*.cs")}
    for name, code in sources.items():
        for forbidden in ("RemoveAbandonedRun", "new StagingPolicy(", "Process", "PowerShell", "SetupEngineHost", "BeginRun",
                          "SetupRunner", "GetCommandLineArgs", "GetEnvironmentVariable", "Registry", "File.Delete", "Directory.Delete",
                          "File.Write", "File.Copy", "Core.Testing"):
            assert forbidden not in code, (name, forbidden)


# =====================================================================================================================
# 2. BUILD
# =====================================================================================================================

def _build(project: Path, *properties: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([_MSBUILD, str(project), "/p:Configuration=Release", "/nologo", "/v:minimal", *properties],
                          capture_output=True, text=True, check=False, env=env)


_READ_PIN = r"""
param([string]$ExePath)
$ErrorActionPreference = 'Stop'
$assembly = [Reflection.Assembly]::LoadFrom($ExePath)
$metadata = @($assembly.GetCustomAttributes([Reflection.AssemblyMetadataAttribute], $false) | Where-Object { $_.Key -like 'SortView.*' } |
              ForEach-Object { "$($_.Key)=$($_.Value)" })
$pin = $assembly.GetType('SortView.Setup.TrustedBundlePin', $true).GetMethod('ForThisInstaller').Invoke($null, @())
ConvertTo-Json -Compress -InputObject ([ordered]@{ metadata = $metadata; state = $pin.State.ToString(); sha = $pin.ManifestSha256; version = $pin.Version })
"""


def _read_pin(tmp: Path, exe: Path) -> dict:
    (tmp / "read_pin.ps1").write_text(_READ_PIN, encoding="utf-8")
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(tmp / "read_pin.ps1"),
                                "-ExePath", str(exe)], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


@needs_bridge
def test_the_default_build_embeds_an_empty_pin_that_trusts_nothing(tmp_path):
    completed = _build(SETUP_PROJECT)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    pin = _read_pin(tmp_path, SETUP_DIR / "bin" / "Release" / "SortView.Setup.exe")
    assert sorted(pin["metadata"]) == ["SortView.BundleManifestSha256=pin:", "SortView.BundleVersion=pin:"]
    assert pin["state"] == "NotConfigured" and pin["sha"] is None and pin["version"] is None


@needs_bridge
@pytest.mark.parametrize("override", [f"/p:SortViewBundleManifestSha256={PIN_SHA}", f"/p:SortViewBundleVersion={VERSION}"])
def test_a_command_line_override_of_the_pin_is_refused(override):
    completed = _build(SETUP_PROJECT, override)
    assert completed.returncode != 0
    assert "is set outside installer\\bundle-pin.props" in completed.stdout + completed.stderr


@needs_bridge
def test_an_environment_variable_cannot_set_the_pin(tmp_path):
    # A project property (the pin file's) always wins over an environment variable of the same name: ignored, not used.
    env = dict(os.environ, SortViewBundleManifestSha256=PIN_SHA, SortViewBundleVersion=VERSION)
    completed = _build(SETUP_PROJECT, env=env)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert _read_pin(tmp_path, SETUP_DIR / "bin" / "Release" / "SortView.Setup.exe")["state"] == "NotConfigured"


@pytest.fixture(scope="module")
def installer_copy(tmp_path_factory) -> Path:
    """A copy of the four installer projects and the pin file, so a pinned build never touches the real pin."""
    copy = tmp_path_factory.mktemp("installer-copy") / "installer"
    for name in ("SortView.Setup", "SortView.Setup.Core", "SortView.Setup.Wizard", "SortView.Setup.Wpf"):
        shutil.copytree(INSTALLER / name, copy / name, ignore=shutil.ignore_patterns("bin", "obj", "Testing"))
    shutil.copyfile(PIN_FILE, copy / "bundle-pin.props")
    return copy


def _pin_copy(copy: Path, sha: str, version: str) -> None:
    text = PIN_FILE.read_text(encoding="utf-8")
    text = text.replace("<SortViewBundleManifestSha256></SortViewBundleManifestSha256>",
                        f"<SortViewBundleManifestSha256>{sha}</SortViewBundleManifestSha256>")
    text = text.replace("<SortViewBundleVersion></SortViewBundleVersion>", f"<SortViewBundleVersion>{version}</SortViewBundleVersion>")
    (copy / "bundle-pin.props").write_text(text, encoding="utf-8")


@needs_bridge
def test_a_pinned_build_embeds_exactly_its_pin_in_the_production_program(tmp_path, installer_copy):
    _pin_copy(installer_copy, PIN_SHA, VERSION)
    completed = _build(installer_copy / "SortView.Setup" / "SortView.Setup.csproj")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    output = installer_copy / "SortView.Setup" / "bin" / "Release"
    pin = _read_pin(tmp_path, output / "SortView.Setup.exe")
    assert sorted(pin["metadata"]) == [f"SortView.BundleManifestSha256=pin:{PIN_SHA}", f"SortView.BundleVersion=pin:{VERSION}"]
    assert (pin["state"], pin["sha"], pin["version"]) == ("Configured", PIN_SHA, VERSION)
    # Still only the program and its three libraries: the pin adds no file.
    names = {p.name for p in output.iterdir() if p.is_file() and not p.name.endswith((".pdb", ".exe.config"))}
    assert names == {"SortView.Setup.exe", "SortView.Setup.Wizard.dll", "SortView.Setup.Wpf.dll", "SortView.Setup.Core.dll"}


@needs_bridge
@pytest.mark.parametrize(("sha", "version", "message"), [
    ("A" * 64, VERSION, "must be 64 lowercase hexadecimal characters"),
    ("a" * 63, VERSION, "must be 64 lowercase hexadecimal characters"),
    (PIN_SHA, "one.two", "is not a release version"),
    (PIN_SHA, "", "together, or neither"),
    ("", VERSION, "together, or neither"),
])
def test_a_malformed_or_half_set_pin_fails_the_build(installer_copy, sha, version, message):
    _pin_copy(installer_copy, sha, version)
    completed = _build(installer_copy / "SortView.Setup" / "SortView.Setup.csproj")
    assert completed.returncode != 0
    assert message in completed.stdout + completed.stderr


# =====================================================================================================================
# 3. BEHAVIOR
# =====================================================================================================================

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bundle(root: Path, version: str = VERSION) -> str:
    root.mkdir(parents=True)
    for path, data in FILES.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    entries = [{"path": p, "sha256": _sha256(d), "size_bytes": len(d)} for p, d in sorted(FILES.items())]
    manifest = (json.dumps({"product": PRODUCT, "version": version, "built_at": "2026-01-01T00:00:00Z", "files": entries}, indent=2)
                + "\n").encode()
    (root / "MANIFEST.json").write_bytes(manifest)
    return _sha256(manifest)


_PROBE = r"""
param([string]$ExePath, [string]$CorePath, [string]$CasesPath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
Add-Type -Path $CorePath
Add-Type -ReferencedAssemblies @($CorePath, 'System.Web.Extensions', 'System.Core') -TypeDefinition @"
using System; using System.Collections; using System.Collections.Generic; using System.IO; using System.Linq; using System.Reflection;
using System.Security.Principal; using System.Threading; using System.Threading.Tasks; using System.Web.Script.Serialization;
using SortView.Setup.Core;

public sealed class Hook : IProgress<StagingProgress> {
    public string Mode; public Action OnFirstFile; public bool OnStaged; public readonly ManualResetEventSlim Reached = new ManualResetEventSlim(false);
    public readonly ManualResetEventSlim Gate = new ManualResetEventSlim(false); int files;
    public void Report(StagingProgress p) {
        if (OnStaged) { if (p.Step == StagingStep.StagedVerified && OnFirstFile != null) OnFirstFile(); return; }
        if (p.Step != StagingStep.FileCopied || Interlocked.Increment(ref files) != 1) return;
        Reached.Set();
        if (Mode == "throw") throw new InvalidOperationException("progress callback failed");
        if (Mode == "gate") Gate.Wait();
        if (OnFirstFile != null) OnFirstFile();
    }
}

public static class PreparationProbe {
    static Assembly setup; static Type prepType, pinType;
    const BindingFlags Any = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;

    static object Pin(string sha, string version) {
        return pinType.GetConstructor(Any, null, new[] { typeof(string), typeof(string) }, null).Invoke(new object[] { sha, version });
    }
    static IDisposable Prep(string source, object pin, string root, Func<bool> elevated) {
        var me = WindowsIdentity.GetCurrent().User;
        var policy = new StagingPolicy(root, new[] { new SecurityIdentifier("S-1-5-18"), new SecurityIdentifier("S-1-5-32-544"),
            new SecurityIdentifier("S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"), me },
            new[] { new SecurityIdentifier("S-1-5-18"), new SecurityIdentifier("S-1-5-32-544"), me }, null);
        return (IDisposable)prepType.GetConstructor(Any, null, new[] { typeof(string), pinType, typeof(StagingPolicy), typeof(Func<bool>) }, null)
            .Invoke(new object[] { source, pin, policy, elevated });
    }
    static Task Start(object prep, IProgress<StagingProgress> hook) { return (Task)prepType.GetMethod("PrepareAsync").Invoke(prep, new object[] { hook }); }
    static object Result(Task t) { t.Wait(); return t.GetType().GetProperty("Result").GetValue(t); }
    static object Get(object o, string name) { return o.GetType().GetProperty(name).GetValue(o); }
    static object Call(object o, string name) { return o.GetType().GetMethod(name).Invoke(o, null); }

    static Dictionary<string, object> Describe(object r) {
        var d = new Dictionary<string, object>();
        d["outcome"] = Get(r, "Outcome").ToString(); d["refusal"] = Get(r, "Refusal").ToString(); d["version"] = Get(r, "Version");
        d["file_count"] = Get(r, "FileCount"); d["worker_failed"] = Get(r, "WorkerFailed");
        d["problems"] = ((IEnumerable)Get(r, "Problems")).Cast<object>().Select(p => p.ToString().Split(':')[0]).ToList();
        d["cleanup_problems"] = ((IEnumerable)Get(r, "CleanupProblems")).Cast<object>().Select(p => p.ToString()).ToList();
        var sv = Get(r, "SourceVerification") as BundleVerification;
        d["source_problems"] = sv == null ? null : sv.Problems.Select(p => p.Code.ToString()).ToList();
        var scan = Get(r, "EarlierRuns") as StagingRunScan;
        d["earlier_runs"] = scan == null ? null : scan.Entries.Select(e => e.Name + "|" + e.State + "|" + e.Reason).ToList();
        return d;
    }
    static List<string> Entries(string root) { return Directory.Exists(root) ? Directory.GetFileSystemEntries(root).Select(Path.GetFileName).OrderBy(x => x).ToList() : new List<string>(); }

    public static Dictionary<string, object> Run(string exePath, string casesJson) {
        setup = Assembly.LoadFrom(exePath);
        prepType = setup.GetType("SortView.Setup.BundlePreparation", true);
        pinType = setup.GetType("SortView.Setup.TrustedBundlePin", true);
        var cases = (IDictionary<string, object>)new JavaScriptSerializer().DeserializeObject(casesJson);
        var outcome = new Dictionary<string, object>();
        foreach (var pair in cases) {
            var c = (IDictionary<string, object>)pair.Value; var r = new Dictionary<string, object>();
            string root = (string)c["root"], kind = (string)c["kind"];
            try {
                Func<bool> elevated = () => true;
                if (kind == "not_elevated") elevated = () => false;
                if (kind == "elevation_check_throws") elevated = () => { throw new InvalidOperationException("elevation check failed"); };
                using (var prep = Prep((string)c["source"], Pin((string)c["sha"], (string)c["version"]), root, elevated)) {
                    var hook = new Hook { Mode = c.ContainsKey("hook") ? (string)c["hook"] : null };
                    if (kind == "cancel") hook.OnFirstFile = () => prepType.GetMethod("Cancel").Invoke(prep, null);
                    // Dispose from the worker itself (a synchronous callback): mid-copy, and once the copy has verified.
                    if (kind == "dispose_from_progress") hook.OnFirstFile = () => prep.Dispose();
                    if (kind == "dispose_from_progress_after_staged") { hook.OnStaged = true; hook.OnFirstFile = () => prep.Dispose(); }
                    if (kind == "overlap" || kind == "dispose_while_preparing") hook.Mode = "gate";
                    Task first = Start(prep, hook);
                    if (kind == "overlap") {
                        hook.Reached.Wait();
                        r["preparing"] = Get(prep, "IsPreparing");
                        r["second"] = Describe(Result(Start(prep, null)));
                        r["release_while_preparing"] = Call(prep, "Release") == null;
                        hook.Gate.Set();
                    }
                    if (kind == "dispose_while_preparing") {
                        hook.Reached.Wait();
                        var disposer = new Thread(() => prep.Dispose()); disposer.Start();
                        Thread.Sleep(200); r["dispose_waiting"] = disposer.IsAlive;  // it waits for the worker
                        hook.Gate.Set(); r["disposer_returned"] = disposer.Join(TimeSpan.FromSeconds(60));
                    }
                    // Bounded: a deadlocked preparation fails the case instead of hanging the probe.
                    bool completed = first.Wait(TimeSpan.FromSeconds(60));
                    r["completed"] = completed;
                    if (!completed) throw new TimeoutException("the preparation did not complete within 60 s (deadlocked)");
                    r["result"] = Describe(Result(first));
                    var staged = Call(prep, "TryGetStagedBundle") as StagedBundle;
                    r["staged_path_exists"] = staged != null && Directory.Exists(staged.BundlePath);
                    r["root_while_held"] = Entries(root);
                    if (kind == "overlap") r["again"] = Describe(Result(Start(prep, null)));
                    if (kind == "valid_release") {
                        var released = (IEnumerable)Call(prep, "Release");
                        r["release_problems"] = released.Cast<object>().Select(p => p.ToString()).ToList();
                        r["after_release"] = Call(prep, "TryGetStagedBundle") == null;
                        r["bundle_path_after_release"] = staged == null ? "none" : staged.BundlePath;
                    }
                    if (kind == "overlap" || kind == "dispose" || kind.StartsWith("dispose_from_progress")) {
                        prep.Dispose();
                        r["after_dispose"] = Call(prep, "TryGetStagedBundle") == null;
                        r["dispose_problems"] = ((IEnumerable)Get(prep, "DisposeCleanupProblems")).Cast<object>().Select(p => p.ToString()).ToList();
                        r["start_after_dispose"] = Describe(Result(Start(prep, null)));
                    }
                }
                r["root_after"] = Entries(root);
            } catch (Exception e) { r["error"] = e.ToString(); }
            outcome[pair.Key] = r;
        }
        // The production factory (never started with a pin here: this build's pin is empty).
        var production = prepType.GetMethod("CreateForThisInstaller").Invoke(null, null);
        var policy = (StagingPolicy)prepType.GetField("policy", Any).GetValue(production);
        outcome["_production"] = new Dictionary<string, object> {
            { "source", prepType.GetField("sourceRoot", Any).GetValue(production) },
            { "policy_root", policy.StagingRoot }, { "production_root", StagingPolicy.Production.StagingRoot },
            { "policy_is_production", typeof(StagingPolicy).GetField("isProduction", Any).GetValue(policy) },
            { "pin_state", Get(prepType.GetField("pin", Any).GetValue(production), "State").ToString() },
            { "result", Describe(Result(Start(production, null))) } };
        ((IDisposable)production).Dispose();
        return outcome;
    }
}
"@
$result = [PreparationProbe]::Run($ExePath, (Get-Content -LiteralPath $CasesPath -Raw))
ConvertTo-Json -InputObject $result -Depth 8 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


@pytest.fixture(scope="module")
def results(tmp_path_factory) -> dict:
    completed = _build(SETUP_PROJECT)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    exe = SETUP_DIR / "bin" / "Release" / "SortView.Setup.exe"
    core = SETUP_DIR / "bin" / "Release" / "SortView.Setup.Core.dll"
    base = tmp_path_factory.mktemp("preparation")
    cases: dict[str, dict] = {}

    def case(name: str, kind: str | None = None, *, sha: str | None = None, version: str = VERSION, tamper: bool = False,
             missing: bool = False, **extra) -> Path:
        source = base / "sources" / name
        root = base / "roots" / name
        root.mkdir(parents=True)
        real_sha = _bundle(source) if not missing else _sha256(b"none")
        if tamper:
            (source / "runtime/_internal/library.dll").write_bytes(b"FIXTURE: NOT A LIBRARY")
        cases[name] = {"kind": kind or name, "source": str(source), "root": str(root), "sha": real_sha if sha is None else sha,
                       "version": version, **extra}
        return root

    case("not_configured", sha="", version="")
    case("invalid_pin", sha="ABC", version="1.0")
    case("not_elevated")
    case("missing_bundle", missing=True)
    case("valid_release")
    case("wrong_hash", sha=_sha256(b"another manifest"))
    case("wrong_version", version="9.9.9")
    case("tampered", tamper=True)
    case("cancel")
    case("progress_throws", hook="throw")
    case("elevation_check_throws")
    case("overlap")
    case("dispose")
    case("dispose_while_preparing")
    case("dispose_from_progress")
    case("dispose_from_progress_after_staged")
    root = case("earlier_runs", kind="valid_release")
    (root / "0123456789abcdef0123456789abcdef").mkdir()  # an earlier run's folder (no marker)
    (root / "notes.txt").write_bytes(b"not a run")
    (base / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    (base / "probe.ps1").write_text(_PROBE, encoding="utf-8")
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(base / "probe.ps1"),
                                "-ExePath", str(exe), "-CorePath", str(core), "-CasesPath", str(base / "cases.json"),
                                "-OutPath", str(base / "out.json")], capture_output=True, text=True, timeout=600, check=False)
    assert (base / "out.json").exists(), completed.stdout + completed.stderr
    return json.loads((base / "out.json").read_text(encoding="utf-8-sig"))


def _r(results: dict, name: str) -> dict:
    r = results[name]
    assert "error" not in r, r.get("error")
    return r


@needs_bridge
@pytest.mark.parametrize(("name", "refusal"), [("not_configured", "PinNotConfigured"), ("invalid_pin", "PinInvalid"),
                                               ("not_elevated", "NotElevated"), ("missing_bundle", "BundleMissing")])
def test_preparation_is_refused_before_anything_is_staged(results, name, refusal):
    r = _r(results, name)
    assert (r["result"]["outcome"], r["result"]["refusal"]) == ("Refused", refusal), r
    assert r["result"]["earlier_runs"] is None  # refused before even the read-only classification
    assert r["root_while_held"] == [] and r["root_after"] == [] and r["staged_path_exists"] is False


@needs_bridge
def test_a_pinned_bundle_is_staged_held_and_released(results):
    r = _r(results, "valid_release")
    result = r["result"]
    assert (result["outcome"], result["version"], result["file_count"]) == ("Prepared", VERSION, len(FILES)), r
    assert r["staged_path_exists"] is True and len(r["root_while_held"]) == 1  # one run folder, while held
    assert r["release_problems"] == [] and r["after_release"] is True and r["bundle_path_after_release"] is None
    assert r["root_after"] == []


@needs_bridge
@pytest.mark.parametrize(("name", "source_problem"), [("wrong_hash", "ManifestHashMismatch"), ("wrong_version", "VersionMismatch"),
                                                      ("tampered", "HashMismatch")])
def test_a_bundle_that_does_not_match_the_pin_is_refused(results, name, source_problem):
    r = _r(results, name)
    assert r["result"]["outcome"] == "Failed" and r["result"]["problems"] == ["SourceNotVerified"], r
    assert r["result"]["source_problems"] == [source_problem]
    assert r["staged_path_exists"] is False and r["root_after"] == []


@needs_bridge
def test_cancellation_and_worker_failures_complete_safely_and_clean_up(results):
    cancel = _r(results, "cancel")
    assert cancel["result"]["outcome"] == "Cancelled" and cancel["result"]["cleanup_problems"] == [], cancel
    thrown = _r(results, "progress_throws")
    assert thrown["result"]["outcome"] == "Failed" and thrown["result"]["problems"] == ["UnexpectedError"], thrown
    worker = _r(results, "elevation_check_throws")
    assert worker["result"]["outcome"] == "Failed" and worker["result"]["worker_failed"] is True, worker
    for r in (cancel, thrown, worker):
        assert r["staged_path_exists"] is False and r["root_while_held"] == [] and r["root_after"] == []


@needs_bridge
def test_preparations_never_overlap(results):
    r = _r(results, "overlap")
    assert r["preparing"] is True
    assert (r["second"]["outcome"], r["second"]["refusal"]) == ("Refused", "AlreadyPreparing")
    assert r["release_while_preparing"] is True  # Release refused while preparing
    assert r["result"]["outcome"] == "Prepared"
    assert (r["again"]["outcome"], r["again"]["refusal"]) == ("Refused", "AlreadyPrepared")


@needs_bridge
@pytest.mark.parametrize("name", ["overlap", "dispose"])
def test_disposing_the_controller_releases_the_staged_bundle(results, name):
    r = _r(results, name)
    assert r["result"]["outcome"] == "Prepared" and len(r["root_while_held"]) == 1
    assert r["after_dispose"] is True and r["dispose_problems"] == [] and r["root_after"] == []
    assert (r["start_after_dispose"]["outcome"], r["start_after_dispose"]["refusal"]) == ("Refused", "Disposed")


@needs_bridge
def test_disposing_while_preparing_waits_for_the_worker_and_leaves_nothing(results):
    r = _r(results, "dispose_while_preparing")
    assert r["dispose_waiting"] is True  # from another thread, Dispose waits for the staging thread...
    assert r["disposer_returned"] is True  # ...and returns once it is done
    assert r["result"]["outcome"] == "Cancelled" and r["root_after"] == [], r


@needs_bridge
@pytest.mark.parametrize("name", ["dispose_from_progress", "dispose_from_progress_after_staged"])
def test_disposing_from_the_workers_own_progress_callback_does_not_deadlock(results, name):
    r = _r(results, name)
    assert r["completed"] is True  # it did not wait for itself
    # Cancelled: mid-copy, staging stopped at its next check; after the copy verified, the staged bundle was never kept.
    assert r["result"]["outcome"] == "Cancelled", r
    assert r["result"]["cleanup_problems"] == [] and r["dispose_problems"] == []  # this cleanup is the result's to report
    assert r["staged_path_exists"] is False and r["after_dispose"] is True
    assert r["root_while_held"] == [] and r["root_after"] == []
    assert (r["start_after_dispose"]["outcome"], r["start_after_dispose"]["refusal"]) == ("Refused", "Disposed")


@needs_bridge
def test_earlier_runs_are_reported_and_never_removed(results):
    r = _r(results, "earlier_runs")
    assert r["result"]["outcome"] == "Prepared"
    assert sorted(r["result"]["earlier_runs"]) == ["0123456789abcdef0123456789abcdef|Unknown|MarkerMissing", "notes.txt|Unknown|NotAFolder"]
    assert r["root_after"] == ["0123456789abcdef0123456789abcdef", "notes.txt"]  # both still there


@needs_bridge
def test_the_production_factory_is_fixed_and_this_build_trusts_no_bundle(results):
    production = results["_production"]
    assert production["policy_is_production"] is True and production["policy_root"] == production["production_root"]
    assert production["source"] == str(SETUP_DIR / "bin" / "Release" / "bundle")
    assert production["pin_state"] == "NotConfigured"
    assert (production["result"]["outcome"], production["result"]["refusal"]) == ("Refused", "PinNotConfigured")
    assert not (Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "SortView").exists()
