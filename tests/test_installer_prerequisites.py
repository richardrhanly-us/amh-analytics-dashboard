"""Tests for the production installer's Welcome and Prerequisites screens, Phase 4B.2
(installer/SortView.Setup.Wizard/PrerequisiteChecks.cs, WelcomePage, PrerequisitesPage, and the production window's
Welcome -> Prerequisites -> Setup navigation).

  1. STATIC checks (run everywhere): the machine reader uses only read-only APIs, and the new screens have no path that
     starts setup.
  2. RULES (Windows only): PrerequisiteRules.Evaluate on hand-made facts -- boundaries, unknown values, Group Policy and
     legacy-task cases. One PowerShell process evaluates every case.
  3. The real READ-ONLY reader on this machine.
  4. NAVIGATION (Windows only): the production window loaded in-process (as in test_installer_production_app.py) with
     injected facts: Next blocked before results, while checking and on any Failed; Check again; a read that throws.

Nothing here runs setup or changes the machine.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from test_installer_bridge import _MSBUILD, _POWERSHELL, needs_bridge

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "installer"
WIZARD_DIR = INSTALLER / "SortView.Setup.Wizard"
CHECKS_CODE = WIZARD_DIR / "PrerequisiteChecks.cs"
PAGE_SOURCES = [WIZARD_DIR / name for name in ("WelcomePage.xaml", "WelcomePage.xaml.cs", "PrerequisitesPage.xaml",
                                               "PrerequisitesPage.xaml.cs", "PrerequisiteChecks.cs", "ReadyPage.xaml",
                                               "ReadyPage.xaml.cs")]
READY_PAGE = WIZARD_DIR / "ReadyPage.xaml"
SETUP_PROJECT = INSTALLER / "SortView.Setup" / "SortView.Setup.csproj"

RESULT_IDS = ["windows_version", "architecture", "dotnet", "powershell", "administrator", "disk_space", "legacy_tasks",
              "script_policy", "existing_collector", "installation_package", "connection", "tech_logic_files"]
REQUIRED_IDS = RESULT_IDS[:7]
ADVISORY_IDS = ["script_policy"]
DEFERRED_IDS = RESULT_IDS[8:]
GIB = 1024 ** 3


def _without_comments(text: str) -> str:
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


# =====================================================================================
# 1. STATIC checks
# =====================================================================================

def test_the_machine_reader_uses_only_read_only_apis():
    code = _without_comments(CHECKS_CODE.read_text(encoding="utf-8"))
    # Every registry key is opened without write access.
    opens = re.findall(r"OpenSubKey\(([^)]*)\)", code)
    assert opens and all(call.endswith(", false") for call in opens), opens
    # No API that writes the registry, the file system or a Scheduled Task, starts a process or runs PowerShell.
    for writer in ("SetValue", "CreateSubKey", "DeleteSubKey", "DeleteValue", "RegisterTask", "DeleteTask", "SetProperty",
                   "Process.", "ProcessStartInfo", "Runspace", "PowerShell.Create", "File.", "Directory.Create",
                   "Directory.Delete"):
        assert writer not in code, writer
    # Task Scheduler is asked only these, by name: connect, the root folder, a task, and a task's Enabled value.
    assert set(re.findall(r'Call\(\w+, "(\w+)"', code)) == {"Connect", "GetFolder", "GetTask"}
    assert set(re.findall(r'Get\(\w+, "(\w+)"', code)) == {"Enabled"}


def test_the_new_screens_have_no_path_that_starts_setup():
    sources = "\n".join(_without_comments(path.read_text(encoding="utf-8")) for path in PAGE_SOURCES)
    for run_path in ("BeginRun", "SetupRunner", "SetupRunSession", "SetupEngineHost", "SimulatedSetupHarness",
                     "StartRequested", "Core.Testing"):
        assert run_path not in sources, run_path


# =====================================================================================
# 2. RULES
# =====================================================================================

#: Facts that pass every check. Each case below changes some of them.
PASSING = {
    "WindowsBuild": 19045, "WindowsUbr": 6466, "WindowsEditionId": "Core", "WindowsInstallationType": "Client",
    "WindowsDisplayVersion": "22H2", "Is64BitOperatingSystem": True, "DotNetRelease": 533325,
    "PowerShellVersion": "5.1.19041.1", "IsAdministrator": True, "SystemDrive": "C:\\", "SystemDriveFreeBytes": 100 * GIB,
    "LegacyTasksRead": True, "LegacyTasks": [], "LegacyAgentFolderPresent": False,
    "MachinePolicyRead": True, "MachinePolicy": None, "UserPolicyRead": True, "UserPolicy": None,
}

_RULES_SCRIPT = r"""
param([string]$WizardPath, [string]$CasesPath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
Add-Type -Path $WizardPath
$cases = Get-Content -LiteralPath $CasesPath -Raw | ConvertFrom-Json
$out = [ordered]@{}
foreach ($case in $cases.PSObject.Properties) {
    if ($case.Name -eq 'null') { $facts = $null }
    elseif ($case.Name -eq 'unknown') { $facts = [SortView.Setup.Wizard.MachineFacts]::Unknown() }
    else {
        $facts = New-Object SortView.Setup.Wizard.MachineFacts
        foreach ($p in $case.Value.PSObject.Properties) {
            if ($p.Name -eq 'LegacyTasks' -and $null -eq $p.Value) { $facts.LegacyTasks = $null }
            elseif ($p.Name -eq 'LegacyTasks') {
                $tasks = @($p.Value | ForEach-Object { $_ } | ForEach-Object {
                    $t = New-Object SortView.Setup.Wizard.LegacyTaskFact
                    $t.Name = $_.Name; $t.Present = $_.Present; $t.Enabled = $_.Enabled; $t })
                $facts.LegacyTasks = [SortView.Setup.Wizard.LegacyTaskFact[]]$tasks
            } elseif ($null -ne $p.Value) { $facts.($p.Name) = $p.Value }  # (PowerShell would turn a null string into '')
        }
    }
    $results = [ordered]@{}
    foreach ($r in [SortView.Setup.Wizard.PrerequisiteRules]::Evaluate($facts)) {
        $results[$r.Id] = [ordered]@{ status = $r.Status.ToString(); kind = $r.Kind.ToString(); detail = $r.Detail; resolution = $r.Resolution; blocks = $r.BlocksNext }
    }
    $out[$case.Name] = $results
}
ConvertTo-Json -InputObject $out -Depth 5 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


def _task(name, present, enabled):
    return {"Name": name, "Present": present, "Enabled": enabled}


SCHEDULER, AGENT = "sortview-scheduler", "SortView Canonical Agent"

#: name -> (changes to PASSING, {result id: expected status}).
CASES = {
    "passing": ({}, {}),
    # Windows: the provisional minimum is build 17763 (Windows 10 1809); Windows 11 is 22000+.
    "win_min": ({"WindowsBuild": 17763}, {}),
    "win_below_min": ({"WindowsBuild": 17762}, {"windows_version": "Failed"}),
    "win_old": ({"WindowsBuild": 10240}, {"windows_version": "Failed"}),
    "win11": ({"WindowsBuild": 22631, "WindowsDisplayVersion": "23H2"}, {}),
    "win_server": ({"WindowsBuild": 20348, "WindowsInstallationType": "Server", "WindowsEditionId": "ServerStandard"},
                   {"windows_version": "Warning"}),
    "win_server_edition_only": ({"WindowsEditionId": "ServerDatacenter"}, {"windows_version": "Warning"}),
    "win_old_server": ({"WindowsBuild": 14393, "WindowsInstallationType": "Server"}, {"windows_version": "Failed"}),
    "win_unknown": ({"WindowsBuild": None}, {"windows_version": "NotChecked"}),
    "win_type_unknown": ({"WindowsInstallationType": None, "WindowsEditionId": None}, {"windows_version": "NotChecked"}),
    # Architecture.
    "x86": ({"Is64BitOperatingSystem": False}, {"architecture": "Failed"}),
    "arch_unknown": ({"Is64BitOperatingSystem": None}, {"architecture": "NotChecked"}),
    # .NET Framework 4.8 = release 528040.
    "dotnet_min": ({"DotNetRelease": 528040}, {}),
    "dotnet_below": ({"DotNetRelease": 528039}, {"dotnet": "Failed"}),
    "dotnet_472": ({"DotNetRelease": 461808}, {"dotnet": "Failed"}),
    "dotnet_unknown": ({"DotNetRelease": None}, {"dotnet": "NotChecked"}),
    # Windows PowerShell 5.1.
    "ps_51": ({"PowerShellVersion": "5.1"}, {}),
    "ps_50": ({"PowerShellVersion": "5.0.10586.117"}, {"powershell": "Failed"}),
    "ps_40": ({"PowerShellVersion": "4.0"}, {"powershell": "Failed"}),
    "ps_unknown": ({"PowerShellVersion": None}, {"powershell": "NotChecked"}),
    "ps_garbage": ({"PowerShellVersion": "five"}, {"powershell": "NotChecked"}),
    # Administrator.
    "not_admin": ({"IsAdministrator": False}, {"administrator": "Failed"}),
    "admin_unknown": ({"IsAdministrator": None}, {"administrator": "NotChecked"}),
    # Disk: under 1 GB Failed, under 5 GB Warning.
    "disk_just_under_1": ({"SystemDriveFreeBytes": GIB - 1}, {"disk_space": "Failed"}),
    "disk_1": ({"SystemDriveFreeBytes": GIB}, {"disk_space": "Warning"}),
    "disk_just_under_5": ({"SystemDriveFreeBytes": 5 * GIB - 1}, {"disk_space": "Warning"}),
    "disk_5": ({"SystemDriveFreeBytes": 5 * GIB}, {}),
    "disk_unknown": ({"SystemDriveFreeBytes": None}, {"disk_space": "NotChecked"}),
    # Legacy tasks.
    "tasks_absent": ({"LegacyTasks": [_task(SCHEDULER, False, None), _task(AGENT, False, None)]}, {}),
    "tasks_disabled": ({"LegacyTasks": [_task(SCHEDULER, True, False), _task(AGENT, True, False)],
                        "LegacyAgentFolderPresent": True}, {}),
    "scheduler_enabled": ({"LegacyTasks": [_task(SCHEDULER, True, True), _task(AGENT, True, False)]},
                          {"legacy_tasks": "Failed"}),
    "agent_enabled": ({"LegacyTasks": [_task(SCHEDULER, False, None), _task(AGENT, True, True)]}, {"legacy_tasks": "Failed"}),
    "task_indeterminate": ({"LegacyTasks": [_task(SCHEDULER, None, None), _task(AGENT, False, None)]},
                           {"legacy_tasks": "NotChecked"}),
    "task_enabled_unknown": ({"LegacyTasks": [_task(SCHEDULER, True, None), _task(AGENT, False, None)]},
                             {"legacy_tasks": "NotChecked"}),
    "scheduler_unreadable": ({"LegacyTasksRead": False, "LegacyTasks": None}, {"legacy_tasks": "NotChecked"}),
    # Script policy (Group Policy: computer first, then user).
    "gpo_allsigned": ({"MachinePolicy": "AllSigned"}, {"script_policy": "Warning"}),
    "gpo_remotesigned": ({"MachinePolicy": "RemoteSigned"}, {"script_policy": "Warning"}),
    "gpo_scripts_off": ({"MachinePolicy": "Restricted"}, {"script_policy": "Warning"}),
    "gpo_user_allsigned": ({"UserPolicy": "AllSigned"}, {"script_policy": "Warning"}),
    "gpo_unrestricted": ({"MachinePolicy": "Unrestricted"}, {}),
    "gpo_bypass": ({"MachinePolicy": "Bypass"}, {}),
    "gpo_machine_wins": ({"MachinePolicy": "Unrestricted", "UserPolicy": "AllSigned"}, {}),
    "gpo_machine_wins_restrictive": ({"MachinePolicy": "AllSigned", "UserPolicy": "Unrestricted"}, {"script_policy": "Warning"}),
    "gpo_machine_unreadable": ({"MachinePolicyRead": False}, {"script_policy": "NotChecked"}),
    "gpo_user_unreadable": ({"UserPolicyRead": False}, {"script_policy": "NotChecked"}),
    "gpo_machine_set_user_unreadable": ({"MachinePolicy": "AllSigned", "UserPolicyRead": False}, {"script_policy": "Warning"}),
    "gpo_unknown_value": ({"MachinePolicy": "SomethingNew"}, {"script_policy": "NotChecked"}),
    # Warnings only.
    "warnings_only": ({"WindowsInstallationType": "Server", "SystemDriveFreeBytes": 2 * GIB, "MachinePolicy": "AllSigned"},
                      {"windows_version": "Warning", "disk_space": "Warning", "script_policy": "Warning"}),
}


@pytest.fixture(scope="module")
def wizard_dll() -> Path:
    completed = subprocess.run([_MSBUILD, str(SETUP_PROJECT), "/p:Configuration=Release", "/nologo", "/v:minimal"],
                               capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return SETUP_PROJECT.parent / "bin" / "Release" / "SortView.Setup.Wizard.dll"


@pytest.fixture(scope="module")
def evaluated(tmp_path_factory, wizard_dll) -> dict:
    work = tmp_path_factory.mktemp("rules")
    cases = {name: {**PASSING, **changes} for name, (changes, _) in CASES.items()}
    cases["unknown"] = {}
    cases["null"] = {}
    (work / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    (work / "rules.ps1").write_text(_RULES_SCRIPT, encoding="utf-8")
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                str(work / "rules.ps1"), "-WizardPath", str(wizard_dll), "-CasesPath", str(work / "cases.json"),
                                "-OutPath", str(work / "out.json")], capture_output=True, text=True, timeout=120, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads((work / "out.json").read_text(encoding="utf-8-sig"))


@needs_bridge
@pytest.mark.parametrize("name", list(CASES))
def test_rule_classification(evaluated, name):
    results = evaluated[name]
    assert list(results) == RESULT_IDS  # every result, in a fixed order
    expected = {rid: "Passed" for rid in RESULT_IDS} | {rid: "NotChecked" for rid in DEFERRED_IDS} | CASES[name][1]
    assert {rid: r["status"] for rid, r in results.items()} == expected
    for rid, result in results.items():
        assert result["kind"] == ("Required" if rid in REQUIRED_IDS else "Advisory" if rid in ADVISORY_IDS else "Deferred")
        # Any Failed blocks Next, and so does a required check that could not be verified; nothing else does.
        unverified = result["kind"] == "Required" and result["status"] == "NotChecked"
        assert result["blocks"] is (result["status"] == "Failed" or unverified)
        if result["status"] in ("Failed", "Warning") or unverified:
            assert result["resolution"], result  # always says what to do


#: name -> the result ids that block Next.
BLOCKING = {
    "win_unknown": ["windows_version"],                       # 1. unknown Windows version
    "win_type_unknown": ["windows_version"],
    "arch_unknown": ["architecture"],                         # 2. unknown architecture
    "dotnet_unknown": ["dotnet"],                             # 3. unknown .NET or PowerShell
    "ps_unknown": ["powershell"],
    "ps_garbage": ["powershell"],
    "admin_unknown": ["administrator"],                       # 4. unknown administrator status
    "disk_unknown": ["disk_space"],                           # 5. unknown disk space
    "scheduler_unreadable": ["legacy_tasks"],                 # 6. Task Scheduler read failure
    "task_indeterminate": ["legacy_tasks"],
    "task_enabled_unknown": ["legacy_tasks"],
    "gpo_unknown_value": [],                                  # 7. unknown script policy is advisory
    "gpo_machine_unreadable": [],
    "gpo_user_unreadable": [],
    "passing": [],                                            # 8. deferred Not checked never blocks
    "unknown": REQUIRED_IDS,                                  # 9. a full read failure blocks
    "null": REQUIRED_IDS,
    "warnings_only": [],                                      # 10. warnings never block
    "not_admin": ["administrator"],
}


@needs_bridge
@pytest.mark.parametrize("name", list(BLOCKING))
def test_what_blocks_next(evaluated, name):
    assert [rid for rid, r in evaluated[name].items() if r["blocks"]] == BLOCKING[name]
    for rid in DEFERRED_IDS:
        assert evaluated[name][rid]["status"] == "NotChecked" and evaluated[name][rid]["blocks"] is False


@needs_bridge
def test_an_unverified_required_check_says_to_retry_or_ask_it(evaluated):
    for rid in REQUIRED_IDS:
        resolution = evaluated["unknown"][rid]["resolution"]
        assert "Check again" in resolution and "IT administrator" in resolution


@needs_bridge
@pytest.mark.parametrize("name", ["unknown", "null"])
def test_unknown_facts_are_never_passed(evaluated, name):
    # Nothing known (a read that failed) -> every check Not checked, never Passed (what blocks: test_what_blocks_next).
    assert {r["status"] for r in evaluated[name].values()} == {"NotChecked"}


@needs_bridge
def test_meeting_the_windows_minimum_is_not_called_validated(evaluated):
    detail = evaluated["win_min"]["windows_version"]["detail"]
    assert "provisional minimum" in detail and "Release testing" in detail
    assert evaluated["win11"]["windows_version"]["detail"].startswith("Windows 11")
    assert evaluated["passing"]["windows_version"]["detail"].startswith("Windows 10 22H2 (build 19045.6466)")


@needs_bridge
def test_details_state_the_limits_of_what_was_checked(evaluated):
    assert "AppLocker" in evaluated["passing"]["script_policy"]["detail"]
    assert "AppLocker" in evaluated["gpo_allsigned"]["script_policy"]["detail"]
    assert "Provisional" in evaluated["passing"]["disk_space"]["detail"]
    assert "future installer package" in evaluated["dotnet_below"]["dotnet"]["detail"]
    assert "disabled" in evaluated["tasks_disabled"]["legacy_tasks"]["detail"]
    assert SCHEDULER in evaluated["scheduler_enabled"]["legacy_tasks"]["detail"]


# =====================================================================================
# 3. The real READ-ONLY reader on this machine
# =====================================================================================

_READ_SCRIPT = r"""
param([string]$WizardPath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
function Get-MachineState {
    [ordered]@{
        tasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue | ForEach-Object { "$($_.TaskPath)$($_.TaskName)|$($_.State -eq 'Disabled')" } | Sort-Object)
        policy = @('HKLM:\SOFTWARE\Policies\Microsoft\Windows\PowerShell', 'HKCU:\SOFTWARE\Policies\Microsoft\Windows\PowerShell' |
                   ForEach-Object { Test-Path $_ })
        agent_folder = (Test-Path 'C:\SortViewAgent')
    }
}
Add-Type -Path $WizardPath
$before = Get-MachineState
$facts = [SortView.Setup.Wizard.MachineFacts]::Read()
$results = [SortView.Setup.Wizard.PrerequisiteRules]::Evaluate($facts)
$report = [ordered]@{
    facts = [ordered]@{ build = $facts.WindowsBuild; x64 = $facts.Is64BitOperatingSystem; dotnet = $facts.DotNetRelease
                        ps = $facts.PowerShellVersion; admin = $facts.IsAdministrator; free = $facts.SystemDriveFreeBytes
                        tasks_read = $facts.LegacyTasksRead; tasks = @($facts.LegacyTasks | ForEach-Object { $_.Name })
                        machine_read = $facts.MachinePolicyRead; user_read = $facts.UserPolicyRead }
    expected_build = [int](Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').CurrentBuildNumber
    results = @($results | ForEach-Object { $_.Id })
    children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $PID").Count
    before = $before
    after = Get-MachineState
}
ConvertTo-Json -InputObject $report -Depth 5 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


@needs_bridge
def test_reading_this_machine_reports_real_facts_and_changes_nothing(tmp_path, wizard_dll):
    script, out = tmp_path / "read.ps1", tmp_path / "read.json"
    script.write_text(_READ_SCRIPT, encoding="utf-8")
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                "-WizardPath", str(wizard_dll), "-OutPath", str(out)],
                               capture_output=True, text=True, timeout=120, check=False)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(out.read_text(encoding="utf-8-sig"))
    facts = report["facts"]
    assert facts["build"] == report["expected_build"]
    assert facts["x64"] is True and facts["dotnet"] >= 528040 and facts["ps"].startswith("5.1")
    assert isinstance(facts["admin"], bool) and facts["free"] > 0
    assert facts["tasks_read"] is True and facts["tasks"] == [SCHEDULER, AGENT]
    assert facts["machine_read"] is True and facts["user_read"] is True
    assert report["results"] == RESULT_IDS
    # Read-only: no process started, no Scheduled Task or policy key changed or created.
    assert report["children"] == 0
    assert report["after"] == report["before"]


# =====================================================================================
# 4. NAVIGATION, in the production window
# =====================================================================================

# Loads the production window in-process (no new process, no elevation) and drives it with injected facts:
# a read held open by a gate (to see "checking"), passing facts, failing facts, and a read that throws.
_NAV_PROBE = r"""
param([string]$ExePath, [string]$OutPath)
$ErrorActionPreference = 'Stop'
Add-Type -ReferencedAssemblies PresentationFramework, PresentationCore, WindowsBase, System.Xaml, System.Core -TypeDefinition @"
using System; using System.Collections; using System.Collections.Generic; using System.Reflection;
using System.Threading; using System.Windows; using System.Windows.Controls.Primitives; using System.Windows.Threading;
public static class NavigationProbe {
    public static readonly ManualResetEventSlim Gate = new ManualResetEventSlim(false);
    public static int Reads;
    static object facts;
    public static object Held() { Interlocked.Increment(ref Reads); Gate.Wait(); return facts; }
    public static object Now() { Interlocked.Increment(ref Reads); return facts; }
    public static object Throw() { Interlocked.Increment(ref Reads); throw new UnauthorizedAccessException("simulated failed read"); }
    static void Pump() {
        var frame = new DispatcherFrame();
        Dispatcher.CurrentDispatcher.BeginInvoke(DispatcherPriority.Background, new Action(() => frame.Continue = false));
        Dispatcher.PushFrame(frame);
    }
    public static Dictionary<string, object> Run(string exePath) {
        var r = new Dictionary<string, object>();
        var thread = new Thread(() => {
            try {
                var window = (Window)Activator.CreateInstance(Assembly.LoadFrom(exePath).GetType("SortView.Setup.MainWindow", true));
                window.WindowStartupLocation = WindowStartupLocation.Manual; window.Left = -32000; window.Top = -32000; window.ShowActivated = false;
                window.Show(); Pump();
                Func<string, FrameworkElement> find = name => (FrameworkElement)window.FindName(name);
                var checks = find("PrerequisitesView");
                var pageType = checks.GetType();
                var factsType = pageType.Assembly.GetType("SortView.Setup.Wizard.MachineFacts", true);
                Func<string, object> prop = name => pageType.GetProperty(name).GetValue(checks);
                Action<string> source = method => pageType.GetProperty("ReadFacts").SetValue(checks, System.Linq.Expressions.Expression.Lambda(
                    typeof(Func<>).MakeGenericType(factsType),
                    System.Linq.Expressions.Expression.Convert(System.Linq.Expressions.Expression.Call(typeof(NavigationProbe).GetMethod(method)), factsType)).Compile());
                Action<bool, long> setFacts = (admin, free) => {
                    var f = Activator.CreateInstance(factsType);
                    var values = new Dictionary<string, object> {
                        { "WindowsBuild", 19045 }, { "WindowsInstallationType", "Client" }, { "Is64BitOperatingSystem", true },
                        { "DotNetRelease", 533325 }, { "PowerShellVersion", "5.1.19041.1" }, { "IsAdministrator", admin },
                        { "SystemDriveFreeBytes", free }, { "LegacyTasksRead", true }, { "MachinePolicyRead", true }, { "UserPolicyRead", true } };
                    foreach (var pair in values) factsType.GetProperty(pair.Key).SetValue(f, pair.Value);
                    factsType.GetProperty("LegacyTasks").SetValue(f, Array.CreateInstance(factsType.Assembly.GetType("SortView.Setup.Wizard.LegacyTaskFact", true), 0));
                    facts = f;
                };
                Action<string> click = name => { (find(name) ?? (FrameworkElement)checks.FindName(name)).RaiseEvent(new RoutedEventArgs(ButtonBase.ClickEvent)); Pump(); };
                Action wait = () => { for (int i = 0; i < 1000 && (bool)prop("IsChecking"); i++) { Thread.Sleep(10); Pump(); } };
                Func<Dictionary<string, object>> snap = () => {
                    var s = new Dictionary<string, object>();
                    s["page"] = window.GetType().GetProperty("CurrentPage").GetValue(window).ToString();
                    foreach (var name in new[] { "WelcomeView", "PrerequisitesView", "ReadyView", "Wizard" }) s[name] = find(name).IsVisible;
                    foreach (var name in new[] { "BackButton", "NextButton", "ExitButton" }) s[name] = find(name).IsEnabled;
                    s["checking"] = prop("IsChecking"); s["can_continue"] = prop("CanContinue");
                    s["check_again"] = ((FrameworkElement)checks.FindName("CheckAgainButton")).IsEnabled;
                    var statuses = new Dictionary<string, object>();
                    var results = prop("Results") as IEnumerable;
                    if (results != null)
                        foreach (var x in results) statuses[(string)x.GetType().GetProperty("Id").GetValue(x)] = x.GetType().GetProperty("Status").GetValue(x).ToString();
                    s["results"] = results == null ? null : statuses;
                    s["reads"] = Reads;
                    return s;
                };
                var steps = new List<object>();
                Action<string> record = label => { var s = snap(); s["step"] = label; steps.Add(s); };

                record("welcome");
                setFacts(true, 100L << 30); source("Held");
                click("NextButton"); record("checking");          // the read is held open: the UI still pumps
                click("NextButton"); record("next_while_checking"); // refused
                click("CheckAgainButton"); record("again_while_checking"); // ignored: one read at a time
                Gate.Set(); wait(); record("passed");
                setFacts(false, 100L << 30); source("Now");
                click("CheckAgainButton"); wait(); record("failed");
                click("NextButton"); record("next_while_failed"); // refused
                source("Throw");
                click("CheckAgainButton"); wait(); record("read_threw");
                click("NextButton"); record("next_after_read_threw"); // refused
                click("BackButton"); record("back_to_welcome");
                click("NextButton"); record("prerequisites_again"); // no automatic re-read
                setFacts(true, 2L << 30); source("Now");             // 2 GB free: a warning only
                click("CheckAgainButton"); wait(); record("passed_again");
                click("NextButton"); record("ready");
                r["ready_notice_visible"] = ((FrameworkElement)find("ReadyView").FindName("DevelopmentNotice")).IsVisible;
                click("NextButton"); record("setup");
                var wizard = find("Wizard");
                r["start_enabled"] = ((FrameworkElement)wizard.FindName("StartButton")).IsEnabled;
                r["installation_enabled"] = wizard.GetType().GetProperty("IsInstallationEnabled").GetValue(wizard);
                click("BackButton"); record("back_to_ready");
                click("BackButton"); record("back_to_prerequisites");
                click("NextButton"); record("ready_again");          // no re-read going forward again
                click("ExitButton");
                r["closed"] = !window.IsVisible;
                r["steps"] = steps;
            } catch (Exception ex) { r["error"] = ex.ToString(); }
        });
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        thread.Join();
        return r;
    }
}
"@
$report = [NavigationProbe]::Run($ExePath)
$report['children'] = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $PID").Count
ConvertTo-Json -InputObject $report -Depth 6 | Set-Content -LiteralPath $OutPath -Encoding UTF8
"""


@pytest.fixture(scope="module")
def navigation(tmp_path_factory, wizard_dll) -> dict:
    work = tmp_path_factory.mktemp("navigation")
    (work / "probe.ps1").write_text(_NAV_PROBE, encoding="utf-8")
    completed = subprocess.run([_POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                                str(work / "probe.ps1"), "-ExePath", str(wizard_dll.parent / "SortView.Setup.exe"),
                                "-OutPath", str(work / "out.json")], capture_output=True, text=True, timeout=180, check=False)
    assert (work / "out.json").exists(), completed.stdout + completed.stderr
    report = json.loads((work / "out.json").read_text(encoding="utf-8-sig"))
    assert "error" not in report, report.get("error")
    report["by_step"] = {step["step"]: step for step in report["steps"]}
    return report


def _buttons(step: dict) -> tuple:
    return step["BackButton"], step["NextButton"], step["ExitButton"]


PAGE_VIEWS = {"Welcome": "WelcomeView", "Prerequisites": "PrerequisitesView", "Ready": "ReadyView", "Setup": "Wizard"}


@needs_bridge
def test_exactly_one_page_is_visible_at_every_step(navigation):
    for step in navigation["steps"]:
        assert {view for view in PAGE_VIEWS.values() if step[view]} == {PAGE_VIEWS[step["page"]]}, step["step"]


@needs_bridge
def test_welcome_comes_first_with_next_and_exit(navigation):
    welcome = navigation["by_step"]["welcome"]
    assert welcome["page"] == "Welcome"
    assert (welcome["WelcomeView"], welcome["PrerequisitesView"], welcome["ReadyView"], welcome["Wizard"]) == (True, False, False, False)
    assert _buttons(welcome) == (False, True, True)
    assert welcome["results"] is None and welcome["reads"] == 0  # nothing is read before Prerequisites


@needs_bridge
def test_next_is_blocked_before_the_first_result_and_while_checking(navigation):
    steps = navigation["by_step"]
    checking = steps["checking"]
    assert checking["page"] == "Prerequisites" and checking["PrerequisitesView"] is True
    assert checking["checking"] is True and checking["results"] is None
    assert _buttons(checking) == (True, False, True) and checking["check_again"] is False
    assert steps["next_while_checking"]["page"] == "Prerequisites"
    assert steps["again_while_checking"]["reads"] == 1  # a second click did not start a second read
    passed = steps["passed"]
    assert passed["checking"] is False and passed["can_continue"] is True and passed["check_again"] is True
    assert list(passed["results"]) == RESULT_IDS and passed["results"]["administrator"] == "Passed"
    assert _buttons(passed) == (True, True, True)


@needs_bridge
def test_any_failed_result_blocks_next_until_checked_again(navigation):
    steps = navigation["by_step"]
    failed = steps["failed"]
    assert failed["reads"] == 2 and failed["results"]["administrator"] == "Failed"
    assert failed["can_continue"] is False and _buttons(failed) == (True, False, True)
    assert steps["next_while_failed"]["page"] == "Prerequisites"
    warned = steps["passed_again"]
    assert warned["results"]["disk_space"] == "Warning"  # a warning does not block
    assert warned["can_continue"] is True and warned["NextButton"] is True


@needs_bridge
def test_a_read_that_throws_shows_every_check_not_checked_and_blocks_next(navigation):
    threw = navigation["by_step"]["read_threw"]
    assert threw["checking"] is False and threw["check_again"] is True
    assert set(threw["results"].values()) == {"NotChecked"} and list(threw["results"]) == RESULT_IDS
    assert threw["can_continue"] is False and _buttons(threw) == (True, False, True)
    assert navigation["by_step"]["next_after_read_threw"]["page"] == "Prerequisites"


@needs_bridge
def test_back_and_next_move_between_pages_without_re_reading(navigation):
    steps = navigation["by_step"]
    assert steps["back_to_welcome"]["page"] == "Welcome"
    again = steps["prerequisites_again"]
    assert again["page"] == "Prerequisites" and again["reads"] == steps["read_threw"]["reads"]
    assert again["results"] == steps["read_threw"]["results"]  # results kept; Check again re-reads


@needs_bridge
def test_the_setup_page_still_cannot_install(navigation):
    setup = navigation["by_step"]["setup"]
    assert setup["page"] == "Setup" and (setup["WelcomeView"], setup["PrerequisitesView"], setup["ReadyView"], setup["Wizard"]) == (False, False, False, True)
    assert _buttons(setup) == (True, False, True)  # no Next past Setup
    assert navigation["installation_enabled"] is False and navigation["start_enabled"] is False
    assert navigation["closed"] is True and navigation["children"] == 0


@needs_bridge
def test_ready_comes_between_prerequisites_and_setup_without_re_reading(navigation):
    steps = navigation["by_step"]
    reads = steps["passed_again"]["reads"]
    ready = steps["ready"]
    assert ready["page"] == "Ready" and _buttons(ready) == (True, True, True)
    assert navigation["ready_notice_visible"] is True
    assert steps["back_to_ready"]["page"] == "Ready" and _buttons(steps["back_to_ready"]) == (True, True, True)
    assert steps["back_to_prerequisites"]["page"] == "Prerequisites"
    assert steps["back_to_prerequisites"]["results"] == steps["passed_again"]["results"]  # results kept
    assert steps["ready_again"]["page"] == "Ready"
    for name in ("ready", "setup", "back_to_ready", "back_to_prerequisites", "ready_again"):
        assert steps[name]["reads"] == reads, name  # no page after Prerequisites re-reads this computer


#: What the Ready page must say: what setup will do, in the order it does it, and that this build installs nothing.
READY_STATEMENTS = [
    "Ready to install",
    "You're almost ready to install SortView Collector.",
    "When setup runs, it will:",
    "1. Install SortView Collector on this computer.",
    "2. Identify the Tech Logic sorter files the Collector reads, and ask you to confirm them or select them yourself.",
    "3. Ask for the one-time enrollment code from your SortView administrator, only if this computer needs to be enrolled.",
    "4. Offer to resume an earlier setup that was interrupted, using its saved enrollment. No new enrollment code is needed.",
    "5. Verify the installation before the Collector's Scheduled Task is enabled.",
    "Setup requires administrator rights on this computer.",
    ("You can cancel setup at any of its questions until the enrollment code is sent. Once enrollment may have begun, setup "
     "cannot be interrupted and finishes on its own."),
    ("Development build: installation is not enabled in this version. Select Next to see the setup screen. Nothing will be "
     "installed, enrolled or changed on this computer."),
]


def test_the_ready_page_says_what_setup_will_do_and_claims_nothing_was_checked():
    texts = re.findall(r'Text="([^"]*)"', _without_comments(READY_PAGE.read_text(encoding="utf-8")))
    texts = [text.replace("&apos;", "'") for text in texts]
    assert texts == READY_STATEMENTS
    # Nothing about this computer has been verified yet, so the page states no result: no file, package, connection,
    # enrollment or installation is described as found, verified or done.
    shown = " ".join(texts).lower()
    for claim in ("found", "detected", "verified", "has been", "have been", "is installed", "is enrolled", "connected",
                  "passed", "c:\\"):
        assert claim not in shown, claim
