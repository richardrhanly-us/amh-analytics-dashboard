using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Security.Principal;
using Microsoft.Win32;

namespace SortView.Setup.Wizard
{
    public enum PrerequisiteStatus
    {
        Passed,
        Warning,
        Failed,
        NotChecked,
    }

    public enum PrerequisiteKind
    {
        /// <summary>The machine must be shown to meet it: Failed or Not checked blocks Next.</summary>
        Required,
        /// <summary>Worth knowing, but this inspection cannot rule everything out: only Failed would block.</summary>
        Advisory,
        /// <summary>Checked later by the setup engine itself; shown here as Not checked, never blocking.</summary>
        Deferred,
    }

    /// <summary>One prerequisite's result, in words for library staff. See <see cref="BlocksNext"/>.</summary>
    public sealed class PrerequisiteResult
    {
        public PrerequisiteResult(string id, string title, PrerequisiteKind kind, PrerequisiteStatus status, string detail, string resolution)
        {
            Id = id;
            Title = title;
            Kind = kind;
            Status = status;
            Detail = detail;
            Resolution = resolution;
        }

        public string Id { get; }

        public string Title { get; }

        public PrerequisiteKind Kind { get; }

        public PrerequisiteStatus Status { get; }

        public string StatusLabel => Status == PrerequisiteStatus.NotChecked ? "Not checked" : Status.ToString();

        public string Detail { get; }

        /// <summary>What to do about it, or empty.</summary>
        public string Resolution { get; }

        /// <summary>Any Failed result, and a Required one that could not be verified.</summary>
        public bool BlocksNext => Status == PrerequisiteStatus.Failed ||
                                  (Kind == PrerequisiteKind.Required && Status == PrerequisiteStatus.NotChecked);
    }

    /// <summary>A legacy SortView Scheduled Task, as Task Scheduler reported it. Null = could not be determined.</summary>
    public sealed class LegacyTaskFact
    {
        public string Name { get; set; }

        public bool? Present { get; set; }

        public bool? Enabled { get; set; }
    }

    /// <summary>
    /// What the machine reported, read-only. Every value may be NULL, meaning "unknown or unreadable" -- the rules then
    /// say Not checked, never Passed. Public and settable so the rules can be tested with any combination.
    /// </summary>
    public sealed class MachineFacts
    {
        public static readonly string[] LegacyTaskNames = { "sortview-scheduler", "SortView Canonical Agent" };
        public const string LegacyAgentFolder = @"C:\SortViewAgent";

        public int? WindowsBuild { get; set; }

        public int? WindowsUbr { get; set; }

        public string WindowsEditionId { get; set; }

        /// <summary>"Client" or "Server" (registry InstallationType).</summary>
        public string WindowsInstallationType { get; set; }

        public string WindowsDisplayVersion { get; set; }

        public bool? Is64BitOperatingSystem { get; set; }

        /// <summary>.NET Framework 4.x "Release" value; null when missing or unreadable.</summary>
        public int? DotNetRelease { get; set; }

        /// <summary>Windows PowerShell engine version (e.g. "5.1.19041.1"); null when missing or unreadable.</summary>
        public string PowerShellVersion { get; set; }

        public bool? IsAdministrator { get; set; }

        public string SystemDrive { get; set; }

        public long? SystemDriveFreeBytes { get; set; }

        /// <summary>Whether Task Scheduler could be read at all.</summary>
        public bool LegacyTasksRead { get; set; }

        public LegacyTaskFact[] LegacyTasks { get; set; }

        public bool? LegacyAgentFolderPresent { get; set; }

        /// <summary>Whether the machine (HKLM) Group Policy PowerShell key could be read (absent counts as read).</summary>
        public bool MachinePolicyRead { get; set; }

        /// <summary>Group Policy "Turn on Script Execution" at machine level: its ExecutionPolicy, or "Restricted" when it is
        /// turned off; null when not configured.</summary>
        public string MachinePolicy { get; set; }

        public bool UserPolicyRead { get; set; }

        public string UserPolicy { get; set; }

        /// <summary>Everything unknown: what a failed read reports.</summary>
        public static MachineFacts Unknown() => new MachineFacts();

