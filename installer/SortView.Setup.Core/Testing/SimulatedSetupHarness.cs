using System;
using System.IO;
using System.Management.Automation;
using System.Management.Automation.Runspaces;
using System.Text.RegularExpressions;
using System.Threading;

namespace SortView.Setup.Core.Testing
{
    /// <summary>A simulated run: a generated mock bundle, scratch roots, and the wrapper overrides to run with.</summary>
    public sealed class SimulationRequest
    {
        /// <summary>The scratch folder everything below must live in; itself under the user's temp folder.</summary>
        public string WorkingRoot { get; set; }

        public string BundleRoot { get; set; }

        public string InstallRoot { get; set; }

        public string DataRoot { get; set; }

        /// <summary>PowerShell that replaces every real side-effect wrapper with a fake (the test suite's shared
        /// override block). Run after the engine is loaded and before Invoke-SetupMain; then checked.</summary>
        public string WrapperOverrides { get; set; }

        /// <summary>For testing the host: every event callback throws.</summary>
        public bool ThrowFromEventCallback { get; set; }
    }

    public sealed class SimulationRefusedException : Exception
    {
        public SimulationRefusedException(string message) : base(message)
        {
        }
    }

    /// <summary>
    /// TEST-ONLY: runs the real engine orchestration (Invoke-SetupMain) through <see cref="SetupEngineHost"/> with every
    /// real side effect simulated. It REFUSES, before anything runs, unless:
    ///  - the bundle is a generated mock: the simulation marker file, a runtime that is not an executable, a manifest
    ///    that lists no files, and stub install/token/finish tools;
    ///  - the bundle's API address is a reserved .invalid host (never a SortView endpoint);
    ///  - the bundle, install and data roots are inside a scratch folder under the user's temp folder, and the stubs'
    ///    record folder (SETUPTEST_FAKE_DIR) is too;
    ///  - after the overrides ran, every side-effect wrapper -- the admin check, HTTP, the runtime, the token store,
    ///    the Scheduled Task, folder ACLs and both console prompts -- is no longer the one setup.ps1 defined.
    /// Production code cannot reach the pre-run seam this uses (SetupEngineHost.RunCore is internal; see AssemblyInfo.cs).
    /// </summary>
    public static class SimulatedSetupHarness
    {
        public const string MarkerFileName = "SORTVIEW-SIMULATION-BUNDLE.txt";
        public const string MarkerText = "Generated SortView setup simulation bundle. It installs nothing.";

        /// <summary>Every wrapper through which setup.ps1 touches the machine, the network or the console.</summary>
        public static readonly string[] SimulatedWrappers =
        {
            "Test-IsAdministrator", "Get-LocalHostName", "Get-RuntimeVersion", "Test-ApiTokenStored", "Invoke-ApiTokenCheck",
            "Get-SortViewTask", "Enable-SortViewTask", "Disable-SortViewTask", "Protect-SetupRecoveryDirectory",
            "Invoke-SetupHttp", "Read-SetupAnswer", "Read-EnrollmentCodeSecure",
        };

        public static SetupRunResult Run(SimulationRequest request, IPromptPresenter presenter, CancellationToken cancellationToken)
        {
            EnsureSimulationOnly(request);
            Action<SetupEvent> onEvent = null;
            if (request.ThrowFromEventCallback)
            {
                onEvent = setupEvent => throw new InvalidOperationException("simulated event callback failure");
            }
            var run = new SetupRunRequest { BundleRoot = request.BundleRoot, InstallRoot = request.InstallRoot, DataRoot = request.DataRoot };
            return new SetupEngineHost().RunCore(run, presenter, onEvent, null, cancellationToken,
                (runspace, scriptPath) => ApplyOverrides(runspace, scriptPath, request.WrapperOverrides));
        }

