"""Tests for protected bundle staging, M2b-1: installer/SortView.Setup.Core/BundleStager.cs.

The stager verifies a source bundle with BundleVerifier, copies exactly the files its manifest lists into a NEW run folder
created with protected permissions (native CreateDirectoryW), checks size and SHA-256 while copying, verifies the staged
copy again on its own, and removes its own run folder -- and nothing else -- on failure, cancellation or Dispose.

  1. STATIC checks (run everywhere): the production entry point always uses StagingPolicy.Production; the stager runs
     nothing, never copies by wildcard, never deletes recursively, and verifies source and copy by the same rules.
  2. BEHAVIOR (Windows only): generated fixture bundles and staging roots in pytest's temporary folders, under a TEST policy
     that also trusts the current user (so no elevation is needed). Nothing is ever created under Program Files, and the
     production policy is only read, never staged with.
"""

from __future__ import annotations

import ctypes
import getpass
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from test_installer_bridge import _MSBUILD, _POWERSHELL, needs_bridge

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_DIR = REPO_ROOT / "installer" / "SortView.Setup.Core"
CORE_PROJECT = CORE_DIR / "SortView.Setup.Core.csproj"
STAGER_CODE = CORE_DIR / "BundleStager.cs"

PRODUCT, VERSION = "SortView Collector", "1.2.3"
FILES = {
    "setup.ps1": b"# fixture, not a script",
    "install.ps1": b"# fixture, not a script",
    "collector_config.example.json": b'{"api_url": "https://fixture.invalid"}',
    "tools/set-api-token.ps1": b"# fixture",
    "runtime/SortViewCollector.exe": b"fixture: not a program",
    "runtime/_internal/library.dll": b"fixture: not a library",
    "runtime/_internal/numpy/core/module.pyd": b"fixture: not an extension",
    "runtime/_internal/data[1].txt": b"a name with brackets",
    "runtime/_internal/zz-last.dll": b"the last file copied",
}
SYSTEM, ADMINISTRATORS = "S-1-5-18", "S-1-5-32-544"
# NT AUTHORITY\LOCAL SERVICE: never in this user's token, elevated or not, so never an owner it may assign.
LOCAL_SERVICE = "S-1-5-19"
WINDOWS_POWERSHELL_32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "SysWOW64" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
TRUSTED_INSTALLER = "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"


# =====================================================================================================================
# 1. STATIC checks
# =====================================================================================================================

def _code() -> str:
    text = STAGER_CODE.read_text(encoding="utf-8")
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def test_the_installer_entry_point_always_uses_the_production_policy():
    code = re.sub(r"\s+", " ", _code())
    assert ("public static StagedBundle Stage(string sourceRoot, string expectedManifestSha256, string expectedVersion, "
            "CancellationToken cancellationToken, IProgress<StagingProgress> progress = null) => "
            "Stage(sourceRoot, expectedManifestSha256, expectedVersion, StagingPolicy.Production, cancellationToken, progress);") in code
    production = code[code.index("public static StagingPolicy Production =>"):code.index("public string StagingRoot")]
    assert 'Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "SortView", "SetupStaging")' in production
    assert "new[] { SystemSid, AdministratorsSid, TrustedInstallerSid }, new[] { SystemSid, AdministratorsSid }, AdministratorsSid" in production
    assert TRUSTED_INSTALLER in code


def test_the_stager_runs_nothing_and_never_copies_or_deletes_by_wildcard_or_recursion():
    code = _code()
    for forbidden in ("System.Diagnostics", "Process", "System.Management.Automation", "PowerShell", "System.Net", "Http", "Socket",
                      "Assembly.Load", "File.Copy", "CopyTo(", "Directory.Move", "File.Move", "MoveTo(", "SearchOption.AllDirectories",
                      '"*"', "Directory.Delete(path, true)", "Directory.Delete(runFolder, true)", ", true);\n"):
        assert forbidden not in code, forbidden
    assert re.findall(r"Directory\.Delete\([^)]*\)", code) == ["Directory.Delete(runFolder, false)", "Directory.Delete(path, false)",
                                                                "Directory.Delete(path, false)"]
    # The only native calls: create-new with a security descriptor, and the run folder's lifetime handle (opened, its
    # attributes read, and the folder finally deleted through it).
    # The classifier adds one read: a handle's security descriptor, into a managed buffer.
    assert code.count("[DllImport(") == 5
    assert set(re.findall(r"static extern \w+ (\w+)\(", code)) == {"CreateDirectoryWithSecurity", "CreateFile", "GetFileInformationByHandle",
                                                                 "SetFileInformationByHandle", "GetKernelObjectSecurity"}
    assert sorted(re.findall(r'EntryPoint = "(\w+)"', code)) == ["CreateDirectoryW", "CreateFileW"]
    # Every handle only ever opens something that EXISTS, on the entry itself (never through a link), and never shares
    # delete: the lifetime handle, and the classifier's root pin, check handle and marker read.
    calls = re.findall(r"CreateFile\((?!string)[^;]*;", code)
    assert len(calls) == 4 and all("OpenExisting" in call and "FileFlagOpenReparsePoint" in call for call in calls), calls
    assert "OpenExisting, FileFlagBackupSemantics | FileFlagOpenReparsePoint" in code
    assert "FileShareRead | FileShareWrite," in code and "FileShareDelete" not in code
    # New files only (CreateNew), never overwriting: the copied files, the staged manifest and the run marker.
    assert code.count("FileMode.CreateNew") == 3 and "FileMode.Create," not in code and "FileMode.OpenOrCreate" not in code


def test_the_injected_fault_is_internal_and_never_honored_by_the_production_policy():
    code = re.sub(r"\s+", " ", _code())
    assert "internal enum StagingFault" in code and "internal StagingFault InjectedFault" in code
    assert "public enum StagingFault" not in code and "public StagingFault" not in code
    # Only the production property builds a production policy; the public constructor never does.
    assert ": this(stagingRoot, trustedPrincipals, accessPrincipals, owner, false)" in code
    production = code[code.index("public static StagingPolicy Production =>"):code.index("internal StagingFault InjectedFault")]
    assert "AdministratorsSid, true);" in production
    # Read as None, and refused when set, on the production policy.
    assert "get => isProduction ? StagingFault.None : injectedFault;" in code
    assert "if (isProduction) { throw new InvalidOperationException(" in code
    # Consulted at exactly the two steps, and nowhere else.
    assert code.count("policy.InjectedFault ==") == 2
    assert "policy.InjectedFault == StagingFault.RunLock" in code and "policy.InjectedFault == StagingFault.RunMarker" in code


def test_the_problem_codes_only_grew():
    body = _code()
    body = body[body.index("public enum StagingProblemCode"):]
    body = body[body.index("{") + 1:body.index("}")]
    # M2b-1's codes in their original order (so their values are unchanged), then M2b-2a's, appended.
    assert re.findall(r"^\s*(\w+),\s*$", body, flags=re.MULTILINE) == [
        "RequestInvalid", "StagingRootInvalid", "UnsupportedVolume", "ReparsePoint", "NotADirectory", "UntrustedOwner",
        "WritableByUntrusted", "CreateFailed", "PermissionsNotApplied", "RunFolderExists", "InsufficientSpace", "SourceNotVerified",
        "SourceChanged", "SourceUnsafe", "CopyFailed", "AccessDenied", "Cancelled", "StagedNotVerified", "CleanupFailed",
        "UnexpectedError", "RunLockFailed", "RunMarkerFailed"]


def test_source_and_staged_copy_are_verified_by_the_same_strict_rules():
    code = _code()
    assert "sourceVerification = BundleVerifier.Verify(sourceRoot, expectedHash, expectedVersion);" in code
    assert "stagedVerification = BundleVerifier.Verify(bundlePath, expectedHash, expectedVersion);" in code
    order = [code.index(s) for s in ("CheckChain(root, policy, problems)", "sourceVerification = BundleVerifier.Verify",
                                     "CreateProtected(runFolder", "runLock = AcquireRunLock(runFolder",
                                     "CheckCreated(runFolder, policy, problems);\n                if",  # again, now it is pinned
                                     "WriteRunMarker(runFolder", "ReadSourceManifest(source", "CopyListedFile(source",
                                     "CheckChain(root, policy, problems, mustExist: true);\n                CheckCreated(runFolder, policy, problems);",
                                     "stagedVerification = BundleVerifier.Verify")]
    assert order == sorted(order)


# =====================================================================================================================
# 2. BEHAVIOR
# =====================================================================================================================

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(files: dict[str, bytes], extra: list[dict] | None = None, version: str = VERSION) -> bytes:
    entries = [{"path": p, "sha256": _sha256(d), "size_bytes": len(d)} for p, d in files.items()] + (extra or [])
    return (json.dumps({"product": PRODUCT, "version": version, "built_at": "2026-01-01T00:00:00Z",
                        "files": sorted(entries, key=lambda e: e["path"])}, indent=2) + "\n").encode()