        /// <summary>
        /// Reads this machine, READ-ONLY: registry keys opened without write access, Task Scheduler asked only for tasks by
        /// name, the drive's free space, and this process's own token. Starts no process and runs no PowerShell. Each fact is
        /// read on its own; one that fails is left unknown.
        /// </summary>
        public static MachineFacts Read()
        {
            var facts = new MachineFacts();
            Try(() =>
            {
                using (RegistryKey key = LocalMachine().OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion", false))
                {
                    if (key == null)
                    {
                        return;
                    }
                    if (int.TryParse(key.GetValue("CurrentBuildNumber") as string, NumberStyles.Integer, CultureInfo.InvariantCulture, out int build))
                    {
                        facts.WindowsBuild = build;
                    }
                    if (key.GetValue("UBR") is int ubr)
                    {
                        facts.WindowsUbr = ubr;
                    }
                    facts.WindowsEditionId = key.GetValue("EditionID") as string;
                    facts.WindowsInstallationType = key.GetValue("InstallationType") as string;
                    facts.WindowsDisplayVersion = (key.GetValue("DisplayVersion") ?? key.GetValue("ReleaseId")) as string;
                }
            });
            Try(() => facts.Is64BitOperatingSystem = Environment.Is64BitOperatingSystem);
            Try(() =>
            {
                using (RegistryKey key = LocalMachine().OpenSubKey(@"SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full", false))
                {
                    if (key?.GetValue("Release") is int release)
                    {
                        facts.DotNetRelease = release;
                    }
                }
            });
            Try(() =>
            {
                using (RegistryKey key = LocalMachine().OpenSubKey(@"SOFTWARE\Microsoft\PowerShell\3\PowerShellEngine", false))
                {
                    facts.PowerShellVersion = key?.GetValue("PowerShellVersion") as string;
                }
            });
            Try(() =>
            {
                using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
                {
                    facts.IsAdministrator = new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);
                }
            });
            Try(() =>
            {
                string root = Path.GetPathRoot(Environment.GetFolderPath(Environment.SpecialFolder.Windows));
                facts.SystemDrive = root;
                facts.SystemDriveFreeBytes = new DriveInfo(root).AvailableFreeSpace;
            });
            Try(() => ReadLegacyTasks(facts));
            Try(() => facts.LegacyAgentFolderPresent = Directory.Exists(LegacyAgentFolder));
            Try(() =>
            {
                facts.MachinePolicy = ReadScriptPolicy(LocalMachine());
                facts.MachinePolicyRead = true;
            });
            Try(() =>
            {
                facts.UserPolicy = ReadScriptPolicy(RegistryKey.OpenBaseKey(RegistryHive.CurrentUser, RegistryView.Default));
                facts.UserPolicyRead = true;
            });
            return facts;
        }

        private static RegistryKey LocalMachine() => RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64);

        /// <summary>The Group Policy script setting under one hive's Policies key, or null when it is not configured.</summary>
        private static string ReadScriptPolicy(RegistryKey hive)
        {
            using (hive)
            using (RegistryKey key = hive.OpenSubKey(@"SOFTWARE\Policies\Microsoft\Windows\PowerShell", false))
            {
                if (key == null)
                {
                    return null;
                }
                if (key.GetValue("EnableScripts") is int enabled && enabled == 0)
                {
                    return "Restricted"; // "Turn on Script Execution" set to Disabled
                }
                return key.GetValue("ExecutionPolicy") as string;
            }
        }

        /// <summary>Asks Task Scheduler for each legacy task BY NAME, read-only: Connect, GetFolder, GetTask, and the
        /// task's Enabled value. Nothing is registered, changed or deleted.</summary>
        private static void ReadLegacyTasks(MachineFacts facts)
        {
            Type serviceType = Type.GetTypeFromProgID("Schedule.Service", false);
            if (serviceType == null)
            {
                return;
            }
            object service = Activator.CreateInstance(serviceType);
            Call(service, "Connect");
            object root = Call(service, "GetFolder", "\\");
            var tasks = new List<LegacyTaskFact>();
            foreach (string name in LegacyTaskNames)
            {
                var fact = new LegacyTaskFact { Name = name };
                try
                {
                    object task = Call(root, "GetTask", name);
                    fact.Present = true;
                    fact.Enabled = Get(task, "Enabled") as bool?;
                }
                catch (TargetInvocationException ex) when (IsNotFound(ex.InnerException))
                {
                    fact.Present = false;
                }
                catch (Exception)
                {
                    // Indeterminate: Present and Enabled stay unknown.
                }
                tasks.Add(fact);
            }
            facts.LegacyTasks = tasks.ToArray();
            facts.LegacyTasksRead = true;
        }

        private static bool IsNotFound(Exception ex)
        {
            const int FileNotFound = unchecked((int)0x80070002);
            const int PathNotFound = unchecked((int)0x80070003);
            return ex != null && (ex.HResult == FileNotFound || ex.HResult == PathNotFound);
        }

