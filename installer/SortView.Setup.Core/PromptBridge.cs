using System;
using System.Collections;
using System.Collections.Generic;
using System.Management.Automation;
using System.Security;
using System.Threading;
using System.Threading.Tasks;

namespace SortView.Setup.Core
{
    /// <summary>
    /// The object registered with the engine (Register-SetupPromptHandler). The engine calls
    /// <see cref="RequestInput"/> synchronously on its pipeline thread; the bridge hands a copy of the question to the
    /// presenter and blocks THAT thread -- never the caller's UI thread -- until the presenter answers, the run is
    /// cancelled, or the bridge is closed. Every failure returns null, which the engine treats as a cancellation.
    /// No timeout: a person is answering.
    /// </summary>
    public sealed class PromptBridge : IDisposable
    {
        private readonly IPromptPresenter presenter;
        private readonly CancellationToken cancellationToken;
        private readonly object gate = new object();
        private readonly List<SecureString> issuedSecrets = new List<SecureString>();
        private bool closed;

        public PromptBridge(IPromptPresenter presenter, CancellationToken cancellationToken)
        {
            this.presenter = presenter ?? throw new ArgumentNullException(nameof(presenter));
            this.cancellationToken = cancellationToken;
        }

        /// <summary>The engine's prompt contract: object RequestInput(string promptId, IDictionary context).</summary>
        public object RequestInput(string promptId, IDictionary context)
        {
            try
            {
                if (IsClosed || cancellationToken.IsCancellationRequested)
                {
                    return null;
                }
                Task<object> pending = presenter.PresentAsync(CopyPrompt(promptId, context), cancellationToken);
                if (pending == null)
                {
                    return null;
                }
                try
                {
                    pending.Wait(cancellationToken);
                }
                catch (Exception)
                {
                    // Cancelled while waiting, or the presenter failed: no answer. An answer that still arrives later
                    // is disposed of, never used.
                    DisposeLateSecret(pending);
                    return null;
                }
                return Accept(pending.Result);
            }
            catch (Exception)
            {
                return null;
            }
        }

        /// <summary>Closes the bridge (later questions get null) and disposes every code it handed to the engine.</summary>
        public void Dispose()
        {
            lock (gate)
            {
                closed = true;
                foreach (SecureString secret in issuedSecrets)
                {
                    secret.Dispose();
                }
                issuedSecrets.Clear();
            }
        }

        private bool IsClosed
        {
            get
            {
                lock (gate)
                {
                    return closed;
                }
            }
        }

        private object Accept(object answer)
        {
            var secret = answer as SecureString;
            if (secret != null)
            {
                lock (gate)
                {
                    if (closed)
                    {
                        secret.Dispose();
                        return null;
                    }
                    // Kept only so it can be disposed when the run ends: the engine may use it twice (the one legacy
                    // retry), so it must outlive this call.
                    issuedSecrets.Add(secret);
                }
                return secret;
            }
            // Strings go to the engine, which checks them. Anything else is not an answer the engine accepts.
            return answer as string;
        }

        private static void DisposeLateSecret(Task<object> pending)
        {
            pending.ContinueWith(
                task => (task.Result as SecureString)?.Dispose(),
                CancellationToken.None,
                TaskContinuationOptions.OnlyOnRanToCompletion,
                TaskScheduler.Default);
        }

        private static SetupPrompt CopyPrompt(string promptId, IDictionary context)
        {
            var copy = new Dictionary<string, object>(StringComparer.Ordinal);
            string schema = null;
            if (context != null)
            {
                foreach (DictionaryEntry entry in context)
                {
                    string key = entry.Key as string;
                    if (key == null)
                    {
                        continue;
                    }
                    object value = entry.Value is PSObject wrapped ? wrapped.BaseObject : entry.Value;
                    if (key == "schema")
                    {
                        schema = value as string;
                    }
                    else
                    {
                        copy[key] = value;
                    }
                }
            }
            return new SetupPrompt(promptId, schema, copy);
        }
    }
}
