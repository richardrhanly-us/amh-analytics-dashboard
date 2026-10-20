using System;
using System.Collections.Generic;
using System.Security;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Threading;

namespace SortView.Setup.Wpf.Testing
{
    /// <summary>What the view saw for one question.</summary>
    public sealed class ShownPrompt
    {
        public string PromptId { get; set; }

        public Guid RunId { get; set; }

        public bool ShownOnUiThread { get; set; }

        public int ThreadId { get; set; }

        public bool AnsweredOnUiThread { get; set; }

        public bool? AnswerAccepted { get; set; }

        public bool Dismissed { get; set; }

        public string Action { get; set; }
    }

    /// <summary>
    /// TEST-ONLY view: plays one scripted action per question, as a dialog would -- from the UI thread, a moment after
    /// the question appeared. Actions:
    ///   yes, no, path (value), secure (value: a read-only SecureString), cancel (the person cancels the question),
    ///   none (never answers), throw (ShowPrompt throws), cancel_session, defer_close, shutdown_dispatcher,
    ///   secure_then_cancel_session (answers the code, then asks the session to cancel),
    ///   late_secure (asks the session to cancel, then tries to answer with a code anyway),
    ///   keep_and_cancel (keeps the request for later, then cancels it),
    ///   stale_then_yes (first tries to answer the kept request of an earlier run, then answers yes).
    /// An exhausted script answers nothing (as none).
    /// </summary>
    public sealed class ScriptedPromptView : IPromptView
    {
        private readonly Dispatcher dispatcher;
        private readonly Queue<KeyValuePair<string, string>> script = new Queue<KeyValuePair<string, string>>();
        private readonly List<SecureString> secrets = new List<SecureString>();
        private readonly object gate = new object();
        private readonly Dictionary<PromptRequest, ShownPrompt> byRequest = new Dictionary<PromptRequest, ShownPrompt>();
        private PromptRequest waiting;

        public ScriptedPromptView(Dispatcher dispatcher, int answerDelayMilliseconds)
        {
            this.dispatcher = dispatcher;
            AnswerDelayMilliseconds = answerDelayMilliseconds;
        }

        public int AnswerDelayMilliseconds { get; }

        public List<ShownPrompt> Shown { get; } = new List<ShownPrompt>();

        public List<string> Notes { get; } = new List<string>();

        public Func<CancelRequestOutcome> CancelSession { get; set; }

        public Func<bool> DeferClose { get; set; }

        public Action ShutdownDispatcher { get; set; }

        /// <summary>A request kept by keep_and_cancel, for a later run to try to answer.</summary>
        public PromptRequest Kept { get; private set; }

        public PromptRequest LastRequest { get; private set; }

        public bool? StaleAnswerAccepted { get; private set; }

        public bool? LateAnswerAccepted { get; private set; }

        public string CancelOutcome { get; private set; }

        public bool? DeferCloseResult { get; private set; }

        public void Enqueue(string action, string value) => script.Enqueue(new KeyValuePair<string, string>(action, value));

        /// <summary>A question is on screen and not yet settled.</summary>
        public bool IsWaiting
        {
            get
            {
                lock (gate)
                {
                    return waiting != null && !waiting.IsCompleted;
                }
            }
        }

        public bool AllSecretsDisposed()
        {
            lock (gate)
            {
                foreach (SecureString secret in secrets)
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

        public int SecretCount
        {
            get
            {
                lock (gate)
                {
                    return secrets.Count;
                }
            }
        }

        public void ShowPrompt(PromptRequest request)
        {
            KeyValuePair<string, string> step = script.Count > 0 ? script.Dequeue() : new KeyValuePair<string, string>("none", null);
            var shown = new ShownPrompt
            {
                PromptId = request.Prompt.PromptId,
                RunId = request.RunId,
                ShownOnUiThread = dispatcher.CheckAccess(),
                ThreadId = Thread.CurrentThread.ManagedThreadId,
                Action = step.Key,
            };
            Shown.Add(shown);
            byRequest[request] = shown;
            LastRequest = request;
            lock (gate)
            {
                waiting = request;
            }
            if (step.Key == "throw")
            {
                throw new InvalidOperationException("simulated view failure");
            }
            if (step.Key == "none")
            {
                return;
            }
            // Answer a moment later, from the UI thread, as a person clicking a button would.
            Task.Delay(AnswerDelayMilliseconds).ContinueWith(_ =>
                dispatcher.BeginInvoke(DispatcherPriority.Normal, new Action(() => Act(request, shown, step.Key, step.Value))));
        }

        public void DismissPrompt(PromptRequest request)
        {
            if (byRequest.TryGetValue(request, out ShownPrompt shown))
            {
                shown.Dismissed = true;
            }
            Notes.Add("dismissed:" + request.Prompt.PromptId + ":onUi=" + dispatcher.CheckAccess());
        }

        private void Act(PromptRequest request, ShownPrompt shown, string action, string value)
        {
            shown.AnsweredOnUiThread = dispatcher.CheckAccess();
            switch (action)
            {
                case "yes":
                    shown.AnswerAccepted = request.TrySubmit("yes");
                    break;
                case "no":
                    shown.AnswerAccepted = request.TrySubmit("no");
                    break;
                case "path":
                    shown.AnswerAccepted = request.TrySubmit(value);
                    break;
                case "secure":
                    shown.AnswerAccepted = request.TrySubmit(NewSecret(value));
                    break;
                case "cancel":
                    shown.AnswerAccepted = request.TryCancel();
                    break;
                case "cancel_session":
                    CancelOutcome = CancelSession?.Invoke().ToString();
                    break;
                case "defer_close":
                    DeferCloseResult = DeferClose?.Invoke();
                    break;
                case "shutdown_dispatcher":
                    ShutdownDispatcher?.Invoke();
                    break;
                case "secure_then_cancel_session":
                    shown.AnswerAccepted = request.TrySubmit(NewSecret(value));
                    CancelOutcome = CancelSession?.Invoke().ToString();
                    break;
                case "late_secure":
                    CancelOutcome = CancelSession?.Invoke().ToString();
                    // Wait (off the UI thread) until the cancellation has settled the question, then answer anyway.
                    Task.Run(() =>
                    {
                        SpinWait.SpinUntil(() => request.IsCompleted, 5000);
                        dispatcher.BeginInvoke(new Action(() => LateAnswerAccepted = request.TrySubmit(NewSecret(value))));
                    });
                    break;
                case "keep_and_cancel":
                    Kept = request;
                    shown.AnswerAccepted = request.TryCancel();
                    break;
                case "stale_then_yes":
                    StaleAnswerAccepted = StaleRequest?.TrySubmit("yes");
                    shown.AnswerAccepted = request.TrySubmit("yes");
                    break;
            }
        }

        /// <summary>For stale_then_yes: a request kept from an earlier run.</summary>
        public PromptRequest StaleRequest { get; set; }

        private SecureString NewSecret(string value)
        {
            var secret = new SecureString();
            foreach (char c in value ?? "")
            {
                secret.AppendChar(c);
            }
            secret.MakeReadOnly();
            lock (gate)
            {
                secrets.Add(secret);
            }
            return secret;
        }
    }
}
