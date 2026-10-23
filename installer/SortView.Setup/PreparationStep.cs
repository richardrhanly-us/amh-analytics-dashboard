using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Threading;
using SortView.Setup.Core;
using SortView.Setup.Wizard;

namespace SortView.Setup
{
    /// <summary>
    /// The Prepare step: owns one <see cref="BundlePreparation"/> (and so the staged bundle and its lifetime handle) and
    /// shows its progress and outcome on a <see cref="PreparePage"/>. Runs on the UI thread only.
    ///
    /// The worker NEVER waits for the UI: progress goes through a Progress&lt;T&gt; bound to this window's dispatcher, which
    /// posts each report; completion is posted with Dispatcher.BeginInvoke. Every preparation has a generation number, and
    /// a report or completion from an earlier one (the step was left, or the window is closing) is ignored.
    /// </summary>
    internal sealed class PreparationStep
    {
        private readonly PreparePage page;
        private readonly Dispatcher dispatcher;
        private readonly Func<BundlePreparation> createPreparation;
        private BundlePreparation preparation;
        private PreparationResult last;
        private int generation;
        private bool awaitingCompletion;
        private bool cancelling;
        private Action closeWhenDone;

        public PreparationStep(PreparePage page, Dispatcher dispatcher, Func<BundlePreparation> createPreparation)
        {
            this.page = page;
            this.dispatcher = dispatcher;
            this.createPreparation = createPreparation;
            page.CancelRequested += (s, e) => Cancel();
            page.RetryRequested += (s, e) => Retry();
        }

        /// <summary>Navigation, the buttons or the window changed what they may do.</summary>
        public event EventHandler StateChanged;

        /// <summary>A preparation is running, or its result has not been shown yet: no navigation.</summary>
        public bool IsBusy => preparation != null && (awaitingCompletion || preparation.IsPreparing);

        /// <summary>A verified bundle is staged and held: Setup may be shown.</summary>
        public bool IsPrepared => !IsBusy && last != null && last.Outcome == PreparationOutcome.Prepared &&
                                  preparation != null && preparation.TryGetStagedBundle() != null;

        /// <summary>The verified bundle this step stages and holds (its protected copy, pinned by its lifetime handle), or
        /// null when it is not prepared. It stays held until the step is left going back, or the window closes.</summary>
        public StagedBundle StagedBundle => IsPrepared ? preparation.TryGetStagedBundle() : null;

        /// <summary>The step was entered going forward (from Ready): prepares afresh. Coming back from Setup, the prepared
        /// bundle is kept and shown again.</summary>
        public void Enter()
        {
            dispatcher.VerifyAccess(); // before anything is created
            if (preparation != null)
            {
                return; // back from Setup: still prepared
            }
            preparation = createPreparation();
            Start();
        }

        /// <summary>The step was left going back (to Ready): the preparation and its staged bundle are released.</summary>
        public IReadOnlyList<StagingProblem> Leave()
        {
            IReadOnlyList<StagingProblem> problems = DisposePreparation();
            page.Reset();
            StateChanged?.Invoke(this, EventArgs.Empty);
            return problems;
        }

        public void Cancel()
        {
            if (preparation != null && IsBusy && !cancelling)
            {
                cancelling = true;
                preparation.Cancel();
                page.ShowCancelling();
            }
        }

        public void Retry()
        {
            if (preparation == null || IsBusy || IsPrepared)
            {
                return;
            }
            Start();
        }

        /// <summary>The window is closing. Returns true if it must wait: a preparation is running; it is cancelled, and
        /// <paramref name="close"/> is called once it has stopped and been released. Otherwise everything is released now.</summary>
        public bool DeferClose(Action close)
        {
            if (!IsBusy)
            {
                DisposePreparation();
                return false;
            }
            closeWhenDone = close;
            Cancel();
            StateChanged?.Invoke(this, EventArgs.Empty);
            return true;
        }

