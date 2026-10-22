using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.IO;
using System.Linq;
using System.Security.Principal;
using System.Threading;
using System.Threading.Tasks;
using SortView.Setup.Core;

namespace SortView.Setup
{
    public enum PreparationOutcome
    {
        /// <summary>The bundle is verified and staged; the controller holds it (see TryGetStagedBundle).</summary>
        Prepared,
        /// <summary>Refused before anything was staged (see Refusal).</summary>
        Refused,
        /// <summary>Staging failed (see Problems); whatever it created was removed (see CleanupProblems).</summary>
        Failed,
        /// <summary>Cancelled; whatever it created was removed (see CleanupProblems).</summary>
        Cancelled,
    }

    public enum PreparationRefusal
    {
        None,
        /// <summary>A preparation is already running: they never overlap.</summary>
        AlreadyPreparing,
        /// <summary>A prepared bundle is held: release it first.</summary>
        AlreadyPrepared,
        Disposed,
        /// <summary>This build trusts no bundle (installer\bundle-pin.props is empty).</summary>
        PinNotConfigured,
        /// <summary>The embedded pin is malformed: no bundle is trusted.</summary>
        PinInvalid,
        /// <summary>This process is not running as an elevated administrator.</summary>
        NotElevated,
        /// <summary>There is no bundle folder beside the program.</summary>
        BundleMissing,
    }

    /// <summary>What one preparation did. Codes, paths and counts only -- never file contents.</summary>
    public sealed class PreparationResult
    {
        private static readonly IReadOnlyList<StagingProblem> None = new ReadOnlyCollection<StagingProblem>(new List<StagingProblem>());

        private PreparationResult(PreparationOutcome outcome, PreparationRefusal refusal, StagedBundle staged, StagingRunScan earlierRuns,
            bool unexpected = false)
        {
            Outcome = outcome;
            Refusal = refusal;
            EarlierRuns = earlierRuns;
            WorkerFailed = unexpected;
            if (staged != null)
            {
                Version = staged.Version;
                FileCount = staged.FileCount;
                SourceVerification = staged.SourceVerification;
                StagedVerification = staged.StagedVerification;
                Problems = staged.Problems;
                CleanupProblems = staged.CleanupProblems;
            }
            else
            {
                Problems = None;
                CleanupProblems = None;
            }
        }

        public PreparationOutcome Outcome { get; }

        public PreparationRefusal Refusal { get; }

        /// <summary>The verified version (Prepared only).</summary>
        public string Version { get; }

        public int FileCount { get; }

        public IReadOnlyList<StagingProblem> Problems { get; }

        /// <summary>Problems removing what a failed or cancelled staging created. Empty means removed, not "unknown".</summary>
        public IReadOnlyList<StagingProblem> CleanupProblems { get; }

        public BundleVerification SourceVerification { get; }

        public BundleVerification StagedVerification { get; }

        /// <summary>Earlier staging runs, classified read-only before staging (null if the check was not reached). For
        /// information only: none of them is ever removed here.</summary>
        public StagingRunScan EarlierRuns { get; }

        /// <summary>The preparation itself failed unexpectedly (outside BundleStager, which reports its own problems), or was
        /// cancelled before staging began. Anything it staged was removed.</summary>
        public bool WorkerFailed { get; }

        internal static PreparationResult Refused(PreparationRefusal refusal) =>
            new PreparationResult(PreparationOutcome.Refused, refusal, null, null);

        internal static PreparationResult From(StagedBundle staged, StagingRunScan earlierRuns)
        {
            PreparationOutcome outcome = staged.IsStaged ? PreparationOutcome.Prepared
                : staged.Problems.Any(p => p.Code == StagingProblemCode.Cancelled) ? PreparationOutcome.Cancelled
                : PreparationOutcome.Failed;
            return new PreparationResult(outcome, PreparationRefusal.None, staged, earlierRuns);
        }

        internal static PreparationResult Cancelled(StagedBundle discarded, StagingRunScan earlierRuns) =>
            new PreparationResult(PreparationOutcome.Cancelled, PreparationRefusal.None, discarded, earlierRuns);

        internal static PreparationResult Unexpected(StagingRunScan earlierRuns, PreparationOutcome outcome) =>
            new PreparationResult(outcome, PreparationRefusal.None, null, earlierRuns, unexpected: outcome == PreparationOutcome.Failed);
    }

