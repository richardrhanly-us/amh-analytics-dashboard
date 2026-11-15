"""Tests for persistent setup diagnostics, I1b: installer/SortView.Setup.Wizard/SetupDiagnosticsLog.cs and its use by the wizard.

Each setup run is written, as it goes, to C:\\ProgramData\\SortViewSetup\\Logs (apart from the Collector's data and from
installation staging), in folders created for SYSTEM and Administrators only. At most 20 logs are kept, each at most 5 MB.
Only what the wizard hands the log is written (the engine's allow-listed events, its console text, which question was asked
and how it was answered -- yes, no or just "answered" -- and the result), and every line is redacted again on the way in.
Copy details names the log file (never its folder); Save a copy saves it only where the person chooses, after a warning. A
log that cannot be created, written or pruned never stops or changes setup.

  1. STATIC checks (run everywhere): the production location and principals, the hosts' wiring, what the wizard may hand
     the log, that nothing about the log can stop a run, and that Save a copy never saves on its own.
  2. BEHAVIOR (Windows only): the built Wizard library in Windows PowerShell, under a TEST policy rooted in pytest's
     temporary folder that also trusts the current user (no elevation needed). The production policy is only read: nothing
     is ever created under C:\\ProgramData.
  3. UI scenarios (Windows only): the simulation window (SortView.Setup.Spike) writes its log inside its own scratch machine;
     Copy details, Save a copy and an unusable log folder are driven through UI Automation.

Not automated here (deferred real-Windows qualification): the production program, elevated, creating C:\\ProgramData\\
SortViewSetup\\Logs owned by Administrators; a folder pre-created there by a standard user being refused; and a real
installation that ends by power loss rather than by the process being ended.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from test_installer_bridge import _MSBUILD, _POWERSHELL, needs_bridge
from test_installer_window import (
    DUMMY_CODE,
    _answer_code,
    assert_window_invariants,
    drive,
    snapshot,
    window_exe,  # noqa: F401  (the built simulation window, a fixture)
    workspace_tools,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "installer"
WIZARD_DIR = INSTALLER / "SortView.Setup.Wizard"
WIZARD_PROJECT = WIZARD_DIR / "SortView.Setup.Wizard.csproj"
LOG_CODE = WIZARD_DIR / "SetupDiagnosticsLog.cs"
SAFE_FILE_CODE = WIZARD_DIR / "SafeNewFile.cs"
WIZARD_CODE = WIZARD_DIR / "SetupWizardView.xaml.cs"
WIZARD_XAML = WIZARD_DIR / "SetupWizardView.xaml"
GUIDANCE_CODE = WIZARD_DIR / "StopGuidance.cs"
PRODUCTION_WINDOW = INSTALLER / "SortView.Setup" / "MainWindow.xaml.cs"
SPIKE_WINDOW = INSTALLER / "SortView.Setup.Spike" / "MainWindow.xaml.cs"
CORE_DIR = INSTALLER / "SortView.Setup.Core"

LOG_NAME = re.compile(r"^setup-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}\.log$")
SYSTEM, ADMINISTRATORS = "S-1-5-18", "S-1-5-32-544"
FULL_CONTROL = 0x1F01FF
MAX_BYTES = 5 * 1024 * 1024
# Synthetic secrets in the shapes the real ones take -- never real credentials.
FAKE_CODE = "SV-ABCD-EFGH-JKMN-PQRS"
FAKE_TOKEN = "Zx9" + "aB3dE5fG7hJ9kL2mN4pQ6rS8tU0vW1yZ3bC5dF7"  # 43 URL-safe characters, as secrets.token_urlsafe(32) gives


def _code(path: Path) -> str:
    return "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.strip().startswith("//"))


def _flat(path: Path) -> str:
    return re.sub(r"\s+", " ", _code(path))


# =====================================================================================================================
# 1. STATIC checks
# =====================================================================================================================

def test_the_production_log_folder_is_its_own_and_only_for_system_and_administrators():
    code = _flat(LOG_CODE)
    assert 'internal const string ProductionRoot = @"C:\\ProgramData\\SortViewSetup";' in code
    assert 'internal const string ProductionLogFolder = @"C:\\ProgramData\\SortViewSetup\\Logs";' in code
    production = code[code.index("public static SetupLogPolicy Production =>"):code.index("public string Root { get; }")]
    assert ("new SetupLogPolicy(ProductionRoot, ProductionLogFolder, new[] { SystemSid, AdministratorsSid, TrustedInstallerSid }, "
            "new[] { SystemSid, AdministratorsSid }, AdministratorsSid, true);") in production
    # Apart from the Collector's data and from installation staging.
    runner = _code(INSTALLER / "SortView.Setup" / "InstallationRunner.cs")
    assert 'DataRoot = @"C:\\ProgramData\\SortViewCollector";' in runner
    assert '"SortView", "SetupStaging"' in _code(CORE_DIR / "BundleStager.cs")
    assert "SortViewCollector" not in code and "SetupStaging" not in code
    # The limits.
    assert "public const int MaxLogs = 20;" in code and "public const long MaxBytes = 5L * 1024 * 1024;" in code


def test_the_injected_fault_is_internal_and_never_honored_by_the_production_policy():
    code = _flat(LOG_CODE)
    assert "internal enum SetupLogFault" in code and "internal SetupLogFault InjectedFault" in code
    assert "public SetupLogFault" not in code and "public enum SetupLogFault" not in code
    assert ": this(root, logFolder, trustedPrincipals, accessPrincipals, owner, false)" in code
    assert "get => isProduction ? SetupLogFault.None : injectedFault;" in code
    assert "if (isProduction) { throw new InvalidOperationException(" in code
    # The Save a copy test hook likewise: internal, never set on (and refused by) the production policy.
    assert "internal Action BeforeCopyCreated" in code and "get => isProduction ? null : beforeCopyCreated;" in code
    assert code.count("if (isProduction) { throw new InvalidOperationException(") == 2


def test_each_host_chooses_its_log_and_production_only_ever_uses_the_production_policy():
    production = _flat(PRODUCTION_WINDOW)
    assert production.count("DiagnosticsLogFactory") == 1
    assert ("Wizard.DiagnosticsLogFactory = () => SetupDiagnosticsLog.Open(SetupLogPolicy.Production, "
            '"Collector release (pinned): " + (pin.IsConfigured ? pin.Version : "none"));') in production
    assert "new SetupLogPolicy(" not in production
    # The simulation window keeps its log inside its own scratch machine, never in ProgramData.
    spike = _flat(SPIKE_WINDOW)
    assert "Wizard.DiagnosticsLogFactory = OpenSimulationLog;" in spike
    assert 'string root = Path.Combine(workspace.Root, "SortViewSetup");' in spike
    assert "SetupLogPolicy.Production" not in spike and "ProgramData" not in spike
    # The wizard itself never chooses a folder.
    wizard = _code(WIZARD_CODE)
    assert "SetupLogPolicy" not in wizard and "ProgramData" not in wizard


def test_the_log_never_sees_anything_typed_and_runs_nothing():
    code = _code(LOG_CODE)
    for forbidden in ("SecureString", "PasswordBox", "CodeBox", "PathBox", "Clipboard", "Marshal", "System.Diagnostics", "Process",
                      "System.Net", "Registry", "UserName", "MachineName", "UserDomainName", "GetEnvironmentVariable", "Console."):
        assert forbidden not in code, forbidden
    # Core's own rule stands: the engine bridge never writes a file. The log lives in the Wizard library.
    assert not (CORE_DIR / "SetupDiagnosticsLog.cs").exists()
    assert '<Compile Include="SetupDiagnosticsLog.cs" />' in WIZARD_PROJECT.read_text(encoding="utf-8")


def test_the_log_creates_only_new_files_and_deletes_only_old_setup_logs():
    code = _code(LOG_CODE)
    # New files only: the log itself (here) and a saved copy (SafeNewFile). Never an existing file, never appended to.
    assert code.count("FileMode.CreateNew") == 1
    assert "SafeNewFile.Create(destination, policy.BeforeCopyCreated, out SaveCopyResult outcome);" in code
    for forbidden in ("FileMode.Create,", "FileMode.OpenOrCreate", "FileMode.Append", "FileMode.Truncate", "File.Copy", "File.Move",
                      "Directory.Delete", "File.Delete", "SearchOption.AllDirectories", "File.WriteAll", "File.AppendAll"):
        assert forbidden not in code, forbidden
    # One deletion: an old setup log, named exactly as setup names them, a plain file, beyond the newest 19.
    assert code.count(".Delete()") == 1 and "old.Delete();" in code
    retention = code[code.index("private void RemoveOldLogs"):code.index("private void CreateFile")]
    assert "FileNamePattern.IsMatch(file.Name)" in retention
    assert "(file.Attributes & (FileAttributes.ReparsePoint | FileAttributes.Directory)) == 0" in retention
    assert ".Skip(SetupLogPolicy.MaxLogs - 1)" in retention
    assert r'new Regex(@"^setup-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}\.log$"' in code


def test_nothing_the_log_does_can_throw_to_setup():
    code = _code(LOG_CODE)
    opening = code[code.index("public static SetupDiagnosticsLog Open("):code.index("private void Start(string[] hostLines)")]
    assert "log.Start(hostLines ?? new string[0]);" in opening and "catch (Exception)" in opening
    write = code[code.index("private void Write(string kind, string text)"):code.index("private static string Clean")]
    assert "catch (Exception)" in write and "Problem = SetupLogProblem.WriteFailed;" in write
    save = code[code.index("public SaveCopyResult SaveCopyTo"):code.index("private void CloseStream")]
    assert "catch (Exception)" in save and "return SaveCopyResult.Failed;" in save
    safe = _code(SAFE_FILE_CODE)
    create = safe[safe.index("internal static FileStream Create("):safe.index("internal static void RemoveOnClose")]
    assert "catch (Exception)" in create and "outcome = SaveCopyResult.Failed;" in create
    # Redaction is bounded: every pattern it uses gives up in time, and a line it cannot check is never written.
    redaction = code[code.index("private const RegexOptions Options"):code.index("public static string Redact")]
    patterns = re.findall(r"new Regex\((.*?)\);", redaction, flags=re.DOTALL)
    assert len(patterns) == 7 and all(p.rstrip().endswith("MatchTimeout") for p in patterns)
    safe = code[code.index("private static string SafeLine"):code.index("private static string Clean")]
    assert "catch (RegexMatchTimeoutException)" in safe and "Redact(line)" in safe
    assert "Clean(SafeLine(line))" in write
    # Every public entry point that writes goes through Write.
    for method in ("public void Note(string text) => Write(", "public void ConsoleText(string text) => Write("):
        assert method in code, method
    wizard = _flat(WIZARD_CODE)
    # The host's factory is called inside a catch, and a run starts the same way whatever it does.
    opener = wizard[wizard.index("private SetupDiagnosticsLog OpenRunLog()"):wizard.index("private void OnLoaded(")]
    assert "try { return DiagnosticsLogFactory?.Invoke() ?? SetupDiagnosticsLog.NotSaved(SetupLogProblem.NotConfigured); }" in opener
    assert "catch (Exception) { return SetupDiagnosticsLog.NotSaved(SetupLogProblem.UnexpectedError); }" in opener
    begin = wizard[wizard.index("public void BeginRun("):wizard.index("private SetupDiagnosticsLog OpenRunLog()")]
    after_open = begin[begin.index("runLog = OpenRunLog();"):]
    assert "return" not in after_open and "throw" not in after_open
    assert after_open.index("runLog = OpenRunLog();") < after_open.index("session = SetupRunSession.Start(")


def test_the_wizard_hands_the_log_only_approved_text():
    wizard = _code(WIZARD_CODE)
    calls = re.findall(r"\b(?:runLog\?|runLog|log)\.(Note|Event|ConsoleText)\(([^;]*)\);", wizard)
    assert sorted(calls) == sorted([
        ("ConsoleText", "text"),
        ("Event", "Format(setupEvent, true).ToList()"),
        ("Note", '"Setup run started."'),
        ("Note", '"Cancel setup selected: " + outcome'),
        ("Note", '"Window close requested: it closes when setup has returned."'),
        ("Note", '"Setup could not run: " + error.GetType().Name + " (message not logged)"'),
        ("Note", "StatusText.Text"),
        ("Note", '"Engine error records: " + result.Diagnostics.Count'),
        ("Note", '"What to do now: " + guidance.CategoryLabel'),
        ("Note", '"Setup run ended."'),
        ("Note", '"Question asked: " + LoggedQuestion(request)'),
        ("Note", '"Question cancelled (" + LoggedQuestion(current) + ")"'),
        ("Note", '"Answer given (" + LoggedQuestion(request) + "): " + said'),
    ])
    # An answer is named only when it is yes or no; a path or a code is only "answered".
    assert ('string said = answer is string text && (text == PromptAnswers.Yes || text == PromptAnswers.No) ? text : "answered";'
            in wizard)
    # The console text and the events written are what Show details shows -- except an unexpected error's free-text
    # message, which is never written (its exception type is), as for a run that could not start at all.
    assert re.search(r"consoleText\.Add\(text\);\s*log\.ConsoleText\(text\);", wizard)
    assert re.search(r"foreach \(string line in Format\(setupEvent, false\)\)\s*\{\s*events\.Add\(line\);\s*\}\s*"
                     r"(?://[^\n]*\s*)?runLog\?\.Event\(Format\(setupEvent, true\)\.ToList\(\)\);", wizard)
    formatting = wizard[wizard.index("private static IEnumerable<string> Format(SetupEvent e, bool forLog)"):]
    unexpected = formatting[formatting.index('case "run_failed_unexpected":'):formatting.index('case "run_finished":')]
    assert '(forLog ? "(message not logged)" : Field(e, "message"))' in unexpected
    assert wizard.count('Field(e, "message")') == 1
    assert wizard.count("error.Message") == 1 and "runLog?.Note(StatusText.Text);" in wizard  # the finished status only
    # The engine's error records are counted, never written.
    assert "Diagnostics)" not in wizard and "result.Diagnostics.Count" in wizard


def test_save_a_copy_asks_for_a_place_warns_and_never_saves_on_its_own():
    wizard = _code(WIZARD_CODE)
    save = wizard[wizard.index("private void SaveLogButton_Click"):]
    assert wizard.count("new SaveFileDialog") == 1 and "new SaveFileDialog" in save
    assert wizard.count("SaveCopyTo(") == 1
    # The copy is written only after the person chose a place in the dialog.
    assert save.index("if (dialog.ShowDialog(Window.GetWindow(this)) != true)") < save.index("log.SaveCopyTo(dialog.FileName)")
    assert "OverwritePrompt = false" in save and "InitialDirectory = Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments)" in save
    assert "sensitive system information" in save  # the dialog's title says it too
    assert "Desktop" not in wizard and "Desktop" not in _code(LOG_CODE)
    # The warning is on screen beside the button, and is its help text.
    xaml = WIZARD_XAML.read_text(encoding="utf-8")
    panel = xaml[xaml.index('x:Name="CopyDetailsPanel"'):xaml.index("</GroupBox>", xaml.index('x:Name="CopyDetailsPanel"'))]
    assert 'x:Name="SaveLogWarning"' in panel and 'Text="{x:Static local:SetupWizardView.SaveLogWarningText}"' in panel
    assert 'x:Name="SaveLogButton"' in panel and 'AutomationProperties.HelpText="{x:Static local:SetupWizardView.SaveLogWarningText}"' in panel
    warning = re.search(r"public const string SaveLogWarningText =(.*?);", wizard, flags=re.DOTALL).group(1)
    warning = "".join(re.findall(r'"([^"]*)"', warning))
    assert "sensitive system information" in warning and "never holds the enrollment code" in warning
    # Enabled only once a run has ended with a saved log.
    assert "SaveLogButton.IsEnabled = !running && runLog != null && runLog.WasCreated;" in wizard


def test_save_a_copy_is_created_through_checked_folder_handles_and_never_follows_a_link():
    safe = _flat(SAFE_FILE_CODE)
    # Every open is by handle, relative to the folder above, and opens a reparse point itself rather than following it.
    folder = safe[safe.index("private static readonly Request Folder"):safe.index("private static readonly Request NewFile")]
    new_file = safe[safe.index("private static readonly Request NewFile"):safe.index("private static SafeFileHandle Open(")]
    assert "Disposition = FileOpen," in folder and "FileDirectoryFile" in folder and "FileOpenReparsePoint" in folder
    assert "Disposition = FileCreate," in new_file and "FileNonDirectoryFile" in new_file and "FileOpenReparsePoint" in new_file
    assert "Share = 0," in new_file
    create = safe[safe.index("internal static FileStream Create("):safe.index("internal static void RemoveOnClose")]
    assert 'Open(null, @"\\??\\" + parts[0] + @"\\", Folder, out int status);' in create
    assert "Open(held[held.Count - 1], parts[i], Folder, out status);" in create
    assert "if (IsReparsePoint(folder)) { outcome = SaveCopyResult.UnsafeLocation; return null; }" in create
    # The file is created only after every folder was opened and checked, relative to the last folder's handle.
    assert create.index("IsReparsePoint(folder)") < create.index("afterFoldersChecked?.Invoke();") < create.index(
        "Open(held[held.Count - 1], parts[parts.Length - 1], NewFile, out status);")
    assert "RootDirectory = parent?.DangerousGetHandle() ?? IntPtr.Zero," in safe
    # A reparse point that cannot be checked is unsafe; only fixed and removable local drives are accepted.
    assert "return true; // cannot tell: treated as unsafe" in safe
    assert "if (type != DriveType.Fixed && type != DriveType.Removable) { return null; }" in safe
    # Nothing is ever opened, written or removed by path again (a partial copy is removed by its handle).
    for forbidden in ("new FileStream(path", "new FileStream(destination", "File.", "Directory.", "CreateFileW", "DeleteFileW", "MoveFile"):
        assert forbidden not in safe.replace("SafeFileHandle", "").replace("FileStream(file,", ""), forbidden
    assert "SetFileInformationByHandle(stream.SafeFileHandle, FileDispositionInfo, ref delete, 1);" in safe
    # What the person is told: fixed text for each outcome, never the path or an error's words.
    wizard = _flat(WIZARD_CODE)
    message = wizard[wizard.index("internal static string SaveCopyMessage"):]
    message = message[:message.index("} }") + 3]
    assert "dialog" not in message and "Exception" not in message and "+ result" not in message
    for case in ("Saved", "AlreadyExists", "UnsafeLocation", "NoLog"):
        assert f"case SaveCopyResult.{case}:" in message
    assert "SaveLogStatus.Text = SaveCopyMessage(log.SaveCopyTo(dialog.FileName));" in wizard


def test_copy_details_names_the_log_file_only():
    guidance = _code(GUIDANCE_CODE)
    log_line = guidance[guidance.index("private static string LogLine"):guidance.index("private static string LogProblemLabel")]
    assert "SetupDiagnosticsLog.FileNamePattern.IsMatch(name)" in log_line
    assert "FilePath" not in guidance and "LogFolder" not in guidance and "Root" not in guidance
    assert 'text.AppendLine("Setup log: " + LogLine(log));' in guidance


# =====================================================================================================================
# 2. BEHAVIOR: the built library in Windows PowerShell
# =====================================================================================================================

@pytest.fixture(scope="module")
def wizard_dll() -> Path:
    completed = subprocess.run([_MSBUILD, str(WIZARD_PROJECT), "/p:Configuration=Release", "/nologo", "/v:minimal"],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return WIZARD_DIR / "bin" / "Release" / "SortView.Setup.Wizard.dll"


_PRELUDE = r"""
param([string]$WizardPath, [string]$OutPath, [string]$Root)
$ErrorActionPreference = 'Stop'
Add-Type -Path $WizardPath
$LogType = [SortView.Setup.Wizard.SetupDiagnosticsLog]
$PolicyType = [SortView.Setup.Wizard.SetupLogPolicy]
$NonPublic = [Reflection.BindingFlags]'NonPublic,Instance'
$user = [Security.Principal.WindowsIdentity]::GetCurrent().User
$system = New-Object Security.Principal.SecurityIdentifier('S-1-5-18')
$admins = New-Object Security.Principal.SecurityIdentifier('S-1-5-32-544')
$ti = New-Object Security.Principal.SecurityIdentifier('S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
function Policy([string]$root) {
    New-Item -ItemType Directory -Force -Path (Split-Path $root) | Out-Null  # the folder that holds the root exists (as ProgramData does in production)
    New-Object SortView.Setup.Wizard.SetupLogPolicy($root, (Join-Path $root 'Logs'),
        [Security.Principal.SecurityIdentifier[]]@($user, $system, $admins, $ti), [Security.Principal.SecurityIdentifier[]]@($user, $system, $admins), $null)
}
function Fault($policy, [string]$name) {
    $property = $PolicyType.GetProperty('InjectedFault', $NonPublic)
    $property.SetValue($policy, [Enum]::Parse($property.PropertyType, $name))
}
function Open($policy) { $LogType::Open($policy, [string[]]@('Collector release (test): 0.0.0')) }
function Describe($log) {
    [ordered]@{ file = $log.FileName; created = $log.WasCreated; problem = [string]$log.Problem; truncated = $log.IsTruncated
                removed = $log.OldLogsRemoved; not_removed = $log.OldLogsNotRemoved; path = $LogType.GetProperty('FilePath', $NonPublic).GetValue($log) }
}
function Acl([string]$path) {
    $acl = Get-Acl -LiteralPath $path
    [ordered]@{ owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value; protected = $acl.AreAccessRulesProtected
                rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | ForEach-Object {
                    [ordered]@{ sid = $_.IdentityReference.Value; rights = [int]$_.FileSystemRights; allow = ($_.AccessControlType -eq 'Allow')
                                inherited = $_.IsInherited; inheritance = [int]$_.InheritanceFlags } }) }
}
function Listing([string]$path) { return ,@(Get-ChildItem -LiteralPath $path -Force | ForEach-Object { $_.Name } | Sort-Object) }
$out = [ordered]@{ error = $null; user = $user.Value }
try {
@@BODY@@
} catch {
    $out.error = $_.Exception.ToString() + ' at ' + $_.InvocationInfo.PositionMessage
}
ConvertTo-Json -InputObject $out -Depth 8 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


