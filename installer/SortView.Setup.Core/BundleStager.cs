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
using System.Threading;

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
        {
            StagingRoot = stagingRoot;
            TrustedPrincipals = new ReadOnlyCollection<SecurityIdentifier>((trustedPrincipals ?? Enumerable.Empty<SecurityIdentifier>()).Distinct().ToList());
            AccessPrincipals = new ReadOnlyCollection<SecurityIdentifier>((accessPrincipals ?? Enumerable.Empty<SecurityIdentifier>()).Distinct().ToList());
            Owner = owner;
        }

        /// <summary>The installer's policy. Built fresh each time; it has no settable state.</summary>
        public static StagingPolicy Production =>
            new StagingPolicy(
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "SortView", "SetupStaging"),
                new[] { SystemSid, AdministratorsSid, TrustedInstallerSid },
                new[] { SystemSid, AdministratorsSid },
                AdministratorsSid);

        public string StagingRoot { get; }

        public IReadOnlyList<SecurityIdentifier> TrustedPrincipals { get; }

        public IReadOnlyList<SecurityIdentifier> AccessPrincipals { get; }

        public SecurityIdentifier Owner { get; }

        internal bool IsTrusted(IdentityReference identity) =>
            identity is SecurityIdentifier sid && TrustedPrincipals.Contains(sid);
    }

    /// <summary>
    /// A bundle staged by <see cref="BundleStager"/>. While it is alive and <see cref="IsStaged"/>, <see cref="BundlePath"/>
    /// holds exactly the verified bundle. Dispose removes the run folder this staging created -- and nothing else.
    /// </summary>
    public sealed class StagedBundle : IDisposable
    {
        private readonly List<StagingProblem> cleanupProblems = new List<StagingProblem>();
        private readonly string stagingRoot;
        private string runFolder;

        internal StagedBundle(IList<StagingProblem> problems, BundleVerification sourceVerification, BundleVerification stagedVerification,
            string stagingRoot, string runFolder, string bundlePath)
        {
            Problems = new ReadOnlyCollection<StagingProblem>(problems);
            SourceVerification = sourceVerification;
            StagedVerification = stagedVerification;
            this.stagingRoot = stagingRoot;
            this.runFolder = runFolder;
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
            if (runFolder != null)
            {
                cleanupProblems.AddRange(BundleStager.RemoveRunFolder(stagingRoot, runFolder));
                runFolder = null;
            }
        }
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
    /// start, then read back; (5) the source manifest read again and required to hash to the expected value, then each
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
                bundlePath = Path.Combine(runFolder, "bundle");
                Directory.CreateDirectory(bundlePath);

                // (5) The manifest again (the source is not trusted for having verified earlier), then the listed files.
                byte[] manifestBytes = ReadSourceManifest(source, expectedHash, problems);
                if (problems.Count > 0)
                {
                    return Fail(problems, sourceVerification, root, runFolder);
                }
                var report = new BundleVerifier.Report();
                BundleVerifier.Manifest manifest = BundleVerifier.Parse(manifestBytes, report);
                if (manifest == null || report.Any)
                {
                    problems.Add(new StagingProblem(StagingProblemCode.SourceChanged, BundleVerifier.ManifestFileName));
                    return Fail(problems, sourceVerification, root, runFolder);
                }
                long needed = SpaceMargin + manifestBytes.Length + manifest.Files.Sum(f => f.SizeBytes);
                if (new DriveInfo(Path.GetPathRoot(root)).AvailableFreeSpace < needed)
                {
                    problems.Add(new StagingProblem(StagingProblemCode.InsufficientSpace, root));
                    return Fail(problems, sourceVerification, root, runFolder);
                }
                int done = 0;
                foreach (BundleVerifier.ManifestFile entry in manifest.Files)
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    CopyListedFile(source, bundlePath, entry, cancellationToken, problems);
                    if (problems.Count > 0)
                    {
                        return Fail(problems, sourceVerification, root, runFolder);
                    }
                    Report(progress, StagingStep.FileCopied, entry.Path, ++done, manifest.Files.Count);
                }
                WriteNew(Path.Combine(bundlePath, BundleVerifier.ManifestFileName), manifestBytes);

                // (6) The chain and the run folder, again.
                CheckChain(root, policy, problems, mustExist: true);
                CheckCreated(runFolder, policy, problems);
                if (problems.Count > 0)
                {
                    return Fail(problems, sourceVerification, root, runFolder);
                }

                // (7) The staged copy, on its own, by the same rules.
                cancellationToken.ThrowIfCancellationRequested();
                stagedVerification = BundleVerifier.Verify(bundlePath, expectedHash, expectedVersion);
                if (!stagedVerification.IsVerified)
                {
                    problems.Add(new StagingProblem(StagingProblemCode.StagedNotVerified, ""));
                    return Fail(problems, sourceVerification, root, runFolder, stagedVerification);
                }
                Report(progress, StagingStep.StagedVerified, null, manifest.Files.Count, manifest.Files.Count);
                return new StagedBundle(problems, sourceVerification, stagedVerification, root, runFolder, bundlePath);
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
            return Fail(problems, sourceVerification, root, runFolder, stagedVerification);
        }

        private static StagedBundle Fail(List<StagingProblem> problems, BundleVerification sourceVerification, string root, string runFolder,
            BundleVerification stagedVerification = null)
        {
            var result = new StagedBundle(problems, sourceVerification, stagedVerification, root, null, null);
            if (runFolder != null)
            {
                result.AddCleanupProblems(RemoveRunFolder(root, runFolder));
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
            if (!ownerOk || !rulesOk || !policy.AccessPrincipals.All(granted.Contains))
            {
                problems.Add(new StagingProblem(StagingProblemCode.PermissionsNotApplied, folder));
            }
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
        internal static IList<StagingProblem> RemoveRunFolder(string stagingRoot, string runFolder)
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
                if ((attributes & FileAttributes.ReparsePoint) != 0 || (attributes & FileAttributes.Directory) == 0)
                {
                    problems.Add(new StagingProblem(StagingProblemCode.CleanupFailed, runFolder));
                    return problems;
                }
                RemoveContents(runFolder, runFolder, problems);
                if (problems.Count == 0)
                {
                    Directory.Delete(runFolder, false);
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
    }
}