        private static void EnsureSimulationOnly(SimulationRequest request)
        {
            if (request == null)
            {
                throw new ArgumentNullException(nameof(request));
            }
            string temp = Full(Path.GetTempPath());
            string working = Full(Required(request.WorkingRoot, "WorkingRoot"));
            Within(working, temp, "WorkingRoot", "the user's temp folder");
            string bundle = Full(Required(request.BundleRoot, "BundleRoot"));
            Within(bundle, working, "BundleRoot", "WorkingRoot");
            Within(Full(Required(request.InstallRoot, "InstallRoot")), working, "InstallRoot", "WorkingRoot");
            Within(Full(Required(request.DataRoot, "DataRoot")), working, "DataRoot", "WorkingRoot");
            Within(Full(Required(Environment.GetEnvironmentVariable("SETUPTEST_FAKE_DIR"), "SETUPTEST_FAKE_DIR")), working,
                "SETUPTEST_FAKE_DIR", "WorkingRoot");
            if (string.IsNullOrWhiteSpace(request.WrapperOverrides))
            {
                Refuse("no wrapper overrides were given");
            }

            string marker = Path.Combine(bundle, MarkerFileName);
            if (!File.Exists(marker) || File.ReadAllText(marker).Trim() != MarkerText)
            {
                Refuse("the bundle is not a generated simulation bundle (no simulation marker)");
            }
            string runtime = Path.Combine(bundle, "runtime", "SortViewCollector.exe");
            if (!File.Exists(runtime))
            {
                Refuse("the bundle has no simulated runtime");
            }
            byte[] head = new byte[2];
            using (FileStream stream = File.OpenRead(runtime))
            {
                if (stream.Read(head, 0, 2) == 2 && head[0] == (byte)'M' && head[1] == (byte)'Z')
                {
                    Refuse("the bundle's runtime is a real executable");
                }
            }
            if (!Regex.IsMatch(ReadBundleFile(bundle, "MANIFEST.json"), "\"files\"\\s*:\\s*\\[\\s*\\]"))
            {
                Refuse("the bundle's manifest lists files (a real release bundle)");
            }
            RequireStub(bundle, "install.ps1", "STUB install.ps1");
            RequireStub(bundle, Path.Combine("tools", "set-api-token.ps1"), "STUB set-api-token");
            RequireStub(bundle, Path.Combine("tools", "finish-install.ps1"), "STUB finish-install.ps1");
            Match api = Regex.Match(ReadBundleFile(bundle, "collector_config.example.json"), "\"api_url\"\\s*:\\s*\"https://([^\"/:]+)");
            if (!api.Success || !api.Groups[1].Value.EndsWith(".invalid", StringComparison.OrdinalIgnoreCase))
            {
                Refuse("the bundle's API address is not a reserved .invalid host");
            }
        }

        private static void ApplyOverrides(Runspace runspace, string scriptPath, string overrides)
        {
            using (PowerShell ps = PowerShell.Create())
            {
                ps.Runspace = runspace;
                ps.AddScript(overrides);
                ps.Invoke();
                if (ps.Streams.Error.Count > 0)
                {
                    Refuse("the wrapper overrides failed: " + ps.Streams.Error[0]);
                }
            }
            foreach (string name in SimulatedWrappers)
            {
                var function = runspace.SessionStateProxy.InvokeCommand.GetCommand(name, CommandTypes.Function) as FunctionInfo;
                if (function == null)
                {
                    Refuse("the wrapper " + name + " is not defined");
                }
                string definedIn = function.ScriptBlock.File;
                if (!string.IsNullOrEmpty(definedIn))
                {
                    Refuse("the wrapper " + name + " is still the real one (defined in " + Path.GetFileName(definedIn) + ")");
                }
            }
        }

        private static void RequireStub(string bundle, string relative, string stubMarker)
        {
            if (!ReadBundleFile(bundle, relative).Contains(stubMarker))
            {
                Refuse("the bundle's " + relative + " is not a simulation stub");
            }
        }

        private static string ReadBundleFile(string bundle, string relative)
        {
            string path = Path.Combine(bundle, relative);
            if (!File.Exists(path))
            {
                Refuse("the bundle has no " + relative);
            }
            return File.ReadAllText(path);
        }

        private static string Required(string value, string name)
        {
            if (string.IsNullOrWhiteSpace(value) || !Path.IsPathRooted(value))
            {
                Refuse(name + " must be an absolute path");
            }
            return value;
        }

        private static string Full(string path) => Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar);

        private static void Within(string path, string root, string name, string rootName)
        {
            if (!path.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            {
                Refuse(name + " must be inside " + rootName);
            }
        }

        private static void Refuse(string reason)
        {
            throw new SimulationRefusedException("Simulation refused: " + reason + ".");
        }
    }
}
