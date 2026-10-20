using System;
using System.Security;
using System.Threading;
using System.Threading.Tasks;
using SortView.Setup.Core;

namespace SortView.Setup.Wpf
{
    /// <summary>
    /// Shows the engine's questions. Both methods are called on the UI thread, and both must return promptly: a view
    /// opens its dialog and returns, then answers later through <see cref="PromptRequest.TrySubmit"/> or
    /// <see cref="PromptRequest.TryCancel"/>.
    /// </summary>
    public interface IPromptView
    {
        /// <summary>A new question. Answer it (once) through the request.</summary>
        void ShowPrompt(PromptRequest request);

        /// <summary>The question was settled without the view -- the run was cancelled, the presenter closed, or the
        /// application is shutting down. Close its dialog; any answer given now is refused.</summary>
        void DismissPrompt(PromptRequest request);
    }

    /// <summary>
    /// One question from one run. It can be settled exactly once: by the view's answer, by the view's cancel, or by
    /// cancellation/shutdown -- whichever comes first. Anything after that is refused, and a refused SecureString is
    /// disposed at once. A request belongs to the run (<see cref="RunId"/>) that asked it and can answer nothing else.
    /// </summary>
    public sealed class PromptRequest
    {
        private readonly TaskCompletionSource<object> completion =
            new TaskCompletionSource<object>(TaskCreationOptions.RunContinuationsAsynchronously);

        private readonly Action<PromptRequest, object> onAccepted;

        internal PromptRequest(Guid runId, SetupPrompt prompt, Action<PromptRequest, object> onAccepted)
        {
            RunId = runId;
            Prompt = prompt;
            this.onAccepted = onAccepted;
        }

        public Guid RunId { get; }

        public SetupPrompt Prompt { get; }

        public bool IsCompleted => completion.Task.IsCompleted;

        internal Task<object> Task => completion.Task;

        /// <summary>
        /// Answers the question: <see cref="PromptAnswers.Yes"/> or <see cref="PromptAnswers.No"/>, a path string, or for
        /// enrollment_code a non-empty SecureString (whose ownership passes to the setup run, which disposes it when it
        /// ends). Returns false -- and disposes a SecureString -- when the question was already settled.
        /// The engine checks the answer itself; nothing here decides what it means.
        /// </summary>
        public bool TrySubmit(object answer)
        {
            if (completion.TrySetResult(answer))
            {
                onAccepted?.Invoke(this, answer);
                return true;
            }
            (answer as SecureString)?.Dispose();
            return false;
        }

        /// <summary>The person cancelled this question. The engine then stops safely (or, for legacy_task_enable,
        /// completes with the task left disabled). Returns false when it was already settled.</summary>
        public bool TryCancel() => completion.TrySetResult(null);

        internal bool Abandon() => completion.TrySetResult(null);
    }
}