def run_ps(wizard_dll: Path, tmp_path: Path, body: str, root: Path | None = None) -> dict:
    root = root or tmp_path / "machine"
    root.mkdir(parents=True, exist_ok=True)
    script, out = tmp_path / "probe.ps1", tmp_path / "probe_out.json"
    script.write_text(_PRELUDE.replace("@@BODY@@", body), encoding="utf-8")
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                "-WizardPath", str(wizard_dll), "-OutPath", str(out), "-Root", str(root)],
                               capture_output=True, text=True, timeout=600, check=False)
    assert out.exists(), completed.stdout + completed.stderr
    result = json.loads(out.read_text(encoding="utf-8-sig"))
    assert result["error"] is None, result["error"]
    return result


def _assert_protected(acl: dict, user: str) -> None:
    assert acl["protected"] is True and acl["owner"] == user  # the test policy leaves the creator as owner
    assert {rule["sid"] for rule in acl["rules"]} == {user, SYSTEM, ADMINISTRATORS}
    for rule in acl["rules"]:
        assert rule["allow"] and not rule["inherited"] and rule["rights"] & FULL_CONTROL == FULL_CONTROL, rule


@needs_bridge
def test_a_new_log_folder_is_created_protected_and_the_log_holds_the_header(tmp_path, wizard_dll):
    result = run_ps(wizard_dll, tmp_path, r"""
    $top = Join-Path $Root 'SortViewSetup'
    $log = Open (Policy $top)
    $log.Note('Setup run started.')
    $log.Event([string[]]@('#1  > machine', '#2  ok machine (ok)'))
    $log.ConsoleText("line one`r`nline two")
    $log.Dispose()
    $log.Note('after close: never written')
    $out.log = Describe $log
    $out.root_acl = Acl $top
    $out.logs_acl = Acl (Join-Path $top 'Logs')
    $out.file_acl = Acl $out.log.path
    $out.listing = Listing (Join-Path $top 'Logs')
    """)
    log = result["log"]
    assert log["created"] is True and log["problem"] == "None" and log["truncated"] is False
    assert LOG_NAME.match(log["file"]) and result["listing"] == [log["file"]]
    _assert_protected(result["root_acl"], result["user"])
    _assert_protected(result["logs_acl"], result["user"])
    # The log file inherits only the protected folder's entries.
    assert {rule["sid"] for rule in result["file_acl"]["rules"]} == {result["user"], SYSTEM, ADMINISTRATORS}
    text = Path(log["path"]).read_text(encoding="utf-8")
    lines = text.splitlines()
    assert all(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z  (setup|event|console) ", line) for line in lines), lines
    assert "SortView Setup diagnostic log (sortview.setup-log/1)" in lines[0]
    assert f"Log file: {log['file']}" in text and "Windows version: " in text and "Collector release (test): 0.0.0" in text
    assert "This log never records enrollment codes, API tokens, passwords or anything typed." in text
    assert "setup   Setup run started." in text and "event   #2  ok machine (ok)" in text
    assert "console line one" in text and "console line two" in text  # each line of text is its own entry
    assert "never written" not in text
    for private in (os.environ.get("USERNAME", "?"), os.environ.get("COMPUTERNAME", "?")):
        assert private.lower() not in lines[0].lower() and f"\\{private}" not in text.split("Collector release")[0]


@needs_bridge
def test_existing_folders_are_reused_only_when_exactly_as_setup_makes_them(tmp_path, wizard_dll):
    result = run_ps(wizard_dll, tmp_path, r"""
    $out.cases = [ordered]@{}
    # Reused: made by setup earlier.
    $top = Join-Path $Root 'reused\SortViewSetup'
    (Open (Policy $top)).Dispose(); $second = Open (Policy $top); $second.Dispose()
    $out.cases.reused = [ordered]@{ log = Describe $second; listing = Listing (Join-Path $top 'Logs') }

    # Someone else may read: an extra access entry.
    $top = Join-Path $Root 'extra\SortViewSetup'
    (Open (Policy $top)).Dispose()
    $dir = Get-Item -LiteralPath $top; $acl = $dir.GetAccessControl([Security.AccessControl.AccessControlSections]::Access)
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier('S-1-1-0')), 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    $dir.SetAccessControl($acl)  # the access entries only (writing the audit section needs a privilege)
    $before = Listing (Join-Path $top 'Logs')
    $out.cases.extra = [ordered]@{ log = Describe (Open (Policy $top)); before = $before; after = Listing (Join-Path $top 'Logs') }

    # Not protected: a folder made the ordinary way, inheriting its parent's entries.
    $top = Join-Path $Root 'inherited\SortViewSetup'
    New-Item -ItemType Directory -Path $top | Out-Null
    $out.cases.inherited = [ordered]@{ log = Describe (Open (Policy $top)); listing = Listing $top }

    # Logs exists but is a plain file.
    $top = Join-Path $Root 'file\SortViewSetup'
    (Open (Policy $top)).Dispose()
    Remove-Item -LiteralPath (Join-Path $top 'Logs') -Recurse -Force
    Set-Content -LiteralPath (Join-Path $top 'Logs') -Value 'x'
    $out.cases.file = [ordered]@{ log = Describe (Open (Policy $top)) }

    # A junction (reparse point) as the root, and as Logs: never followed, nothing written at the target.
    $target = Join-Path $Root 'junction-target'
    New-Item -ItemType Directory -Path $target | Out-Null
    $top = Join-Path $Root 'junction\SortViewSetup'
    New-Item -ItemType Directory -Force -Path (Split-Path $top) | Out-Null
    cmd /c mklink /J "$top" "$target" | Out-Null
    $out.cases.junction_root = [ordered]@{ log = Describe (Open (Policy $top)); target = Listing $target }
    $top = Join-Path $Root 'junction2\SortViewSetup'
    (Open (Policy $top)).Dispose()
    $logs = Join-Path $top 'Logs'
    Remove-Item -LiteralPath $logs -Recurse -Force
    cmd /c mklink /J "$logs" "$target" | Out-Null
    $out.cases.junction_logs = [ordered]@{ log = Describe (Open (Policy $top)); target = Listing $target }
    """)
    cases = result["cases"]
    reused = cases["reused"]
    assert reused["log"]["created"] is True and len(reused["listing"]) == 2
    for name in ("extra", "inherited", "file", "junction_root", "junction_logs"):
        assert cases[name]["log"]["created"] is False and cases[name]["log"]["problem"] == "FolderUntrusted", name
    assert cases["extra"]["before"] == cases["extra"]["after"]  # nothing added to a folder it does not trust
    assert cases["inherited"]["listing"] == []  # no Logs folder made below it
    assert cases["junction_root"]["target"] == [] and cases["junction_logs"]["target"] == []


@needs_bridge
def test_folders_above_the_log_root_may_allow_creating_but_not_deleting_or_repermissioning(tmp_path, wizard_dll):
    result = run_ps(wizard_dll, tmp_path, r"""
    function Grant([string]$path, [string]$rights) {
        $acl = Get-Acl -LiteralPath $path
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier('S-1-5-32-545')), $rights, 'None', 'None', 'Allow')))
        Set-Acl -LiteralPath $path -AclObject $acl
    }
    $out.cases = [ordered]@{}
    # As C:\ProgramData lets BUILTIN\Users create files and folders: tolerated.
    foreach ($case in @(@('create_only', 'Write, ReadAndExecute'), @('delete', 'Delete'), @('delete_children', 'DeleteSubdirectoriesAndFiles'),
                        @('change_permissions', 'ChangePermissions'), @('take_ownership', 'TakeOwnership'), @('full_control', 'FullControl'))) {
        $parent = Join-Path $Root $case[0]
        New-Item -ItemType Directory -Path $parent | Out-Null
        Grant $parent $case[1]
        $log = Open (Policy (Join-Path $parent 'SortViewSetup')); $log.Dispose()
        $out.cases[$case[0]] = [ordered]@{ log = Describe $log; listing = Listing $parent }
    }
    """)
    cases = result["cases"]
    assert cases["create_only"]["log"]["created"] is True
    for name in ("delete", "delete_children", "change_permissions", "take_ownership", "full_control"):
        assert cases[name]["log"]["created"] is False and cases[name]["log"]["problem"] == "FolderUntrusted", name
        assert cases[name]["listing"] == [], name  # nothing created below an unsafe folder


@needs_bridge
def test_at_most_twenty_logs_are_kept_and_nothing_else_is_touched(tmp_path, wizard_dll):
    result = run_ps(wizard_dll, tmp_path, r"""
    $top = Join-Path $Root 'SortViewSetup'
    $logs = Join-Path $top 'Logs'
    (Open (Policy $top)).Dispose()
    Get-ChildItem -LiteralPath $logs | Remove-Item -Force
    for ($i = 1; $i -le 25; $i++) { Set-Content -LiteralPath (Join-Path $logs ('setup-200001{0:D2}-000000-0000000a.log' -f $i)) -Value "old $i" }
    foreach ($other in 'notes.txt', 'setup-1.log', 'SETUP-20000101-000000-0000000A.log.bak', 'setup-20000101-000000-0000000g.log') {
        Set-Content -LiteralPath (Join-Path $logs $other) -Value 'keep'
    }
    New-Item -ItemType Directory -Path (Join-Path $logs 'setup-19990101-000000-0000000b.log') | Out-Null  # a folder named like a log
    $log = Open (Policy $top); $log.Dispose()
    $out.first = [ordered]@{ log = Describe $log; listing = Listing $logs }
    # The next run: still twenty.
    $log = Open (Policy $top); $log.Dispose()
    $out.second = [ordered]@{ log = Describe $log; listing = Listing $logs }
    # A log that cannot be removed: counted and noted, never fatal.
    $policy = Policy $top; Fault $policy 'Retention'
    $log = Open $policy; $log.Dispose()
    $out.fault = [ordered]@{ log = Describe $log; listing = Listing $logs }
    """)
    others = {"notes.txt", "setup-1.log", "SETUP-20000101-000000-0000000A.log.bak", "setup-20000101-000000-0000000g.log",
              "setup-19990101-000000-0000000b.log"}
    folder_named_like_a_log = "setup-19990101-000000-0000000b.log"

    def setup_logs(listing: list[str]) -> list[str]:
        return [name for name in listing if LOG_NAME.match(name) and name != folder_named_like_a_log]

    first = result["first"]
    logs = setup_logs(first["listing"])
    assert len(logs) == 20 and first["log"]["removed"] == 6 and first["log"]["not_removed"] == 0
    assert set(first["listing"]) - set(logs) == others  # nothing else touched
    # The oldest went: what is left is the newest 19 old logs and the new one.
    assert sorted(logs)[:19] == [f"setup-200001{i:02d}-000000-0000000a.log" for i in range(7, 26)]
    assert first["log"]["file"] in logs
    second = result["second"]
    assert len(setup_logs(second["listing"])) == 20 and second["log"]["removed"] == 1
    assert set(second["listing"]) - set(setup_logs(second["listing"])) == others
    fault = result["fault"]
    assert fault["log"]["created"] is True and fault["log"]["problem"] == "None"
    assert fault["log"]["removed"] == 0 and fault["log"]["not_removed"] == 1
    assert len(setup_logs(fault["listing"])) == 21  # one over, until the next run can remove it
    assert "Old setup logs removed: 0; could not be removed: 1" in Path(fault["log"]["path"]).read_text(encoding="utf-8")


@needs_bridge
def test_a_log_stops_at_five_megabytes_with_a_marker(tmp_path, wizard_dll):
    result = run_ps(wizard_dll, tmp_path, r"""
    $log = Open (Policy (Join-Path $Root 'SortViewSetup'))
    $line = 'x' * 1000
    for ($i = 0; $i -lt 6000 -and -not $log.IsTruncated; $i++) { $log.ConsoleText("$i $line") }
    $out.lines_offered = $i
    $log.ConsoleText('after the limit: never written')
    $log.Note('after the limit: never written either')
    $out.log = Describe $log
    $log.Dispose()
    """)
    log = result["log"]
    assert log["truncated"] is True and log["problem"] == "None" and log["created"] is True
    data = Path(log["path"]).read_bytes()
    assert MAX_BYTES - 2048 < len(data) <= MAX_BYTES
    last = data.decode("utf-8").splitlines()[-1]
    assert last.endswith("[log truncated: the 5 MB limit was reached; later entries were not saved]")
    assert b"never written" not in data


@needs_bridge
def test_lines_are_cleaned_and_shortened(tmp_path, wizard_dll):
    result = run_ps(wizard_dll, tmp_path, r"""
    $log = Open (Policy (Join-Path $Root 'SortViewSetup'))
    $log.ConsoleText("bell$([char]7) and escape$([char]27)[31m here")
    $log.ConsoleText("fake`r2099-01-01T00:00:00.000Z  setup   forged entry")
    $log.ConsoleText('y ' * 10000)
    # A token cut by the 8000-character limit: no piece of it is left (too short to be recognised as a token).
    $log.ConsoleText(('a ' * 3995) + '@@TOKEN@@ tail')
    # A line built to make a pattern work hard: written, or (if it cannot be checked in time) replaced by a notice.
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $log.ConsoleText(('a-' * 3500) + ' end')
    $out.hard_ms = $watch.ElapsedMilliseconds
    $out.log = Describe $log
    $log.Dispose()
    """.replace("@@TOKEN@@", FAKE_TOKEN))
    content = Path(result["log"]["path"]).read_text(encoding="utf-8")
    lines = content.splitlines()
    assert any(line.endswith("console bell? and escape?[31m here") for line in lines)
    # A carriage return starts a new entry, with its own prefix: a forged entry is still marked as console text.
    assert any(line.endswith("console 2099-01-01T00:00:00.000Z  setup   forged entry") for line in lines)
    long = next(line for line in lines if "y y y" in line)
    assert long.endswith(" [line shortened]") and long.count("y") == 4000
    assert FAKE_TOKEN[:8] not in content and "tail" not in content
    assert any(line.endswith(("a-a- end", "[line not written: it could not be checked for secrets in time]")) for line in lines)
    assert result["hard_ms"] < 3000  # bounded: every pattern gives up after 250 ms


#: (input, what must be gone, what must stay) -- every secret here is synthetic.
REDACTIONS = [
    (f"Enrollment code {FAKE_CODE} was typed", FAKE_CODE, "Enrollment code [redacted enrollment code] was typed"),
    ("sv-abcd-efgh-jkmn-pqrs lower case", "abcd-efgh", None),
    ("SVABCDEFGHJKMNPQRS no separators", "ABCDEFGHJKMNPQRS", None),
    ("ABCD-EFGH-JKMN-PQRS without the prefix", "ABCD-EFGH", None),
    (f"Authorization: Bearer {FAKE_TOKEN}", FAKE_TOKEN, "Authorization: [redacted]"),
    ("proxy-authorization=Basic dXNlcjpwYXNz", "dXNlcjpwYXNz", None),
    ("sent Bearer abc.def-ghi_jkl to the server", "abc.def-ghi_jkl", "sent Bearer [redacted] to the server"),
    ('{"agent_token": "tok-123-not-real", "installation_id": 41}', "tok-123-not-real", '"installation_id": 41'),
    ("password=hunter2-not-real; next", "hunter2-not-real", "; next"),
    ("client_secret: 'abc not real'", "abc not real", None),
    ("api_key=k-1", "k-1", None),
    ("enrollment_code=XYZ", "=XYZ", None),
    ("https://admin:pa55-not-real@sortview.invalid/api", "pa55-not-real", "https://[redacted]@sortview.invalid/api"),
    (f"the token {FAKE_TOKEN} was issued", FAKE_TOKEN, "the token [redacted] was issued"),
    # The focused review's confirmed misses (synthetic values).
    ("password=no-way-jose", "no-way-jose", "password=[redacted]"),
    ("pwd: true-story", "true-story", "pwd: [redacted]"),
    ("password=12345678", "12345678", "password=[redacted]"),
    ('"Authorization": "abcDEF123secretvalue"', "abcDEF123secretvalue", None),
    ('{"authorization":"Zz9-not-real"}', "Zz9-not-real", None),
    ('"auth": "s3cr3tValue"', "s3cr3tValue", '"auth": [redacted]'),
    ("key=Zx9QwErTy7", "Zx9QwErTy7", "key=[redacted]"),
    ("X-Api-Key: q1w2e3-not-real", "q1w2e3-not-real", None),
    ("private_key: 'k3y not real'", "k3y not real", None),
    ("passphrase=correct-horse", "correct-horse", None),
    ("https://host.invalid/x?sig=ab%2Bcd%2Fef12AB&se=2026", "ab%2Bcd%2Fef12AB", "&se=2026"),
    ("https://user:p@ss-not-real@host.invalid/", "ss-not-real", "https://[redacted]@host.invalid/"),
    ("code SV ABCD EFGH JKMN PQRS typed with spaces", "ABCD EFGH", None),
    ("SV-ABCD-EFGO-JKMN-PQRS mistyped", "EFGO", None),
]
#: Ordinary diagnostics that must be left exactly as they are.
KEPT = [
    "when they have been asked",
    "test-case-path-name",
    "OK: token stored in api_token.dpapi (DPAPI, Administrators + SYSTEM only; value never shown).",
    "API token stored: yes",
    "token_stored=True",
    "exit code 1",
    "Manifest SHA-256 6f91da7da2253a4cdef85e4fa6f25789dd8a48a932bc4ae884eac1465ece3193",
    "Run 1b4e28ba-2fa1-11d2-883f-0016d3cca427",
    r"C:\SortView\Collector\runtime\SortViewCollector.exe",
    "Enrollment (contract 2 requested): Refused [contract_not_permitted]; code not_used",
    "Answer given (enrollment_code): answered",
    "Question asked: enrollment_code",
    # Near misses that must stay as they are.
    "KeyId: k-2026",
    "monkey=banana",
    "token_count=none",
    "Old setup logs removed: 3",
    "Could not reach https://api.sortview.invalid:8443/v1 over HTTPS",
    "Run 1B4E28BA-2FA1-11D2-883F-0016D3CCA427",
    "#12  ■ UNEXPECTED ERROR: IOException: (message not logged)",
]


@needs_bridge
def test_redaction_removes_secret_shapes_and_keeps_ordinary_diagnostics(tmp_path, wizard_dll):
    texts = [text for text, _, _ in REDACTIONS] + KEPT
    (tmp_path / "texts.json").write_text(json.dumps(texts), encoding="utf-8")
    result = run_ps(wizard_dll, tmp_path, r"""
    $texts = ConvertFrom-Json -InputObject (Get-Content -LiteralPath (Join-Path (Split-Path $OutPath) 'texts.json') -Raw)
    $out.redacted = @($texts | ForEach-Object { $LogType::Redact($_) })
    $log = Open (Policy (Join-Path $Root 'SortViewSetup'))
    foreach ($text in $texts) { $log.ConsoleText($text); $log.Note($text); $log.Event([string[]]@($text)) }
    $out.log = Describe $log
    $log.Dispose()
    """)
    redacted = dict(zip(texts, result["redacted"], strict=True))
    for text, gone, kept in REDACTIONS:
        assert gone not in redacted[text], (text, redacted[text])
        assert "[redacted" in redacted[text], (text, redacted[text])
        if kept:
            assert kept in redacted[text], (text, redacted[text])
    for text in KEPT:
        assert redacted[text] == text
    # Through every kind of entry, the file never holds the secrets.
    content = Path(result["log"]["path"]).read_text(encoding="utf-8")
    for _, gone, _ in REDACTIONS:
        assert gone not in content, gone
    for text in KEPT:
        assert content.count(text) == 3, text


@needs_bridge
def test_every_failure_leaves_a_log_that_is_simply_not_saved_and_never_throws(tmp_path, wizard_dll):
    result = run_ps(wizard_dll, tmp_path, r"""
    function Exercise($log) {
        $log.Note('note'); $log.ConsoleText('text'); $log.Event([string[]]@('event')); $log.Event($null); $log.ConsoleText($null)
        $saved = [string]$log.SaveCopyTo((Join-Path $Root ('copy-' + [guid]::NewGuid().ToString('N') + '.log')))
        $log.Dispose(); $log.Dispose()
        $saved
    }
    $out.cases = [ordered]@{}
    foreach ($fault in 'CheckFolder', 'CreateFolder', 'CreateFile') {
        $top = Join-Path $Root "$fault\SortViewSetup"
        $policy = Policy $top; Fault $policy $fault
        $log = Open $policy
        $out.cases[$fault] = [ordered]@{ log = Describe $log; saved = (Exercise $log); exists = (Test-Path -LiteralPath (Join-Path $top 'Logs')) }
    }
    # Writing fails once the log is open: it stops, says so, and later calls do nothing.
    $policy = Policy (Join-Path $Root 'Write\SortViewSetup')
    $log = Open $policy
    Fault $policy 'Write'
    $log.ConsoleText('lost')
    Fault $policy 'None'
    $log.ConsoleText('after the failure: never written')
    $out.cases.Write = [ordered]@{ log = Describe $log; saved = (Exercise $log) }
    # Saving a copy fails.
    $policy = Policy (Join-Path $Root 'Save\SortViewSetup')
    $log = Open $policy; $log.Note('kept'); Fault $policy 'SaveCopy'
    $out.cases.SaveCopy = [ordered]@{ log = Describe $log; saved = (Exercise $log) }
    # No policy, and a host with no log at all.
    $out.cases.Null = [ordered]@{ log = Describe ($LogType::Open($null, $null)); saved = (Exercise ($LogType::Open($null, $null))) }
    $out.cases.NotSaved = [ordered]@{ log = Describe ($LogType::NotSaved('FolderUntrusted')); saved = (Exercise ($LogType::NotSaved('None'))) }
    # A policy that makes no sense: the log folder outside its root.
    $bad = New-Object SortView.Setup.Wizard.SetupLogPolicy((Join-Path $Root 'a'), (Join-Path $Root 'b\Logs'),
        [Security.Principal.SecurityIdentifier[]]@($user), [Security.Principal.SecurityIdentifier[]]@($user), $null)
    $out.cases.Outside = [ordered]@{ log = Describe ($LogType::Open($bad, $null)); exists = (Test-Path -LiteralPath (Join-Path $Root 'b')) }
    """)
    cases = result["cases"]
    expected = {"CheckFolder": "UnexpectedError", "CreateFolder": "FolderNotCreated", "CreateFile": "FileNotCreated"}
    for fault, problem in expected.items():
        assert cases[fault]["log"]["created"] is False and cases[fault]["log"]["problem"] == problem, fault
        assert cases[fault]["saved"] == "NoLog"
    assert cases["CheckFolder"]["exists"] is False and cases["CreateFolder"]["exists"] is False
    write = cases["Write"]
    assert write["log"]["created"] is True and write["log"]["problem"] == "WriteFailed" and write["saved"] == "Saved"
    content = Path(write["log"]["path"]).read_text(encoding="utf-8")
    assert "diagnostic log" in content and "lost" not in content and "never written" not in content
    assert cases["SaveCopy"]["log"]["problem"] == "None" and cases["SaveCopy"]["saved"] == "Failed"
    assert cases["Null"]["log"]["problem"] == "NotConfigured" and cases["Null"]["saved"] == "NoLog"
    assert cases["NotSaved"]["log"]["problem"] == "FolderUntrusted" and cases["NotSaved"]["saved"] == "NoLog"
    assert cases["Outside"]["log"]["problem"] == "NotConfigured" and cases["Outside"]["exists"] is False


@needs_bridge
def test_save_a_copy_writes_a_new_file_only(tmp_path, wizard_dll):
    result = run_ps(wizard_dll, tmp_path, r"""
    $log = Open (Policy (Join-Path $Root 'SortViewSetup'))
    $log.Note('while open')
    $copy = Join-Path $Root 'chosen\copy.log'
    New-Item -ItemType Directory -Path (Split-Path $copy) | Out-Null
    $out.open_copy = [string]$log.SaveCopyTo($copy)
    $log.Note('after the copy'); $log.Dispose()
    $existing = Join-Path $Root 'chosen\existing.log'
    Set-Content -LiteralPath $existing -Value 'the person''s own file'
    $out.over_existing = [string]$log.SaveCopyTo($existing)
    $out.existing = [string](Get-Content -LiteralPath $existing -Raw)  # a plain string, not one carrying provider properties
    $out.over_folder = [string]$log.SaveCopyTo((Split-Path $copy))
    $out.missing_folder = [string]$log.SaveCopyTo((Join-Path $Root 'no\such\folder\copy.log'))
    $out.empty = [string]$log.SaveCopyTo('')
    $out.log = Describe $log
    $out.copy = $copy
    """)
    assert result["open_copy"] == "Saved"  # also while the log is still being written
    copied = Path(result["copy"]).read_text(encoding="utf-8")
    assert "while open" in copied and "after the copy" not in copied
    assert result["over_existing"] == "AlreadyExists" and result["existing"].strip() == "the person's own file"
    assert result["over_folder"] == "AlreadyExists"
    assert result["missing_folder"] == "Failed" and result["empty"] == "UnsafeLocation"


@needs_bridge
def test_save_a_copy_never_follows_a_link_in_the_destination(tmp_path, wizard_dll):
    # The elevated installer must not be steered by a folder the person did not choose. Junctions need no privilege, so
    # they are always tested; symbolic links only where this session may create them (otherwise reported, not faked).
    result = run_ps(wizard_dll, tmp_path, r"""
    $policy = Policy (Join-Path $Root 'SortViewSetup')
    $log = Open $policy; $log.Note('the protected log'); $log.Dispose()
    $logPath = $LogType.GetProperty('FilePath', $NonPublic).GetValue($log)
    $before = (Get-FileHash -LiteralPath $logPath).Hash
    $dest = Join-Path $Root 'dest'; $elsewhere = Join-Path $Root 'elsewhere'
    New-Item -ItemType Directory -Force -Path $dest, $elsewhere | Out-Null
    function Junction([string]$link, [string]$target) { cmd /c mklink /J "$link" "$target" | Out-Null }
    $hook = $PolicyType.GetProperty('BeforeCopyCreated', $NonPublic)
    $out.cases = [ordered]@{}
    # 1. The confirmed case: a junction as a folder of the chosen path.
    Junction (Join-Path $dest 'via') $elsewhere
    $out.cases.junction_folder = [string]$log.SaveCopyTo((Join-Path $dest 'via\copy.log'))
    # 2. Deeper: a plain folder below a junction.
    New-Item -ItemType Directory -Path (Join-Path $elsewhere 'deeper') | Out-Null
    $out.cases.below_junction = [string]$log.SaveCopyTo((Join-Path $dest 'via\deeper\copy.log'))
    # 3. The chosen name itself is a junction.
    $out.cases.junction_name = [string]$log.SaveCopyTo((Join-Path $dest 'via'))
    # 4. A folder swapped for a junction AFTER it was checked and before the file is created: the file still goes into the
    #    folder that was checked (wherever it was moved), never through the junction.
    New-Item -ItemType Directory -Path (Join-Path $dest 'swap') | Out-Null
    $hook.SetValue($policy, [Action]{
        Rename-Item -LiteralPath (Join-Path $dest 'swap') -NewName 'swap-moved'
        Junction (Join-Path $dest 'swap') $elsewhere
    })
    $out.cases.swapped_after_check = [string]$log.SaveCopyTo((Join-Path $dest 'swap\late.log'))
    $hook.SetValue($policy, $null)
    $out.swap_is_junction = [bool]((Get-Item -LiteralPath (Join-Path $dest 'swap') -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)
    $out.late_in_moved_folder = Test-Path -LiteralPath (Join-Path $dest 'swap-moved\late.log')
    # 5. Not a plain local path.
    $out.cases.network = [string]$log.SaveCopyTo('\\localhost\C$\Windows\Temp\copy.log')
    $out.cases.device_path = [string]$log.SaveCopyTo('\\?\' + (Join-Path $dest 'device.log'))
    $out.cases.stream = [string]$log.SaveCopyTo((Join-Path $dest 'copy.log:hidden'))
    $out.cases.device_name = [string]$log.SaveCopyTo((Join-Path $dest 'NUL.log'))
    $out.cases.relative = [string]$log.SaveCopyTo('copy.log')
    # 6. Symbolic links, where they can be made.
    $victim = Join-Path $elsewhere 'victim.log'; Set-Content -LiteralPath $victim -Value 'not to be replaced'
    $out.symlinks = $true
    try {
        New-Item -ItemType SymbolicLink -Path (Join-Path $dest 'linkdir') -Target $elsewhere -ErrorAction Stop | Out-Null
        New-Item -ItemType SymbolicLink -Path (Join-Path $dest 'link.log') -Target $victim -ErrorAction Stop | Out-Null
        New-Item -ItemType SymbolicLink -Path (Join-Path $dest 'dangling.log') -Target (Join-Path $elsewhere 'none.log') -ErrorAction Stop | Out-Null
    } catch { $out.symlinks = $false }
    if ($out.symlinks) {
        $out.cases.symlink_folder = [string]$log.SaveCopyTo((Join-Path $dest 'linkdir\copy.log'))
        $out.cases.symlink_name = [string]$log.SaveCopyTo((Join-Path $dest 'link.log'))
        $out.cases.dangling_symlink_name = [string]$log.SaveCopyTo((Join-Path $dest 'dangling.log'))
        $out.dangling_target = Test-Path -LiteralPath (Join-Path $elsewhere 'none.log')
    }
    $out.victim = [string](Get-Content -LiteralPath $victim -Raw)
    # A plain folder still works, and the protected log was only read.
    New-Item -ItemType Directory -Path (Join-Path $dest 'plain') | Out-Null
    $out.cases.plain = [string]$log.SaveCopyTo((Join-Path $dest 'plain\copy.log'))
    $out.log_unchanged = ((Get-FileHash -LiteralPath $logPath).Hash -eq $before)
    $out.elsewhere = Listing $elsewhere
    foreach ($name in 'via', 'swap') { cmd /c rmdir (Join-Path $dest $name) }
    """)
    cases = result["cases"]
    assert cases["junction_folder"] == "UnsafeLocation"
    assert cases["below_junction"] == "UnsafeLocation"
    assert cases["junction_name"] == "AlreadyExists"
    assert result["swap_is_junction"] is True  # the swap really happened
    assert cases["swapped_after_check"] == "Saved" and result["late_in_moved_folder"] is True
    for case in ("network", "device_path", "stream", "device_name", "relative"):
        assert cases[case] == "UnsafeLocation", case
    assert cases["plain"] == "Saved" and result["log_unchanged"] is True
    # Nothing ever arrived through a link, and nothing there was changed.
    assert result["elsewhere"] == ["deeper", "victim.log"] and result["victim"].strip() == "not to be replaced"
    if not result["symlinks"]:
        pytest.skip("junction and swap cases passed; symbolic-link cases need the create-symbolic-link privilege or Developer Mode")
    assert cases["symlink_folder"] == "UnsafeLocation"
    assert cases["symlink_name"] == "AlreadyExists"
    assert cases["dangling_symlink_name"] == "AlreadyExists" and result["dangling_target"] is False


@needs_bridge
def test_the_production_policy_is_only_read_and_refuses_a_fault(tmp_path, wizard_dll):
    result = run_ps(wizard_dll, tmp_path, r"""
    $production = $PolicyType::Production
    $out.root = $production.Root; $out.folder = $production.LogFolder; $out.owner = $production.Owner.Value
    $out.trusted = @($production.TrustedPrincipals | ForEach-Object { $_.Value })
    $out.access = @($production.AccessPrincipals | ForEach-Object { $_.Value })
    $out.fault = [string]$PolicyType.GetProperty('InjectedFault', $NonPublic).GetValue($production)
    try { Fault $production 'Write'; $out.refused = $false } catch { $out.refused = $true }
    try { $PolicyType.GetProperty('BeforeCopyCreated', $NonPublic).SetValue($production, [Action]{}); $out.hook_refused = $false } catch { $out.hook_refused = $true }
    $out.hook = $null -eq $PolicyType.GetProperty('BeforeCopyCreated', $NonPublic).GetValue($production)
    $out.limits = @($PolicyType::MaxLogs, $PolicyType::MaxBytes)
    """)
    assert result["root"] == r"C:\ProgramData\SortViewSetup" and result["folder"] == r"C:\ProgramData\SortViewSetup\Logs"
    assert result["owner"] == ADMINISTRATORS and set(result["access"]) == {SYSTEM, ADMINISTRATORS}
    assert set(result["trusted"]) == {SYSTEM, ADMINISTRATORS, "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"}
    assert result["fault"] == "None" and result["refused"] is True
    assert result["hook"] is True and result["hook_refused"] is True
    assert result["limits"] == [20, MAX_BYTES]


@needs_bridge
def test_what_was_written_survives_the_program_being_ended(tmp_path, wizard_dll):
    # A separate PowerShell process writes, then ends itself abruptly (no Dispose, no clean exit).
    root = tmp_path / "machine" / "SortViewSetup"
    root.parent.mkdir(parents=True)
    script = tmp_path / "killed.ps1"
    script.write_text(_PRELUDE.split("$out = [ordered]")[0] + r"""
$log = Open (Policy $Root)
$log.Note('Setup run started.')
$log.ConsoleText('=== Step 3: checking the connection ===')
Set-Content -LiteralPath $OutPath -Value $log.FileName
Stop-Process -Id $PID -Force
""", encoding="utf-8")
    name_file = tmp_path / "name.txt"
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                "-WizardPath", str(wizard_dll), "-OutPath", str(name_file), "-Root", str(root)],
                               capture_output=True, text=True, timeout=300, check=False)
    assert completed.returncode != 0  # it did not exit normally
    name = name_file.read_text(encoding="utf-8-sig").strip()
    assert LOG_NAME.match(name)
    content = (root / "Logs" / name).read_text(encoding="utf-8")
    assert "Setup run started." in content and "=== Step 3: checking the connection ===" in content


