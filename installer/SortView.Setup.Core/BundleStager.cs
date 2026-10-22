using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace SortView.Setup.Core
{
    /// <summary>Why a bundle was not staged, or not cleaned up. Paths are folder paths or bundle-relative file paths --
    /// never file contents.</summary>
    public enum StagingProblemCode
    {
        /// <summary>The expected hash or version, the source path or the run id is not acceptable.</summary>
        RequestInvalid,
        /// <summary>The staging root is not an absolute local path below a drive root.</summary>
        StagingRootInvalid,
        /// <summary>The staging volume is not NTFS or ReFS (no permissions to rely on).</summary>
        UnsupportedVolume,
        /// <summary>A junction, symbolic link or other reparse point where a plain folder or file must be.</summary>
        ReparsePoint,
        NotADirectory,
        /// <summary>A folder in the staging chain is owned by a principal the policy does not trust.</summary>
        UntrustedOwner,
        /// <summary>A folder in the staging chain lets a principal the policy does not trust change it.</summary>
        WritableByUntrusted,
        /// <summary>A folder could not be created, or not with the required owner and permissions.</summary>
        CreateFailed,
        /// <summary>A newly created folder does not have exactly the required owner and permissions.</summary>
        PermissionsNotApplied,
        /// <summary>The run folder already exists: staging only ever uses a folder it has just created.</summary>
        RunFolderExists,
        InsufficientSpace,
        /// <summary>The source bundle did not pass BundleVerifier (see SourceVerification).</summary>
        SourceNotVerified,
        /// <summary>The source changed after it was verified: its manifest or a file no longer matches.</summary>
        SourceChanged,
        /// <summary>A source path component is a reparse point, or not a plain file or folder.</summary>
        SourceUnsafe,
        CopyFailed,
        AccessDenied,
        Cancelled,
        /// <summary>The staged copy did not pass BundleVerifier (see StagedVerification).</summary>
        StagedNotVerified,
        /// <summary>The staging folder could not be removed completely, or was not a safe target to remove.</summary>
        CleanupFailed,
        UnexpectedError,
        /// <summary>The run folder could not be pinned open (the handle that keeps it from being deleted or renamed while
        /// the run is alive), or the handle did not show a plain folder.</summary>
        RunLockFailed,
        /// <summary>The run's identity marker (staging-run.json, beside the bundle) could not be written.</summary>
        RunMarkerFailed,
    }

    /// <summary>TEST ONLY: a failure to inject at one step, honored only by a policy that is not
    /// <see cref="StagingPolicy.Production"/>.</summary>
    internal enum StagingFault
    {
        None,
        /// <summary>Fail after the run folder's handle is opened, before it is kept.</summary>
        RunLock,
        /// <summary>Fail after the marker file is created, before it is written.</summary>
        RunMarker,
    }

    public sealed class StagingProblem
    {
        internal StagingProblem(StagingProblemCode code, string path)
        {
            Code = code;
            Path = path ?? "";
        }

        public StagingProblemCode Code { get; }

        public string Path { get; }

        public override string ToString() => Code + (Path.Length == 0 ? "" : ": " + Path);
    }

    public enum StagingStep
    {
        SourceVerified,
        FileCopied,
        StagedVerified,
    }

    /// <summary>Progress, reported on the staging thread, synchronously.</summary>
    public sealed class StagingProgress
    {
        internal StagingProgress(StagingStep step, string path, int done, int total)
        {
            Step = step;
            Path = path;
            Done = done;
            Total = total;
        }

        public StagingStep Step { get; }

        /// <summary>For FileCopied: the bundle-relative path just copied.</summary>
        public string Path { get; }

        public int Done { get; }

        public int Total { get; }
    }

    /// <summary>
    /// Where staging happens and whom it trusts. <see cref="Production"/> is the installer's: %ProgramFiles%\SortView\
    /// SetupStaging, trusting only SYSTEM, Administrators and TrustedInstaller, folders created for SYSTEM and Administrators
    /// only and owned by Administrators. Any other policy (tests use one rooted in a temporary folder that also trusts the
    /// current user) is built explicitly; nothing ever falls back to it.
    /// </summary>
    public sealed class StagingPolicy
    {
        private static readonly SecurityIdentifier SystemSid = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);
        private static readonly SecurityIdentifier AdministratorsSid = new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null);
        // NT SERVICE\TrustedInstaller, owner of the Windows and Program Files folders.
        private static readonly SecurityIdentifier TrustedInstallerSid =
            new SecurityIdentifier("S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464");

        /// <param name="stagingRoot">Absolute path of the folder run folders are created in.</param>
        /// <param name="trustedPrincipals">Who may own, and change, any folder from the drive root down to the staging root.</param>
        /// <param name="accessPrincipals">Who gets full control of the folders staging creates (no one else gets anything).
        /// Each must also be trusted.</param>
        /// <param name="owner">The owner of the folders staging creates, or null to leave the creator as owner (it must then be
        /// trusted).</param>
        public StagingPolicy(string stagingRoot, IEnumerable<SecurityIdentifier> trustedPrincipals,
            IEnumerable<SecurityIdentifier> accessPrincipals, SecurityIdentifier owner)
            : this(stagingRoot, trustedPrincipals, accessPrincipals, owner, false)
        {
        }

        private StagingPolicy(string stagingRoot, IEnumerable<SecurityIdentifier> trustedPrincipals,
            IEnumerable<SecurityIdentifier> accessPrincipals, SecurityIdentifier owner, bool isProduction)
        {
            StagingRoot = stagingRoot;
            TrustedPrincipals = new ReadOnlyCollection<SecurityIdentifier>((trustedPrincipals ?? Enumerable.Empty<SecurityIdentifier>()).Distinct().ToList());
            AccessPrincipals = new ReadOnlyCollection<SecurityIdentifier>((accessPrincipals ?? Enumerable.Empty<SecurityIdentifier>()).Distinct().ToList());
            Owner = owner;
            this.isProduction = isProduction;
        }

        private readonly bool isProduction;
        private StagingFault injectedFault;

        /// <summary>The installer's policy. Built fresh each time; it has no settable state.</summary>
        public static StagingPolicy Production =>
            new StagingPolicy(
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "SortView", "SetupStaging"),
                new[] { SystemSid, AdministratorsSid, TrustedInstallerSid },
                new[] { SystemSid, AdministratorsSid },
                AdministratorsSid,
                true);

        /// <summary>TEST ONLY (internal; tests reach it by reflection on a policy they built): a failure to inject. Always
        /// None for the production policy, which refuses to have one set.</summary>
        internal StagingFault InjectedFault
        {
            get => isProduction ? StagingFault.None : injectedFault;
            set
            {
                if (isProduction)
                {
                    throw new InvalidOperationException("The production staging policy takes no injected fault.");
                }
                injectedFault = value;
            }
        }

        public string StagingRoot { get; }

        public IReadOnlyList<SecurityIdentifier> TrustedPrincipals { get; }

        public IReadOnlyList<SecurityIdentifier> AccessPrincipals { get; }

        public SecurityIdentifier Owner { get; }

        internal bool IsTrusted(IdentityReference identity) =>
            identity is SecurityIdentifier sid && TrustedPrincipals.Contains(sid);
    }

    /// <summary>
    /// A bundle staged by <see cref="BundleStager"/>. While it is alive and <see cref="IsStaged"/>, <see cref="BundlePath"/>
    /// holds exactly the verified bundle, and the run folder is pinned: this object holds a handle that keeps it from being
    /// deleted or renamed by anyone (this process included). Dispose removes the run folder this staging created -- and
    /// nothing else -- and releases the handle. Keep this object referenced for as long as <see cref="BundlePath"/> is used:
    /// if it is dropped, the handle is released when it is collected.
    /// </summary>
    public sealed class StagedBundle : IDisposable
    {
        private readonly List<StagingProblem> cleanupProblems = new List<StagingProblem>();
        private readonly string stagingRoot;
        private string runFolder;
        private SafeFileHandle runLock;

        internal StagedBundle(IList<StagingProblem> problems, BundleVerification sourceVerification, BundleVerification stagedVerification,
            string stagingRoot, string runFolder, string bundlePath, SafeFileHandle runLock = null)
        {
            Problems = new ReadOnlyCollection<StagingProblem>(problems);
            SourceVerification = sourceVerification;
            StagedVerification = stagedVerification;
            this.stagingRoot = stagingRoot;
            this.runFolder = runFolder;
            this.runLock = runLock;
            IsStaged = problems.Count == 0 && bundlePath != null && stagedVerification != null && stagedVerification.IsVerified;
            BundlePath = IsStaged ? bundlePath : null;
            Version = IsStaged ? stagedVerification.Version : null;
            FileCount = IsStaged ? stagedVerification.FileCount : 0;
        }

        /// <summary>True only when every step passed: the source verified, every file was copied matching its manifest entry
        /// into a folder this staging created with the required owner and permissions, and the staged copy verified on its own.</summary>
        public bool IsStaged { get; }

        /// <summary>The staged bundle folder (holding MANIFEST.json), or null when not staged or once disposed.</summary>
        public string BundlePath { get; private set; }

        public string Version { get; }

        public int FileCount { get; }

        public IReadOnlyList<StagingProblem> Problems { get; }

        /// <summary>BundleVerifier's result for the source (null if staging stopped before it ran).</summary>
        public BundleVerification SourceVerification { get; }

        /// <summary>BundleVerifier's result for the staged copy (null if staging stopped before it ran).</summary>
        public BundleVerification StagedVerification { get; }

        public bool IsDisposed { get; private set; }

        /// <summary>Problems removing the run folder, on Dispose (or on a failed staging, already done).</summary>
        public IReadOnlyList<StagingProblem> CleanupProblems => cleanupProblems.AsReadOnly();

        internal void AddCleanupProblems(IEnumerable<StagingProblem> problems) => cleanupProblems.AddRange(problems);

        public void Dispose()
        {
            if (IsDisposed)
            {
                return;
            }
            IsDisposed = true;
            BundlePath = null;
            SafeFileHandle held = runLock;
            runLock = null;
            if (runFolder != null)
            {
                cleanupProblems.AddRange(BundleStager.RemoveRunFolder(stagingRoot, runFolder, held)); // closes the handle
                runFolder = null;
            }
            else
            {
                held?.Dispose();
            }
        }
    }

    /// <summary>What <see cref="BundleStager.ClassifyRuns(StagingPolicy, CancellationToken)"/> found for one entry of the
    /// staging root. None of these is permission to delete anything.</summary>
    public enum StagingRunState
    {
        /// <summary>The folder could not be opened for the classifier's check because something holds it open without
        /// sharing delete: a live staging run -- or another program. It is not proof that a SortView process is alive.</summary>
        Active,
        /// <summary>Every check passed and no conflicting handle was held at that moment: it looks exactly like a SortView
        /// run nobody is using. A candidate only -- never by itself permission to delete it.</summary>
        AbandonedCandidate,
        /// <summary>Anything that could not be confidently classified; <see cref="StagingRunEntry.Reason"/> says why.</summary>
        Unknown,
    }

    public enum StagingRunReason
    {
        /// <summary>AbandonedCandidate: every check passed.</summary>
        None,
        /// <summary>Active: a sharing conflict on the classifier's check.</summary>
        InUse,
        NotAFolder,
        ReparsePoint,
        /// <summary>The name is not a run id (32 lowercase hexadecimal characters).</summary>
        NotARunName,
        /// <summary>No staging-run.json (an older run, a run not yet marked, or one being removed). Such a folder is never
        /// opened for the check, so a staging still being set up is never disturbed.</summary>
        MarkerMissing,
        /// <summary>The marker is not a plain file (a folder or a link), or is too large.</summary>
        MarkerUnsafe,
        MarkerInvalid,
        /// <summary>The marker names a different run id than its folder.</summary>
        MarkerMismatch,
        /// <summary>The folder's owner or permissions are not exactly those staging creates.</summary>
        UnexpectedSecurity,
        AccessDenied,
        /// <summary>It disappeared while being classified.</summary>
        Vanished,
        /// <summary>The check failed with another Windows error (see <see cref="StagingRunEntry.Detail"/>).</summary>
        ProbeFailed,
        Error,
    }

    /// <summary>One direct child of the staging root, as classified. Names, paths, reasons and codes only -- never file
    /// contents.</summary>
    public sealed class StagingRunEntry
    {
        internal StagingRunEntry(string name, string path, StagingRunState state, StagingRunReason reason, string detail = "",
            string runId = null, string manifestSha256 = null, string createdUtc = null)
        {
            Name = name;
            Path = path;
            State = state;
            Reason = reason;
            Detail = detail ?? "";
            RunId = runId;
            ManifestSha256 = manifestSha256;
            CreatedUtc = createdUtc;
        }

        public string Name { get; }

        public string Path { get; }

        public StagingRunState State { get; }

        public StagingRunReason Reason { get; }

        /// <summary>For example the Windows error code; empty when there is nothing to add.</summary>
        public string Detail { get; }

        /// <summary>From a valid marker only (AbandonedCandidate).</summary>
        public string RunId { get; }

        /// <summary>From a valid marker only (AbandonedCandidate).</summary>
        public string ManifestSha256 { get; }

        /// <summary>From a valid marker only, for information: never used to classify.</summary>
        public string CreatedUtc { get; }

        public override string ToString() => Name + ": " + State + (Reason == StagingRunReason.None ? "" : " (" + Reason + ")") +
            (Detail.Length == 0 ? "" : " " + Detail);
    }

    /// <summary>The result of a read-only classification of a staging root's entries.</summary>
    public sealed class StagingRunScan
    {
        internal StagingRunScan(bool completed, bool rootExists, IList<StagingProblem> rootProblems, IList<StagingRunEntry> entries, bool truncated)
        {
            Completed = completed;
            RootExists = rootExists;
            RootProblems = new ReadOnlyCollection<StagingProblem>(rootProblems);
            Entries = new ReadOnlyCollection<StagingRunEntry>(entries);
            Truncated = truncated;
        }

        /// <summary>False when the root was refused, the scan was cancelled, or it stopped on an error (see
        /// <see cref="RootProblems"/>). Entries already classified are kept.</summary>
        public bool Completed { get; }

        /// <summary>False when the staging root does not exist (nothing to classify; nothing is created).</summary>
        public bool RootExists { get; }

        /// <summary>Why the root was refused, by the same checks (and codes) as staging's.</summary>
        public IReadOnlyList<StagingProblem> RootProblems { get; }

        public IReadOnlyList<StagingRunEntry> Entries { get; }

        /// <summary>The root held more entries than are classified in one scan.</summary>
        public bool Truncated { get; }
    }

    /// <summary>
    /// Stages a frozen Collector release bundle: verifies the source with <see cref="BundleVerifier"/>, copies exactly the
    /// files its manifest lists into a new run folder with protected permissions, and verifies the copy again on its own.
    /// It never runs anything from the bundle, never copies by wildcard, never follows a link, and never repairs anything:
    /// an unsafe folder or permission is refused. A failed or cancelled staging removes the run folder it created.
    ///
    /// Order: (1) the request; (2) the staging chain, drive root to staging root -- plain folders, trusted owners, nobody
    /// untrusted able to change them (at the drive root only "create folders" is tolerated, as Windows grants it to every
    /// user there), NTFS or ReFS; (3) BundleVerifier on the source; (4) missing staging-root folders and the run folder,
    /// each created new (CreateDirectoryW, failing if it exists) with its owner and protected permissions in place from the
    /// start, then read back; (4b) the run folder pinned by a handle held until the run is disposed (no one can delete or
    /// rename it meanwhile), checked again through and under that handle, and marked with staging-run.json beside the
    /// bundle; (5) the source manifest read again and required to hash to the expected value, then each
    /// listed file copied -- its path validated, every source component checked to be a plain folder or file, its size
    /// and SHA-256 computed while copying; (6) the chain and run-folder permissions checked again; (7) BundleVerifier on
    /// the staged copy, with the same expected hash and version.
    /// </summary>
    public static class BundleStager
    {
        private const int ErrorAlreadyExists = 183;
        private const int ErrorFileExists = 80;
        private const long SpaceMargin = 64L * 1024 * 1024;
        private const int CopyBufferSize = 81920;

        // Any right that lets a principal change a folder, what it holds, or who may: write data / create files, append
        // data / create folders, write extended attributes, delete child, write attributes, delete, change permissions,
        // take ownership, and generic write / all.
        private const int WriteRights = 0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000 | 0x10000000 | 0x40000000;
        // At a drive root Windows lets every user create folders (and nothing else): the only right tolerated there.
        private const int CreateFoldersRight = 0x4;
        // OWNER RIGHTS: an entry for this SID applies to the object's current owner, whoever that is.
        private static readonly SecurityIdentifier OwnerRightsSid = new SecurityIdentifier("S-1-3-4");

        /// <summary>The installer's entry point: always <see cref="StagingPolicy.Production"/>.</summary>
        public static StagedBundle Stage(string sourceRoot, string expectedManifestSha256, string expectedVersion,
            CancellationToken cancellationToken, IProgress<StagingProgress> progress = null) =>
            Stage(sourceRoot, expectedManifestSha256, expectedVersion, StagingPolicy.Production, cancellationToken, progress);

        /// <summary>Stages under <paramref name="policy"/>. <paramref name="runId"/> names the run folder; it is a new random id
        /// unless given (an existing folder is never reused: CreateDirectoryW fails and staging stops).</summary>
        public static StagedBundle Stage(string sourceRoot, string expectedManifestSha256, string expectedVersion, StagingPolicy policy,
            CancellationToken cancellationToken, IProgress<StagingProgress> progress = null, Guid? runId = null)
        {
            var problems = new List<StagingProblem>();
            BundleVerification sourceVerification = null;
            BundleVerification stagedVerification = null;
            string root = null;
            string runFolder = null;
            string bundlePath = null;
            SafeFileHandle runLock = null;
            try
            {
                // (1) The request.
                string expectedHash = (expectedManifestSha256 ?? "").Trim();
                if (!BundleVerifier.Sha256Pattern.IsMatch(expectedHash) || (expectedVersion != null && !BundleVerifier.IsValidVersion(expectedVersion)) ||
                    policy == null || policy.AccessPrincipals.Count == 0 || policy.AccessPrincipals.Any(p => !policy.TrustedPrincipals.Contains(p)) ||
                    (policy.Owner != null && !policy.TrustedPrincipals.Contains(policy.Owner)) || (runId.HasValue && runId.Value == Guid.Empty))
                {
                    problems.Add(new StagingProblem(StagingProblemCode.RequestInvalid, ""));
                    return new StagedBundle(problems, null, null, null, null, null);
                }
                root = NormalizeLocalPath(policy.StagingRoot);
                if (root == null)
                {
                    problems.Add(new StagingProblem(StagingProblemCode.StagingRootInvalid, policy.StagingRoot));
                    return new StagedBundle(problems, null, null, null, null, null);
                }

                // (2) The staging chain, before anything is read or created.
                List<string> missing = CheckChain(root, policy, problems);
                if (problems.Count > 0)
                {
                    return new StagedBundle(problems, null, null, root, null, null);
                }

                // (3) The whole source, by the same strict rules as the staged copy will be.
                sourceVerification = BundleVerifier.Verify(sourceRoot, expectedHash, expectedVersion);
                Report(progress, StagingStep.SourceVerified, null, 0, sourceVerification.FileCount);
                if (!sourceVerification.IsVerified)
                {
                    problems.Add(new StagingProblem(StagingProblemCode.SourceNotVerified, ""));
                    return new StagedBundle(problems, sourceVerification, null, root, null, null);
                }
                string source = Path.GetFullPath(sourceRoot).TrimEnd('\\', '/');
                cancellationToken.ThrowIfCancellationRequested();

                // (4) Missing staging-root folders, then the run folder: each created new, protected from the start.
                foreach (string folder in missing)
                {
                    CreateProtected(folder, policy, problems, tolerateExisting: true);
                    if (problems.Count > 0)
                    {
                        return new StagedBundle(problems, sourceVerification, null, root, null, null);
                    }
                }
                runFolder = Path.Combine(root, (runId ?? Guid.NewGuid()).ToString("N"));
                CreateProtected(runFolder, policy, problems, tolerateExisting: false);
                if (problems.Count > 0)
                {
                    // Not ours to remove: either it existed already, or it was never created.
                    runFolder = null;
                    return new StagedBundle(problems, sourceVerification, null, root, null, null);
                }

                // (4b) Pin the run folder for the life of the run, check it again now it cannot be swapped, and mark it.
                runLock = AcquireRunLock(runFolder, policy, problems);
                if (problems.Count > 0)
                {
                    return Fail(problems, sourceVerification, root, runFolder, runLock: runLock);
                }
                CheckCreated(runFolder, policy, problems);
                if (problems.Count > 0)
                {
                    return Fail(problems, sourceVerification, root, runFolder, runLock: runLock);
                }
                WriteRunMarker(runFolder, expectedHash, policy, problems);
                if (problems.Count > 0)
                {
                    return Fail(problems, sourceVerification, root, runFolder, runLock: runLock);
                }
                bundlePath = Path.Combine(runFolder, "bundle");
                Directory.CreateDirectory(bundlePath);

                // (5) The manifest again (the source is not trusted for having verified earlier), then the listed files.
                byte[] manifestBytes = ReadSourceManifest(source, expectedHash, problems);
                if (problems.Count > 0)
                {
                    return Fail(problems, sourceVerification, root, runFolder, runLock: runLock);
                }
                var report = new BundleVerifier.Report();
                BundleVerifier.Manifest manifest = BundleVerifier.Parse(manifestBytes, report);
                if (manifest == null || report.Any)
                {
                    problems.Add(new StagingProblem(StagingProblemCode.SourceChanged, BundleVerifier.ManifestFileName));
                    return Fail(problems, sourceVerification, root, runFolder, runLock: runLock);
                }
                long needed = SpaceMargin + manifestBytes.Length + manifest.Files.Sum(f => f.SizeBytes);
                if (new DriveInfo(Path.GetPathRoot(root)).AvailableFreeSpace < needed)
                {
                    problems.Add(new StagingProblem(StagingProblemCode.InsufficientSpace, root));
                    return Fail(problems, sourceVerification, root, runFolder, runLock: runLock);
                }
                int done = 0;
                foreach (BundleVerifier.ManifestFile entry in manifest.Files)
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    CopyListedFile(source, bundlePath, entry, cancellationToken, problems);
                    if (problems.Count > 0)
                    {
                        return Fail(problems, sourceVerification, root, runFolder, runLock: runLock);
                    }
                    Report(progress, StagingStep.FileCopied, entry.Path, ++done, manifest.Files.Count);
                }
                WriteNew(Path.Combine(bundlePath, BundleVerifier.ManifestFileName), manifestBytes);

                // (6) The chain and the run folder, again.
                CheckChain(root, policy, problems, mustExist: true);
                CheckCreated(runFolder, policy, problems);
                if (problems.Count > 0)
                {
                    return Fail(problems, sourceVerification, root, runFolder, runLock: runLock);
                }

                // (7) The staged copy, on its own, by the same rules.
                cancellationToken.ThrowIfCancellationRequested();
                stagedVerification = BundleVerifier.Verify(bundlePath, expectedHash, expectedVersion);
                if (!stagedVerification.IsVerified)
                {
                    problems.Add(new StagingProblem(StagingProblemCode.StagedNotVerified, ""));
                    return Fail(problems, sourceVerification, root, runFolder, stagedVerification, runLock);
                }
                Report(progress, StagingStep.StagedVerified, null, manifest.Files.Count, manifest.Files.Count);
                var staged = new StagedBundle(problems, sourceVerification, stagedVerification, root, runFolder, bundlePath, runLock);
                runLock = null; // the staged bundle owns it now
                return staged;
            }
            catch (OperationCanceledException)
            {
                problems.Add(new StagingProblem(StagingProblemCode.Cancelled, ""));
            }
            catch (UnauthorizedAccessException)
            {
                problems.Add(new StagingProblem(StagingProblemCode.AccessDenied, ""));
            }
            catch (IOException)
            {
                problems.Add(new StagingProblem(StagingProblemCode.CopyFailed, ""));
            }
            catch (Exception)
            {
                problems.Add(new StagingProblem(StagingProblemCode.UnexpectedError, ""));
            }
            // Every exit after the run folder was pinned passes runLock on: to the StagedBundle on success, otherwise to Fail,
            // whose cleanup closes it (RemoveRunFolder closes it in a finally, and never throws).
            return Fail(problems, sourceVerification, root, runFolder, stagedVerification, runLock);
        }

        /// <summary>A failed staging: removes the run folder it created (through <paramref name="runLock"/> when it was
        /// pinned, which also closes the handle).</summary>
        private static StagedBundle Fail(List<StagingProblem> problems, BundleVerification sourceVerification, string root, string runFolder,
            BundleVerification stagedVerification = null, SafeFileHandle runLock = null)
        {
            var result = new StagedBundle(problems, sourceVerification, stagedVerification, root, null, null);
            if (runFolder != null)
            {
                result.AddCleanupProblems(RemoveRunFolder(root, runFolder, runLock));
            }
            else
            {
                runLock?.Dispose();
            }
            return result;
        }

        private static void Report(IProgress<StagingProgress> progress, StagingStep step, string path, int done, int total) =>
            progress?.Report(new StagingProgress(step, path, done, total));

        // ---- the staging chain -------------------------------------------------------------------------------------

        /// <summary>An absolute local drive path (not UNC, not a device path), normalized, without a trailing separator, and
        /// not a drive root itself.</summary>
        private static string NormalizeLocalPath(string path)
        {
            if (string.IsNullOrWhiteSpace(path) || path.StartsWith(@"\\", StringComparison.Ordinal) || path.StartsWith("//", StringComparison.Ordinal) ||
                !Path.IsPathRooted(path))
            {
                return null;
            }
            try
            {
                string full = Path.GetFullPath(path).TrimEnd('\\', '/');
                return full.Length > 3 && full[1] == ':' && full[2] == '\\' ? full : null;
            }
            catch (Exception)
            {
                return null;
            }
        }

        /// <summary>Checks every existing folder from the drive root down to <paramref name="root"/>; returns the folders
        /// still to create (only ever the last ones of the chain). Never changes anything.</summary>
        private static List<string> CheckChain(string root, StagingPolicy policy, List<StagingProblem> problems, bool mustExist = false)
        {
            var missing = new List<string>();
            string driveRoot = Path.GetPathRoot(root);
            string format;
            try
            {
                format = new DriveInfo(driveRoot).DriveFormat;
            }
            catch (Exception)
            {
                format = null;
            }
            if (!string.Equals(format, "NTFS", StringComparison.OrdinalIgnoreCase) && !string.Equals(format, "ReFS", StringComparison.OrdinalIgnoreCase))
            {
                problems.Add(new StagingProblem(StagingProblemCode.UnsupportedVolume, driveRoot));
                return missing;
            }
            var chain = new List<string> { driveRoot };
            string[] parts = root.Substring(driveRoot.Length).Split('\\');
            string current = driveRoot.TrimEnd('\\');
            foreach (string part in parts)
            {
                current = current + "\\" + part;
                chain.Add(current);
            }
            foreach (string folder in chain)
            {
                FileAttributes attributes;
                try
                {
                    attributes = File.GetAttributes(folder); // the entry itself: a junction reports ReparsePoint here
                }
                catch (Exception e) when (e is FileNotFoundException || e is DirectoryNotFoundException)
                {
                    if (mustExist)
                    {
                        problems.Add(new StagingProblem(StagingProblemCode.NotADirectory, folder));
                        return missing;
                    }
                    missing.Add(folder);
                    continue;
                }
                if (missing.Count > 0)
                {
                    // Something appeared below a folder that does not exist: never.
                    problems.Add(new StagingProblem(StagingProblemCode.StagingRootInvalid, folder));
                    return missing;
                }
                CheckExistingFolder(folder, attributes, policy, string.Equals(folder, driveRoot, StringComparison.OrdinalIgnoreCase), problems);
                if (problems.Count > 0)
                {
                    return missing;
                }
            }
            return missing;
        }

        private static void CheckExistingFolder(string folder, FileAttributes attributes, StagingPolicy policy, bool isDriveRoot,
            List<StagingProblem> problems)
        {
            if ((attributes & FileAttributes.ReparsePoint) != 0)
            {
                problems.Add(new StagingProblem(StagingProblemCode.ReparsePoint, folder));
                return;
            }
            if ((attributes & FileAttributes.Directory) == 0)
            {
                problems.Add(new StagingProblem(StagingProblemCode.NotADirectory, folder));
                return;
            }
            DirectorySecurity security = new DirectoryInfo(folder).GetAccessControl(AccessControlSections.Access | AccessControlSections.Owner);
            if (!policy.IsTrusted(security.GetOwner(typeof(SecurityIdentifier))))
            {
                problems.Add(new StagingProblem(StagingProblemCode.UntrustedOwner, folder));
                return;
            }
            foreach (FileSystemAccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier)))
            {
                if (rule.AccessControlType != AccessControlType.Allow || (rule.PropagationFlags & PropagationFlags.InheritOnly) != 0 ||
                    policy.IsTrusted(rule.IdentityReference) || OwnerRightsSid.Equals(rule.IdentityReference))
                {
                    // A deny grants nothing; an inherit-only entry does not apply to this folder; OWNER RIGHTS grants only
                    // whoever owns the folder, already required (above) to be trusted.
                    continue;
                }
                int rights = (int)rule.FileSystemRights & WriteRights;
                if (isDriveRoot)
                {
                    rights &= ~CreateFoldersRight;
                }
                if (rights != 0)
                {
                    problems.Add(new StagingProblem(StagingProblemCode.WritableByUntrusted, folder));
                    return;
                }
            }
        }

        // ---- creating folders ----------------------------------------------------------------------------------------

        [StructLayout(LayoutKind.Sequential)]
        private struct SecurityAttributes
        {
            public int Length;
            public IntPtr SecurityDescriptor;
            public int InheritHandle;
        }

        // A bool return is marshalled as a 4-byte Win32 BOOL by default; the error is read back with Win32Exception (which
        // reads the thread's last Win32 error, kept by SetLastError = true).
        [DllImport("kernel32.dll", EntryPoint = "CreateDirectoryW", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool CreateDirectoryWithSecurity(string path, ref SecurityAttributes attributes);

        // The run folder's lifetime handle: list access (so an exclusive open by anyone else conflicts) and DELETE (so the
        // folder is finally removed through this same handle), sharing read and write but NOT delete -- so while it is
        // open no one can delete or rename the folder, or rename the folders above it. Opened on the entry itself, never
        // through a link.
        private const uint FileListDirectory = 0x1;
        private const uint DeleteAccess = 0x10000;
        private const uint SynchronizeAccess = 0x100000;
        private const uint FileShareRead = 0x1;
        private const uint FileShareWrite = 0x2;
        private const uint OpenExisting = 3;
        private const uint FileFlagBackupSemantics = 0x02000000;
        private const uint FileFlagOpenReparsePoint = 0x00200000;
        private const int FileDispositionInfoClass = 4;

        [DllImport("kernel32.dll", EntryPoint = "CreateFileW", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern SafeFileHandle CreateFile(string path, uint access, uint share, IntPtr securityAttributes, uint creation,
            uint flags, IntPtr template);

        [StructLayout(LayoutKind.Sequential)]
        private struct ByHandleFileInformation
        {
            public uint FileAttributes;
            public System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
            public uint VolumeSerialNumber;
            public uint FileSizeHigh;
            public uint FileSizeLow;
            public uint NumberOfLinks;
            public uint FileIndexHigh;
            public uint FileIndexLow;
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GetFileInformationByHandle(SafeFileHandle handle, out ByHandleFileInformation information);

        [StructLayout(LayoutKind.Sequential)]
        private struct FileDispositionInfo
        {
            public byte DeleteFile;
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetFileInformationByHandle(SafeFileHandle handle, int informationClass, ref FileDispositionInfo information,
            int size);

        /// <summary>Whether <paramref name="handle"/> is open on a plain folder (a directory, not a reparse point).</summary>
        private static bool IsPlainFolderHandle(SafeFileHandle handle) =>
            GetFileInformationByHandle(handle, out ByHandleFileInformation information) &&
            (information.FileAttributes & (uint)FileAttributes.Directory) != 0 &&
            (information.FileAttributes & (uint)FileAttributes.ReparsePoint) == 0;

        /// <summary>Opens the run folder's lifetime handle and checks, through it, that it is a plain folder. Returns it, or
        /// null with a RunLockFailed problem -- and then no handle is left open.</summary>
        private static SafeFileHandle AcquireRunLock(string runFolder, StagingPolicy policy, List<StagingProblem> problems)
        {
            SafeFileHandle handle = CreateFile(runFolder, FileListDirectory | DeleteAccess | SynchronizeAccess, FileShareRead | FileShareWrite,
                IntPtr.Zero, OpenExisting, FileFlagBackupSemantics | FileFlagOpenReparsePoint, IntPtr.Zero);
            bool kept = false;
            try
            {
                if (handle.IsInvalid)
                {
                    problems.Add(new StagingProblem(StagingProblemCode.RunLockFailed, runFolder + " (error " + new Win32Exception().NativeErrorCode + ")"));
                    return null;
                }
                if (!IsPlainFolderHandle(handle))
                {
                    problems.Add(new StagingProblem(StagingProblemCode.RunLockFailed, runFolder));
                    return null;
                }
                if (policy.InjectedFault == StagingFault.RunLock)
                {
                    problems.Add(new StagingProblem(StagingProblemCode.RunLockFailed, runFolder + " (injected test fault)"));
                    return null;
                }
                kept = true;
                return handle;
            }
            finally
            {
                if (!kept)
                {
                    handle.Dispose();
                }
            }
        }

        /// <summary>The run's identity marker, beside (never inside) the bundle: created new, and holding nothing secret. It
        /// identifies the run; on its own it proves nothing (anyone who may change the run folder may write one).</summary>
        internal const string RunMarkerFileName = "staging-run.json";

        private static void WriteRunMarker(string runFolder, string expectedHash, StagingPolicy policy, List<StagingProblem> problems)
        {
            // Every value is a fixed string, a 32-hex run id, a 64-hex hash or an invariant timestamp: nothing to escape.
            string json = "{\n" +
                "  \"schema\": \"sortview.staging-run/1\",\n" +
                "  \"run_id\": \"" + Path.GetFileName(runFolder) + "\",\n" +
                "  \"manifest_sha256\": \"" + expectedHash.ToLowerInvariant() + "\",\n" +
                "  \"created_utc\": \"" + DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", System.Globalization.CultureInfo.InvariantCulture) + "\"\n" +
                "}\n";
            byte[] bytes = new UTF8Encoding(false).GetBytes(json);
            try
            {
                using (var output = new FileStream(Path.Combine(runFolder, RunMarkerFileName), FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    if (policy.InjectedFault == StagingFault.RunMarker)
                    {
                        throw new IOException("injected test fault"); // the file now exists, empty: cleanup must remove it
                    }
                    output.Write(bytes, 0, bytes.Length);
                    output.Flush(true);
                }
            }
            catch (Exception e) when (e is IOException || e is UnauthorizedAccessException)
            {
                problems.Add(new StagingProblem(StagingProblemCode.RunMarkerFailed, RunMarkerFileName));
            }
        }

        private static DirectorySecurity ProtectedSecurity(StagingPolicy policy)
        {
            var security = new DirectorySecurity();
            security.SetAccessRuleProtection(true, false);
            if (policy.Owner != null)
            {
                security.SetOwner(policy.Owner);
            }
            foreach (SecurityIdentifier sid in policy.AccessPrincipals)
            {
                security.AddAccessRule(new FileSystemAccessRule(sid, FileSystemRights.FullControl,
                    InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None, AccessControlType.Allow));
            }
            return security;
        }

        /// <summary>Creates <paramref name="folder"/> NEW, with the policy's owner and protected permissions in its security
        /// descriptor from the start (CreateDirectoryW fails if anything is already there), then reads them back.
        /// <paramref name="tolerateExisting"/>: for a staging-root folder that appeared meanwhile -- it is then checked like
        /// any existing folder of the chain, never adopted as one staging created.</summary>
        private static void CreateProtected(string folder, StagingPolicy policy, List<StagingProblem> problems, bool tolerateExisting)
        {
            byte[] descriptor = ProtectedSecurity(policy).GetSecurityDescriptorBinaryForm();
            GCHandle pinned = GCHandle.Alloc(descriptor, GCHandleType.Pinned);
            try
            {
                var attributes = new SecurityAttributes
                {
                    Length = Marshal.SizeOf(typeof(SecurityAttributes)), // 24 bytes in a 64-bit process, 12 in a 32-bit one
                    SecurityDescriptor = pinned.AddrOfPinnedObject(),
                    InheritHandle = 0,
                };
                if (!CreateDirectoryWithSecurity(folder, ref attributes))
                {
                    int error = new Win32Exception().NativeErrorCode;
                    if (error == ErrorAlreadyExists || error == ErrorFileExists)
                    {
                        if (tolerateExisting)
                        {
                            CheckExistingFolder(folder, File.GetAttributes(folder), policy, false, problems);
                            return;
                        }
                        problems.Add(new StagingProblem(StagingProblemCode.RunFolderExists, folder));
                        return;
                    }
                    problems.Add(new StagingProblem(StagingProblemCode.CreateFailed, folder + " (error " + error + ")"));
                    return;
                }
            }
            finally
            {
                pinned.Free();
            }
            CheckCreated(folder, policy, problems);
        }

        /// <summary>A folder staging created: a plain folder, protected (no inherited entries), owned as the policy says,
        /// granting full control to exactly the policy's access principals and nothing to anyone else.</summary>
        private static void CheckCreated(string folder, StagingPolicy policy, List<StagingProblem> problems)
        {
            FileAttributes attributes = File.GetAttributes(folder);
            if ((attributes & FileAttributes.ReparsePoint) != 0 || (attributes & FileAttributes.Directory) == 0)
            {
                problems.Add(new StagingProblem(StagingProblemCode.ReparsePoint, folder));
                return;
            }
            DirectorySecurity security = new DirectoryInfo(folder).GetAccessControl(AccessControlSections.Access | AccessControlSections.Owner);
            if (!IsCreatedSecurity(security, policy))
            {
                problems.Add(new StagingProblem(StagingProblemCode.PermissionsNotApplied, folder));
            }
        }

        /// <summary>The security of a folder staging created: owned as the policy says, protected (no inherited entries), and
        /// granting full control to exactly the policy's access principals and nothing to anyone else.</summary>
        private static bool IsCreatedSecurity(DirectorySecurity security, StagingPolicy policy)
        {
            var owner = security.GetOwner(typeof(SecurityIdentifier)) as SecurityIdentifier;
            bool ownerOk = policy.Owner != null ? policy.Owner.Equals(owner) : owner != null && policy.TrustedPrincipals.Contains(owner);
            var granted = new HashSet<SecurityIdentifier>();
            bool rulesOk = security.AreAccessRulesProtected;
            foreach (FileSystemAccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier)))
            {
                var sid = rule.IdentityReference as SecurityIdentifier;
                if (rule.IsInherited || rule.AccessControlType != AccessControlType.Allow || sid == null || !policy.AccessPrincipals.Contains(sid) ||
                    (rule.FileSystemRights & FileSystemRights.FullControl) != FileSystemRights.FullControl)
                {
                    rulesOk = false;
                    continue;
                }
                granted.Add(sid);
            }
            return ownerOk && rulesOk && policy.AccessPrincipals.All(granted.Contains);
        }

        // ---- copying -------------------------------------------------------------------------------------------------

        private static byte[] ReadSourceManifest(string source, string expectedHash, List<StagingProblem> problems)
        {
            string path = Path.Combine(source, BundleVerifier.ManifestFileName);
            if (!IsPlainSourcePath(source, BundleVerifier.ManifestFileName))
            {
                problems.Add(new StagingProblem(StagingProblemCode.SourceUnsafe, BundleVerifier.ManifestFileName));
                return null;
            }
            var report = new BundleVerifier.Report();
            byte[] bytes = BundleVerifier.ReadAll(new FileInfo(path), BundleVerifier.ManifestFileName, report);
            if (bytes == null || report.Any || !string.Equals(BundleVerifier.Sha256Hex(bytes), expectedHash, StringComparison.OrdinalIgnoreCase))
            {
                problems.Add(new StagingProblem(StagingProblemCode.SourceChanged, BundleVerifier.ManifestFileName));
                return null;
            }
            return bytes;
        }

        /// <summary>Every component of the source path, from the bundle folder down, is a plain folder, and the last one a
        /// plain file -- never a reparse point.</summary>
        private static bool IsPlainSourcePath(string source, string relative)
        {
            FileAttributes rootAttributes = File.GetAttributes(source);
            if ((rootAttributes & FileAttributes.ReparsePoint) != 0 || (rootAttributes & FileAttributes.Directory) == 0)
            {
                return false;
            }
            string current = source;
            string[] segments = relative.Split('/');
            for (int i = 0; i < segments.Length; i++)
            {
                current = current + "\\" + segments[i];
                FileAttributes attributes = File.GetAttributes(current);
                bool last = i == segments.Length - 1;
                if ((attributes & FileAttributes.ReparsePoint) != 0 || ((attributes & FileAttributes.Directory) != 0) == last)
                {
                    return false;
                }
            }
            return true;
        }

        private static void CopyListedFile(string source, string bundlePath, BundleVerifier.ManifestFile entry, CancellationToken cancellationToken,
            List<StagingProblem> problems)
        {
            // Validated twice over (BundleVerifier on the source, and here) before it is ever joined to a folder.
            if (!BundleVerifier.IsSafeRelativePath(entry.Path) ||
                string.Equals(entry.Path, BundleVerifier.ManifestFileName, StringComparison.OrdinalIgnoreCase))
            {
                problems.Add(new StagingProblem(StagingProblemCode.SourceUnsafe, entry.Path));
                return;
            }
            string relative = entry.Path.Replace('/', '\\');
            string from = source + "\\" + relative;
            string to = bundlePath + "\\" + relative;
            if (!to.StartsWith(bundlePath + "\\", StringComparison.OrdinalIgnoreCase) || !from.StartsWith(source + "\\", StringComparison.OrdinalIgnoreCase))
            {
                problems.Add(new StagingProblem(StagingProblemCode.SourceUnsafe, entry.Path));
                return;
            }
            bool plain;
            try
            {
                plain = IsPlainSourcePath(source, entry.Path);
            }
            catch (Exception e) when (e is FileNotFoundException || e is DirectoryNotFoundException)
            {
                problems.Add(new StagingProblem(StagingProblemCode.SourceChanged, entry.Path));
                return;
            }
            if (!plain)
            {
                problems.Add(new StagingProblem(StagingProblemCode.SourceUnsafe, entry.Path));
                return;
            }
            Directory.CreateDirectory(Path.GetDirectoryName(to)); // inside the new, protected run folder: inherits its permissions
            FileStream opened;
            try
            {
                opened = new FileStream(from, FileMode.Open, FileAccess.Read, FileShare.Read, CopyBufferSize, FileOptions.SequentialScan);
            }
            catch (Exception e) when (e is FileNotFoundException || e is DirectoryNotFoundException)
            {
                problems.Add(new StagingProblem(StagingProblemCode.SourceChanged, entry.Path));
                return;
            }
            using (FileStream input = opened)
            using (var output = new FileStream(to, FileMode.CreateNew, FileAccess.Write, FileShare.None, CopyBufferSize))
            using (SHA256 sha = SHA256.Create())
            {
                var buffer = new byte[CopyBufferSize];
                long copied = 0;
                int read;
                while ((read = input.Read(buffer, 0, buffer.Length)) > 0)
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    copied += read;
                    if (copied > entry.SizeBytes)
                    {
                        break; // never more than the manifest says: the file changed
                    }
                    sha.TransformBlock(buffer, 0, read, null, 0);
                    output.Write(buffer, 0, read);
                }
                sha.TransformFinalBlock(new byte[0], 0, 0);
                output.Flush(true);
                string hash = string.Concat(sha.Hash.Select(b => b.ToString("x2", System.Globalization.CultureInfo.InvariantCulture)));
                if (copied != entry.SizeBytes || !string.Equals(hash, entry.Sha256, StringComparison.OrdinalIgnoreCase))
                {
                    problems.Add(new StagingProblem(StagingProblemCode.SourceChanged, entry.Path));
                }
            }
        }

        private static void WriteNew(string path, byte[] bytes)
        {
            using (var output = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                output.Write(bytes, 0, bytes.Length);
                output.Flush(true);
            }
        }

        // ---- cleanup -------------------------------------------------------------------------------------------------

        /// <summary>
        /// Removes a run folder staging created, and nothing else: it must be a direct child of the staging root, named as a
        /// run id, and a plain folder. Its contents are removed bottom-up by walking it, never by a recursive delete: a
        /// reparse point found inside is removed as a link only (its target is never entered), and every path removed is
        /// checked to be inside the run folder. Returns the problems; never throws.
        /// </summary>
        internal static IList<StagingProblem> RemoveRunFolder(string stagingRoot, string runFolder, SafeFileHandle runLock = null)
        {
            var problems = new List<StagingProblem>();
            try
            {
                Guid ignored;
                string parent = Path.GetDirectoryName(runFolder);
                if (stagingRoot == null || !string.Equals(parent, stagingRoot, StringComparison.OrdinalIgnoreCase) ||
                    !Guid.TryParseExact(Path.GetFileName(runFolder), "N", out ignored))
                {
                    problems.Add(new StagingProblem(StagingProblemCode.CleanupFailed, runFolder));
                    return problems;
                }
                FileAttributes attributes = File.GetAttributes(runFolder);
                if ((attributes & FileAttributes.ReparsePoint) != 0 || (attributes & FileAttributes.Directory) == 0 ||
                    (runLock != null && !IsPlainFolderHandle(runLock)))
                {
                    problems.Add(new StagingProblem(StagingProblemCode.CleanupFailed, runFolder));
                    return problems;
                }
                RemoveContents(runFolder, runFolder, problems);
                if (problems.Count == 0)
                {
                    if (runLock != null)
                    {
                        // Pinned: delete through the handle that pins it (no moment when it is unpinned but still there);
                        // it goes when the handle is closed, below.
                        var disposition = new FileDispositionInfo { DeleteFile = 1 };
                        if (!SetFileInformationByHandle(runLock, FileDispositionInfoClass, ref disposition, Marshal.SizeOf(typeof(FileDispositionInfo))))
                        {
                            problems.Add(new StagingProblem(StagingProblemCode.CleanupFailed, runFolder + " (error " + new Win32Exception().NativeErrorCode + ")"));
                        }
                    }
                    else
                    {
                        Directory.Delete(runFolder, false);
                    }
                }
            }
            catch (Exception e) when (e is FileNotFoundException || e is DirectoryNotFoundException)
            {
                // Already gone.
            }
            catch (Exception)
            {
                problems.Add(new StagingProblem(StagingProblemCode.CleanupFailed, runFolder));
            }
            finally
            {
                runLock?.Dispose(); // on every path: a run's handle is never left open by its cleanup
            }
            return problems;
        }

        private static void RemoveContents(string folder, string runFolder, List<StagingProblem> problems)
        {
            foreach (FileSystemInfo entry in new DirectoryInfo(folder).EnumerateFileSystemInfos().ToList())
            {
                string path = entry.FullName;
                try
                {
                    if (!path.StartsWith(runFolder + "\\", StringComparison.OrdinalIgnoreCase))
                    {
                        problems.Add(new StagingProblem(StagingProblemCode.CleanupFailed, path));
                        continue;
                    }
                    bool isLink = (entry.Attributes & FileAttributes.ReparsePoint) != 0;
                    bool isFolder = (entry.Attributes & FileAttributes.Directory) != 0;
                    if (isFolder && !isLink)
                    {
                        RemoveContents(path, runFolder, problems);
                        Directory.Delete(path, false);
                    }
                    else if (isFolder)
                    {
                        Directory.Delete(path, false); // a junction or directory link: removes the link, never its target
                    }
                    else
                    {
                        if ((entry.Attributes & FileAttributes.ReadOnly) != 0)
                        {
                            entry.Attributes &= ~FileAttributes.ReadOnly;
                        }
                        File.Delete(path); // a file link is removed as a link
                    }
                }
                catch (Exception)
                {
                    problems.Add(new StagingProblem(StagingProblemCode.CleanupFailed, path));
                }
            }
        }

        // ---- classifying existing runs (READ ONLY) ---------------------------------------------------------------------
        //
        // Never creates, writes, moves, renames or deletes anything, and never changes a permission. It opens handles only
        // to look: the staging root (pinned for the scan) and, for a folder that already carries a plain-file marker, a
        // short-lived check handle. Both share read and write but not delete, so while open nothing can rename or delete
        // what they are on. Neither ever marks anything for deletion. The bundle folder is never entered.

        /// <summary>At most this many root entries are classified in one scan (then <see cref="StagingRunScan.Truncated"/>).</summary>
        internal const int MaxRunEntries = 1024;
        private const int MaxMarkerBytes = 4096;
        private const uint ReadControlAccess = 0x20000;
        private const uint FileReadAttributesAccess = 0x80;
        private const uint FileReadDataAccess = 0x1;
        private const int ErrorFileNotFound = 2;
        private const int ErrorPathNotFound = 3;
        private const int ErrorAccessDenied = 5;
        private const int ErrorSharingViolation = 32;
        private const uint OwnerSecurityInformation = 0x1;
        private const uint DaclSecurityInformation = 0x4;
        private static readonly string[] MarkerKeys = { "schema", "run_id", "manifest_sha256", "created_utc" };
        private static readonly System.Text.RegularExpressions.Regex RunIdPattern =
            new System.Text.RegularExpressions.Regex("^[0-9a-f]{32}$", System.Text.RegularExpressions.RegexOptions.CultureInvariant);
        private static readonly System.Text.RegularExpressions.Regex LowerSha256Pattern =
            new System.Text.RegularExpressions.Regex("^[0-9a-f]{64}$", System.Text.RegularExpressions.RegexOptions.CultureInvariant);
        private static readonly System.Text.RegularExpressions.Regex CreatedUtcPattern =
            new System.Text.RegularExpressions.Regex(@"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$", System.Text.RegularExpressions.RegexOptions.CultureInvariant);

        // Reads a handle's security descriptor straight into a managed buffer (no unmanaged memory to copy or free).
        [DllImport("advapi32.dll", SetLastError = true)]
        private static extern bool GetKernelObjectSecurity(SafeFileHandle handle, uint requestedInformation, byte[] descriptor, uint length,
            out uint lengthNeeded);

        /// <summary>The installer's entry point: always <see cref="StagingPolicy.Production"/>.</summary>
        public static StagingRunScan ClassifyRuns(CancellationToken cancellationToken) => ClassifyRuns(StagingPolicy.Production, cancellationToken);

        /// <summary>
        /// Classifies each direct child of <paramref name="policy"/>'s staging root, READ ONLY. The root and the chain above it
        /// are checked (as staging checks them), the root is pinned for the scan and checked again, then each entry is
        /// classified: Active (something holds it open without sharing delete), AbandonedCandidate (every identity, security and
        /// handle check passed), or Unknown (anything else). Never throws.
        /// </summary>
        public static StagingRunScan ClassifyRuns(StagingPolicy policy, CancellationToken cancellationToken)
        {
            var rootProblems = new List<StagingProblem>();
            var entries = new List<StagingRunEntry>();
            try
            {
                if (policy == null || policy.AccessPrincipals.Count == 0 || policy.AccessPrincipals.Any(p => !policy.TrustedPrincipals.Contains(p)) ||
                    (policy.Owner != null && !policy.TrustedPrincipals.Contains(policy.Owner)))
                {
                    rootProblems.Add(new StagingProblem(StagingProblemCode.RequestInvalid, ""));
                    return new StagingRunScan(false, false, rootProblems, entries, false);
                }
                string root = NormalizeLocalPath(policy.StagingRoot);
                if (root == null)
                {
                    rootProblems.Add(new StagingProblem(StagingProblemCode.StagingRootInvalid, policy.StagingRoot));
                    return new StagingRunScan(false, false, rootProblems, entries, false);
                }
                List<string> missing = CheckChain(root, policy, rootProblems);
                if (rootProblems.Count > 0)
                {
                    return new StagingRunScan(false, false, rootProblems, entries, false);
                }
                if (missing.Count > 0)
                {
                    return new StagingRunScan(true, false, rootProblems, entries, false); // no root: nothing to classify
                }

                // Pin the root for the scan (it, and the folders above it, cannot be renamed or deleted meanwhile), and check
                // the chain again now that it cannot be swapped.
                using (SafeFileHandle rootPin = CreateFile(root, FileListDirectory | ReadControlAccess | FileReadAttributesAccess | SynchronizeAccess,
                    FileShareRead | FileShareWrite, IntPtr.Zero, OpenExisting, FileFlagBackupSemantics | FileFlagOpenReparsePoint, IntPtr.Zero))
                {
                    if (rootPin.IsInvalid)
                    {
                        int error = new Win32Exception().NativeErrorCode;
                        rootProblems.Add(new StagingProblem(error == ErrorAccessDenied ? StagingProblemCode.AccessDenied : StagingProblemCode.StagingRootInvalid,
                            root + " (error " + error + ")"));
                        return new StagingRunScan(false, true, rootProblems, entries, false);
                    }
                    if (!IsPlainFolderHandle(rootPin))
                    {
                        rootProblems.Add(new StagingProblem(StagingProblemCode.ReparsePoint, root));
                        return new StagingRunScan(false, true, rootProblems, entries, false);
                    }
                    CheckChain(root, policy, rootProblems, mustExist: true);
                    if (rootProblems.Count > 0)
                    {
                        return new StagingRunScan(false, true, rootProblems, entries, false);
                    }

                    // Direct children only, by their directory-listing entries; never recursively.
                    List<FileSystemInfo> children = new DirectoryInfo(root).EnumerateFileSystemInfos().Take(MaxRunEntries + 1).ToList();
                    bool truncated = children.Count > MaxRunEntries;
                    foreach (FileSystemInfo child in children.Take(MaxRunEntries))
                    {
                        cancellationToken.ThrowIfCancellationRequested();
                        entries.Add(ClassifyEntry(child, policy));
                    }
                    return new StagingRunScan(true, true, rootProblems, entries, truncated);
                }
            }
            catch (OperationCanceledException)
            {
                rootProblems.Add(new StagingProblem(StagingProblemCode.Cancelled, ""));
            }
            catch (UnauthorizedAccessException)
            {
                rootProblems.Add(new StagingProblem(StagingProblemCode.AccessDenied, ""));
            }
            catch (Exception)
            {
                rootProblems.Add(new StagingProblem(StagingProblemCode.UnexpectedError, ""));
            }
            return new StagingRunScan(false, true, rootProblems, entries, false);
        }

        /// <summary>One root entry. The first failing check decides; anything uncertain is Unknown. Never throws.</summary>
        private static StagingRunEntry ClassifyEntry(FileSystemInfo child, StagingPolicy policy)
        {
            string name = child.Name;
            string path = child.FullName;
            Func<StagingRunReason, string, StagingRunEntry> unknown = (reason, detail) =>
                new StagingRunEntry(name, path, StagingRunState.Unknown, reason, detail);
            try
            {
                // (a) The listing entry itself: a link is never opened; only a folder can be a run.
                FileAttributes attributes = child.Attributes;
                if ((attributes & FileAttributes.ReparsePoint) != 0)
                {
                    return unknown(StagingRunReason.ReparsePoint, "");
                }
                if ((attributes & FileAttributes.Directory) == 0)
                {
                    return unknown(StagingRunReason.NotAFolder, "");
                }
                // (b) A run id: 32 lowercase hexadecimal characters (necessary, never sufficient).
                if (!RunIdPattern.IsMatch(name))
                {
                    return unknown(StagingRunReason.NotARunName, "");
                }
                // (c) The marker, by its attributes only (nothing opened). A folder without a plain-file marker is NEVER opened
                // for the check: it may be a run still being set up (staging writes the marker only once it holds its lock).
                string markerPath = path + "\\" + RunMarkerFileName;
                FileAttributes markerAttributes;
                try
                {
                    markerAttributes = File.GetAttributes(markerPath);
                }
                catch (Exception e) when (e is FileNotFoundException || e is DirectoryNotFoundException)
                {
                    return unknown(Directory.Exists(path) ? StagingRunReason.MarkerMissing : StagingRunReason.Vanished, "");
                }
                if ((markerAttributes & (FileAttributes.ReparsePoint | FileAttributes.Directory)) != 0)
                {
                    return unknown(StagingRunReason.MarkerUnsafe, "");
                }

                // (d) The check handle: DELETE access (conflicts with a staging run's lifetime handle, which does not share
                // delete) sharing read and write but not delete (so while open, nobody can rename or delete the folder).
                using (SafeFileHandle check = CreateFile(path, DeleteAccess | ReadControlAccess | FileReadAttributesAccess | SynchronizeAccess,
                    FileShareRead | FileShareWrite, IntPtr.Zero, OpenExisting, FileFlagBackupSemantics | FileFlagOpenReparsePoint, IntPtr.Zero))
                {
                    if (check.IsInvalid)
                    {
                        int error = new Win32Exception().NativeErrorCode;
                        switch (error)
                        {
                            case ErrorSharingViolation:
                                return new StagingRunEntry(name, path, StagingRunState.Active, StagingRunReason.InUse, "(error 32)");
                            case ErrorFileNotFound:
                            case ErrorPathNotFound:
                                return unknown(StagingRunReason.Vanished, "");
                            case ErrorAccessDenied:
                                return unknown(StagingRunReason.AccessDenied, "(error 5)");
                            default:
                                return unknown(StagingRunReason.ProbeFailed, "(error " + error + ")");
                        }
                    }

                    // (e) While pinned, through the handle: a plain folder, with exactly the security staging creates...
                    if (!IsPlainFolderHandle(check))
                    {
                        return unknown(StagingRunReason.ReparsePoint, "");
                    }
                    DirectorySecurity security = ReadSecurity(check);
                    if (security == null)
                    {
                        return unknown(StagingRunReason.AccessDenied, "(security unreadable)");
                    }
                    if (!IsCreatedSecurity(security, policy))
                    {
                        return unknown(StagingRunReason.UnexpectedSecurity, "");
                    }
                    // ...and a marker that is a plain, small file whose contents name this very run.
                    byte[] markerBytes;
                    StagingRunReason markerProblem = ReadMarker(markerPath, out markerBytes);
                    if (markerProblem != StagingRunReason.None)
                    {
                        return unknown(markerProblem, "");
                    }
                    string runId, manifestSha256, createdUtc;
                    StagingRunReason contentProblem = ParseMarker(markerBytes, name, out runId, out manifestSha256, out createdUtc);
                    if (contentProblem != StagingRunReason.None)
                    {
                        return unknown(contentProblem, "");
                    }
                    // (f) Complete evidence, and nothing held it: a candidate (never, by itself, permission to delete).
                    return new StagingRunEntry(name, path, StagingRunState.AbandonedCandidate, StagingRunReason.None, "", runId, manifestSha256, createdUtc);
                }
            }
            catch (UnauthorizedAccessException)
            {
                return unknown(StagingRunReason.AccessDenied, "");
            }
            catch (Exception e) when (e is FileNotFoundException || e is DirectoryNotFoundException)
            {
                return unknown(StagingRunReason.Vanished, "");
            }
            catch (Exception)
            {
                return unknown(StagingRunReason.Error, "");
            }
        }

        /// <summary>The owner and permissions of what <paramref name="handle"/> is open on, read through the handle (never by
        /// path); null if they cannot be read.</summary>
        private static DirectorySecurity ReadSecurity(SafeFileHandle handle)
        {
            const uint information = OwnerSecurityInformation | DaclSecurityInformation;
            uint needed;
            // First the size (fails with ERROR_INSUFFICIENT_BUFFER), then the descriptor itself, self-relative.
            GetKernelObjectSecurity(handle, information, null, 0, out needed);
            if (needed == 0 || needed > 65536)
            {
                return null;
            }
            var bytes = new byte[needed];
            if (!GetKernelObjectSecurity(handle, information, bytes, needed, out needed))
            {
                return null;
            }
            var security = new DirectorySecurity();
            security.SetSecurityDescriptorBinaryForm(bytes, AccessControlSections.Owner | AccessControlSections.Access);
            return security;
        }

        /// <summary>Reads the marker through a handle opened on the entry itself (never through a link), sharing read only:
        /// it must be a plain file of at most <see cref="MaxMarkerBytes"/>.</summary>
        private static StagingRunReason ReadMarker(string markerPath, out byte[] bytes)
        {
            bytes = null;
            SafeFileHandle handle = CreateFile(markerPath, FileReadDataAccess | FileReadAttributesAccess | SynchronizeAccess, FileShareRead, IntPtr.Zero,
                OpenExisting, FileFlagOpenReparsePoint, IntPtr.Zero);
            if (handle.IsInvalid)
            {
                int error = new Win32Exception().NativeErrorCode;
                handle.Dispose();
                return error == ErrorFileNotFound || error == ErrorPathNotFound ? StagingRunReason.MarkerMissing : StagingRunReason.MarkerUnsafe;
            }
            using (var stream = new FileStream(handle, FileAccess.Read)) // owns the handle from here
            {
                if (!GetFileInformationByHandle(handle, out ByHandleFileInformation information) ||
                    (information.FileAttributes & (uint)(FileAttributes.Directory | FileAttributes.ReparsePoint)) != 0 ||
                    information.FileSizeHigh != 0 || information.FileSizeLow > MaxMarkerBytes)
                {
                    return StagingRunReason.MarkerUnsafe;
                }
                var buffer = new byte[MaxMarkerBytes + 1];
                int total = 0, read;
                while (total < buffer.Length && (read = stream.Read(buffer, total, buffer.Length - total)) > 0)
                {
                    total += read;
                }
                if (total > MaxMarkerBytes)
                {
                    return StagingRunReason.MarkerUnsafe;
                }
                bytes = new byte[total];
                Array.Copy(buffer, bytes, total);
                return StagingRunReason.None;
            }
        }

        /// <summary>The marker's contents, strictly: UTF-8 without a byte-order mark; a JSON object of exactly the four keys,
        /// each appearing once, all strings; the exact schema; a lowercase run id that is this folder's name; a lowercase
        /// SHA-256; and the timestamp format staging writes.</summary>
        private static StagingRunReason ParseMarker(byte[] bytes, string folderName, out string runId, out string manifestSha256, out string createdUtc)
        {
            runId = manifestSha256 = createdUtc = null;
            if (bytes.Length >= 3 && bytes[0] == 0xEF && bytes[1] == 0xBB && bytes[2] == 0xBF)
            {
                return StagingRunReason.MarkerInvalid;
            }
            IDictionary<string, object> map;
            string text;
            try
            {
                text = new UTF8Encoding(false, true).GetString(bytes);
                var serializer = new System.Web.Script.Serialization.JavaScriptSerializer { MaxJsonLength = MaxMarkerBytes, RecursionLimit = 2 };
                map = serializer.DeserializeObject(text) as IDictionary<string, object>;
            }
            catch (Exception)
            {
                return StagingRunReason.MarkerInvalid;
            }
            if (map == null || map.Count != MarkerKeys.Length || MarkerKeys.Any(k => !map.ContainsKey(k) || !(map[k] is string)) ||
                MarkerKeys.Any(k => CountOccurrences(text, "\"" + k + "\"") != 1)) // a repeated key would be silently overwritten
            {
                return StagingRunReason.MarkerInvalid;
            }
            string schema = (string)map["schema"], id = (string)map["run_id"], sha = (string)map["manifest_sha256"], created = (string)map["created_utc"];
            if (!string.Equals(schema, "sortview.staging-run/1", StringComparison.Ordinal) || !RunIdPattern.IsMatch(id) ||
                !LowerSha256Pattern.IsMatch(sha) || !CreatedUtcPattern.IsMatch(created) ||
                !DateTime.TryParseExact(created, "yyyy-MM-dd'T'HH:mm:ss.fff'Z'", System.Globalization.CultureInfo.InvariantCulture,
                    System.Globalization.DateTimeStyles.AdjustToUniversal | System.Globalization.DateTimeStyles.AssumeUniversal, out DateTime _))
            {
                return StagingRunReason.MarkerInvalid;
            }
            if (!string.Equals(id, folderName, StringComparison.Ordinal))
            {
                return StagingRunReason.MarkerMismatch;
            }
            runId = id;
            manifestSha256 = sha;
            createdUtc = created;
            return StagingRunReason.None;
        }

        private static int CountOccurrences(string text, string value)
        {
            int count = 0;
            for (int at = text.IndexOf(value, StringComparison.Ordinal); at >= 0; at = text.IndexOf(value, at + value.Length, StringComparison.Ordinal))
            {
                count++;
            }
            return count;
        }
    }
}
