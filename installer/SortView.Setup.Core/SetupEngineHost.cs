using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.IO;
using System.Management.Automation;
using System.Management.Automation.Runspaces;
using System.Threading;

namespace SortView.Setup.Core
{
    /// <summary>
    /// Runs the SortView setup engine (a release bundle's setup.ps1) in-process in Windows PowerShell 5.1.
    ///
    /// Each run gets a NEW runspace: the engine is dot-sourced from the bundle, structured events are turned on
    /// (Enable-SetupEvents), a <see cref="PromptBridge"/> is registered as its prompt handler
    /// (Register-SetupPromptHandler), and Invoke-SetupMain runs. The runspace -- and with it the handler and the event
    /// switch -- is closed and disposed when the run ends, so nothing carries over to a later run; the bridge then
    /// disposes every code the presenter handed over. The engine itself decides everything; this class only hosts it.
    ///
    /// <see cref="Run"/> is synchronous and blocks the calling thread for the whole setup: call it from a background
    /// thread. Events and console text are delivered on the engine's pipeline thread.
    /// </summary>
    public sealed class SetupEngineHost
    {
        public const string EventTag = "SortView.Setup.Event";
        public const string ConsoleTextTag = "PSHOST";
        public const string SetupScriptFileName = "setup.ps1";

        /// <summary>Runs setup from <paramref name="request"/>'s bundle. There is deliberately no way to run any other
        /// PowerShell. Cancelling <paramref name="cancellationToken"/> only ever cancels a question being asked (the
        /// engine then stops safely); it never interrupts a step that is running.</summary>
        public SetupRunResult Run(SetupRunRequest request, IPromptPresenter presenter, Action<SetupEvent> onEvent,
            Action<string> onConsoleText, CancellationToken cancellationToken)
        {
            return RunCore(request, presenter, onEvent, onConsoleText, cancellationToken, null);
        }

        /// <summary>The run itself. <paramref name="beforeRun"/> is called in the loaded runspace right before
        /// Invoke-SetupMain; it is reachable only from the (unshipped) simulation harness -- see AssemblyInfo.cs.</summary>
        internal SetupRunResult RunCore(SetupRunRequest request, IPromptPresenter presenter, Action<SetupEvent> onEvent,
            Action<string> onConsoleText, CancellationToken cancellationToken, Action<Runspace, string> beforeRun)
        {
            string scriptPath = ValidateRequest(request);
            if (presenter == null)
            {
                throw new ArgumentNullException(nameof(presenter));
            }

            var result = new SetupRunResult();
            InitialSessionState state = InitialSessionState.CreateDefault();
            // In-process only: the engine and its tools are scripts, which the machine's own policy might otherwise
            // block. A policy set by Group Policy still applies.
            state.ExecutionPolicy = Microsoft.PowerShell.ExecutionPolicy.Bypass;

            using (var bridge = new PromptBridge(presenter, cancellationToken))
            {
                Runspace runspace = RunspaceFactory.CreateRunspace(state);
                try
                {
                    runspace.Open();
                    result.RunspaceId = runspace.InstanceId;
                    InvokeStep(runspace, "load the setup engine", ps => ps.AddScript(". $args[0]").AddArgument(scriptPath));
                    InvokeStep(runspace, "turn on setup events", ps => ps.AddCommand("Enable-SetupEvents"));
                    InvokeStep(runspace, "register the prompt handler",
                        ps => ps.AddCommand("Register-SetupPromptHandler").AddParameter("Handler", bridge));
                    beforeRun?.Invoke(runspace, scriptPath);
                    RunEngine(runspace, request, result, onEvent, onConsoleText);
                }
                finally
                {
                    runspace.Close();
                    result.RunspaceStateAfterRun = runspace.RunspaceStateInfo.State.ToString();
                    runspace.Dispose();
                }
            }
            return result;
        }

        private static string ValidateRequest(SetupRunRequest request)
        {
            if (request == null)
            {
                throw new ArgumentNullException(nameof(request));
            }
            foreach (KeyValuePair<string, string> root in new Dictionary<string, string>
            {
                { "BundleRoot", request.BundleRoot }, { "InstallRoot", request.InstallRoot }, { "DataRoot", request.DataRoot },
            })
            {
                if (string.IsNullOrWhiteSpace(root.Value) || !Path.IsPathRooted(root.Value))
                {
                    throw new ArgumentException(root.Key + " must be an absolute path.", nameof(request));
                }
            }
            string scriptPath = Path.Combine(Path.GetFullPath(request.BundleRoot), SetupScriptFileName);
            if (!File.Exists(scriptPath))
            {
                throw new ArgumentException("The bundle has no " + SetupScriptFileName + ".", nameof(request));
            }
            return scriptPath;
        }

