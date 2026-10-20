using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Management;
using System.Security;
using System.Threading;
using System.Threading.Tasks;

namespace SortView.Setup.Core.Testing
{
    /// <summary>One question the presenter was asked, as it saw it.</summary>
    public sealed class RecordedPrompt
    {
        public string PromptId { get; set; }

        public string Schema { get; set; }

        public Dictionary<string, object> Context { get; set; }

        public int ThreadId { get; set; }

        public int ChildProcessCount { get; set; }
    }

    /// <summary>
    /// TEST-ONLY presenter: answers each question with the next scripted answer, from ANOTHER thread after a short
    /// delay (as a UI would), and records what it was asked. Answer kinds:
    ///   string (the value as given), secure (a read-only SecureString of the value), null, object, int,
    ///   throw (PresentAsync throws), fault (a faulted task), cancel_run (never answers; cancels the run's token).
    /// An exhausted script answers null.
    /// </summary>
    public sealed class ScriptedPromptPresenter : IPromptPresenter
    {
        private readonly Queue<KeyValuePair<string, string>> answers = new Queue<KeyValuePair<string, string>>();
        private readonly List<SecureString> issued = new List<SecureString>();
        private readonly object gate = new object();

        public List<RecordedPrompt> Prompts { get; } = new List<RecordedPrompt>();

        /// <summary>The run's cancellation source, for cancel_run.</summary>
        public CancellationTokenSource RunCancellation { get; set; }

        public int AnswerDelayMilliseconds { get; set; } = 20;

        public void Enqueue(string kind, string value) => answers.Enqueue(new KeyValuePair<string, string>(kind, value));

        public int IssuedSecretCount
        {
            get
            {
                lock (gate)
                {
                    return issued.Count;
                }
            }
        }

        /// <summary>Whether every SecureString this presenter handed out has since been disposed (by the bridge).</summary>
        public bool AllIssuedSecretsDisposed()
        {
            lock (gate)
            {
                foreach (SecureString secret in issued)
                {
                    try
                    {
                        int unused = secret.Length;
                        return false;
                    }
                    catch (ObjectDisposedException)
                    {
                    }
                }
                return true;
            }
        }

        public Task<object> PresentAsync(SetupPrompt prompt, CancellationToken cancellationToken)
        {
            Prompts.Add(new RecordedPrompt
            {
                PromptId = prompt.PromptId,
                Schema = prompt.Schema,
                Context = new Dictionary<string, object>(prompt.Context as IDictionary<string, object> ?? new Dictionary<string, object>()),
                ThreadId = Thread.CurrentThread.ManagedThreadId,
                ChildProcessCount = ChildProcesses.Count(),
            });
            if (answers.Count == 0)
            {
                return Task.FromResult<object>(null);
            }
            KeyValuePair<string, string> next = answers.Dequeue();
            switch (next.Key)
            {
                case "throw":
                    throw new InvalidOperationException("simulated presenter failure");
                case "fault":
                    return Task.FromException<object>(new InvalidOperationException("simulated presenter fault"));
                case "cancel_run":
                    var never = new TaskCompletionSource<object>(TaskCreationOptions.RunContinuationsAsynchronously);
                    cancellationToken.Register(() => never.TrySetCanceled());
                    CancellationTokenSource source = RunCancellation;
                    Task.Run(async () =>
                    {
                        await Task.Delay(AnswerDelayMilliseconds).ConfigureAwait(false);
                        source?.Cancel();
                    });
                    return never.Task;
                default:
                    return Task.Run(async () =>
                    {
                        await Task.Delay(AnswerDelayMilliseconds).ConfigureAwait(false);
                        return Make(next.Key, next.Value);
                    });
            }
        }

        private object Make(string kind, string value)
        {
            switch (kind)
            {
                case "string":
                    return value;
                case "secure":
                    var secret = new SecureString();
                    foreach (char c in value ?? "")
                    {
                        secret.AppendChar(c);
                    }
                    secret.MakeReadOnly();
                    lock (gate)
                    {
                        issued.Add(secret);
                    }
                    return secret;
                case "object":
                    return new object();
                case "int":
                    return 1;
                default:
                    return null;
            }
        }
    }

    /// <summary>How many processes the current process has started that are still running.</summary>
    public static class ChildProcesses
    {
        public static int Count()
        {
            int self = Process.GetCurrentProcess().Id;
            using (var searcher = new ManagementObjectSearcher("SELECT ProcessId FROM Win32_Process WHERE ParentProcessId = " + self))
            using (ManagementObjectCollection found = searcher.Get())
            {
                return found.Count;
            }
        }
    }
}