    /// <summary>
    /// Prepares the trusted Collector bundle for a later setup: on a background worker it checks the embedded pin, that this
    /// process is an elevated administrator, and that the bundle folder beside the program exists; classifies earlier staging
    /// runs read-only (for information -- nothing is removed); then verifies and stages the bundle with BundleStager under
    /// the production policy, against the PINNED manifest hash and version only.
    ///
    /// It owns the staged bundle and its lifetime handle: the bundle is usable only while this controller holds it, and is
    /// disposed on Release, on a cancelled or failed preparation, and on Dispose. Preparations never overlap; cancellation is
    /// cooperative (Stage checks it per file and per block), so a staging thread is never abandoned -- Dispose waits for it.
    /// It runs nothing from the bundle and starts no process.
    /// </summary>
    public sealed class BundlePreparation : IDisposable
    {
        /// <summary>The bundle folder, beside the program (until a signed download carries the bundle itself).</summary>
        public const string BundleFolderName = "bundle";

        private readonly object gate = new object();
        private readonly string sourceRoot;
        private readonly TrustedBundlePin pin;
        private readonly StagingPolicy policy;
        private readonly Func<bool> isElevated;
        private CancellationTokenSource cancellation;
        private Task<PreparationResult> running;
        // The thread running Work, while it runs: a progress callback the caller supplies runs on it.
        private Thread workerThread;
        private StagedBundle staged;
        private bool disposed;
        private IReadOnlyList<StagingProblem> disposeCleanupProblems = new List<StagingProblem>();

        /// <summary>The installer's preparation: the pin embedded in this program, the bundle folder beside it, the production
        /// staging policy, and the real elevation check. Nothing about it can be chosen by a caller.</summary>
        public static BundlePreparation CreateForThisInstaller() =>
            new BundlePreparation(Path.Combine(ProgramFolder(), BundleFolderName), TrustedBundlePin.ForThisInstaller(),
                StagingPolicy.Production, IsElevatedAdministrator);

        /// <summary>The folder this program itself is in -- not the hosting process's base folder, which differs whenever
        /// another process loads this assembly.</summary>
        private static string ProgramFolder() => Path.GetDirectoryName(typeof(BundlePreparation).Assembly.Location);

        /// <summary>TEST SEAM (internal): fixture bundle, pin, disposable staging policy and elevation check.</summary>
        internal BundlePreparation(string sourceRoot, TrustedBundlePin pin, StagingPolicy policy, Func<bool> isElevated)
        {
            this.sourceRoot = sourceRoot ?? throw new ArgumentNullException(nameof(sourceRoot));
            this.pin = pin ?? throw new ArgumentNullException(nameof(pin));
            this.policy = policy ?? throw new ArgumentNullException(nameof(policy));
            this.isElevated = isElevated ?? throw new ArgumentNullException(nameof(isElevated));
        }

        public bool IsPreparing
        {
            get
            {
                lock (gate)
                {
                    return running != null && !running.IsCompleted;
                }
            }
        }

        /// <summary>Problems removing the staged bundle when this controller was disposed.</summary>
        public IReadOnlyList<StagingProblem> DisposeCleanupProblems
        {
            get
            {
                lock (gate)
                {
                    return disposeCleanupProblems;
                }
            }
        }

        /// <summary>Starts a preparation. Refused (at once, without starting anything) while one is running, while a prepared
        /// bundle is held, or once disposed. <paramref name="progress"/> is called on the worker, synchronously.</summary>
        public Task<PreparationResult> PrepareAsync(IProgress<StagingProgress> progress = null)
        {
            lock (gate)
            {
                if (disposed)
                {
                    return Task.FromResult(PreparationResult.Refused(PreparationRefusal.Disposed));
                }
                if (running != null && !running.IsCompleted)
                {
                    return Task.FromResult(PreparationResult.Refused(PreparationRefusal.AlreadyPreparing));
                }
                if (staged != null)
                {
                    return Task.FromResult(PreparationResult.Refused(PreparationRefusal.AlreadyPrepared));
                }
                cancellation?.Dispose();
                cancellation = new CancellationTokenSource();
                CancellationToken token = cancellation.Token;
                running = Task.Factory.StartNew(() => Work(token, progress), CancellationToken.None, TaskCreationOptions.LongRunning,
                    TaskScheduler.Default);
                return running;
            }
        }

        /// <summary>Asks a running preparation to stop at its next check. Nothing happens if none is running.</summary>
        public void Cancel()
        {
            lock (gate)
            {
                if (running != null && !running.IsCompleted)
                {
                    cancellation.Cancel();
                }
            }
        }

        /// <summary>The staged bundle, only while prepared and held (never once released, disposed or not staged).</summary>
        public StagedBundle TryGetStagedBundle()
        {
            lock (gate)
            {
                return staged != null && !staged.IsDisposed && staged.IsStaged && !disposed ? staged : null;
            }
        }

