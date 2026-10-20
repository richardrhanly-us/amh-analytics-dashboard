using System;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Threading;
using SortView.Setup.Core;

namespace SortView.Setup.Wpf
{
    /// <summary>Runs one setup: the engine host's Run, or an equivalent (the simulation harness, in tests).</summary>
    public delegate SetupRunResult SetupRunner(IPromptPresenter presenter, Action<SetupEvent> onEvent, Action<string> onConsoleText,
        CancellationToken cancellationToken);

    /// <summary>What a cancellation request will do.</summary>
    public enum CancelRequestOutcome
    {
        /// <summary>The run had already finished.</summary>
        NotRunning,

        /// <summary>A question was waiting: it is cancelled now and the engine stops safely from it.</summary>
        CancelledAtQuestion,

        /// <summary>The engine is working: the step in progress finishes; the next question, if any, is cancelled.</summary>
        WillStopAtNextQuestion,

        /// <summary>An enrollment code was already handed to the engine. Nothing in progress is interrupted -- the
        /// enrollment's outcome must be known -- so the run continues; a remaining question (only
        /// legacy_task_enable) is cancelled, which leaves the Scheduled Task disabled.</summary>
        FinishingAfterEnrollment,
    }

    /// <summary>
    /// One setup run hosted from a WPF application. <see cref="Start(SetupRunner, Dispatcher, IPromptView, Action{SetupEvent}, Action{string})"/>
    /// returns at once: the engine runs on a dedicated background thread, its questions are posted to the UI thread
    /// (<see cref="DispatcherPromptPresenter"/>), and its events and console text are posted there too, in order.
    ///
    /// Cancellation is cooperative only. <see cref="RequestCancel"/> settles a waiting question (or the next one) with
    /// null; it never stops PowerShell or its thread, so a step in progress -- an enrollment request above all -- always
    /// runs to its end. <see cref="DeferClose"/> lets a window keep itself open until <see cref="Completion"/>; nothing
    /// here can stop the process being killed or Windows logging off, which the engine's own resume records cover.
    /// </summary>
    public sealed class SetupRunSession
    {
        private readonly Dispatcher dispatcher;
        private readonly DispatcherPromptPresenter presenter;
        private readonly CancellationTokenSource cancellation = new CancellationTokenSource();

        private SetupRunSession(Dispatcher dispatcher, IPromptView view)
        {
            this.dispatcher = dispatcher ?? throw new ArgumentNullException(nameof(dispatcher));
            presenter = new DispatcherPromptPresenter(dispatcher, view);
        }

        /// <summary>Starts a real setup run of <paramref name="request"/>'s bundle.</summary>
        public static SetupRunSession Start(SetupEngineHost host, SetupRunRequest request, Dispatcher dispatcher, IPromptView view,
            Action<SetupEvent> onEvent, Action<string> onConsoleText)
        {
            if (host == null)
            {
                throw new ArgumentNullException(nameof(host));
            }
            return Start((p, e, t, token) => host.Run(request, p, e, t, token), dispatcher, view, onEvent, onConsoleText);
        }

        /// <summary>Starts <paramref name="runner"/> on a background thread. <paramref name="onEvent"/> and
        /// <paramref name="onConsoleText"/> are called on the dispatcher's thread.</summary>
        public static SetupRunSession Start(SetupRunner runner, Dispatcher dispatcher, IPromptView view, Action<SetupEvent> onEvent,
            Action<string> onConsoleText)
        {
            if (runner == null)
            {
                throw new ArgumentNullException(nameof(runner));
            }
            var session = new SetupRunSession(dispatcher, view);
            session.Completion = Task.Factory.StartNew(
                () =>
                {
                    try
                    {
                        return runner(session.presenter, session.Post(onEvent), session.Post(onConsoleText), session.cancellation.Token);
                    }
                    finally
                    {
                        // The run is over: no question of it can be answered any more.
                        session.presenter.Dispose();
                    }
                },
                CancellationToken.None, TaskCreationOptions.LongRunning, TaskScheduler.Default);
            return session;
        }

        public Guid RunId => presenter.RunId;

        /// <summary>Completes when the engine has returned (faulted only if it could not be loaded).</summary>
        public Task<SetupRunResult> Completion { get; private set; }

        public bool IsRunning => !Completion.IsCompleted;

        /// <summary>An enrollment code was handed to the engine, so an enrollment request may have begun.</summary>
        public bool EnrollmentMayHaveBegun => presenter.EnrollmentCodeSubmitted;

        /// <summary>Asks the run to stop at its next safe point (a question). Never interrupts a step in progress.</summary>
        public CancelRequestOutcome RequestCancel()
        {
            if (Completion.IsCompleted)
            {
                return CancelRequestOutcome.NotRunning;
            }
            bool waiting = presenter.HasPendingPrompt;
            bool afterEnrollment = presenter.EnrollmentCodeSubmitted;
            cancellation.Cancel();
            if (afterEnrollment)
            {
                return CancelRequestOutcome.FinishingAfterEnrollment;
            }
            return waiting ? CancelRequestOutcome.CancelledAtQuestion : CancelRequestOutcome.WillStopAtNextQuestion;
        }

        /// <summary>
        /// For a window's Closing handler: while the run is still going, requests cancellation and returns true -- keep
        /// the window open (e.Cancel = true) and close it when <see cref="Completion"/> finishes. Returns false once the
        /// run is over.
        /// </summary>
        public bool DeferClose()
        {
            if (Completion.IsCompleted)
            {
                return false;
            }
            RequestCancel();
            return true;
        }

        private Action<T> Post<T>(Action<T> callback)
        {
            if (callback == null)
            {
                return null;
            }
            return value =>
            {
                if (!dispatcher.HasShutdownStarted)
                {
                    dispatcher.BeginInvoke(DispatcherPriority.Normal, callback, value);
                }
            };
        }
    }
}
