using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Threading;
using System.Threading.Tasks;

namespace SortView.Setup.Core
{
    /// <summary>
    /// One question the setup engine is asking (setup-collector.ps1, schema sortview.setup.prompt/1): its stable id
    /// and a read-only copy of the closed, non-secret context the engine sent with it.
    /// </summary>
    public sealed class SetupPrompt
    {
        internal SetupPrompt(string promptId, string schema, IDictionary<string, object> context)
        {
            PromptId = promptId;
            Schema = schema;
            Context = new ReadOnlyDictionary<string, object>(context);
        }

        /// <summary>enrollment_code, sources_confirm, source_path, resume_confirm or legacy_task_enable.</summary>
        public string PromptId { get; }

        public string Schema { get; }

        public IReadOnlyDictionary<string, object> Context { get; }
    }

    /// <summary>
    /// Answers the engine's questions. The task's result is the answer the engine expects for the prompt -- the exact
    /// string <see cref="PromptAnswers.Yes"/> or <see cref="PromptAnswers.No"/>, a path string, or for
    /// enrollment_code a non-empty <see cref="System.Security.SecureString"/> -- or null to cancel. The engine checks
    /// every answer and treats anything else, a faulted task or a cancelled one as a cancellation, never a default.
    /// A SecureString handed back becomes the bridge's to dispose once the run has finished.
    /// </summary>
    public interface IPromptPresenter
    {
        Task<object> PresentAsync(SetupPrompt prompt, CancellationToken cancellationToken);
    }

    public static class PromptAnswers
    {
        public const string Yes = "yes";
        public const string No = "no";
    }

    /// <summary>One structured setup event (schema sortview.setup.event/1), read from the Information stream.</summary>
    public sealed class SetupEvent
    {
        internal SetupEvent(string schema, int sequence, string type, string stage, IDictionary<string, object> fields)
        {
            Schema = schema;
            Sequence = sequence;
            Type = type;
            Stage = stage;
            Fields = new ReadOnlyDictionary<string, object>(fields);
        }

        public string Schema { get; }

        public int Sequence { get; }

        /// <summary>stage_started, stage_completed, stage_skipped, mode_detected, enrollment_result, input_cancelled,
        /// run_stopped, run_failed_unexpected or run_finished.</summary>
        public string Type { get; }

        public string Stage { get; }

        /// <summary>The event's own allow-listed fields: strings, integers, booleans, string arrays or null.</summary>
        public IReadOnlyDictionary<string, object> Fields { get; }
    }

    /// <summary>What to run: the release bundle that holds setup.ps1, and where the Collector is installed.</summary>
    public sealed class SetupRunRequest
    {
        public string BundleRoot { get; set; }

        public string InstallRoot { get; set; }

        public string DataRoot { get; set; }
    }

    /// <summary>The outcome of one run. Nothing in it holds an answer the presenter gave.</summary>
    public sealed class SetupRunResult
    {
        private readonly List<SetupEvent> events = new List<SetupEvent>();
        private readonly List<string> consoleText = new List<string>();
        private readonly List<string> diagnostics = new List<string>();

        /// <summary>Invoke-SetupMain's exit code (0 = complete, 1 = a step failed, 2 = refused, or a tool's own code).
        /// Null only when the engine produced no exit code.</summary>
        public int? ExitCode { get; internal set; }

        public IReadOnlyList<SetupEvent> Events => events;

        /// <summary>The engine's ordinary console text (Write-Host), kept apart from the events. In memory only.</summary>
        public IReadOnlyList<string> ConsoleText => consoleText;

        /// <summary>PowerShell error records and host-side notes, for diagnosis. In memory only.</summary>
        public IReadOnlyList<string> Diagnostics => diagnostics;

        /// <summary>The runspace this run used. Every run gets a new one.</summary>
        public Guid RunspaceId { get; internal set; }

        /// <summary>The runspace's state once the run ended (Closed when it was shut down).</summary>
        public string RunspaceStateAfterRun { get; internal set; }

        internal void AddEvent(SetupEvent setupEvent) => events.Add(setupEvent);

        internal void AddConsoleText(string text) => consoleText.Add(text);

        internal void AddDiagnostic(string text) => diagnostics.Add(text);
    }

    /// <summary>The engine could not be loaded or prepared. Never thrown for a setup that ran and stopped.</summary>
    public sealed class SetupHostException : Exception
    {
        public SetupHostException(string message) : base(message)
        {
        }
    }
}