        private static object Call(object target, string method, params object[] args) =>
            target.GetType().InvokeMember(method, BindingFlags.InvokeMethod, null, target, args, CultureInfo.InvariantCulture);

        private static object Get(object target, string property) =>
            target.GetType().InvokeMember(property, BindingFlags.GetProperty, null, target, null, CultureInfo.InvariantCulture);

        private static void Try(Action read)
        {
            try
            {
                read();
            }
            catch (Exception)
            {
                // That fact stays unknown: its rule says Not checked.
            }
        }
    }

    /// <summary>
    /// PURE: turns machine facts into results for library staff. Never says Passed without a fact that shows it; anything
    /// unknown is Not checked. Thresholds and minimums are provisional (see each rule).
    /// </summary>
    public static class PrerequisiteRules
    {
        /// <summary>Provisional minimum: Windows 10 version 1809 (build 17763), the NBPL AMH PC's version.</summary>
        public const int MinimumWindowsBuild = 17763;
        public const int Windows11FirstBuild = 22000;
        /// <summary>.NET Framework 4.8.</summary>
        public const int MinimumDotNetRelease = 528040;
        /// <summary>Provisional: to be validated against the real package size and temporary extraction space before release.</summary>
        public const long FailBelowBytes = 1L * 1024 * 1024 * 1024;
        public const long WarnBelowBytes = 5L * 1024 * 1024 * 1024;

        /// <summary>Advisory: Group Policy is only one of the controls that can stop scripts (AppLocker, WDAC...).</summary>
        private static readonly string[] AdvisoryIds = { "script_policy" };

        private const string UnknownRequiredResolution =
            "Select Check again. If this keeps happening, contact your IT administrator or SortView support.";

        private static readonly string[] NonRestrictivePolicies = { "Unrestricted", "Bypass" };
        private static readonly string[] RestrictivePolicies = { "Restricted", "AllSigned", "RemoteSigned", "Default" };

        public static IReadOnlyList<PrerequisiteResult> Evaluate(MachineFacts facts)
        {
            if (facts == null)
            {
                facts = MachineFacts.Unknown();
            }
            return new[]
            {
                Windows(facts),
                Architecture(facts),
                DotNet(facts),
                PowerShell(facts),
                Administrator(facts),
                DiskSpace(facts),
                LegacyTasks(facts),
                ScriptPolicy(facts),
                Deferred("existing_collector", "Existing SortView Collector",
                    "Setup checks for an existing installation itself, before it changes anything."),
                Deferred("installation_package", "Installation package",
                    "Setup verifies the installation package's files before it trusts any of them. (A signed single download comes later.)"),
                Deferred("connection", "Connection to SortView",
                    "Setup checks the connection to SortView just before asking for the enrollment code."),
                Deferred("tech_logic_files", "Tech Logic files",
                    "You choose and confirm the Tech Logic files in a later step; setup checks they exist."),
            };
        }

        private static PrerequisiteResult Windows(MachineFacts f)
        {
            const string id = "windows_version", title = "Windows version";
            if (f.WindowsBuild == null)
            {
                return Result(id, title, PrerequisiteStatus.NotChecked, "The Windows version could not be read.", "");
            }
            int build = f.WindowsBuild.Value;
            string name = (build >= Windows11FirstBuild ? "Windows 11" : "Windows 10") +
                (string.IsNullOrEmpty(f.WindowsDisplayVersion) ? "" : " " + f.WindowsDisplayVersion) +
                " (build " + build + (f.WindowsUbr == null ? "" : "." + f.WindowsUbr) + ")";
            bool server = (f.WindowsInstallationType ?? "").IndexOf("Server", StringComparison.OrdinalIgnoreCase) >= 0 ||
                          (f.WindowsEditionId ?? "").IndexOf("Server", StringComparison.OrdinalIgnoreCase) >= 0;
            if (build < MinimumWindowsBuild)
            {
                return Result(id, title, PrerequisiteStatus.Failed,
                    "This computer runs build " + build + ", older than the minimum (Windows 10 version 1809, build 17763).",
                    "Update Windows to version 1809 or later, or install on a newer computer.");
            }
            if (server)
            {
                return Result(id, title, PrerequisiteStatus.Warning,
                    "This is a Windows Server edition (build " + build + "). SortView has not been validated on Windows Server.",
                    "You can continue, but contact SortView support before relying on it.");
            }
            if (f.WindowsInstallationType == null)
            {
                return Result(id, title, PrerequisiteStatus.NotChecked,
                    "Build " + build + " meets the minimum, but whether this is a client or server edition could not be read.", "");
            }
            return Result(id, title, PrerequisiteStatus.Passed,
                name + " meets the provisional minimum (Windows 10 version 1809). Release testing on this version is still to be done.", "");
        }

        private static PrerequisiteResult Architecture(MachineFacts f)
        {
            const string id = "architecture", title = "64-bit Windows";
            switch (f.Is64BitOperatingSystem)
            {
                case true: return Result(id, title, PrerequisiteStatus.Passed, "This is 64-bit Windows.", "");
                case false:
                    return Result(id, title, PrerequisiteStatus.Failed, "This is 32-bit Windows. SortView Collector needs 64-bit Windows.",
                        "Install on a computer running 64-bit Windows.");
                default: return Result(id, title, PrerequisiteStatus.NotChecked, "Whether Windows is 64-bit could not be determined.", "");
            }
        }

        private static PrerequisiteResult DotNet(MachineFacts f)
        {
            const string id = "dotnet", title = ".NET Framework 4.8";
            const string later = " (Checking this before SortView Setup even starts is part of the future installer package.)";
            if (f.DotNetRelease == null)
            {
                return Result(id, title, PrerequisiteStatus.NotChecked, "The installed .NET Framework version could not be read." + later, "");
            }
            if (f.DotNetRelease.Value >= MinimumDotNetRelease)
            {
                return Result(id, title, PrerequisiteStatus.Passed, ".NET Framework 4.8 or later is installed.", "");
            }
            return Result(id, title, PrerequisiteStatus.Failed, "An older .NET Framework is installed (release " + f.DotNetRelease + ")." + later,
                "Install .NET Framework 4.8 from Microsoft (or through Windows Update), then run SortView Setup again.");
        }

        private static PrerequisiteResult PowerShell(MachineFacts f)
        {
            const string id = "powershell", title = "Windows PowerShell 5.1";
            if (string.IsNullOrWhiteSpace(f.PowerShellVersion) || !Version.TryParse(f.PowerShellVersion.Trim(), out Version version))
            {
                return Result(id, title, PrerequisiteStatus.NotChecked, "The Windows PowerShell version could not be read.", "");
            }
            if (version.Major == 5 && version.Minor >= 1)
            {
                return Result(id, title, PrerequisiteStatus.Passed, "Windows PowerShell " + version + " is installed.", "");
            }
            return Result(id, title, PrerequisiteStatus.Failed, "Windows PowerShell " + version + " is installed; SortView setup needs version 5.1.",
                "Install Windows Management Framework 5.1 from Microsoft, then run SortView Setup again.");
        }

        private static PrerequisiteResult Administrator(MachineFacts f)
        {
            const string id = "administrator", title = "Administrator rights";
            switch (f.IsAdministrator)
            {
                case true: return Result(id, title, PrerequisiteStatus.Passed, "SortView Setup is running with administrator rights.", "");
                case false:
                    return Result(id, title, PrerequisiteStatus.Failed, "SortView Setup is not running with administrator rights.",
                        "Close SortView Setup, start it again, and approve the Windows administrator prompt (or ask your IT administrator).");
                default: return Result(id, title, PrerequisiteStatus.NotChecked, "Whether SortView Setup has administrator rights could not be determined.", "");
            }
        }

        private static PrerequisiteResult DiskSpace(MachineFacts f)
        {
            const string id = "disk_space", title = "Free disk space";
            const string provisional = " (Provisional limits, to be confirmed against the final installation package.)";
            if (f.SystemDriveFreeBytes == null)
            {
                return Result(id, title, PrerequisiteStatus.NotChecked, "The free space on the system drive could not be read.", "");
            }
            long free = f.SystemDriveFreeBytes.Value;
            string where = (f.SystemDrive ?? "the system drive") + " has " + Gigabytes(free) + " free.";
            if (free < FailBelowBytes)
            {
                return Result(id, title, PrerequisiteStatus.Failed, where + " At least 1 GB is needed." + provisional,
                    "Free up space on " + (f.SystemDrive ?? "the system drive") + ", then select Check again.");
            }
            if (free < WarnBelowBytes)
            {
                return Result(id, title, PrerequisiteStatus.Warning, where + " 5 GB or more is recommended for logs and updates." + provisional,
                    "You can continue; consider freeing some space.");
            }
            return Result(id, title, PrerequisiteStatus.Passed, where + provisional, "");
        }

        private static PrerequisiteResult LegacyTasks(MachineFacts f)
        {
            const string id = "legacy_tasks", title = "Older SortView agent";
            if (!f.LegacyTasksRead || f.LegacyTasks == null)
            {
                return Result(id, title, PrerequisiteStatus.NotChecked, "Task Scheduler could not be read to look for an older SortView agent.", "");
            }
            var enabled = new List<string>();
            var disabled = new List<string>();
            foreach (LegacyTaskFact task in f.LegacyTasks)
            {
                if (task.Present == null || (task.Present == true && task.Enabled == null))
                {
                    return Result(id, title, PrerequisiteStatus.NotChecked,
                        "Whether the older Scheduled Task '" + task.Name + "' exists and is enabled could not be determined.", "");
                }
                if (task.Present == true)
                {
                    (task.Enabled == true ? enabled : disabled).Add("'" + task.Name + "'");
                }
            }
            string folder = f.LegacyAgentFolderPresent == true ? " The older agent's folder " + MachineFacts.LegacyAgentFolder + " is still present." : "";
            if (enabled.Count > 0)
            {
                return Result(id, title, PrerequisiteStatus.Failed,
                    "An older SortView agent is still scheduled to run: " + string.Join(", ", enabled) + " is enabled." + folder +
                    " Running it alongside the new Collector could send duplicate or conflicting data.",
                    "Contact SortView support before installing; they will help move this computer to the new Collector.");
            }
            string detail = disabled.Count > 0
                ? "The older SortView task " + string.Join(", ", disabled) + " is present but disabled, so it does not run."
                : "No older SortView agent task is scheduled.";
            return Result(id, title, PrerequisiteStatus.Passed, detail + folder, "");
        }

        private static PrerequisiteResult ScriptPolicy(MachineFacts f)
        {
            const string id = "script_policy", title = "PowerShell script policy";
            const string scope = " Only the Group Policy setting was checked; other controls (such as AppLocker) were not.";
            if (!f.MachinePolicyRead)
            {
                return Result(id, title, PrerequisiteStatus.NotChecked, "The computer's Group Policy script setting could not be read.", "");
            }
            // Group Policy for the computer takes precedence over Group Policy for the user.
            string level = f.MachinePolicy != null ? "computer" : "user";
            string policy = f.MachinePolicy;
            if (policy == null)
            {
                if (!f.UserPolicyRead)
                {
                    return Result(id, title, PrerequisiteStatus.NotChecked,
                        "No computer Group Policy script setting is set, but the user's Group Policy setting could not be read.", "");
                }
                policy = f.UserPolicy;
            }
            if (policy == null)
            {
                return Result(id, title, PrerequisiteStatus.Passed,
                    "No Group Policy restricts PowerShell scripts, so setup can use its own setting for its scripts." + scope, "");
            }
            if (Array.Exists(NonRestrictivePolicies, p => string.Equals(p, policy, StringComparison.OrdinalIgnoreCase)))
            {
                return Result(id, title, PrerequisiteStatus.Passed,
                    "Group Policy (" + level + ") sets PowerShell scripts to " + policy + ", which does not block setup's scripts." + scope, "");
            }
            if (Array.Exists(RestrictivePolicies, p => string.Equals(p, policy, StringComparison.OrdinalIgnoreCase)))
            {
                return Result(id, title, PrerequisiteStatus.Warning,
                    "Group Policy (" + level + ") restricts PowerShell scripts (" + policy + "). It may stop SortView's setup scripts, which are not yet signed." + scope,
                    "Ask your IT administrator to allow SortView's setup, or wait for the signed SortView installer.");
            }
            return Result(id, title, PrerequisiteStatus.NotChecked,
                "Group Policy (" + level + ") sets PowerShell scripts to an unrecognised value ('" + policy + "').", "");
        }

        private static PrerequisiteResult Deferred(string id, string title, string detail) =>
            new PrerequisiteResult(id, title, PrerequisiteKind.Deferred, PrerequisiteStatus.NotChecked, detail, "");

        /// <summary>A machine check's result: Required unless listed as advisory. A Required check that could not be
        /// verified blocks Next, so it always says what to do.</summary>
        private static PrerequisiteResult Result(string id, string title, PrerequisiteStatus status, string detail, string resolution)
        {
            PrerequisiteKind kind = Array.IndexOf(AdvisoryIds, id) >= 0 ? PrerequisiteKind.Advisory : PrerequisiteKind.Required;
            if (kind == PrerequisiteKind.Required && status == PrerequisiteStatus.NotChecked && string.IsNullOrEmpty(resolution))
            {
                resolution = UnknownRequiredResolution;
            }
            return new PrerequisiteResult(id, title, kind, status, detail, resolution);
        }

        private static string Gigabytes(long bytes) =>
            (bytes / (1024.0 * 1024 * 1024)).ToString("0.0", CultureInfo.InvariantCulture) + " GB";
    }
}
