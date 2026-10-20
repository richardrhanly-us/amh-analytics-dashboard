using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Security;
using System.Threading;
using System.Threading.Tasks;
using SortView.Setup.Core;
using SortView.Setup.Core.Testing;

namespace SortView.Setup.Wpf.Testing
{
    /// <summary>One run through <see cref="SetupRunSession"/>, as the tests see it.</summary>
    public sealed class SessionReport
    {
        public string Refused { get; set; }

        public int? ExitCode { get; set; }

        public bool TimedOut { get; set; }

        public List<string> Events { get; } = new List<string>();

        public List<string> CancelledPromptIds { get; } = new List<string>();

        public List<ShownPrompt> Shown { get; set; }

        public List<string> ViewNotes { get; set; }

        public int UiThreadId { get; set; }

        public int RunnerThreadId { get; set; }

        public bool RunnerIsBackground { get; set; }

        public bool RunnerOnUiThread { get; set; }

        public long StartMilliseconds { get; set; }

        public bool RunningWhenStartReturned { get; set; }

        public List<long> ResponsivenessWhileWaiting { get; } = new List<long>();

        public string CancelOutcome { get; set; }

        public string CancelOutcomeAtStart { get; set; }

        public bool? DeferCloseResult { get; set; }

        public bool DeferCloseAfterRun { get; set; }

        public string CancelAfterRun { get; set; }

        public bool EnrollmentMayHaveBegun { get; set; }

        public bool? LateAnswerAccepted { get; set; }

        public bool? StaleAnswerAccepted { get; set; }

        public bool SecretsDisposed { get; set; }

        public int SecretCount { get; set; }

        public string RunId { get; set; }

        public string RunspaceState { get; set; }
    }

    /// <summary>What a single question did, for presenter-level scenarios.</summary>
    public sealed class PresenterReport
    {
        public string Result { get; set; }

        public bool Settled { get; set; }

        public long SettledMilliseconds { get; set; }

        public int ShownCount { get; set; }

        public bool ShownOnUiThread { get; set; }

        public bool Dismissed { get; set; }

        public bool? LateAnswerAccepted { get; set; }

        public bool SecretsDisposed { get; set; }

        public long ResponsivenessWhileWaiting { get; set; } = -2;

        public List<string> Notes { get; set; }
    }

    /// <summary>TEST-ONLY scenario runners, called from tests/test_installer_dispatcher.py.</summary>
    public static class DispatcherScenarios
    {
        private const int RunLimitMilliseconds = 120000;

        /// <summary>A full simulated setup through SetupRunSession, answered by a scripted view on a real UI thread.</summary>
        public static SessionReport RunSession(SimulationRequest request, string[] actions, string[] values, int answerDelayMilliseconds,
            bool cancelAtStart)
        {
            using (var host = new DispatcherTestHost())
            {
                var view = new ScriptedPromptView(host.Dispatcher, answerDelayMilliseconds);
                for (int i = 0; i < actions.Length; i++)
                {
                    view.Enqueue(actions[i], values != null && i < values.Length ? values[i] : null);
                }
                return Run(host, view, request, cancelAtStart);
            }
        }

        /// <summary>Two runs one after the other with the same view: the second first tries to answer a question the
        /// first run asked.</summary>
        public static SessionReport[] RunTwoSessions(SimulationRequest request, string[] firstActions, string[] secondActions,
            string[] secondValues, int answerDelayMilliseconds)
        {
            using (var host = new DispatcherTestHost())
            {
                var first = new ScriptedPromptView(host.Dispatcher, answerDelayMilliseconds);
                foreach (string action in firstActions)
                {
                    first.Enqueue(action, null);
                }
                SessionReport one = Run(host, first, request, false);
                var second = new ScriptedPromptView(host.Dispatcher, answerDelayMilliseconds) { StaleRequest = first.Kept };
                for (int i = 0; i < secondActions.Length; i++)
                {
                    second.Enqueue(secondActions[i], i < secondValues.Length ? secondValues[i] : null);
                }
                SessionReport two = Run(host, second, request, false);
                return new[] { one, two };
            }
        }

