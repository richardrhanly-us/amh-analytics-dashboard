using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using SortView.Setup.Core.Testing;

namespace SortView.Setup.Spike.Simulation
{
    /// <summary>Which simulated server the next run talks to.</summary>
    public enum SimulatedContract
    {
        Legacy,
        ContractV2,
    }

    /// <summary>Where the next simulated run is made to fail, so an interrupted setup can be resumed.</summary>
    public enum SimulatedFault
    {
        None,
        DuringInstall,
        DuringVerification,
    }

    /// <summary>
    /// SIMULATION ONLY: one simulated machine in a scratch folder under %TEMP%\SortViewSetupSimulation. It holds a
    /// GENERATED mock release bundle -- the repository's real setup.ps1 plus stub tools, a runtime that is not an
    /// executable, a manifest listing no files, the simulation marker and an API address on the reserved
    /// sortview-api.invalid host -- the scratch install and data roots, simulated Tech Logic files, and the record folder
    /// of the fakes. Runs go through SimulatedSetupHarness, which checks all of that again and replaces every side-effect
    /// wrapper before the engine runs. There is no way to point this at any other bundle.
    ///
    /// The fakes are the test suite's own (Simulation\*.ps1, kept equal to tests/test_collector_setup.py), with two
    /// changes so no secret ever reaches a file: the simulated token store records only THAT a token was stored, and the
    /// call recorder never writes the enrollment code a request carried.
    /// </summary>
    public sealed class SimulationWorkspace
    {
        public const string SimulationApiUrl = "https://sortview-api.invalid";
        private const string SlowVariable = "SORTVIEW_SIMULATION_SLOW";

        private static readonly string[] SourceFiles = { "Checkins.txt", "Rejects.txt", "ACS Log.txt" };
        private static readonly string[] ModeVariables =
        {
            "SETUPTEST_INSTALL_MODE", "SETUPTEST_FINISH_MODE", "SETUPTEST_CHECK_MODE", "SETUPTEST_TOKEN_MODE", "SETUPTEST_VERIFY_MODE",
            "SETUPTEST_ENABLE_FAILS", "SETUPTEST_PROTECT_FAILS", "SETUPTEST_NO_CODE", SlowVariable,
        };

        private readonly string releaseVersion;
        private readonly string simulatedToken = "SimulatedToken" + Guid.NewGuid().ToString("N");
        private bool codeUsed;

        private SimulationWorkspace(string root, string releaseVersion)
        {
            Root = root;
            this.releaseVersion = releaseVersion;
        }

        public string Root { get; }

        public string BundleRoot => Path.Combine(Root, "bundle");

        public string FakeDir => Path.Combine(Root, "fake");

        public string SourceDir => Path.Combine(Root, "TechLogic");

        public SimulationRequest Request { get; private set; }

        /// <summary>The repository this developer tool runs from (it reads the real setup engine from there).</summary>
        public static string FindRepositoryRoot()
        {
            var directory = new DirectoryInfo(AppDomain.CurrentDomain.BaseDirectory);
            while (directory != null)
            {
                if (File.Exists(Path.Combine(directory.FullName, "collector", "deploy", "setup-collector.ps1")))
                {
                    return directory.FullName;
                }
                directory = directory.Parent;
            }
            throw new InvalidOperationException("The SortView repository (collector\\deploy\\setup-collector.ps1) was not found above this program.");
        }

        public static SimulationWorkspace Create(string repositoryRoot)
        {
            string deploy = Path.Combine(repositoryRoot, "collector", "deploy");
            string version = ReadReleaseVersion(Path.Combine(repositoryRoot, "collector", "__init__.py"));
            string root = Path.Combine(Path.GetTempPath(), "SortViewSetupSimulation", DateTime.Now.ToString("yyyyMMdd-HHmmss") + "-" + Guid.NewGuid().ToString("N").Substring(0, 8));
            var workspace = new SimulationWorkspace(root, version);
            workspace.Build(deploy);
            return workspace;
        }

        /// <summary>Removes this simulated machine's scratch folder.</summary>
        public void Delete()
        {
            if (Directory.Exists(Root))
            {
                Directory.Delete(Root, true);
            }
        }

        /// <summary>Sets the simulated server and the fakes up for the next run. Only the process's own environment is
        /// touched, and only with the fakes' fixed switches and the scratch record folder.</summary>
        public void PrepareRun(SimulatedContract contract, SimulatedFault fault, bool slowSteps, bool standardFilesPresent)
        {
            foreach (string name in ModeVariables)
            {
                Environment.SetEnvironmentVariable(name, null);
            }
            Environment.SetEnvironmentVariable("SETUPTEST_FAKE_DIR", FakeDir);
            if (fault == SimulatedFault.DuringInstall)
            {
                Environment.SetEnvironmentVariable("SETUPTEST_INSTALL_MODE", "fail");
            }
            else if (fault == SimulatedFault.DuringVerification)
            {
                Environment.SetEnvironmentVariable("SETUPTEST_FINISH_MODE", "fail_bootstrap");
            }
            if (slowSteps)
            {
                Environment.SetEnvironmentVariable(SlowVariable, "1");
            }

            foreach (string counter in Directory.GetFiles(FakeDir, "http_count_*.txt"))
            {
                File.Delete(counter);
            }
            File.WriteAllText(Path.Combine(FakeDir, "http.json"), ServerScript(contract), new UTF8Encoding(false));

            foreach (string name in SourceFiles)
            {
                string present = Path.Combine(SourceDir, name), hidden = present + ".hidden";
                if (standardFilesPresent && File.Exists(hidden) && !File.Exists(present))
                {
                    File.Move(hidden, present);
                }
                else if (!standardFilesPresent && File.Exists(present))
                {
                    File.Move(present, hidden);
                }
            }
        }