@needs_bridge
def test_copy_details_names_the_log_or_says_it_was_not_saved(tmp_path, wizard_dll):
    result = run_ps(wizard_dll, tmp_path, r"""
    function Summary($log) {
        $guidance = New-Object SortView.Setup.Wizard.StopGuidance
        $guidance.Finish($true)
        $guidance.DiagnosticSummary([DateTimeOffset]::new(2026, 11, 14, 9, 30, 0, [TimeSpan]::Zero), $log)
    }
    $policy = Policy (Join-Path $Root 'SortViewSetup')
    $saved = Open $policy; $saved.Dispose()
    $out.saved = [ordered]@{ file = $saved.FileName; summary = (Summary $saved) }
    $broken = Open $policy; Fault $policy 'Write'; $broken.Note('x'); Fault $policy 'None'
    $out.broken = Summary $broken
    $out.untrusted = Summary ($LogType::NotSaved('FolderUntrusted'))
    $out.none = Summary $null
    $guidance = New-Object SortView.Setup.Wizard.StopGuidance; $guidance.Finish($true)
    $out.without = $guidance.DiagnosticSummary([DateTimeOffset]::new(2026, 11, 14, 9, 30, 0, [TimeSpan]::Zero))
    """)
    saved = result["saved"]
    assert f"Setup log: {saved['file']}" in saved["summary"].splitlines()
    assert saved["summary"].rstrip().endswith("This summary holds no enrollment code, API token, secret, file path or setup log.")
    assert "Setup log: setup-" in result["broken"] and "(incomplete: writing stopped)" in result["broken"]
    assert "Setup log: not saved (log folder not trusted)" in result["untrusted"]
    assert "Setup log: not saved (no log folder)" in result["none"]
    for summary in (saved["summary"], result["broken"], result["untrusted"], result["none"]):
        assert "\\" not in summary and "ProgramData" not in summary  # a file name, never a path
    assert "Setup log" not in result["without"]  # the summary without a log is unchanged