        private static SessionReport Run(DispatcherTestHost host, ScriptedPromptView view, SimulationRequest request, bool cancelAtStart)
        {
            var report = new SessionReport { UiThreadId = host.ThreadId };
            SetupRunner runner = (presenter, onEvent, onConsoleText, token) =>
            {
                report.RunnerThreadId = Thread.CurrentThread.ManagedThreadId;
                report.RunnerIsBackground = Thread.CurrentThread.IsBackground;
                report.RunnerOnUiThread = host.Dispatcher.CheckAccess();
                return SimulatedSetupHarness.Run(request, presenter, token);
            };

            var watch = Stopwatch.StartNew();
            SetupRunSession session = SetupRunSession.Start(runner, host.Dispatcher, view, null, null);
            report.StartMilliseconds = watch.ElapsedMilliseconds;
            report.RunningWhenStartReturned = session.IsRunning;
            report.RunId = session.RunId.ToString();
            view.CancelSession = session.RequestCancel;
            view.DeferClose = session.DeferClose;
            view.ShutdownDispatcher = () => host.Dispatcher.BeginInvokeShutdown(System.Windows.Threading.DispatcherPriority.Normal);
            if (cancelAtStart)
            {
                report.CancelOutcomeAtStart = session.RequestCancel().ToString();
            }

            // While the engine waits for an answer, the UI thread must keep running other work.
            while (!session.Completion.Wait(50) && watch.ElapsedMilliseconds < RunLimitMilliseconds)
            {
                if (view.IsWaiting && !host.Dispatcher.HasShutdownStarted)
                {
                    report.ResponsivenessWhileWaiting.Add(host.MeasureResponsiveness(2000));
                }
            }
            report.TimedOut = !session.Completion.IsCompleted;
            if (session.Completion.IsCompleted)
            {
                if (session.Completion.IsFaulted)
                {
                    Exception inner = session.Completion.Exception.GetBaseException();
                    report.Refused = inner.GetType().Name + ": " + inner.Message;
                }
                else
                {
                    SetupRunResult result = session.Completion.Result;
                    report.ExitCode = result.ExitCode;
                    report.RunspaceState = result.RunspaceStateAfterRun;
                    foreach (SetupEvent setupEvent in result.Events)
                    {
                        object detail = null;
                        foreach (string key in new[] { "outcome", "reason", "enrollment_outcome", "result", "mode", "prompt_id" })
                        {
                            if (setupEvent.Fields.TryGetValue(key, out object value) && value != null)
                            {
                                detail = value;
                                break;
                            }
                        }
                        report.Events.Add(setupEvent.Type + ":" + setupEvent.Stage + (detail == null ? "" : ":" + detail));
                        if (setupEvent.Type == "input_cancelled")
                        {
                            report.CancelledPromptIds.Add((string)setupEvent.Fields["prompt_id"]);
                        }
                    }
                }
            }
            // Let any late answer and dismissal still queued on the UI thread run before reading the view.
            if (!host.Dispatcher.HasShutdownStarted)
            {
                Thread.Sleep(view.AnswerDelayMilliseconds + 200);
                host.MeasureResponsiveness(2000);
            }
            report.CancelOutcome = view.CancelOutcome;
            report.DeferCloseResult = view.DeferCloseResult;
            report.DeferCloseAfterRun = session.DeferClose();
            report.CancelAfterRun = session.RequestCancel().ToString();
            report.EnrollmentMayHaveBegun = session.EnrollmentMayHaveBegun;
            report.LateAnswerAccepted = view.LateAnswerAccepted;
            report.StaleAnswerAccepted = view.StaleAnswerAccepted;
            report.SecretsDisposed = view.AllSecretsDisposed();
            report.SecretCount = view.SecretCount;
            report.Shown = view.Shown;
            report.ViewNotes = view.Notes;
            return report;
        }