        /// <summary>The simulated server has enrolled this machine: like the real one, it refuses the code from now on.</summary>
        public void MarkCodeUsed() => codeUsed = true;

        public bool CodeUsed => codeUsed;

        private void Build(string deploy)
        {
            string tools = Path.Combine(BundleRoot, "tools");
            Directory.CreateDirectory(tools);
            Directory.CreateDirectory(Path.Combine(BundleRoot, "runtime", "_internal"));
            Directory.CreateDirectory(FakeDir);
            Directory.CreateDirectory(SourceDir);
            var encoding = new UTF8Encoding(false);

            // The REAL engine, from the repository.
            File.Copy(Path.Combine(deploy, "setup-collector.ps1"), Path.Combine(BundleRoot, "setup.ps1"));
            File.WriteAllBytes(Path.Combine(BundleRoot, "runtime", "SortViewCollector.exe"), Encoding.ASCII.GetBytes("fake-exe"));
            File.WriteAllText(Path.Combine(BundleRoot, "MANIFEST.json"),
                "{\"product\": \"SortView Collector\", \"version\": " + Json(releaseVersion) + ", \"files\": []}", encoding);
            var sources = new StringBuilder();
            string[] names = { "checkins", "rejects", "acs" };
            for (int i = 0; i < names.Length; i++)
            {
                string path = Path.Combine(SourceDir, SourceFiles[i]);
                File.WriteAllText(path, "data\n", encoding);
                sources.Append(i == 0 ? "" : ", ").Append("{\"name\": ").Append(Json(names[i])).Append(", \"path\": ").Append(Json(path)).Append('}');
            }
            File.WriteAllText(Path.Combine(BundleRoot, "collector_config.example.json"),
                "{\"api_url\": " + Json(SimulationApiUrl) + ", \"sources\": [" + sources + "]}", encoding);
            File.WriteAllText(Path.Combine(BundleRoot, SimulatedSetupHarness.MarkerFileName), SimulatedSetupHarness.MarkerText, encoding);

            // Stub tools carrying the REAL parameter blocks (a parameter the real tool lacks fails binding).
            string slow = "if ($env:" + SlowVariable + ") { Start-Sleep -Seconds 3 }\n";
            File.WriteAllText(Path.Combine(BundleRoot, "install.ps1"),
                ParamBlock(Path.Combine(deploy, "install-release.ps1")) + "\n" + slow + Asset("install-stub.ps1"), encoding);
            File.WriteAllText(Path.Combine(tools, "set-api-token.ps1"),
                ParamBlock(Path.Combine(deploy, "set-collector-api-token.ps1")) + "\n" + TokenStubKeepingNoValue(), encoding);
            File.WriteAllText(Path.Combine(tools, "finish-install.ps1"),
                ParamBlock(Path.Combine(deploy, "finish-collector-install.ps1")) + "\n" + slow + Asset("finish-stub.ps1"), encoding);
            File.WriteAllText(Path.Combine(tools, "preflight-system.ps1"), "# stub\n", encoding);
            File.WriteAllText(Path.Combine(tools, "register-task.ps1"), "# stub\n", encoding);

            Request = new SimulationRequest
            {
                WorkingRoot = Root,
                BundleRoot = BundleRoot,
                InstallRoot = Path.Combine(Root, "install"),
                DataRoot = Path.Combine(Root, "data"),
                WrapperOverrides = Overrides(),
            };
        }

        private string Overrides()
        {
            string shared = Asset("wrapper-overrides.ps1")
                .Replace("@@ADMIN@@", "$true")
                .Replace("@@RUNTIME_VERSION@@", releaseVersion)
                .Replace("@@CODE@@", "SIMULATION-CONSOLE-PROMPT-IS-NEVER-USED");
            return "$FakeDir = '" + FakeDir.Replace("'", "''") + "'\n" + shared + "\n" +
                // The window's own call recorder: identical, except an enrollment request's code is never written down.
                "function Add-Call {\n" +
                "    param($Record)\n" +
                "    if ($null -ne $Record.body -and $null -ne $Record.body.PSObject.Properties['enrollment_code']) { $Record.body.enrollment_code = '<not recorded>' }\n" +
                "    Add-Content -LiteralPath \"$FakeDir\\calls.jsonl\" -Value ($Record | ConvertTo-Json -Compress -Depth 6)\n" +
                "}\n";
        }