# =====================================================================================================================
# 3. UI scenarios: the simulation window
# =====================================================================================================================

def _log_folder(result: dict) -> Path:
    return Path(result["workspace"]) / "SortViewSetup" / "Logs"


@needs_bridge
def test_copy_details_names_the_saved_log_and_save_a_copy_saves_only_where_chosen(tmp_path, window_exe):  # noqa: F811
    steps = [
        {"op": "select", "id": "FaultInstall"},
        {"op": "click", "id": "StartButton"},
        {"op": "wait_prompt", "prompt": "sources_confirm"}, {"op": "click", "id": "UseStandardFilesButton"},
        *_answer_code(),
        {"op": "wait_status", "contains": "Finished"}, {"op": "snapshot", "label": "stopped"},
        {"op": "click", "id": "CopyDetailsButton"}, {"op": "wait_text", "id": "CopyStatus", "contains": "Copied"},
        {"op": "read_clipboard"},
        # Cancelled: nothing saved.
        {"op": "click", "id": "SaveLogButton"},
        {"op": "save_dialog", "title": "Save a copy of the setup log", "cancel": True},
        {"op": "wait_text", "id": "SaveLogStatus", "contains": "No copy was saved"},
        # Through a junction: refused with a clear message, and nothing written where the junction leads.
        {"op": "make_junction", "value": "{workspace}\\chosen\\via", "target": "{workspace}\\elsewhere"},
        {"op": "click", "id": "SaveLogButton"},
        {"op": "save_dialog", "title": "Save a copy of the setup log", "value": "{workspace}\\chosen\\via\\copy.log"},
        {"op": "wait_text", "id": "SaveLogStatus", "contains": "Nothing was saved"}, {"op": "snapshot", "label": "refused"},
        # Saved where chosen.
        {"op": "make_file", "value": "{workspace}\\chosen\\placeholder.txt"},
        {"op": "click", "id": "SaveLogButton"},
        {"op": "save_dialog", "title": "Save a copy of the setup log", "value": "{workspace}\\chosen\\copy.log"},
        {"op": "wait_text", "id": "SaveLogStatus", "contains": "A copy of the setup log was saved"}, {"op": "snapshot", "label": "saved"},
        {"op": "close"}, {"op": "wait_exit"},
    ]

    result = drive(window_exe, tmp_path, steps)

    stopped = snapshot(result, "stopped")
    assert "sensitive system information" in stopped["outcome"]  # the warning, on screen beside the button
    [copied] = result["clipboard"]
    logs = sorted(p.name for p in _log_folder(result).iterdir())
    assert len(logs) == 1 and LOG_NAME.match(logs[0])
    assert f"Setup log: {logs[0]}" in copied and "\\" not in copied and result["workspace"] not in copied
    assert all(title.startswith("Save a copy of the setup log") for title in result["dialog_titles"])
    assert len(result["dialog_titles"]) == 3
    log = (_log_folder(result) / logs[0]).read_text(encoding="utf-8")
    # The run as Show details showed it, plus the questions and the result -- never the code (the window invariants also
    # check every file of the simulated machine for the code and the simulated token).
    assert "Question asked: sources_confirm" in log and "Answer given (sources_confirm): yes" in log
    assert "Question asked: enrollment_code" in log and "Answer given (enrollment_code): answered" in log
    assert "SETUP STOPPED at" in log and "Finished: exit code 3" in log and "What to do now: You can try again" in log
    assert "Setup run ended." in log and DUMMY_CODE not in log
    assert any(line.split("  ", 1)[1].startswith("console") for line in log.splitlines())
    # The copy: exactly the log, only where chosen; nothing anywhere else.
    copy = Path(result["workspace"]) / "chosen" / "copy.log"
    assert copy.read_bytes() == (_log_folder(result) / logs[0]).read_bytes()
    assert sorted(p.name for p in copy.parent.iterdir()) == ["copy.log", "placeholder.txt", "via"]
    # Refused through the junction: nothing reached the folder it leads to.
    assert list((Path(result["workspace"]) / "elsewhere").iterdir()) == []
    os.rmdir(copy.parent / "via")  # the junction only (never what it leads to)
    assert_window_invariants(result)