        /// <summary>Releases the preparation (waiting for its worker if one is still running -- it never needs this thread),
        /// returning cleanup problems. Safe to call more than once.</summary>
        public IReadOnlyList<StagingProblem> DisposePreparation()
        {
            generation++;
            awaitingCompletion = false;
            cancelling = false;
            last = null;
            BundlePreparation held = preparation;
            preparation = null;
            if (held == null)
            {
                return new List<StagingProblem>();
            }
            held.Dispose();
            return held.DisposeCleanupProblems;
        }

        private void Start()
        {
            dispatcher.VerifyAccess(); // the UI thread only: never started from anywhere else
            int current = ++generation;
            last = null;
            awaitingCompletion = true;
            cancelling = false;
            page.ShowEarlierRuns(null);
            page.ShowCount(0, 0);
            page.ShowWorking("Checking the installation package...");
            // Each report is POSTED to this window's dispatcher; the worker never waits for it. Progress<T> posts to the
            // context current when it is created, so it is created under this dispatcher's own -- never whatever is
            // current (none, or the thread pool's, would run the handler on a worker thread).
            Progress<StagingProgress> progress;
            SynchronizationContext previous = SynchronizationContext.Current;
            SynchronizationContext.SetSynchronizationContext(new DispatcherSynchronizationContext(dispatcher));
            try
            {
                progress = new Progress<StagingProgress>(p => OnProgress(current, p));
            }
            finally
            {
                SynchronizationContext.SetSynchronizationContext(previous);
            }
            Task<PreparationResult> task = preparation.PrepareAsync(progress);
            task.ContinueWith(done => dispatcher.BeginInvoke(new Action(() => OnCompleted(current, done))), TaskScheduler.Default);
            StateChanged?.Invoke(this, EventArgs.Empty);
        }

        private void OnProgress(int current, StagingProgress progress)
        {
            if (current != generation || cancelling)
            {
                return; // from a preparation this step has left, or one being cancelled
            }
            switch (progress.Step)
            {
                case StagingStep.SourceVerified:
                    page.ShowWorking("Copying the installation files to a protected folder...");
                    page.ShowCount(0, progress.Total);
                    break;
                case StagingStep.FileCopied:
                    page.ShowCount(progress.Done, progress.Total);
                    break;
                case StagingStep.StagedVerified:
                    page.ShowWorking("Checking the copied files...");
                    break;
            }
        }

        private void OnCompleted(int current, Task<PreparationResult> done)
        {
            if (current != generation)
            {
                return;
            }
            awaitingCompletion = false;
            cancelling = false;
            last = done.Status == TaskStatus.RanToCompletion ? done.Result : null;
            if (closeWhenDone != null)
            {
                Action close = closeWhenDone;
                closeWhenDone = null;
                List<StagingProblem> problems = (last?.CleanupProblems ?? new List<StagingProblem>()).Concat(DisposePreparation()).ToList();
                if (problems.Count > 0)
                {
                    System.Windows.MessageBox.Show("Setup could not remove every temporary file it copied to the protected setup folder:\n" +
                        string.Join("\n", problems), "SortView Setup", System.Windows.MessageBoxButton.OK, System.Windows.MessageBoxImage.Warning);
                }
                close();
                return;
            }
            ShowResult(last);
            StateChanged?.Invoke(this, EventArgs.Empty);
        }

        private void ShowResult(PreparationResult result)
        {
            if (result == null)
            {
                page.ShowOutcome("Preparation stopped because of an unexpected error.", "Select Try again.", null, true);
                return;
            }
            page.ShowEarlierRuns(EarlierRunsText(result.EarlierRuns));
            var details = new List<string>();
            details.AddRange(result.Problems.Select(p => p.ToString()));
            if (result.SourceVerification != null)
            {
                details.AddRange(result.SourceVerification.Problems.Select(p => "Package: " + p));
            }
            details.AddRange(result.CleanupProblems.Select(p => "Not removed: " + p));
            string cleanup = result.CleanupProblems.Count == 0 ? ""
                : " Some temporary files could not be removed (see the details); they are in the protected setup folder.";
            switch (result.Outcome)
            {
                case PreparationOutcome.Prepared:
                    page.ShowOutcome("The installation package is verified and ready.",
                        "SortView Collector " + result.Version + ": " + result.FileCount + " files, checked and copied to a protected folder.",
                        details, false);
                    page.ShowCount(result.FileCount, result.FileCount);
                    return;
                case PreparationOutcome.Cancelled:
                    page.ShowOutcome("Preparation was cancelled.", "Nothing was installed." + cleanup, details, true);
                    return;
                case PreparationOutcome.Refused:
                    ShowRefusal(result.Refusal);
                    return;
                default:
                    ShowFailure(result, details, cleanup);
                    return;
            }
        }