        /// <summary>Removes the prepared bundle (for example when going back), returning any cleanup problems. Refused (null)
        /// while a preparation is running; an empty list when nothing was held.</summary>
        public IReadOnlyList<StagingProblem> Release()
        {
            lock (gate)
            {
                if (running != null && !running.IsCompleted)
                {
                    return null;
                }
                return ReleaseHeld();
            }
        }

        /// <summary>Cancels a running preparation and WAITS for its worker to return (never abandoning it), then removes the
        /// prepared bundle.
        ///
        /// Called from the worker itself -- from a synchronous progress callback -- it cannot wait for its own thread: it then
        /// marks the controller disposed, requests cancellation and RETURNS BEFORE CLEANUP HAS COMPLETED. The worker, which is
        /// that same thread, carries on as soon as the callback returns: staging stops at its next check and removes what it
        /// created, and a bundle staged meanwhile is never kept. That cleanup's problems are in the preparation's own
        /// result (PreparationResult.CleanupProblems), not in DisposeCleanupProblems.</summary>
        public void Dispose()
        {
            Task<PreparationResult> worker;
            lock (gate)
            {
                if (disposed)
                {
                    return;
                }
                disposed = true;
                worker = running;
                if (worker != null && !worker.IsCompleted)
                {
                    cancellation.Cancel();
                    if (ReferenceEquals(Thread.CurrentThread, workerThread))
                    {
                        // Never wait for oneself. Nothing is held (a preparation never runs while a bundle is held), and the
                        // token source stays undisposed: the worker is still using it.
                        return;
                    }
                }
            }
            try
            {
                worker?.Wait();
            }
            catch (AggregateException)
            {
                // The worker reports its own failures; it never throws (see Work).
            }
            lock (gate)
            {
                disposeCleanupProblems = ReleaseHeld();
                cancellation?.Dispose();
                cancellation = null;
            }
        }

        private IReadOnlyList<StagingProblem> ReleaseHeld()
        {
            StagedBundle held = staged;
            staged = null;
            if (held == null)
            {
                return new List<StagingProblem>();
            }
            held.Dispose();
            return held.CleanupProblems;
        }

        private PreparationResult Work(CancellationToken token, IProgress<StagingProgress> progress)
        {
            StagingRunScan earlierRuns = null;
            StagedBundle result = null;
            lock (gate)
            {
                workerThread = Thread.CurrentThread;
            }
            try
            {
                if (pin.State == TrustedBundlePinState.NotConfigured)
                {
                    return PreparationResult.Refused(PreparationRefusal.PinNotConfigured);
                }
                if (!pin.IsConfigured)
                {
                    return PreparationResult.Refused(PreparationRefusal.PinInvalid);
                }
                if (!isElevated())
                {
                    return PreparationResult.Refused(PreparationRefusal.NotElevated);
                }
                if (!BundleFolderExists())
                {
                    return PreparationResult.Refused(PreparationRefusal.BundleMissing);
                }
                earlierRuns = BundleStager.ClassifyRuns(policy, token); // read-only: classifies, never removes
                token.ThrowIfCancellationRequested();

                result = BundleStager.Stage(sourceRoot, pin.ManifestSha256, pin.Version, policy, token, progress);
                lock (gate)
                {
                    if (result.IsStaged && !disposed && !token.IsCancellationRequested)
                    {
                        staged = result;
                        result = null; // held: no longer this method's to remove
                        return PreparationResult.From(staged, earlierRuns);
                    }
                }
                if (result.IsStaged)
                {
                    // Staged, but cancelled or disposed meanwhile: never kept.
                    StagedBundle discarded = result;
                    result = null;
                    discarded.Dispose();
                    return PreparationResult.Cancelled(discarded, earlierRuns);
                }
                return PreparationResult.From(result, earlierRuns); // Stage already removed what it created
            }
            catch (OperationCanceledException)
            {
                return PreparationResult.Unexpected(earlierRuns, PreparationOutcome.Cancelled);
            }
            catch (Exception)
            {
                return PreparationResult.Unexpected(earlierRuns, PreparationOutcome.Failed);
            }
            finally
            {
                result?.Dispose(); // only ever a staging not handed over
                lock (gate)
                {
                    workerThread = null;
                }
            }
        }

        private bool BundleFolderExists()
        {
            try
            {
                return (File.GetAttributes(sourceRoot) & FileAttributes.Directory) != 0; // a link is left to BundleVerifier to refuse
            }
            catch (Exception e) when (e is FileNotFoundException || e is DirectoryNotFoundException || e is ArgumentException ||
                                      e is NotSupportedException)
            {
                return false;
            }
        }

        private static bool IsElevatedAdministrator()
        {
            using (WindowsIdentity identity = WindowsIdentity.GetCurrent())
            {
                // True only for an elevated token: in a filtered (UAC) token the Administrators group is deny-only.
                return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator);
            }
        }
    }
}