@needs_bridge
def test_an_unusable_log_folder_never_changes_the_run(tmp_path, window_exe):  # noqa: F811
    def steps(block: bool) -> list[dict]:
        return [
            *([{"op": "make_file", "value": "{workspace}\\SortViewSetup"}] if block else []),
            {"op": "select", "id": "FaultInstall"},
            {"op": "click", "id": "StartButton"},
            {"op": "wait_prompt", "prompt": "sources_confirm"}, {"op": "click", "id": "UseStandardFilesButton"},
            *_answer_code(),
            {"op": "wait_status", "contains": "Finished"}, {"op": "snapshot", "label": "stopped"},
            {"op": "click", "id": "CopyDetailsButton"}, {"op": "wait_text", "id": "CopyStatus", "contains": "Copied"},
            {"op": "read_clipboard"},
            {"op": "close"}, {"op": "wait_exit"},
        ]

    blocked = drive(window_exe, tmp_path / "blocked", steps(True))
    normal = drive(window_exe, tmp_path / "normal", steps(False))

    # The same run, call for call, with the same result and guidance: only the log line differs.
    assert workspace_tools(blocked) == workspace_tools(normal)
    b, n = snapshot(blocked, "stopped"), snapshot(normal, "stopped")
    assert b["status"] == n["status"] and "exit code 3" in b["status"]
    assert b["steps"] == n["steps"]
    # (Each simulated machine has its own scratch folder, which the engine's own words name.)
    assert [e.replace(blocked["workspace"], "{ws}") for e in b["events"]] == [e.replace(normal["workspace"], "{ws}") for e in n["events"]]
    strip = lambda text: re.sub(r"(?m)^(Time|Setup log): .*$", "", text)
    assert strip(blocked["clipboard"][0]) == strip(normal["clipboard"][0])
    assert "Setup log: not saved (log folder not trusted)" in blocked["clipboard"][0]
    assert (Path(blocked["workspace"]) / "SortViewSetup").is_file()  # left exactly as it was
    assert_window_invariants(blocked)
    assert_window_invariants(normal)