        /// <summary>
        /// One question presented straight through a DispatcherPromptPresenter (no engine), from a background thread
        /// as the bridge does it. Modes: answer, dispose_while_waiting, shutdown_while_waiting, already_shut_down,
        /// view_throws, token_cancel, late_answer, responsive_while_waiting.
        /// </summary>
        public static PresenterReport PresentOnce(string mode)
        {
            using (var host = new DispatcherTestHost())
            {
                var view = new ScriptedPromptView(host.Dispatcher, 50);
                view.Enqueue(mode == "answer" ? "yes" : mode == "view_throws" ? "throw" : "none", null);
                var presenter = new DispatcherPromptPresenter(host.Dispatcher, view);
                var cancellation = new CancellationTokenSource();
                var report = new PresenterReport();
                if (mode == "already_shut_down")
                {
                    host.Shutdown();
                }
                SetupPrompt prompt = SamplePrompt();
                var watch = Stopwatch.StartNew();
                // Asked from a pool thread, as the engine's pipeline thread asks (Task.Run unwraps to the presenter's task).
                Task<object> answer = Task.Run(() => presenter.PresentAsync(prompt, cancellation.Token));
                if (mode != "already_shut_down" && mode != "view_throws" && mode != "answer")
                {
                    SpinWait.SpinUntil(() => view.IsWaiting, 5000);
                }
                switch (mode)
                {
                    case "dispose_while_waiting":
                        presenter.Dispose();
                        break;
                    case "shutdown_while_waiting":
                        host.Dispatcher.BeginInvokeShutdown(System.Windows.Threading.DispatcherPriority.Normal);
                        break;
                    case "token_cancel":
                    case "late_answer":
                        cancellation.Cancel();
                        break;
                    case "responsive_while_waiting":
                        report.ResponsivenessWhileWaiting = host.MeasureResponsiveness(2000);
                        presenter.Dispose();
                        break;
                }
                report.Settled = answer.Wait(10000);
                report.SettledMilliseconds = watch.ElapsedMilliseconds;
                report.Result = report.Settled ? (answer.Result == null ? "null" : answer.Result.ToString()) : "(still waiting)";
                if (mode == "late_answer" && view.LastRequest != null)
                {
                    var secret = new SecureString();
                    secret.AppendChar('x');
                    report.LateAnswerAccepted = view.LastRequest.TrySubmit(secret);
                    report.SecretsDisposed = IsDisposed(secret);
                }
                if (!host.Dispatcher.HasShutdownStarted)
                {
                    host.MeasureResponsiveness(2000); // let a queued dismissal run
                }
                report.ShownCount = view.Shown.Count;
                report.ShownOnUiThread = view.Shown.Count > 0 && view.Shown[0].ShownOnUiThread;
                report.Dismissed = view.Shown.Count > 0 && view.Shown[0].Dismissed;
                report.Notes = view.Notes;
                presenter.Dispose();
                return report;
            }
        }

        /// <summary>Events and console text raised on the engine's thread arrive on the UI thread, all of them, in order.</summary>
        public static string[] DeliverConsoleText(int count)
        {
            using (var host = new DispatcherTestHost())
            {
                var received = new List<string>();
                var view = new ScriptedPromptView(host.Dispatcher, 10);
                SetupRunner runner = (presenter, onEvent, onConsoleText, token) =>
                {
                    for (int i = 0; i < count; i++)
                    {
                        onConsoleText("line " + i);
                    }
                    return null;
                };
                SetupRunSession session = SetupRunSession.Start(runner, host.Dispatcher, view, null,
                    text => received.Add(text + (host.Dispatcher.CheckAccess() ? "|ui" : "|not-ui")));
                session.Completion.Wait(10000);
                host.MeasureResponsiveness(5000); // everything posted before this has run
                return received.ToArray();
            }
        }

        private static SetupPrompt SamplePrompt()
        {
            // SetupPrompt is created only by the bridge; build one the way the engine's handler call would.
            var bridgeHost = new CapturingPresenter();
            new PromptBridge(bridgeHost, CancellationToken.None).RequestInput("sources_confirm",
                new System.Collections.Hashtable { { "schema", "sortview.setup.prompt/1" }, { "checkins", "C:\\TL\\Checkins.txt" } });
            return bridgeHost.Captured;
        }

        private static bool IsDisposed(SecureString secret)
        {
            try
            {
                int unused = secret.Length;
                return false;
            }
            catch (ObjectDisposedException)
            {
                return true;
            }
        }

        private sealed class CapturingPresenter : IPromptPresenter
        {
            public SetupPrompt Captured { get; private set; }

            public Task<object> PresentAsync(SetupPrompt prompt, CancellationToken cancellationToken)
            {
                Captured = prompt;
                return Task.FromResult<object>(null);
            }
        }
    }
}