        private void ShowRefusal(PreparationRefusal refusal)
        {
            switch (refusal)
            {
                case PreparationRefusal.PinNotConfigured:
                case PreparationRefusal.PinInvalid:
                    page.ShowOutcome("This setup program does not trust any installation package.",
                        "It was built without a trusted SortView Collector package, so nothing can be prepared. Use the setup program " +
                        "provided by SortView.", new[] { "Refused: " + refusal }, false);
                    return;
                case PreparationRefusal.NotElevated:
                    page.ShowOutcome("Setup is not running with administrator rights.",
                        "Close setup, then start it again with Run as administrator.", new[] { "Refused: " + refusal }, false);
                    return;
                case PreparationRefusal.BundleMissing:
                    page.ShowOutcome("The installation package was not found.",
                        "Setup looks for it in the 'bundle' folder next to the setup program. Copy the complete package there, then " +
                        "select Try again.", new[] { "Refused: " + refusal }, true);
                    return;
                default:
                    page.ShowOutcome("The installation files could not be prepared.", "Select Try again.", new[] { "Refused: " + refusal }, true);
                    return;
            }
        }

        private void ShowFailure(PreparationResult result, List<string> details, string cleanup)
        {
            if (result.WorkerFailed || result.Problems.Any(p => p.Code == StagingProblemCode.UnexpectedError))
            {
                page.ShowOutcome("Preparation stopped because of an unexpected error.", "Nothing was installed. Select Try again." + cleanup, details, true);
                return;
            }
            if (result.Problems.Any(p => p.Code == StagingProblemCode.SourceNotVerified) && result.SourceVerification != null)
            {
                var codes = result.SourceVerification.Problems.Select(p => p.Code).ToList();
                string stage = codes.Contains(BundleProblemCode.ManifestHashMismatch)
                        ? "This installation package is not the one this setup program trusts."
                    : codes.Contains(BundleProblemCode.VersionMismatch)
                        ? "This installation package is a different version than this setup program expects."
                    : "The installation package is damaged or incomplete.";
                page.ShowOutcome(stage, "Copy the complete package from SortView again, then select Try again." + cleanup, details, true);
                return;
            }
            string detail = result.Problems.Any(p => p.Code == StagingProblemCode.InsufficientSpace)
                    ? "There is not enough free disk space on this computer."
                : result.Problems.Any(p => p.Code == StagingProblemCode.SourceChanged)
                    ? "The installation package changed while it was being copied."
                : result.Problems.Any(p => p.Code == StagingProblemCode.UntrustedOwner || p.Code == StagingProblemCode.WritableByUntrusted ||
                                           p.Code == StagingProblemCode.ReparsePoint || p.Code == StagingProblemCode.UnsupportedVolume)
                    ? "The protected setup folder is not safe to use. Contact your IT administrator."
                : "See the details.";
            page.ShowOutcome("The installation files could not be prepared.", detail + cleanup, details, true);
        }

        /// <summary>Earlier setup attempts' folders that look abandoned or could not be classified: information only.</summary>
        private static string EarlierRunsText(StagingRunScan scan)
        {
            int count = scan?.Entries.Count(e => e.State == StagingRunState.AbandonedCandidate || e.State == StagingRunState.Unknown) ?? 0;
            if (count == 0)
            {
                return null;
            }
            return count + (count == 1 ? " folder" : " folders") + " from earlier setup attempts remain in the protected setup folder. " +
                   "Setup does not remove them automatically; this does not stop setup.";
        }
    }
}