        private static string TokenStubKeepingNoValue()
        {
            const string keepsValue = "Set-Content -LiteralPath (Join-Path $env:SETUPTEST_FAKE_DIR 'machine_token.txt') -Value $plain -NoNewline";
            string stub = Asset("token-stub.ps1");
            if (!stub.Contains(keepsValue))
            {
                throw new InvalidOperationException("The token stub changed; the simulation window will not run a store that might keep the value.");
            }
            return stub.Replace(keepsValue,
                "Set-Content -LiteralPath (Join-Path $env:SETUPTEST_FAKE_DIR 'machine_token.txt') -Value 'stored (simulated; the value is not kept)' -NoNewline");
        }

        private string ServerScript(SimulatedContract contract)
        {
            string reachable = "{\"status\": 200, \"content\": " + Json("{\"status\": \"SortView API running\"}") + "}";
            string enroll;
            if (codeUsed)
            {
                // A code is used exactly once: a repeat enrollment is refused.
                enroll = "{\"status\": 400, \"content\": \"{}\", \"failure\": \"HTTP 400\"}";
            }
            else if (contract == SimulatedContract.ContractV2)
            {
                enroll = "{\"status\": 200, \"content\": " + Json(EnrollmentBody(true)) + "}";
            }
            else
            {
                string refused = "{\"code\": \"contract_not_permitted\", \"detail\": \"A fixed sentence from the simulated server.\"}";
                enroll = "[{\"status\": 409, \"content\": " + Json(refused) + "}, {\"status\": 200, \"content\": " + Json(EnrollmentBody(false)) + "}]";
            }
            return "{\"Get /\": " + reachable + ", \"Post /collector/enroll\": " + enroll + "}";
        }

        private string EnrollmentBody(bool contractV2)
        {
            string body = "{\"customer_id\": 7, \"branch_id\": 3, \"installation_id\": 41, \"agent_token\": " + Json(simulatedToken);
            if (contractV2)
            {
                body += ", \"contract_version\": 2, \"contract_mode\": \"v2\", \"key_id\": \"3f2b8c1e-4d5a-4b6c-8d7e-9f0a1b2c3d4e\"" +
                        ", \"timezone\": \"America/New_York\", \"classification_rules\": {\"schema_version\": 1" +
                        ", \"destinations\": [{\"slug\": \"simulated_annex\", \"contains\": \"SIMULATED ANNEX\"}]" +
                        ", \"branch_services_names\": [\"SIMULATED BRANCH ACCOUNT\"], \"collection_services_names\": [\"SIMULATED COLLECTION ACCOUNT\"]" +
                        ", \"branch_services_da_patterns\": [], \"collection_services_da_patterns\": [\"SIMULATED PATTERN\"]}";
            }
            return body + "}";
        }

        private static string ParamBlock(string script)
        {
            string text = File.ReadAllText(script).Replace("\r\n", "\n");
            Match block = Regex.Match(text, @"^\[CmdletBinding\(\)\]\s*\nparam\(.*?^\)", RegexOptions.Multiline | RegexOptions.Singleline);
            if (!block.Success)
            {
                throw new InvalidOperationException("No parameter block in " + Path.GetFileName(script) + ".");
            }
            return block.Value;
        }

        private static string Asset(string name)
        {
            using (Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("Simulation." + name))
            {
                if (stream == null)
                {
                    throw new InvalidOperationException("The simulation asset " + name + " is missing from this build.");
                }
                using (var reader = new StreamReader(stream, Encoding.UTF8))
                {
                    return reader.ReadToEnd().Replace("\r\n", "\n");
                }
            }
        }

        private static string ReadReleaseVersion(string initFile)
        {
            Match version = Regex.Match(File.ReadAllText(initFile), "__version__\\s*=\\s*\"([^\"]+)\"");
            if (!version.Success)
            {
                throw new InvalidOperationException("collector.__version__ was not found.");
            }
            return version.Groups[1].Value;
        }

        private static string Json(string value)
        {
            var escaped = new StringBuilder("\"");
            foreach (char c in value)
            {
                switch (c)
                {
                    case '"': escaped.Append("\\\""); break;
                    case '\\': escaped.Append("\\\\"); break;
                    case '\n': escaped.Append("\\n"); break;
                    case '\r': escaped.Append("\\r"); break;
                    case '\t': escaped.Append("\\t"); break;
                    default:
                        if (c < 0x20)
                        {
                            escaped.Append("\\u").Append(((int)c).ToString("x4"));
                        }
                        else
                        {
                            escaped.Append(c);
                        }
                        break;
                }
            }
            return escaped.Append('"').ToString();
        }

        /// <summary>Every file of this simulated machine, for inspection.</summary>
        public IEnumerable<string> AllFiles() => Directory.Exists(Root) ? Directory.GetFiles(Root, "*", SearchOption.AllDirectories) : new string[0];
    }
}
