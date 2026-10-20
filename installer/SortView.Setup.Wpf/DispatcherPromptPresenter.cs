using System;
using System.Collections.Generic;
using System.Security;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Threading;
using SortView.Setup.Core;

namespace SortView.Setup.Wpf
{
    /// <summary>
    /// Presents the engine's questions on a WPF UI thread. Called by the bridge on the engine's pipeline thread, it only
    /// POSTS the question (Dispatcher.BeginInvoke -- never Invoke) and returns a task the view completes later, so the
    /// UI thread never waits for PowerShell and PowerShell never waits on the UI thread's message loop.
    ///
    /// A question is settled with null -- which the engine treats as a cancellation, never a default -- when the run's
    /// token is cancelled, the presenter is closed, the dispatcher shuts down, posting to it fails, or the view throws.
    /// Nothing waits for ever unless a person simply has not answered yet (there is no timeout). One presenter serves one
    /// run (<see cref="RunId"/>).
    /// </summary>
    public sealed class DispatcherPromptPresenter : IPromptPresenter, IDisposable
    {
        private readonly Dispatcher dispatcher;
        private readonly IPromptView view;
        private readonly object gate = new object();
        private readonly List<PromptRequest> pending = new List<PromptRequest>();
        private bool closed;
        private int codeSubmitted;

        public DispatcherPromptPresenter(Dispatcher dispatcher, IPromptView view)
        {
            this.dispatcher = dispatcher ?? throw new ArgumentNullException(nameof(dispatcher));
            this.view = view ?? throw new ArgumentNullException(nameof(view));
            dispatcher.ShutdownStarted += OnDispatcherShutdown;
        }

        public Guid RunId { get; } = Guid.NewGuid();

        /// <summary>An enrollment code was handed to the engine: from here on it may send the enrollment request, whose
        /// outcome must never be interrupted.</summary>
        public bool EnrollmentCodeSubmitted => Volatile.Read(ref codeSubmitted) == 1;

        /// <summary>Whether a question is waiting for an answer right now.</summary>
        public bool HasPendingPrompt
        {
            get
            {
                lock (gate)
                {
                    return pending.Exists(request => !request.IsCompleted);
                }
            }
        }

        public Task<object> PresentAsync(SetupPrompt prompt, CancellationToken cancellationToken)
        {
            var request = new PromptRequest(RunId, prompt, OnAccepted);
            lock (gate)
            {
                if (closed)
                {
                    return System.Threading.Tasks.Task.FromResult<object>(null);
                }
                pending.Add(request);
            }
            request.Task.ContinueWith(OnSettled, request, CancellationToken.None, TaskContinuationOptions.ExecuteSynchronously,
                TaskScheduler.Default);
            if (cancellationToken.CanBeCanceled)
            {
                CancellationTokenRegistration registration = cancellationToken.Register(() => Abandon(request));
                request.Task.ContinueWith(task => registration.Dispose(), TaskScheduler.Default);
            }
            Post(request);
            return request.Task;
        }

        /// <summary>Closes the presenter: every waiting question is settled with null and dismissed; later ones get null.</summary>
        public void Dispose()
        {
            PromptRequest[] waiting;
            lock (gate)
            {
                if (closed)
                {
                    return;
                }
                closed = true;
                waiting = pending.ToArray();
            }
            dispatcher.ShutdownStarted -= OnDispatcherShutdown;
            foreach (PromptRequest request in waiting)
            {
                Abandon(request);
            }
        }

        private void Post(PromptRequest request)
        {
            if (dispatcher.HasShutdownStarted)
            {
                Abandon(request);
                return;
            }
            try
            {
                DispatcherOperation operation = dispatcher.BeginInvoke(DispatcherPriority.Normal, new Action(() => Show(request)));
                if (operation.Status == DispatcherOperationStatus.Aborted)
                {
                    Abandon(request);
                }
                else
                {
                    operation.Aborted += (sender, e) => Abandon(request);
                }
            }
            catch (Exception)
            {
                Abandon(request);
            }
        }

        private void Show(PromptRequest request)
        {
            if (request.IsCompleted)
            {
                return; // settled before the UI got to it (cancelled, closed): never shown
            }
            try
            {
                view.ShowPrompt(request);
            }
            catch (Exception)
            {
                request.Abandon();
            }
        }

        private void Abandon(PromptRequest request)
        {
            if (!request.Abandon())
            {
                return; // already answered or settled
            }
            if (dispatcher.HasShutdownStarted)
            {
                return;
            }
            try
            {
                dispatcher.BeginInvoke(DispatcherPriority.Normal, new Action(() =>
                {
                    try
                    {
                        view.DismissPrompt(request);
                    }
                    catch (Exception)
                    {
                        // The question is settled either way; a view that cannot close its dialog changes nothing.
                    }
                }));
            }
            catch (Exception)
            {
                // Same: nothing is waiting on the dismissal.
            }
        }

        private void OnSettled(Task<object> task, object state)
        {
            lock (gate)
            {
                pending.Remove((PromptRequest)state);
            }
        }

        private void OnAccepted(PromptRequest request, object answer)
        {
            // Set the moment the view's answer is accepted, before the engine can act on it.
            if (request.Prompt.PromptId == "enrollment_code" && answer is SecureString)
            {
                Interlocked.Exchange(ref codeSubmitted, 1);
            }
        }

        private void OnDispatcherShutdown(object sender, EventArgs e) => Dispose();
    }
}