def _bundle(root: Path, files: dict[str, bytes] | None = None, manifest: bytes | None = None) -> str:
    files = FILES if files is None else files
    root.mkdir(parents=True)
    for path, data in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(data)
    data = _manifest(files) if manifest is None else manifest
    (root / "MANIFEST.json").write_bytes(data)
    return _sha256(data)


def _attributes(path: Path, add: int) -> None:
    current = ctypes.windll.kernel32.GetFileAttributesW(str(path))
    assert ctypes.windll.kernel32.SetFileAttributesW(str(path), current | add), path


def _junction(link: Path, target: Path) -> None:
    completed = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _is_reparse(path: Path) -> bool:
    return bool(os.lstat(path).st_file_attributes & 0x400)


def _icacls(path: Path, *args: str) -> None:
    completed = subprocess.run(["icacls", str(path), *args], capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr


# What another process can do to a run folder. Opens on the entry itself (never through a link); renames are put back at
# once if they ever succeed, so a failing check cannot leave the fixture moved.
_PIN_CS = r"""
public static class Pin {
    const uint DELETE = 0x10000, OPEN_EXISTING = 3, BACKUP = 0x02000000, OPEN_REPARSE = 0x00200000;
    [System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
    static extern Microsoft.Win32.SafeHandles.SafeFileHandle CreateFileW(string p, uint access, uint share, System.IntPtr sa, uint disp, uint flags, System.IntPtr t);
    [System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
    static extern bool MoveFileExW(string a, string b, uint flags);
    [System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
    static extern bool RemoveDirectoryW(string p);
    static string Error() { return "error " + System.Runtime.InteropServices.Marshal.GetLastWin32Error(); }
    public static string ExclusiveDeleteOpen(string p) {
        using (var h = CreateFileW(p, DELETE, 0, System.IntPtr.Zero, OPEN_EXISTING, BACKUP | OPEN_REPARSE, System.IntPtr.Zero)) return h.IsInvalid ? Error() : "OK";
    }
    public static string Remove(string p) { return RemoveDirectoryW(p) ? "REMOVED" : Error(); }
    public static string RenameAndBack(string p) {
        if (!MoveFileExW(p, p + "-renamed", 0)) return Error();
        MoveFileExW(p + "-renamed", p, 0);
        return "RENAMED";
    }
    public static System.Collections.Generic.Dictionary<string, object> Live(string run) {
        return new System.Collections.Generic.Dictionary<string, object> {
            { "exclusive_delete_open", ExclusiveDeleteOpen(run) }, { "remove", Remove(run) }, { "rename", RenameAndBack(run) },
            { "rename_root", RenameAndBack(System.IO.Path.GetDirectoryName(run)) } };
    }
}
"""

# A second process's view of a run folder: "live" tries to remove and rename it (and its staging root); "exclusive" only
# tries the exclusive delete-access open a future cleanup would use.
_OTHER_PROCESS = r"""
param([string]$Path, [string]$Mode = 'live')
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @"
""" + _PIN_CS + r"""
"@
if ($Mode -eq 'exclusive') { ConvertTo-Json -Compress -InputObject @{ exclusive_delete_open = [Pin]::ExclusiveDeleteOpen($Path) } }
else { ConvertTo-Json -Compress -InputObject ([Pin]::Live($Path)) }
"""

_PROBE = r"""
param([string]$CorePath, [string]$CasesPath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
Add-Type -Path $CorePath
Add-Type -ReferencedAssemblies @($CorePath, 'System.Web.Extensions', 'System.Core') -TypeDefinition @"
using System; using System.Collections; using System.Collections.Generic; using System.IO; using System.Linq;
using System.Security.AccessControl; using System.Security.Cryptography; using System.Security.Principal; using System.Threading;
using System.Web.Script.Serialization; using System.Reflection; using SortView.Setup.Core;
""" + _PIN_CS + r"""
// TEST HOOKS, reached only through the stager's public progress callback (called synchronously on the staging thread).
public sealed class Hook : IProgress<StagingProgress> {
    readonly IDictionary<string, object> spec; readonly string source; readonly CancellationTokenSource cancel;
    public readonly List<string> Seen = new List<string>();
    public Hook(IDictionary<string, object> spec, string source, CancellationTokenSource cancel) { this.spec = spec; this.source = source; this.cancel = cancel; }
    public void Report(StagingProgress p) {
        Seen.Add(p.Step + ":" + p.Path);
        if (spec == null || p.Step != StagingStep.FileCopied || p.Done != 1) return;
        string kind = (string)spec["kind"];
        string target = Path.Combine(source, ((string)spec["file"]).Replace('/', '\\'));
        if (kind == "modify") File.WriteAllBytes(target, Convert.FromBase64String((string)spec["data"]));
        else if (kind == "delete") File.Delete(target);
        else if (kind == "deny_read") {
            var security = File.GetAccessControl(target);
            security.AddAccessRule(new FileSystemAccessRule(WindowsIdentity.GetCurrent().User, FileSystemRights.ReadData, AccessControlType.Deny));
            File.SetAccessControl(target, security);
        }
        else if (kind == "cancel") cancel.Cancel();
    }
}

public static class StagingProbe {
    static string Sha(string path) { using (var s = SHA256.Create()) using (var f = File.OpenRead(path)) return string.Concat(s.ComputeHash(f).Select(b => b.ToString("x2"))); }

    static StagingPolicy Policy(IDictionary<string, object> c) {
        Func<string, IEnumerable<SecurityIdentifier>> sids = key => ((IEnumerable)c[key]).Cast<string>().Select(s => s == "ME" ? WindowsIdentity.GetCurrent().User : new SecurityIdentifier(s));
        object owner = c.ContainsKey("owner") ? c["owner"] : null;
        var policy = new StagingPolicy((string)c["root"], sids("trusted"), sids("access"), owner == null ? null : new SecurityIdentifier((string)owner));
        if (c.ContainsKey("fault")) FaultProperty().SetValue(policy, Enum.Parse(FaultProperty().PropertyType, (string)c["fault"]));
        return policy;
    }

    // The TEST-ONLY injected fault: internal, so reached by reflection, on a policy this probe built.
    static PropertyInfo FaultProperty() {
        return typeof(StagingPolicy).GetProperty("InjectedFault", BindingFlags.Instance | BindingFlags.NonPublic);
    }

    static Dictionary<string, object> Describe(StagedBundle staged, string root) {
        var r = new Dictionary<string, object>();
        r["staged"] = staged.IsStaged; r["version"] = staged.Version; r["file_count"] = staged.FileCount;
        r["problems"] = staged.Problems.Select(p => p.Code.ToString()).ToList();
        r["problem_paths"] = staged.Problems.Select(p => p.Path).ToList();
        r["source_problems"] = staged.SourceVerification == null ? null : staged.SourceVerification.Problems.Select(p => p.Code.ToString()).ToList();
        r["source_verified"] = staged.SourceVerification != null && staged.SourceVerification.IsVerified;
        r["staged_verified"] = staged.StagedVerification != null && staged.StagedVerification.IsVerified;
        r["cleanup_problems"] = staged.CleanupProblems.Select(p => p.ToString()).ToList();
        r["bundle_path"] = staged.BundlePath;
        if (staged.IsStaged) {
            var files = new Dictionary<string, object>();
            foreach (var f in new DirectoryInfo(staged.BundlePath).EnumerateFiles("*", SearchOption.AllDirectories))
                files[f.FullName.Substring(staged.BundlePath.Length + 1).Replace('\\', '/')] = Sha(f.FullName);
            r["staged_files"] = files;
            string run = Path.GetDirectoryName(staged.BundlePath);
            var security = new DirectoryInfo(run).GetAccessControl(AccessControlSections.Access | AccessControlSections.Owner);
            r["run_owner"] = security.GetOwner(typeof(SecurityIdentifier)).Value;
            r["run_protected"] = security.AreAccessRulesProtected;
            r["run_rules"] = security.GetAccessRules(true, true, typeof(SecurityIdentifier)).Cast<FileSystemAccessRule>()
                .Select(x => x.IdentityReference.Value + "|" + x.AccessControlType + "|" + x.FileSystemRights + "|" + x.IsInherited + "|" + x.InheritanceFlags).ToList();
            r["run_folder"] = run;
            r["run_entries"] = new DirectoryInfo(run).EnumerateFileSystemInfos().Select(e => e.Name).OrderBy(n => n, StringComparer.Ordinal).ToList();
            string marker = Path.Combine(run, "staging-run.json");
            r["marker"] = File.Exists(marker) ? File.ReadAllText(marker) : null;
        }
        return r;
    }

    static List<string> RunFolders(string root) {
        return Directory.Exists(root) ? Directory.GetDirectories(root).Select(Path.GetFileName).ToList() : new List<string>();
    }

    public static Dictionary<string, object> Run(string casesJson) {
        var cases = (IDictionary<string, object>)new JavaScriptSerializer().DeserializeObject(casesJson);
        var outcome = new Dictionary<string, object>();
        foreach (var pair in cases) {
            var c = (IDictionary<string, object>)pair.Value;
            var r = new Dictionary<string, object>();
            try {
                if (c.ContainsKey("concurrent")) {
                    var results = new StagedBundle[2];
                    var errors = new string[2];
                    var sources = ((IEnumerable)c["sources"]).Cast<string>().ToList();
                    var threads = Enumerable.Range(0, 2).Select(i => new Thread(() => {
                        // An exception escaping a thread would end the whole process: recorded instead.
                        try { results[i] = BundleStager.Stage(sources[i], (string)c["hash"], null, Policy(c), CancellationToken.None); }
                        catch (Exception e) { errors[i] = e.ToString(); }
                    })).ToList();
                    threads.ForEach(t => t.Start()); threads.ForEach(t => t.Join());
                    if (errors.Any(e => e != null)) throw new InvalidOperationException(string.Join(" | ", errors.Where(e => e != null)));
                    r["results"] = results.Select(x => Describe(x, (string)c["root"])).ToList();
                    foreach (var x in results) x.Dispose();
                    r["cleanup_after_dispose"] = results.SelectMany(x => x.CleanupProblems.Select(p => p.ToString())).ToList();
                } else {
                    var cancel = new CancellationTokenSource();
                    var hook = new Hook(c.ContainsKey("hook") ? (IDictionary<string, object>)c["hook"] : null, (string)c["source"], cancel);
                    Guid? runId = c.ContainsKey("run_id") ? (Guid?)new Guid((string)c["run_id"]) : null;
                    var staged = BundleStager.Stage((string)c["source"], (string)c["hash"], (string)c["version"], Policy(c), cancel.Token, hook, runId);
                    r = Describe(staged, (string)c["root"]);
                    r["progress"] = hook.Seen;
                    if (staged.IsStaged && c.ContainsKey("other_process")) {
                        // While the run is alive: this process, then another one, try to remove and rename it.
                        string liveRun = Path.GetDirectoryName(staged.BundlePath);
                        r["live_same_process"] = Pin.Live(liveRun);
                        var other = new System.Diagnostics.ProcessStartInfo((string)c["powershell"],
                            "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"" + (string)c["other_process"] + "\" -Path \"" + liveRun + "\"");
                        other.UseShellExecute = false; other.CreateNoWindow = true; other.RedirectStandardOutput = true;
                        using (var p = System.Diagnostics.Process.Start(other)) { r["live_other_process"] = p.StandardOutput.ReadToEnd().Trim(); p.WaitForExit(); }
                    }
                    if (staged.IsStaged && c.ContainsKey("plant_junction")) {
                        // A link inside the staged tree before cleanup: removed as a link, its target never entered.
                        var psi = new System.Diagnostics.ProcessStartInfo("cmd", "/c mklink /J \"" + Path.Combine(staged.BundlePath, "runtime\\linked") + "\" \"" + (string)c["plant_junction"] + "\"");
                        psi.UseShellExecute = false; psi.CreateNoWindow = true;
                        using (var p = System.Diagnostics.Process.Start(psi)) p.WaitForExit();
                    }
                    string run = staged.IsStaged ? Path.GetDirectoryName(staged.BundlePath) : null;
                    staged.Dispose();
                    staged.Dispose(); // twice: harmless
                    r["disposed"] = staged.IsDisposed;
                    r["path_after_dispose"] = staged.BundlePath;
                    r["run_folder_gone"] = run == null || !Directory.Exists(run);
                    r["cleanup_after_dispose"] = staged.CleanupProblems.Select(p => p.ToString()).ToList();
                }
                r["root_entries_after"] = RunFolders((string)c["root"]);
            } catch (Exception e) { r["error"] = e.ToString(); }
            outcome[pair.Key] = r;
        }
        var production = StagingPolicy.Production;
        outcome["_process_bits"] = IntPtr.Size * 8;
        outcome["_production"] = new Dictionary<string, object> {
            { "root", production.StagingRoot }, { "program_files", Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles) },
            { "trusted", production.TrustedPrincipals.Select(s => s.Value).ToList() }, { "access", production.AccessPrincipals.Select(s => s.Value).ToList() },
            { "owner", production.Owner.Value } };
        // The production policy takes no injected fault: setting one is refused, and it always reads None.
        string faultSet;
        try { FaultProperty().SetValue(production, Enum.Parse(FaultProperty().PropertyType, "RunMarker")); faultSet = "ACCEPTED"; }
        catch (TargetInvocationException e) { faultSet = e.InnerException.GetType().Name; }
        ((Dictionary<string, object>)outcome["_production"])["fault_set"] = faultSet;
        ((Dictionary<string, object>)outcome["_production"])["fault_read"] = FaultProperty().GetValue(production).ToString();
        ((Dictionary<string, object>)outcome["_production"])["fault_public"] =
            typeof(StagingPolicy).GetProperty("InjectedFault") != null || typeof(StagingPolicy).Assembly.GetType("SortView.Setup.Core.StagingFault").IsPublic;
        return outcome;
    }
}
"@
$result = [StagingProbe]::Run((Get-Content -LiteralPath $CasesPath -Raw))
ConvertTo-Json -InputObject $result -Depth 8 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


@pytest.fixture(scope="module")
def core_dll() -> Path:
    completed = subprocess.run([_MSBUILD, str(CORE_PROJECT), "/p:Configuration=Release", "/nologo", "/v:minimal"],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return CORE_DIR / "bin" / "Release" / "SortView.Setup.Core.dll"


def _policy(root: Path, **overrides) -> dict:
    policy = {"root": str(root), "trusted": [SYSTEM, ADMINISTRATORS, TRUSTED_INSTALLER, "ME"], "access": [SYSTEM, ADMINISTRATORS, "ME"],
              "owner": None}
    policy.update(overrides)
    return policy


def _build(base: Path) -> tuple[dict, dict]:
    cases: dict[str, dict] = {}
    notes: dict = {"denied": [], "sources": {}, "outside": None, "preexisting": None, "roots": []}

    def case(name: str, *, files=None, manifest=None, version=None, root=None, **extra) -> Path:
        source = base / "sources" / name
        sha = _bundle(source, files, manifest)
        root = root or base / "roots" / name
        if not extra.pop("root_prepared", False):
            root.mkdir(parents=True, exist_ok=True)
        cases[name] = {"source": str(source), "hash": sha, "version": version, **_policy(root), **extra}
        notes["sources"][name] = source
        notes["roots"].append(root)
        return source

    case("valid")
    case("valid_with_version", version=VERSION)
    case("version_mismatch", version="9.9.9")
    case("invalid_version", version="1.2; x")
    case("wrong_hash")
    cases["wrong_hash"]["hash"] = _sha256(b"another manifest")
    case("invalid_hash")
    cases["invalid_hash"]["hash"] = "not-a-hash"

    source = case("modified_before")
    (source / "runtime/_internal/library.dll").write_bytes(b"FIXTURE: NOT A LIBRARY")
    source = case("missing_before")
    (source / "runtime/_internal/library.dll").unlink()
    source = case("unlisted_file")
    (source / "notes.txt").write_bytes(b"unlisted")
    source = case("unlisted_hidden_file")
    (source / "runtime/_internal/planted.dll").write_bytes(b"planted")
    _attributes(source / "runtime/_internal/planted.dll", 0x2)
    source = case("unlisted_folder")
    (source / "runtime/empty").mkdir()

    files = dict(FILES)
    hidden = base / "sources" / "listed_hidden_and_system"
    _bundle(hidden, files)
    _attributes(hidden / "runtime/_internal/library.dll", 0x2)
    _attributes(hidden / "runtime/SortViewCollector.exe", 0x4)
    root = base / "roots" / "listed_hidden_and_system"
    root.mkdir(parents=True)
    cases["listed_hidden_and_system"] = {"source": str(hidden), "hash": _sha256(_manifest(files)), "version": None, **_policy(root)}
    notes["sources"]["listed_hidden_and_system"] = hidden

    outside = base / "outside"
    outside.mkdir()
    (outside / "from-outside.dll").write_bytes(b"outside")
    notes["outside"] = outside
    source = case("source_junction")
    _junction(source / "runtime/_internal/linked", outside)
    case("unsafe_manifest_path", manifest=_manifest(FILES, extra=[{"path": "../escape.dll", "sha256": "0" * 64, "size_bytes": 0}]))

    # Changes after the source verified, made through the progress callback once the FIRST file is copied (files are
    # copied in manifest order, so "runtime/_internal/zz-last.dll" is still to come).
    case("changed_during_copy", hook={"kind": "modify", "file": "runtime/_internal/zz-last.dll",
                                       "data": "VEhFIExBU1QgRklMRSBDT1BJRUQ="})  # same size, different bytes
    case("grew_during_copy", hook={"kind": "modify", "file": "runtime/_internal/zz-last.dll",
                                    "data": "dGhlIGxhc3QgZmlsZSBjb3BpZWQgYW5kIG1vcmUgYW5kIG1vcmU="})
    case("deleted_during_copy", hook={"kind": "delete", "file": "runtime/_internal/zz-last.dll"})
    source = case("unreadable_during_copy", hook={"kind": "deny_read", "file": "runtime/_internal/zz-last.dll"})
    notes["denied"].append(source / "runtime/_internal/zz-last.dll")
    case("cancelled", hook={"kind": "cancel", "file": "setup.ps1"})
    # TEST-ONLY injected faults (test policies only): the run folder's handle opened but not kept, and the marker created
    # but not written. Either way the run folder must go, and no handle may stay open (one would block its removal).
    case("fault_run_lock", fault="RunLock")
    case("fault_run_marker", fault="RunMarker")

    # The destination.
    root = base / "roots" / "root_missing" / "SortView" / "SetupStaging"
    case("root_missing_is_created", root=root, root_prepared=True)
    root = base / "roots" / "writable_root"
    case("writable_root", root=root)
    notes["writable_root"] = root
    # C:\ is owned by TrustedInstaller: a policy that does not trust it refuses there, on ownership.
    case("untrusted_owner")
    cases["untrusted_owner"]["trusted"] = [SYSTEM, ADMINISTRATORS, "ME"]
    # Not trusting the current user: the first folder it may write to (its profile) is refused, on permissions.
    case("profile_writable_by_untrusted_user")
    cases["profile_writable_by_untrusted_user"]["trusted"] = [SYSTEM, ADMINISTRATORS, TRUSTED_INSTALLER]
    cases["profile_writable_by_untrusted_user"]["access"] = [SYSTEM, ADMINISTRATORS]
    real = base / "roots" / "real-target"
    real.mkdir(parents=True)
    _junction(base / "roots" / "junction_root", real)
    case("junction_root", root=base / "roots" / "junction_root", root_prepared=True)
    (base / "roots" / "parent-target").mkdir(parents=True)
    _junction(base / "roots" / "junction_parent", base / "roots" / "parent-target")
    case("junction_parent", root=base / "roots" / "junction_parent" / "stage", root_prepared=True)
    (base / "roots" / "a-file").write_bytes(b"not a folder")
    case("root_is_a_file", root=base / "roots" / "a-file", root_prepared=True)
    case("access_not_trusted")
    cases["access_not_trusted"]["access"] = [SYSTEM, ADMINISTRATORS, "ME", "S-1-1-0"]

    preexisting_id = "0123456789abcdef0123456789abcdef"
    root = base / "roots" / "collision"
    case("collision", root=root, run_id="01234567-89ab-cdef-0123-456789abcdef")
    (root / preexisting_id).mkdir()
    (root / preexisting_id / "someone-elses.txt").write_bytes(b"not ours")
    notes["preexisting"] = root / preexisting_id

    case("cleanup_with_junction", plant_junction=str(outside))
    # A run-folder owner Windows will not let this process assign: CreateDirectoryW fails (ERROR_INVALID_OWNER), so the
    # folder is never created -- not created with some other owner or permissions.
    case("unassignable_owner")
    cases["unassignable_owner"]["trusted"] = [SYSTEM, ADMINISTRATORS, TRUSTED_INSTALLER, "ME", LOCAL_SERVICE]
    cases["unassignable_owner"]["owner"] = LOCAL_SERVICE
    root = base / "roots" / "concurrent"
    root.mkdir(parents=True)
    sources = [base / "sources" / f"concurrent_{i}" for i in (1, 2)]
    sha = ""
    for s in sources:
        sha = _bundle(s)
    cases["concurrent"] = {"concurrent": True, "sources": [str(s) for s in sources], "hash": sha, **_policy(root)}
    return cases, notes


def _run_probe(base: Path, cases: dict, core_dll: Path, powershell: str) -> dict:
    if "valid" in cases:  # while it is alive, another process (always 64-bit Windows PowerShell) tries to remove or rename it
        (base / "other_process.ps1").write_text(_OTHER_PROCESS, encoding="utf-8")
        cases["valid"] = {**cases["valid"], "other_process": str(base / "other_process.ps1"), "powershell": _POWERSHELL}
    (base / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    (base / "probe.ps1").write_text(_PROBE, encoding="utf-8")
    completed = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                str(base / "probe.ps1"), "-CorePath", str(core_dll), "-CasesPath", str(base / "cases.json"),
                                "-OutPath", str(base / "out.json")], capture_output=True, text=True, timeout=600, check=False)
    assert (base / "out.json").exists(), completed.stdout + completed.stderr
    return json.loads((base / "out.json").read_text(encoding="utf-8-sig"))


def _remove_links(base: Path) -> None:
    for dirpath, dirnames, _ in os.walk(base, topdown=True, followlinks=False):  # junctions removed as links only
        for name in list(dirnames):
            if _is_reparse(Path(dirpath) / name):
                os.rmdir(Path(dirpath) / name)
                dirnames.remove(name)


@pytest.fixture(scope="module")
def results(tmp_path_factory, core_dll) -> dict:
    base = tmp_path_factory.mktemp("staging")
    cases, notes = _build(base)
    user = getpass.getuser()
    _icacls(notes["writable_root"], "/grant", "*S-1-1-0:(OI)(CI)(W)")  # Everyone may write: a fixture folder only
    try:
        out = _run_probe(base, cases, core_dll, _POWERSHELL)
    finally:
        for path in notes["denied"]:
            if path.exists():
                _icacls(path, "/remove:d", user)
        _icacls(notes["writable_root"], "/remove:g", "*S-1-1-0")
    out["_notes"] = notes
    out["_leftovers"] = sorted(str(p) for root in notes["roots"] if root.is_dir() and not _is_reparse(root)
                               for p in root.iterdir() if p.name != notes["preexisting"].name)
    _remove_links(base)
    return out


# The native call in a 32-bit process (SysWOW64 Windows PowerShell): SECURITY_ATTRIBUTES is 12 bytes there, not 24.
CASES_32 = ("valid", "listed_hidden_and_system", "collision", "unassignable_owner", "cleanup_with_junction", "concurrent", "cancelled",
            "fault_run_lock", "fault_run_marker")


@pytest.fixture(scope="module")
def results32(tmp_path_factory, core_dll) -> dict:
    if not WINDOWS_POWERSHELL_32.exists():
        pytest.skip("no 32-bit Windows PowerShell on this machine")
    base = tmp_path_factory.mktemp("staging32")
    cases, notes = _build(base)
    try:
        out = _run_probe(base, {name: cases[name] for name in CASES_32}, core_dll, str(WINDOWS_POWERSHELL_32))
    finally:
        _remove_links(base)
    out["_notes"] = notes
    return out


def _r(results: dict, name: str) -> dict:
    result = results[name]
    assert "error" not in result, result.get("error")
    return result


@needs_bridge
@pytest.mark.parametrize("name", ["valid", "valid_with_version", "listed_hidden_and_system", "root_missing_is_created"])
def test_a_verified_bundle_is_staged_exactly_and_removed_on_dispose(results, name):
    r = _r(results, name)
    assert r["staged"] is True and r["problems"] == [] and r["source_verified"] and r["staged_verified"], r
    assert r["version"] == VERSION and r["file_count"] == len(FILES)
    # Exactly the listed files plus the manifest, byte for byte the originals.
    source = results["_notes"]["sources"][name]
    expected = {p: _sha256(d) for p, d in FILES.items()} | {"MANIFEST.json": _sha256((source / "MANIFEST.json").read_bytes())}
    assert r["staged_files"] == expected
    # The run folder: protected, owned by the creator (this test policy names no owner), full control for exactly the
    # access principals, nothing inherited.
    assert r["run_protected"] is True
    me = r["run_owner"]
    assert sorted(rule.split("|")[0] for rule in r["run_rules"]) == sorted([SYSTEM, ADMINISTRATORS, me])
    assert all(rule.endswith("|Allow|FullControl|False|ContainerInherit, ObjectInherit") for rule in r["run_rules"]), r["run_rules"]
    # Dispose removed it, and only it.
    assert r["disposed"] is True and r["path_after_dispose"] is None and r["run_folder_gone"] is True
    assert r["cleanup_after_dispose"] == [] and r["root_entries_after"] == []
    assert r["progress"][0] == "SourceVerified:" and r["progress"][-1] == "StagedVerified:"


EXPECTED = {
    "version_mismatch": (["SourceNotVerified"], ["VersionMismatch"]),
    "invalid_version": (["RequestInvalid"], None),
    "wrong_hash": (["SourceNotVerified"], ["ManifestHashMismatch"]),
    "invalid_hash": (["RequestInvalid"], None),
    "modified_before": (["SourceNotVerified"], ["HashMismatch"]),
    "missing_before": (["SourceNotVerified"], ["FileMissing"]),
    "unlisted_file": (["SourceNotVerified"], ["UnexpectedFile"]),
    "unlisted_hidden_file": (["SourceNotVerified"], ["UnexpectedFile"]),
    "unlisted_folder": (["SourceNotVerified"], ["UnexpectedDirectory"]),
    "source_junction": (["SourceNotVerified"], ["ReparsePoint"]),
    "unsafe_manifest_path": (["SourceNotVerified"], ["UnsafePath"]),
    "changed_during_copy": (["SourceChanged"], []),
    "grew_during_copy": (["SourceChanged"], []),
    "deleted_during_copy": (["SourceChanged"], []),
    "unreadable_during_copy": (["AccessDenied"], []),
    "cancelled": (["Cancelled"], []),
    "writable_root": (["WritableByUntrusted"], None),
    "untrusted_owner": (["UntrustedOwner"], None),
    "profile_writable_by_untrusted_user": (["WritableByUntrusted"], None),
    "junction_root": (["ReparsePoint"], None),
    "junction_parent": (["ReparsePoint"], None),
    "root_is_a_file": (["NotADirectory"], None),
    "access_not_trusted": (["RequestInvalid"], None),
    "collision": (["RunFolderExists"], []),
    "unassignable_owner": (["CreateFailed"], []),
    "fault_run_lock": (["RunLockFailed"], []),
    "fault_run_marker": (["RunMarkerFailed"], []),
}


@needs_bridge
@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_unsafe_or_failed_staging_is_refused_and_leaves_nothing(results, name):
    r = _r(results, name)
    problems, source_problems = EXPECTED[name]
    assert r["staged"] is False and r["bundle_path"] is None, r
    assert r["problems"] == problems, r
    if source_problems is None:
        assert r["source_problems"] is None, r  # stopped before the source was even read
    else:
        assert r["source_problems"] == source_problems, r
    assert r["cleanup_problems"] == [] and r["cleanup_after_dispose"] == [], r
    # Nothing left in the staging root (the collision's pre-existing folder is not ours and is never touched).
    assert r["root_entries_after"] == ([] if name != "collision" else ["0123456789abcdef0123456789abcdef"]), r


@needs_bridge
def test_a_folder_that_already_exists_is_never_reused_or_touched(results):
    preexisting = results["_notes"]["preexisting"]
    assert (preexisting / "someone-elses.txt").read_bytes() == b"not ours"
    assert sorted(p.name for p in preexisting.iterdir()) == ["someone-elses.txt"]


@needs_bridge
def test_the_problem_names_where_the_chain_is_unsafe(results):
    assert results["writable_root"]["problem_paths"] == [results["writable_root"]["problem_paths"][0]]
    assert results["writable_root"]["problem_paths"][0].endswith("writable_root")
    assert results["junction_root"]["problem_paths"][0].endswith("junction_root")
    assert results["junction_parent"]["problem_paths"][0].endswith("junction_parent")
    assert results["untrusted_owner"]["problem_paths"] == ["C:\\"]
    profile = str(Path(os.environ["USERPROFILE"]))
    assert results["profile_writable_by_untrusted_user"]["problem_paths"][0].lower() == profile.lower()


@needs_bridge
def test_cleanup_removes_a_link_without_entering_it(results):
    r = _r(results, "cleanup_with_junction")
    assert r["staged"] is True and r["run_folder_gone"] is True and r["cleanup_after_dispose"] == [], r
    outside = results["_notes"]["outside"]
    assert (outside / "from-outside.dll").read_bytes() == b"outside"  # the link's target survived


@needs_bridge
def test_concurrent_stagings_are_independent(results):
    r = _r(results, "concurrent")
    first, second = r["results"]
    assert first["staged"] and second["staged"], r
    assert first["run_folder"] != second["run_folder"]
    assert first["staged_files"] == second["staged_files"]
    assert r["cleanup_after_dispose"] == [] and r["root_entries_after"] == []


#: While a run is alive: an exclusive delete-access open, removing it and renaming it all fail with a sharing violation
#: (32), and its staging root cannot be renamed either (access denied, 5) -- from this process and from any other.
LIVE = {"exclusive_delete_open": "error 32", "remove": "error 32", "rename": "error 32", "rename_root": "error 5"}


@needs_bridge
def test_a_staged_run_is_marked_beside_its_bundle(results):
    r = _r(results, "valid")
    assert r["run_entries"] == ["bundle", "staging-run.json"]
    marker = json.loads(r["marker"])
    assert list(marker) == ["schema", "run_id", "manifest_sha256", "created_utc"]
    assert marker["schema"] == "sortview.staging-run/1"
    assert marker["run_id"] == Path(r["run_folder"]).name and re.fullmatch(r"[0-9a-f]{32}", marker["run_id"])
    assert marker["manifest_sha256"] == _sha256((results["_notes"]["sources"]["valid"] / "MANIFEST.json").read_bytes())
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", marker["created_utc"])
    # Not part of the bundle: the staged copy verified on its own, and holds exactly the listed files and manifest.
    assert r["staged_verified"] is True and "staging-run.json" not in r["staged_files"]


@needs_bridge
def test_a_live_run_cannot_be_deleted_or_renamed_by_this_or_another_process(results):
    r = _r(results, "valid")
    assert r["live_same_process"] == LIVE
    assert json.loads(r["live_other_process"]) == LIVE
    # ...and Dispose, through the handle that pinned it, removed it completely.
    assert r["run_folder_gone"] is True and r["cleanup_after_dispose"] == [] and r["root_entries_after"] == []


@needs_bridge
def test_an_injected_lock_or_marker_failure_says_where(results):
    assert _r(results, "fault_run_lock")["problem_paths"][0].endswith("(injected test fault)")
    assert _r(results, "fault_run_marker")["problem_paths"] == ["staging-run.json"]


@needs_bridge
def test_the_production_policy_cannot_take_an_injected_fault(results):
    production = results["_production"]
    assert production["fault_set"] == "InvalidOperationException" and production["fault_read"] == "None"
    assert production["fault_public"] is False


_KILLED_CHILD = r"""
param([string]$CorePath, [string]$Source, [string]$Hash, [string]$Root, [string]$Signal)
$ErrorActionPreference = 'Stop'
Add-Type -Path $CorePath
$me = [Security.Principal.WindowsIdentity]::GetCurrent().User
$system = New-Object Security.Principal.SecurityIdentifier('S-1-5-18')
$admins = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
$installer = New-Object Security.Principal.SecurityIdentifier('TRUSTED_INSTALLER_SID')
$policy = New-Object SortView.Setup.Core.StagingPolicy($Root, [Security.Principal.SecurityIdentifier[]]@($system, $admins, $installer, $me),
    [Security.Principal.SecurityIdentifier[]]@($system, $admins, $me), $null)
$staged = [SortView.Setup.Core.BundleStager]::Stage($Source, $Hash, 'VERSION', $policy, [Threading.CancellationToken]::None, $null, [guid]::NewGuid())
$text = if ($staged.IsStaged) { $staged.BundlePath } else { 'FAILED: ' + ((@($staged.Problems) | ForEach-Object { $_.ToString() }) -join '; ') }
[IO.File]::WriteAllText($Signal + '.tmp', $text)
[IO.File]::Move($Signal + '.tmp', $Signal)
Start-Sleep -Seconds 300   # killed long before this
""".replace("TRUSTED_INSTALLER_SID", TRUSTED_INSTALLER).replace("'VERSION'", f"'{VERSION}'")


def _other_process(script: Path, run: Path, mode: str) -> dict:
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                "-Path", str(run), "-Mode", mode], capture_output=True, text=True, timeout=120, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip())


@needs_bridge
def test_a_killed_staging_process_releases_its_lock_and_leaves_its_run_untouched(tmp_path, core_dll):
    source, root, signal = tmp_path / "source", tmp_path / "root", tmp_path / "staged.txt"
    sha = _bundle(source)
    root.mkdir()
    (tmp_path / "child.ps1").write_text(_KILLED_CHILD, encoding="utf-8")
    other = tmp_path / "other_process.ps1"
    other.write_text(_OTHER_PROCESS, encoding="utf-8")
    child = subprocess.Popen([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(tmp_path / "child.ps1"),
                              "-CorePath", str(core_dll), "-Source", str(source), "-Hash", sha, "-Root", str(root), "-Signal", str(signal)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 120
        while not signal.exists():
            assert child.poll() is None, child.stderr.read()
            assert time.monotonic() < deadline, "the child never staged"
            time.sleep(0.1)
        bundle = signal.read_text(encoding="utf-8")
        assert not bundle.startswith("FAILED"), bundle
        run = Path(bundle).parent
        assert _other_process(other, run, "live") == LIVE  # pinned while the child lives
    finally:
        child.kill()  # TerminateProcess: no Dispose, no finally -- as in a crash
        child.wait(timeout=30)
    # Windows closed the dead process's handle: the run is no longer pinned...
    assert _other_process(other, run, "exclusive") == {"exclusive_delete_open": "OK"}
    # ...and nothing removed it: no automatic cleanup of an abandoned run.
    assert (run / "staging-run.json").is_file() and (run / "bundle" / "MANIFEST.json").is_file()
    assert [p.name for p in root.iterdir()] == [run.name]
    shutil.rmtree(run)  # the test's own fixture (the test policy gives this user full control of it)


@needs_bridge
def test_no_staging_leaves_anything_behind(results):
    # Every staging root holds nothing but what the tests put there themselves (the root_missing case's created
    # SortView\SetupStaging chain is the staging root itself, and is empty).
    assert results["_leftovers"] == [], results["_leftovers"]


@needs_bridge
def test_the_production_policy_is_program_files_and_administrators_only(results):
    production = results["_production"]
    assert production["root"] == str(Path(production["program_files"]) / "SortView" / "SetupStaging")
    assert production["trusted"] == [SYSTEM, ADMINISTRATORS, TRUSTED_INSTALLER]
    assert production["access"] == [SYSTEM, ADMINISTRATORS]
    assert production["owner"] == ADMINISTRATORS


@needs_bridge
def test_an_owner_that_cannot_be_assigned_fails_closed_and_creates_nothing(results):
    r = _r(results, "unassignable_owner")
    assert r["source_verified"] is True  # it got as far as creating the run folder
    assert r["problems"] == ["CreateFailed"] and r["problem_paths"][0].endswith("(error 1307)"), r  # ERROR_INVALID_OWNER
    assert r["root_entries_after"] == [] and r["cleanup_problems"] == [], r


@needs_bridge
def test_the_native_staging_call_works_in_a_32_bit_process(results32):
    assert results32["_process_bits"] == 32
    valid = _r(results32, "valid")
    assert valid["staged"] is True and valid["run_protected"] is True and valid["run_folder_gone"] is True, valid
    assert sorted(rule.split("|")[0] for rule in valid["run_rules"]) == sorted([SYSTEM, ADMINISTRATORS, valid["run_owner"]])
    assert _r(results32, "listed_hidden_and_system")["staged"] is True
    assert _r(results32, "collision")["problems"] == ["RunFolderExists"]
    unassignable = _r(results32, "unassignable_owner")
    assert unassignable["problems"] == ["CreateFailed"] and unassignable["problem_paths"][0].endswith("(error 1307)")
    assert unassignable["root_entries_after"] == []
    assert _r(results32, "cleanup_with_junction")["run_folder_gone"] is True
    assert (results32["_notes"]["outside"] / "from-outside.dll").read_bytes() == b"outside"
    concurrent = _r(results32, "concurrent")
    assert all(x["staged"] for x in concurrent["results"]) and concurrent["root_entries_after"] == []
    # The lifetime handle's native calls (CreateFileW, GetFileInformationByHandle, SetFileInformationByHandle) in 32 bits.
    assert valid["live_same_process"] == LIVE and json.loads(valid["live_other_process"]) == LIVE
    assert json.loads(valid["marker"])["run_id"] == Path(valid["run_folder"]).name
    for name, problem in (("cancelled", "Cancelled"), ("fault_run_lock", "RunLockFailed"), ("fault_run_marker", "RunMarkerFailed")):
        failed = _r(results32, name)
        assert failed["problems"] == [problem] and failed["cleanup_problems"] == [] and failed["root_entries_after"] == [], failed


@needs_bridge
def test_the_main_run_is_a_64_bit_process(results):
    assert results["_process_bits"] == 64


# =====================================================================================================================
# 3. CLASSIFYING EXISTING RUNS (M2b-2b): read only
# =====================================================================================================================

def _classifier_code() -> str:
    code = _code()
    return code[code.index("internal const int MaxRunEntries"):]  # the classifier is the last section of BundleStager


def test_the_classifier_is_read_only_and_never_enters_a_run():
    code = _classifier_code()
    for writer in ("Directory.Delete", "File.Delete", "SetFileInformationByHandle", "MoveFile", "MoveTo(", "Move(", "SetAccessControl",
                   "SetOwner", "CreateDirectory", "FileMode.", "WriteAll", ".Write(", "DeleteOnClose", "0x04000000", "FileShareDelete",
                   "Directory.Enumerate", "SearchOption", "EnumerateFiles", "GetDirectories"):
        assert writer not in code, writer
    # Direct children of the pinned root only: one listing, of the root itself.
    assert code.count("EnumerateFileSystemInfos()") == 1 and "new DirectoryInfo(root).EnumerateFileSystemInfos()" in code
    # A folder without a plain-file marker is never opened for the check: the marker pre-check comes first.
    classify = code[code.index("private static StagingRunEntry ClassifyEntry"):code.index("private static DirectorySecurity ReadSecurity")]
    assert classify.index("File.GetAttributes(markerPath)") < classify.index("CreateFile(path, DeleteAccess")
    # The production entry point always uses the production policy.
    assert re.sub(r"\s+", " ", code).count(
        "public static StagingRunScan ClassifyRuns(CancellationToken cancellationToken) => ClassifyRuns(StagingPolicy.Production, cancellationToken);") == 1


_CLASSIFY_PROBE = r"""
param([string]$CorePath, [string]$ArgsPath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
Add-Type -Path $CorePath
Add-Type -ReferencedAssemblies @($CorePath, 'System.Web.Extensions', 'System.Core') -TypeDefinition @"
using System; using System.Collections; using System.Collections.Generic; using System.IO; using System.Linq; using System.Security.Principal;
using System.Threading; using System.Web.Script.Serialization; using SortView.Setup.Core;
public static class ClassifyProbe {
    static StagingPolicy Policy(IDictionary<string, object> a) {
        var me = WindowsIdentity.GetCurrent().User;
        Func<string, SecurityIdentifier> sid = s => s == "ME" ? me : new SecurityIdentifier(s);
        var trusted = ((IEnumerable)a["trusted"]).Cast<string>().Select(sid);
        var access = ((IEnumerable)a["access"]).Cast<string>().Select(sid);
        return new StagingPolicy((string)a["root"], trusted, access, a["owner"] == null ? null : sid((string)a["owner"]));
    }
    static Dictionary<string, object> Describe(StagingRunScan scan) {
        return new Dictionary<string, object> {
            { "completed", scan.Completed }, { "root_exists", scan.RootExists }, { "truncated", scan.Truncated },
            { "root_problems", scan.RootProblems.Select(p => p.Code.ToString()).ToList() },
            { "entries", scan.Entries.Select(e => new Dictionary<string, object> {
                { "name", e.Name }, { "state", e.State.ToString() }, { "reason", e.Reason.ToString() }, { "detail", e.Detail },
                { "run_id", e.RunId }, { "manifest_sha256", e.ManifestSha256 }, { "created_utc", e.CreatedUtc } }).ToList() } };
    }
    public static Dictionary<string, object> Run(string json) {
        var a = (IDictionary<string, object>)new JavaScriptSerializer().DeserializeObject(json);
        var policy = Policy(a);
        var r = new Dictionary<string, object> { { "bits", IntPtr.Size * 8 } };
        string mode = (string)a["mode"];
        if (mode == "classify") {
            r["scan"] = Describe(BundleStager.ClassifyRuns(policy, CancellationToken.None));
        } else if (mode == "cancelled") {
            r["scan"] = Describe(BundleStager.ClassifyRuns(policy, new CancellationToken(true)));
        } else if (mode == "live_here") {
            // A run alive in THIS process, classified by this process; then disposed, and classified again.
            var staged = BundleStager.Stage((string)a["source"], (string)a["hash"], (string)a["version"], policy, CancellationToken.None);
            r["staged"] = staged.IsStaged;
            r["run"] = staged.IsStaged ? Path.GetFileName(Path.GetDirectoryName(staged.BundlePath)) : null;
            r["during"] = Describe(BundleStager.ClassifyRuns(policy, CancellationToken.None));
            staged.Dispose();
            r["cleanup"] = staged.CleanupProblems.Select(p => p.ToString()).ToList();
            r["after"] = Describe(BundleStager.ClassifyRuns(policy, CancellationToken.None));
        } else if (mode == "interference") {
            // One thread stages and disposes over and over; two others classify the same root the whole time.
            int iterations = Convert.ToInt32(a["iterations"]);
            var stageProblems = new List<string>(); var cleanupProblems = new List<string>(); var errors = new List<string>();
            var live = new HashSet<string>(); var violations = new List<string>(); var states = new Dictionary<string, int>();
            int scans = 0, incomplete = 0; bool done = false; object gate = new object();
            var stager = new Thread(() => {
                try {
                    for (int i = 0; i < iterations; i++) {
                        var staged = BundleStager.Stage((string)a["source"], (string)a["hash"], (string)a["version"], policy, CancellationToken.None);
                        string run = staged.IsStaged ? Path.GetFileName(Path.GetDirectoryName(staged.BundlePath)) : null;
                        lock (gate) { stageProblems.AddRange(staged.Problems.Select(p => p.ToString())); if (run != null) live.Add(run); }
                        Thread.Sleep(2);
                        staged.Dispose();
                        lock (gate) { cleanupProblems.AddRange(staged.CleanupProblems.Select(p => p.ToString())); }
                    }
                } catch (Exception e) { lock (gate) errors.Add(e.ToString()); }
                finally { done = true; }
            });
            Func<Thread> classifier = () => new Thread(() => {
                try {
                    while (!done) {
                        var scan = BundleStager.ClassifyRuns(policy, CancellationToken.None);
                        lock (gate) {
                            scans++; if (!scan.Completed) incomplete++;
                            foreach (var e in scan.Entries) {
                                string key = e.State + "/" + e.Reason; states[key] = states.ContainsKey(key) ? states[key] + 1 : 1;
                                if (e.State == StagingRunState.AbandonedCandidate) violations.Add(e.ToString()); // every run here is live
                            }
                        }
                    }
                } catch (Exception e) { lock (gate) errors.Add(e.ToString()); }
            });
            var threads = new[] { stager, classifier(), classifier() };
            foreach (var t in threads) t.Start();
            foreach (var t in threads) t.Join();
            r["stage_problems"] = stageProblems; r["cleanup_problems"] = cleanupProblems; r["errors"] = errors;
            r["violations"] = violations; r["scans"] = scans; r["incomplete"] = incomplete; r["runs"] = live.Count; r["states"] = states;
            r["after"] = Describe(BundleStager.ClassifyRuns(policy, CancellationToken.None));
        }
        return r;
    }
}
"@
$result = [ClassifyProbe]::Run((Get-Content -LiteralPath $ArgsPath -Raw))
ConvertTo-Json -InputObject $result -Depth 8 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


def _test_policy(root: Path, **overrides) -> dict:
    return {"root": str(root), "trusted": [SYSTEM, ADMINISTRATORS, TRUSTED_INSTALLER, "ME"], "access": [SYSTEM, ADMINISTRATORS, "ME"],
            "owner": None, **overrides}


def _classify(work: Path, core_dll: Path, mode: str = "classify", powershell: str = _POWERSHELL, **args) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    tag = f"{mode}-{len(list(work.glob('args-*.json')))}"
    (work / "classify.ps1").write_text(_CLASSIFY_PROBE, encoding="utf-8")
    (work / f"args-{tag}.json").write_text(json.dumps({"mode": mode, "version": VERSION, **args}), encoding="utf-8")
    out = work / f"out-{tag}.json"
    completed = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(work / "classify.ps1"),
                                "-CorePath", str(core_dll), "-ArgsPath", str(work / f"args-{tag}.json"), "-OutPath", str(out)],
                               capture_output=True, text=True, timeout=600, check=False)
    assert out.exists(), completed.stdout + completed.stderr
    return json.loads(out.read_text(encoding="utf-8-sig"))