        private static void InvokeStep(Runspace runspace, string what, Action<PowerShell> configure)
        {
            using (PowerShell ps = PowerShell.Create())
            {
                ps.Runspace = runspace;
                configure(ps);
                ps.Invoke();
                if (ps.Streams.Error.Count > 0)
                {
                    throw new SetupHostException("Could not " + what + ": " + ps.Streams.Error[0]);
                }
            }
        }

        private static void RunEngine(Runspace runspace, SetupRunRequest request, SetupRunResult result,
            Action<SetupEvent> onEvent, Action<string> onConsoleText)
        {
            using (PowerShell ps = PowerShell.Create())
            {
                ps.Runspace = runspace;
                ps.Streams.Information.DataAdded += (sender, e) =>
                    OnInformation(ps.Streams.Information[e.Index], result, onEvent, onConsoleText);
                ps.AddCommand("Invoke-SetupMain")
                    .AddParameter("InstallRoot", request.InstallRoot)
                    .AddParameter("DataRoot", request.DataRoot);
                Collection<PSObject> output = ps.Invoke();
                foreach (ErrorRecord error in ps.Streams.Error)
                {
                    result.AddDiagnostic(error.ToString());
                }
                result.ExitCode = ReadExitCode(output);
            }
        }

        private static void OnInformation(InformationRecord record, SetupRunResult result, Action<SetupEvent> onEvent,
            Action<string> onConsoleText)
        {
            if (record == null)
            {
                return;
            }
            if (record.Tags.Contains(EventTag))
            {
                SetupEvent setupEvent = ToSetupEvent(record.MessageData as PSObject);
                if (setupEvent == null)
                {
                    result.AddDiagnostic("A tagged setup event could not be read.");
                    return;
                }
                result.AddEvent(setupEvent);
                Deliver(onEvent, setupEvent, result);
            }
            else if (record.Tags.Contains(ConsoleTextTag))
            {
                string text = (record.MessageData as HostInformationMessage)?.Message ?? record.MessageData?.ToString() ?? "";
                result.AddConsoleText(text);
                Deliver(onConsoleText, text, result);
            }
        }

        private static void Deliver<T>(Action<T> callback, T value, SetupRunResult result)
        {
            if (callback == null)
            {
                return;
            }
            try
            {
                callback(value);
            }
            catch (Exception ex)
            {
                // A presentation failure never reaches the engine or changes the run.
                result.AddDiagnostic("A host callback failed (" + ex.GetType().Name + ").");
            }
        }

        private static SetupEvent ToSetupEvent(PSObject data)
        {
            if (data == null)
            {
                return null;
            }
            string schema = null, type = null, stage = null;
            int sequence = 0;
            var fields = new Dictionary<string, object>(StringComparer.Ordinal);
            foreach (PSPropertyInfo property in data.Properties)
            {
                object value = Unwrap(property.Value);
                switch (property.Name)
                {
                    case "schema": schema = value as string; break;
                    case "seq": sequence = value is int number ? number : 0; break;
                    case "type": type = value as string; break;
                    case "stage": stage = value as string; break;
                    default: fields[property.Name] = value; break;
                }
            }
            return type == null ? null : new SetupEvent(schema, sequence, type, stage, fields);
        }

        private static object Unwrap(object value)
        {
            if (value is PSObject wrapped)
            {
                value = wrapped.BaseObject;
            }
            if (value is object[] items)
            {
                var lines = new string[items.Length];
                for (int i = 0; i < items.Length; i++)
                {
                    lines[i] = Unwrap(items[i]) as string;
                }
                return lines;
            }
            return value;
        }

        private static int? ReadExitCode(Collection<PSObject> output)
        {
            if (output == null || output.Count == 0)
            {
                return null;
            }
            object last = output[output.Count - 1]?.BaseObject;
            if (last is int code)
            {
                return code;
            }
            return null;
        }
    }
}