def _by_name(scan: dict) -> dict:
    return {e["name"]: e for e in scan["entries"]}


def _start_staging_children(work: Path, core_dll: Path, source: Path, sha: str, root: Path, count: int, tag: str) -> list:
    """`count` separate processes, each staging into `root` and staying alive (holding its run's lifetime handle)."""
    (work / "child.ps1").write_text(_KILLED_CHILD, encoding="utf-8")
    children = []
    for i in range(count):
        signal = work / f"staged-{tag}-{i}.txt"
        proc = subprocess.Popen([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(work / "child.ps1"),
                                 "-CorePath", str(core_dll), "-Source", str(source), "-Hash", sha, "-Root", str(root), "-Signal", str(signal)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        children.append((proc, signal))
    runs = []
    deadline = time.monotonic() + 180
    try:
        for proc, signal in children:
            while not signal.exists():
                assert proc.poll() is None, proc.stderr.read()
                assert time.monotonic() < deadline, "a child never staged"
                time.sleep(0.1)
            bundle = signal.read_text(encoding="utf-8")
            assert not bundle.startswith("FAILED"), bundle
            runs.append(Path(bundle).parent)
    except BaseException:
        _kill([(proc, None) for proc, _ in children])
        raise
    return [(proc, run) for (proc, _), run in zip(children, runs)]


def _kill(children: list) -> None:
    for proc, _ in children:
        proc.kill()  # TerminateProcess: as in a crash, nothing is disposed
    for proc, _ in children:
        proc.wait(timeout=30)


def _snapshot(root: Path) -> dict:
    """Everything a read-only scan must leave exactly as it was: every entry under the root (links not entered), its
    attributes, size, write time and (files) SHA-256, and the owner and permissions of the root and its direct children."""
    entries = {}
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            info = os.lstat(path)
            is_file = name in filenames and not _is_reparse(path)
            entries[str(path.relative_to(root))] = (info.st_file_attributes, info.st_size, info.st_mtime_ns, _sha256(path.read_bytes()) if is_file else None)
        dirnames[:] = [d for d in dirnames if not _is_reparse(Path(dirpath) / d)]
    script = ("Get-ChildItem -LiteralPath $env:SV_SNAPSHOT_ROOT -Force | ForEach-Object { $_.Name + '|' + (Get-Acl -LiteralPath $_.FullName).Sddl }; "
              "'.|' + (Get-Acl -LiteralPath $env:SV_SNAPSHOT_ROOT).Sddl")
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True,
                               check=False, env={**os.environ, "SV_SNAPSHOT_ROOT": str(root)})
    assert completed.returncode == 0, completed.stderr
    return {"entries": entries, "security": sorted(completed.stdout.splitlines())}


#: The marker variants written into abandoned runs, and the reason each must give.
MARKER_CASES = {
    "invalid_json": "MarkerInvalid",
    "wrong_schema": "MarkerInvalid",
    "extra_key": "MarkerInvalid",
    "missing_key": "MarkerInvalid",
    "uppercase_hash": "MarkerInvalid",
    "repeated_key": "MarkerInvalid",
    "bom": "MarkerInvalid",
    "other_run_id": "MarkerMismatch",
    "oversized": "MarkerUnsafe",
}
#: Other changes made to abandoned runs, and the reason each must give.
RUN_CASES = {"no_marker": "MarkerMissing", "marker_is_folder": "MarkerUnsafe", "marker_is_junction": "MarkerUnsafe",
             "extra_permission": "UnexpectedSecurity"}


def _marker_variant(name: str, marker: dict) -> bytes:
    variants = {
        "wrong_schema": {**marker, "schema": "sortview.staging-run/2"},
        "extra_key": {**marker, "pid": "1234"},
        "missing_key": {k: v for k, v in marker.items() if k != "created_utc"},
        "uppercase_hash": {**marker, "manifest_sha256": marker["manifest_sha256"].upper()},
        "other_run_id": {**marker, "run_id": "f" * 32},
    }
    if name in variants:
        return json.dumps(variants[name]).encode()
    if name == "repeated_key":
        return json.dumps(marker)[:-1].encode() + b', "run_id": "' + marker["run_id"].encode() + b'"}'
    if name == "bom":
        return b"\xef\xbb\xbf" + json.dumps(marker).encode()
    if name == "oversized":
        return json.dumps(marker).encode() + b" " * 5000
    return b"{ not json"


@pytest.fixture(scope="module")
def abandoned(tmp_path_factory, core_dll) -> dict:
    """A staging root holding: runs alive in other processes, runs whose process was killed (then altered into every unsafe
    variant), and plain unsafe entries -- classified while the live ones lived, with snapshots around a scan."""
    base = tmp_path_factory.mktemp("classify")
    source, root, work = base / "source", base / "root", base / "work"
    sha = _bundle(source)
    root.mkdir()
    work.mkdir()
    killed_names = ["candidate", *MARKER_CASES, *RUN_CASES]
    live = _start_staging_children(work, core_dll, source, sha, root, 2, "live")
    try:
        killed = _start_staging_children(work, core_dll, source, sha, root, len(killed_names), "killed")
        _kill(killed)
        runs = dict(zip(killed_names, (run for _, run in killed)))
        marker = json.loads((runs["candidate"] / "staging-run.json").read_text(encoding="utf-8"))
        for name in MARKER_CASES:
            (runs[name] / "staging-run.json").write_bytes(_marker_variant(name, {**marker, "run_id": runs[name].name}))
        (runs["no_marker"] / "staging-run.json").unlink()  # an older run, or one not yet marked
        (runs["marker_is_folder"] / "staging-run.json").unlink()
        (runs["marker_is_folder"] / "staging-run.json").mkdir()
        outside = base / "outside"
        outside.mkdir()
        (outside / "sentinel.txt").write_bytes(b"outside, never touched")
        (runs["marker_is_junction"] / "staging-run.json").unlink()
        _junction(runs["marker_is_junction"] / "staging-run.json", outside)
        _icacls(runs["extra_permission"], "/grant", "*S-1-1-0:(R)")  # Everyone may read: not what staging creates
        # Plain unsafe entries beside the runs.
        (root / ("a" * 32)).write_bytes(b"a file with a run-id name")
        _junction(root / ("b" * 32), outside)
        for name in ("notes", "C" * 32, "{" + "d" * 32 + "}"):
            (root / name).mkdir()
        (root / ("e" * 32)).mkdir()  # run-named, with ordinary inherited permissions, and a perfect marker of its own
        (root / ("e" * 32) / "staging-run.json").write_text(json.dumps({**marker, "run_id": "e" * 32}), encoding="utf-8")
        before = _snapshot(root)
        scan = _classify(work, core_dll, **_test_policy(root))
        after = _snapshot(root)
        scan32 = _classify(work, core_dll, powershell=str(WINDOWS_POWERSHELL_32), **_test_policy(root)) if WINDOWS_POWERSHELL_32.exists() else None
        owner_policy = _classify(work, core_dll, **_test_policy(root, owner=ADMINISTRATORS))
    finally:
        _kill(live)
    after_live_ended = _classify(work, core_dll, **_test_policy(root))
    result = {"scan": scan, "scan32": scan32, "owner_policy": owner_policy, "after_live_ended": after_live_ended, "before": before,
              "after": after, "runs": {k: v.name for k, v in runs.items()}, "live": [run.name for _, run in live], "sha": sha, "outside": outside}
    _remove_links(base)
    return result


@needs_bridge
def test_runs_alive_in_other_processes_are_active(abandoned):
    entries = _by_name(abandoned["scan"]["scan"])
    for name in abandoned["live"]:
        assert (entries[name]["state"], entries[name]["reason"]) == ("Active", "InUse"), entries[name]
    # Once those processes ended (killed: nothing disposed), the same runs are candidates -- and still there.
    later = _by_name(abandoned["after_live_ended"]["scan"])
    for name in abandoned["live"]:
        assert (later[name]["state"], later[name]["reason"]) == ("AbandonedCandidate", "None"), later[name]


@needs_bridge
def test_a_run_whose_process_was_killed_is_an_abandoned_candidate(abandoned):
    entry = _by_name(abandoned["scan"]["scan"])[abandoned["runs"]["candidate"]]
    assert (entry["state"], entry["reason"]) == ("AbandonedCandidate", "None"), entry
    assert entry["run_id"] == abandoned["runs"]["candidate"] and entry["manifest_sha256"] == abandoned["sha"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", entry["created_utc"])


@needs_bridge
@pytest.mark.parametrize("name", [*MARKER_CASES, *RUN_CASES])
def test_a_killed_run_with_missing_or_bad_evidence_is_unknown(abandoned, name):
    entry = _by_name(abandoned["scan"]["scan"])[abandoned["runs"][name]]
    assert (entry["state"], entry["reason"]) == ("Unknown", {**MARKER_CASES, **RUN_CASES}[name]), entry
    assert entry["run_id"] is None and entry["manifest_sha256"] is None  # nothing taken from unverified evidence


@needs_bridge
@pytest.mark.parametrize(("name", "reason"), [("a" * 32, "NotAFolder"), ("b" * 32, "ReparsePoint"), ("notes", "NotARunName"),
                                              ("C" * 32, "NotARunName"), ("{" + "d" * 32 + "}", "NotARunName"),
                                              ("e" * 32, "UnexpectedSecurity")])
def test_unsafe_entries_beside_the_runs_are_unknown(abandoned, name, reason):
    entry = _by_name(abandoned["scan"]["scan"])[name]
    assert (entry["state"], entry["reason"]) == ("Unknown", reason), entry


@needs_bridge
def test_a_scan_changes_nothing_and_never_follows_a_link(abandoned):
    assert abandoned["after"] == abandoned["before"]
    assert (abandoned["outside"] / "sentinel.txt").read_bytes() == b"outside, never touched"
    scan = abandoned["scan"]["scan"]
    assert scan["completed"] is True and scan["root_exists"] is True and scan["root_problems"] == [] and scan["truncated"] is False
    # Only the root's direct children: nothing from inside a run, or from behind the junction, is listed.
    assert all("\\" not in e["name"] and e["name"] not in ("bundle", "staging-run.json", "sentinel.txt") for e in scan["entries"])


@needs_bridge
def test_an_owner_other_than_the_policys_is_unexpected_security(abandoned):
    # The same runs, judged by a policy whose run folders are owned by Administrators: these (owned by this user) are not.
    entry = _by_name(abandoned["owner_policy"]["scan"])[abandoned["runs"]["candidate"]]
    assert (entry["state"], entry["reason"]) == ("Unknown", "UnexpectedSecurity"), entry


@needs_bridge
def test_the_classifier_works_in_a_32_bit_process(abandoned):
    if abandoned["scan32"] is None:
        pytest.skip("no 32-bit Windows PowerShell on this machine")
    assert abandoned["scan32"]["bits"] == 32 and abandoned["scan"]["bits"] == 64

    def simplify(result: dict) -> dict:
        return {e["name"]: (e["state"], e["reason"]) for e in result["scan"]["entries"]}

    assert simplify(abandoned["scan32"]) == simplify(abandoned["scan"])


@needs_bridge
def test_a_run_alive_in_the_classifying_process_is_active_and_gone_after_dispose(tmp_path, core_dll):
    source, root = tmp_path / "source", tmp_path / "root"
    sha = _bundle(source)
    root.mkdir()
    for powershell in [_POWERSHELL] + ([str(WINDOWS_POWERSHELL_32)] if WINDOWS_POWERSHELL_32.exists() else []):
        r = _classify(tmp_path / "work", core_dll, "live_here", powershell=powershell, source=str(source), hash=sha, **_test_policy(root))
        assert r["staged"] is True, r
        during = _by_name(r["during"])
        assert list(during) == [r["run"]] and (during[r["run"]]["state"], during[r["run"]]["reason"]) == ("Active", "InUse")
        assert r["cleanup"] == [] and r["after"]["entries"] == [] and r["after"]["completed"] is True


@needs_bridge
def test_classifying_never_disturbs_a_concurrent_staging_and_never_calls_a_live_run_abandoned(tmp_path, core_dll):
    source, root = tmp_path / "source", tmp_path / "root"
    sha = _bundle(source)
    root.mkdir()
    r = _classify(tmp_path / "work", core_dll, "interference", source=str(source), hash=sha, iterations=40, **_test_policy(root))
    assert r["errors"] == [] and r["runs"] == 40, r
    assert r["stage_problems"] == [] and r["cleanup_problems"] == [], r  # no staging failed, and every Dispose cleaned up
    assert r["violations"] == [], r  # a live run was never an AbandonedCandidate
    assert r["scans"] > 0 and r["incomplete"] == 0, r  # two concurrent classifiers, every scan completed
    assert set(r["states"]) <= {"Active/InUse", "Unknown/MarkerMissing", "Unknown/Vanished"}, r["states"]
    assert r["after"]["entries"] == []


@needs_bridge
def test_an_unsafe_missing_or_invalid_root_is_refused_without_enumerating(tmp_path, core_dll):
    work = tmp_path / "work"
    writable = tmp_path / "writable"
    (writable / ("0" * 32)).mkdir(parents=True)
    _icacls(writable, "/grant", "*S-1-1-0:(OI)(CI)(W)")  # Everyone may write: a fixture folder only
    try:
        refused = _classify(work, core_dll, **_test_policy(writable))["scan"]
    finally:
        _icacls(writable, "/remove:g", "*S-1-1-0")
    assert refused["root_problems"] == ["WritableByUntrusted"] and refused["entries"] == [] and refused["completed"] is False
    (tmp_path / "real").mkdir()
    _junction(tmp_path / "junction", tmp_path / "real")
    linked = _classify(work, core_dll, **_test_policy(tmp_path / "junction"))["scan"]
    os.rmdir(tmp_path / "junction")
    assert linked["root_problems"] == ["ReparsePoint"] and linked["entries"] == []
    missing = _classify(work, core_dll, **_test_policy(tmp_path / "missing" / "SetupStaging"))["scan"]
    assert (missing["completed"], missing["root_exists"], missing["root_problems"], missing["entries"]) == (True, False, [], [])
    assert not (tmp_path / "missing").exists()  # nothing created
    invalid = _classify(work, core_dll, **_test_policy(tmp_path, access=[SYSTEM, ADMINISTRATORS, "ME", "S-1-1-0"]))["scan"]
    assert invalid["root_problems"] == ["RequestInvalid"] and invalid["entries"] == []


@needs_bridge
def test_cancellation_and_the_entry_limit(tmp_path, core_dll):
    root = tmp_path / "root"
    root.mkdir()
    for i in range(1025):  # one more than a scan classifies
        (root / f"entry-{i:04d}").mkdir()
    work = tmp_path / "work"
    limited = _classify(work, core_dll, **_test_policy(root))["scan"]
    assert limited["truncated"] is True and limited["completed"] is True and len(limited["entries"]) == 1024
    assert {(e["state"], e["reason"]) for e in limited["entries"]} == {("Unknown", "NotARunName")}
    cancelled = _classify(work, core_dll, "cancelled", **_test_policy(root))["scan"]
    assert cancelled["completed"] is False and cancelled["root_problems"] == ["Cancelled"] and cancelled["entries"] == []
