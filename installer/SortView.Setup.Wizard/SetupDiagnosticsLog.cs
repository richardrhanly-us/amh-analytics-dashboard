using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;

namespace SortView.Setup.Wizard
{
    /// <summary>TEST ONLY (internal; tests set it by reflection on a policy they built): a failure to inject.</summary>
    internal enum SetupLogFault
    {
        None,
        CheckFolder,
        CreateFolder,
        CreateFile,
        Retention,
        Write,
        SaveCopy,
    }

    /// <summary>Why no setup log was saved, or why it stopped early. Fixed codes only (Copy details may name one).</summary>
    public enum SetupLogProblem
    {
        None,
        NotConfigured,
        FolderUntrusted,
        FolderNotCreated,
        FileNotCreated,
        WriteFailed,
        UnexpectedError,
    }

    /// <summary>
    /// Where setup logs are kept and whom that folder trusts. <see cref="Production"/> is the installer's:
    /// C:\ProgramData\SortViewSetup\Logs -- apart from the Collector's data (C:\ProgramData\SortViewCollector) and from
    /// installation staging -- created for SYSTEM and Administrators only, owned by Administrators. Any other policy (tests
    /// and the simulation window use one in a temporary folder that also trusts the current user) is built explicitly.
    /// </summary>
    public sealed class SetupLogPolicy
    {
        public const int MaxLogs = 20;
        public const long MaxBytes = 5L * 1024 * 1024;

        internal const string ProductionRoot = @"C:\ProgramData\SortViewSetup";
        internal const string ProductionLogFolder = @"C:\ProgramData\SortViewSetup\Logs";

        private static readonly SecurityIdentifier SystemSid = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);
        private static readonly SecurityIdentifier AdministratorsSid = new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null);
        private static readonly SecurityIdentifier TrustedInstallerSid =
            new SecurityIdentifier("S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464");

        private readonly bool isProduction;
        private SetupLogFault injectedFault;
        private Action beforeCopyCreated;

        /// <param name="root">The setup log's own top folder (C:\ProgramData\SortViewSetup in production). It and every
        /// folder below it, down to <paramref name="logFolder"/>, must be exactly as setup creates them.</param>
        /// <param name="logFolder">The folder the logs are written to, inside <paramref name="root"/>.</param>
        /// <param name="trustedPrincipals">Who may own the folders, and hold delete or permission rights on the folders above
        /// <paramref name="root"/>.</param>
        /// <param name="accessPrincipals">Who gets full control of the setup log folders (no one else gets anything).</param>
        /// <param name="owner">The owner of the folders created, or null to leave the creator as owner.</param>
        public SetupLogPolicy(string root, string logFolder, IEnumerable<SecurityIdentifier> trustedPrincipals,
            IEnumerable<SecurityIdentifier> accessPrincipals, SecurityIdentifier owner)
            : this(root, logFolder, trustedPrincipals, accessPrincipals, owner, false)
        {
        }

        private SetupLogPolicy(string root, string logFolder, IEnumerable<SecurityIdentifier> trustedPrincipals,
            IEnumerable<SecurityIdentifier> accessPrincipals, SecurityIdentifier owner, bool isProduction)
        {
            Root = root;
            LogFolder = logFolder;
            TrustedPrincipals = new ReadOnlyCollection<SecurityIdentifier>((trustedPrincipals ?? Enumerable.Empty<SecurityIdentifier>()).Distinct().ToList());
            AccessPrincipals = new ReadOnlyCollection<SecurityIdentifier>((accessPrincipals ?? Enumerable.Empty<SecurityIdentifier>()).Distinct().ToList());
            Owner = owner;
            this.isProduction = isProduction;
        }

        /// <summary>The installer's policy. Built fresh each time; it has no settable state.</summary>
        public static SetupLogPolicy Production =>
            new SetupLogPolicy(ProductionRoot, ProductionLogFolder,
                new[] { SystemSid, AdministratorsSid, TrustedInstallerSid },
                new[] { SystemSid, AdministratorsSid },
                AdministratorsSid,
                true);

        public string Root { get; }

        public string LogFolder { get; }

        public IReadOnlyList<SecurityIdentifier> TrustedPrincipals { get; }

        public IReadOnlyList<SecurityIdentifier> AccessPrincipals { get; }

        public SecurityIdentifier Owner { get; }

        /// <summary>TEST ONLY: always None for the production policy, which refuses to have one set.</summary>
        internal SetupLogFault InjectedFault
        {
            get => isProduction ? SetupLogFault.None : injectedFault;
            set
            {
                if (isProduction)
                {
                    throw new InvalidOperationException("The production setup log policy takes no injected fault.");
                }
                injectedFault = value;
            }
        }

        /// <summary>TEST ONLY: runs while a copy is being saved, once the destination's folders are checked and just before
        /// the file is created (to change the path at that moment). Always null for the production policy, which refuses one.</summary>
        internal Action BeforeCopyCreated
        {
            get => isProduction ? null : beforeCopyCreated;
            set
            {
                if (isProduction)
                {
                    throw new InvalidOperationException("The production setup log policy takes no test hook.");
                }
                beforeCopyCreated = value;
            }
        }

        internal bool IsTrusted(IdentityReference identity) => identity is SecurityIdentifier sid && TrustedPrincipals.Contains(sid);
    }

    /// <summary>
    /// One setup run's diagnostic log, written as the run goes so that what was written survives closing, cancelling or
    /// the program being ended. It holds only what the wizard hands it -- the engine's allow-listed events as the wizard
    /// shows them, the engine's console text, which question was asked and that it was answered (never the answer), and
    /// the run's result -- and every line is redacted again on the way in (enrollment codes, tokens, passwords,
    /// authorization headers) as a second line of defence. It never receives anything typed.
    ///
    /// Nothing here ever throws to its caller: a log that cannot be created, written or pruned is simply not saved (or
    /// stops), and says why with a fixed code. Setup itself never depends on it.
    /// </summary>
    public sealed class SetupDiagnosticsLog : IDisposable
    {
        internal const string Schema = "sortview.setup-log/1";
        internal static readonly Regex FileNamePattern = new Regex(@"^setup-[0-9]{8}-[0-9]{6}-[0-9a-f]{8}\.log$", RegexOptions.CultureInvariant);
        internal const string TruncatedMarker = "[log truncated: the 5 MB limit was reached; later entries were not saved]";
        private const int TruncationReserve = 512;
        private const int MaxLineLength = 8000;
        // The owner's implicit rights: granted only to the owner, which must be trusted already.
        private static readonly SecurityIdentifier OwnerRightsSid = new SecurityIdentifier("S-1-3-4");
        // Rights on a folder ABOVE the log root that would let someone else delete, rename or re-permission what lies below
        // it. Creating files and folders there (as BUILTIN\Users may in C:\ProgramData) is tolerated: the log's own folders
        // are checked exactly.
        private const int DangerousAncestorRights = (int)(FileSystemRights.Delete | FileSystemRights.DeleteSubdirectoriesAndFiles |
            FileSystemRights.ChangePermissions | FileSystemRights.TakeOwnership) | 0x10000000 /* GENERIC_ALL */;
        private static readonly UTF8Encoding Utf8 = new UTF8Encoding(false);

        private readonly object gate = new object();
        private readonly SetupLogPolicy policy;
        private FileStream stream;
        private long bytesWritten;
        private bool retentionUnchecked;

        private SetupDiagnosticsLog(SetupLogPolicy policy, SetupLogProblem problem)
        {
            this.policy = policy;
            Problem = problem;
        }

        /// <summary>The log's file name (never a path), once the file was created; otherwise null.</summary>
        public string FileName { get; private set; }

        /// <summary>The full path of the log, once created (for saving a copy; never put in Copy details).</summary>
        internal string FilePath { get; private set; }

        public bool WasCreated => FileName != null;

        /// <summary>Why the log was not saved, or stopped early. None while all is well.</summary>
        public SetupLogProblem Problem { get; private set; }

        /// <summary>The 5 MB limit was reached; later entries were dropped.</summary>
        public bool IsTruncated { get; private set; }

        public int OldLogsRemoved { get; private set; }

        public int OldLogsNotRemoved { get; private set; }

        /// <summary>A log that was not saved, for a host that has none (or whose log could not be opened).</summary>
        public static SetupDiagnosticsLog NotSaved(SetupLogProblem problem) =>
            new SetupDiagnosticsLog(null, problem == SetupLogProblem.None ? SetupLogProblem.NotConfigured : problem);

        /// <summary>Opens a new log under <paramref name="policy"/>: checks (or creates) its folder, removes the oldest logs
        /// so that at most <see cref="SetupLogPolicy.MaxLogs"/> remain with the new one, creates the file and writes the
        /// header. Never throws: on any failure the log is not saved and <see cref="Problem"/> says why.
        /// <paramref name="hostLines"/>: fixed, non-secret facts from the host (such as the pinned Collector release).</summary>
        public static SetupDiagnosticsLog Open(SetupLogPolicy policy, params string[] hostLines)
        {
            if (policy == null)
            {
                return NotSaved(SetupLogProblem.NotConfigured);
            }
            var log = new SetupDiagnosticsLog(policy, SetupLogProblem.None);
            try
            {
                log.Start(hostLines ?? new string[0]);
            }
            catch (Exception)
            {
                log.Problem = log.WasCreated ? SetupLogProblem.WriteFailed : SetupLogProblem.UnexpectedError;
                log.CloseStream();
            }
            return log;
        }

        // ---- opening --------------------------------------------------------------------------------------------------

        private void Start(string[] hostLines)
        {
            string root = NormalizeLocalPath(policy.Root), folder = NormalizeLocalPath(policy.LogFolder);
            if (root == null || folder == null || !folder.StartsWith(root + "\\", StringComparison.OrdinalIgnoreCase) ||
                policy.AccessPrincipals.Count == 0 || policy.AccessPrincipals.Any(p => !policy.TrustedPrincipals.Contains(p)) ||
                (policy.Owner != null && !policy.TrustedPrincipals.Contains(policy.Owner)))
            {
                Problem = SetupLogProblem.NotConfigured;
                return;
            }
            if (policy.InjectedFault == SetupLogFault.CheckFolder)
            {
                throw new IOException("injected test fault");
            }
            if (!AncestorsAreSafe(Path.GetDirectoryName(root)))
            {
                Problem = SetupLogProblem.FolderUntrusted;
                return;
            }
            var chain = new List<string> { root };
            foreach (string part in folder.Substring(root.Length + 1).Split('\\'))
            {
                chain.Add(chain[chain.Count - 1] + "\\" + part);
            }
            foreach (string each in chain)
            {
                if (!Exists(each))
                {
                    if (!CreateProtected(each))
                    {
                        return;
                    }
                }
                else if (!IsOwnFolder(each))
                {
                    Problem = SetupLogProblem.FolderUntrusted;
                    return;
                }
            }
            try
            {
                RemoveOldLogs(folder);
            }
            catch (Exception)
            {
                retentionUnchecked = true; // never fatal: the new log is still written
            }
            CreateFile(folder);
            if (!WasCreated)
            {
                return;
            }
            Write("setup", "SortView Setup diagnostic log (" + Schema + ")");
            Write("setup", "Log file: " + FileName);
            Write("setup", "SortView Setup version: " + (Assembly.GetEntryAssembly()?.GetName().Version?.ToString() ?? "unknown"));
            Write("setup", "Windows version: " + Environment.OSVersion.Version + (Environment.Is64BitOperatingSystem ? ", 64-bit" : ", 32-bit"));
            foreach (string line in hostLines)
            {
                Write("setup", line);
            }
            Write("setup", retentionUnchecked ? "Old setup logs could not be checked."
                : "Old setup logs removed: " + OldLogsRemoved.ToString(CultureInfo.InvariantCulture) +
                  (OldLogsNotRemoved > 0 ? "; could not be removed: " + OldLogsNotRemoved.ToString(CultureInfo.InvariantCulture) : ""));
            Write("setup", "This log never records enrollment codes, API tokens, passwords or anything typed. It can contain folder paths and other system information.");
        }

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

        private static bool Exists(string path)
        {
            try
            {
                File.GetAttributes(path);
                return true;
            }
            catch (Exception e) when (e is FileNotFoundException || e is DirectoryNotFoundException)
            {
                return false;
            }
        }

        private static bool IsPlainFolder(string path)
        {
            FileAttributes attributes = File.GetAttributes(path); // the entry itself: a junction or link reports ReparsePoint
            return (attributes & FileAttributes.ReparsePoint) == 0 && (attributes & FileAttributes.Directory) != 0;
        }

        /// <summary>Every folder from the drive root down to <paramref name="top"/> (the folder that holds the log root):
        /// a plain folder, owned by a trusted principal, and letting no one else delete, rename or re-permission anything.</summary>
        private bool AncestorsAreSafe(string top)
        {
            string driveRoot = Path.GetPathRoot(top);
            var chain = new List<string> { driveRoot };
            string current = driveRoot.TrimEnd('\\');
            foreach (string part in top.Substring(driveRoot.Length).Split(new[] { '\\' }, StringSplitOptions.RemoveEmptyEntries))
            {
                current = current + "\\" + part;
                chain.Add(current);
            }
            foreach (string folder in chain)
            {
                if (!Exists(folder) || !IsPlainFolder(folder))
                {
                    return false;
                }
                DirectorySecurity security = new DirectoryInfo(folder).GetAccessControl(AccessControlSections.Access | AccessControlSections.Owner);
                if (!policy.IsTrusted(security.GetOwner(typeof(SecurityIdentifier))))
                {
                    return false;
                }
                foreach (FileSystemAccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier)))
                {
                    if (rule.AccessControlType != AccessControlType.Allow || (rule.PropagationFlags & PropagationFlags.InheritOnly) != 0 ||
                        policy.IsTrusted(rule.IdentityReference) || OwnerRightsSid.Equals(rule.IdentityReference))
                    {
                        continue;
                    }
                    if (((int)rule.FileSystemRights & DangerousAncestorRights) != 0)
                    {
                        return false;
                    }
                }
            }
            return true;
        }

        /// <summary>A setup log folder: a plain folder, owned by a trusted principal, protected (nothing inherited), and
        /// granting full control to exactly the policy's access principals and nothing to anyone else.</summary>
        private bool IsOwnFolder(string folder)
        {
            if (!IsPlainFolder(folder))
            {
                return false;
            }
            DirectorySecurity security = new DirectoryInfo(folder).GetAccessControl(AccessControlSections.Access | AccessControlSections.Owner);
            var owner = security.GetOwner(typeof(SecurityIdentifier)) as SecurityIdentifier;
            if (owner == null || !policy.TrustedPrincipals.Contains(owner) || !security.AreAccessRulesProtected)
            {
                return false;
            }
            var granted = new HashSet<SecurityIdentifier>();
            foreach (FileSystemAccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier)))
            {
                var sid = rule.IdentityReference as SecurityIdentifier;
                if (rule.IsInherited || rule.AccessControlType != AccessControlType.Allow || sid == null || !policy.AccessPrincipals.Contains(sid) ||
                    (rule.FileSystemRights & FileSystemRights.FullControl) != FileSystemRights.FullControl)
                {
                    return false;
                }
                granted.Add(sid);
            }
            return policy.AccessPrincipals.All(granted.Contains);
        }

        /// <summary>Creates <paramref name="folder"/> with its protected permissions in place from the start, then checks
        /// them exactly (a folder that appeared meanwhile is never adopted unless it is exactly right).</summary>
        private bool CreateProtected(string folder)
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
            try
            {
                if (policy.InjectedFault == SetupLogFault.CreateFolder)
                {
                    throw new IOException("injected test fault");
                }
                Directory.CreateDirectory(folder, security);
            }
            catch (Exception)
            {
                Problem = SetupLogProblem.FolderNotCreated;
                return false;
            }
            if (!IsOwnFolder(folder))
            {
                Problem = SetupLogProblem.FolderUntrusted;
                return false;
            }
            return true;
        }

        /// <summary>Keeps the newest <see cref="SetupLogPolicy.MaxLogs"/> - 1 setup logs, so that with the new one there are
        /// at most <see cref="SetupLogPolicy.MaxLogs"/>. Only plain files named exactly like a setup log are counted or
        /// removed; anything else in the folder is left alone. A log that cannot be removed is counted, never fatal.</summary>
        private void RemoveOldLogs(string folder)
        {
            List<FileInfo> logs = new DirectoryInfo(folder).GetFiles("setup-*.log")
                .Where(file => FileNamePattern.IsMatch(file.Name) &&
                               (file.Attributes & (FileAttributes.ReparsePoint | FileAttributes.Directory)) == 0)
                .OrderByDescending(file => file.Name, StringComparer.Ordinal)
                .ToList();
            foreach (FileInfo old in logs.Skip(SetupLogPolicy.MaxLogs - 1))
            {
                try
                {
                    if (policy.InjectedFault == SetupLogFault.Retention)
                    {
                        throw new IOException("injected test fault");
                    }
                    old.Delete();
                    OldLogsRemoved++;
                }
                catch (Exception)
                {
                    OldLogsNotRemoved++;
                }
            }
        }

        private void CreateFile(string folder)
        {
            string name = "setup-" + DateTime.UtcNow.ToString("yyyyMMdd-HHmmss", CultureInfo.InvariantCulture) + "-" +
                Guid.NewGuid().ToString("N").Substring(0, 8) + ".log";
            string path = Path.Combine(folder, name);
            try
            {
                if (policy.InjectedFault == SetupLogFault.CreateFile)
                {
                    throw new IOException("injected test fault");
                }
                // New only (never an existing file or link), readable by others while open, every write sent straight to disk.
                stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.Read, 4096, FileOptions.WriteThrough);
            }
            catch (Exception)
            {
                Problem = SetupLogProblem.FileNotCreated;
                return;
            }
            FileName = name;
            FilePath = path;
        }

        // ---- writing --------------------------------------------------------------------------------------------------

        /// <summary>A note from the wizard: its own fixed text, with at most a value from a closed set.</summary>
        public void Note(string text) => Write("setup", text);

        /// <summary>One engine event, as the wizard shows it under Show details.</summary>
        public void Event(IEnumerable<string> lines)
        {
            foreach (string line in lines ?? Enumerable.Empty<string>())
            {
                Write("event", line);
            }
        }

        /// <summary>The engine's console text.</summary>
        public void ConsoleText(string text) => Write("console", text);

        private void Write(string kind, string text)
        {
            lock (gate)
            {
                if (stream == null)
                {
                    return;
                }
                try
                {
                    if (policy.InjectedFault == SetupLogFault.Write)
                    {
                        throw new IOException("injected test fault");
                    }
                    string stamp = DateTime.UtcNow.ToString("yyyy-MM-dd'T'HH:mm:ss.fff'Z'", CultureInfo.InvariantCulture);
                    foreach (string line in (text ?? "").Replace("\r\n", "\n").Split('\n', '\r'))
                    {
                        byte[] bytes = Utf8.GetBytes(stamp + "  " + kind.PadRight(8) + Clean(SafeLine(line)) + "\r\n");
                        if (bytesWritten + bytes.Length > SetupLogPolicy.MaxBytes - TruncationReserve)
                        {
                            byte[] marker = Utf8.GetBytes(stamp + "  " + "setup".PadRight(8) + TruncatedMarker + "\r\n");
                            stream.Write(marker, 0, marker.Length);
                            bytesWritten += marker.Length;
                            IsTruncated = true;
                            CloseStream();
                            return;
                        }
                        stream.Write(bytes, 0, bytes.Length);
                        bytesWritten += bytes.Length;
                    }
                    stream.Flush();
                }
                catch (Exception)
                {
                    Problem = SetupLogProblem.WriteFailed;
                    CloseStream();
                }
            }
        }

        /// <summary>One line, at most <see cref="MaxLineLength"/> characters, redacted. A shortened line also loses its last
        /// run of token characters, so no cut-off piece of a token is ever left too short to be recognised. A line that
        /// cannot be checked in time is not written at all.</summary>
        private static string SafeLine(string line)
        {
            bool shortened = line.Length > MaxLineLength;
            if (shortened)
            {
                line = TrailingTokenRun.Replace(line.Substring(0, MaxLineLength), "");
            }
            try
            {
                return Redact(line) + (shortened ? " [line shortened]" : "");
            }
            catch (RegexMatchTimeoutException)
            {
                return "[line not written: it could not be checked for secrets in time]";
            }
        }

        /// <summary>No control characters: a line can never pretend to be another entry.</summary>
        private static string Clean(string line)
        {
            var text = new StringBuilder(line.Length);
            foreach (char c in line)
            {
                text.Append(char.IsControl(c) && c != '\t' ? '?' : c);
            }
            return text.ToString();
        }

        // ---- redaction (defence in depth: nothing secret is ever handed to the log on purpose) ------------------------

        private const RegexOptions Options = RegexOptions.CultureInvariant | RegexOptions.IgnoreCase;
        // Every pattern gives up after this long on one line (the log is written on the UI thread).
        private static readonly TimeSpan MatchTimeout = TimeSpan.FromMilliseconds(250);
        private static readonly Regex TrailingTokenRun = new Regex(@"[A-Za-z0-9_-]+$", RegexOptions.CultureInvariant, MatchTimeout);
        // SortView enrollment codes: SV-XXXX-XXXX-XXXX-XXXX. With the SV prefix: in any case, with dashes, underscores,
        // single spaces or nothing between the groups, and any letter or digit (a mistyped code too). Without the prefix:
        // only the upper-case dashed form from the code's own alphabet (no 0/O, 1/I/L), not touching another dash, so
        // ordinary words ("test-case-path-name", "when they have been") and GUIDs are never taken for a code.
        private static readonly Regex EnrollmentCode = new Regex(
            @"(?<![A-Za-z0-9])SV[ _-]?(?:[A-Z0-9]{4}[ _-]?){3}[A-Z0-9]{4}(?![A-Za-z0-9])" +
            @"|(?<![A-Za-z0-9_-])(?-i:[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4})(?![A-Za-z0-9_-])", Options, MatchTimeout);
        // An authorization value, in header, assignment or JSON form ("Authorization": "..."): the rest of the line.
        private static readonly Regex AuthorizationHeader = new Regex(@"\b((?:proxy-)?authorization)\b[""']?\s*[:=]\s*.*", Options, MatchTimeout);
        private static readonly Regex Scheme = new Regex(@"\b(bearer|basic|digest)\s+[A-Za-z0-9._~+/=-]{6,}", Options, MatchTimeout);
        // user:password@ in a URL, up to the LAST @ before the host (a password may hold an @ itself).
        private static readonly Regex UrlCredentials = new Regex(@"\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s]*@", Options, MatchTimeout);
        // key=value or key: value (quoted or not) for credential-like keys: any key containing token, secret, password,
        // passwd, pwd, passphrase, api key, enrollment code, cookie, credential or auth, and keys that are or end in key,
        // sig or signature. Only a whole true/false/yes/no/null/none is left (so "token_stored=True" stays, but
        // "password=no-way" and "password=12345678" do not).
        private static readonly Regex SecretValue = new Regex(
            @"([""']?\b(?:[\w-]*(?:token|secret|passw(?:or)?d|pwd|passphrase|api[_-]?key|enrollment[_-]?code|cookie|credential|auth)[\w-]*" +
            @"|(?:[\w-]*[_-])?(?:key|sig|signature))\b[""']?\s*[:=]\s*)" +
            @"(?!(?:true|false|yes|no|null|none)(?:[\s,;&""'})\]]|$))(""[^""]*""|'[^']*'|[^\s,;&""']+)", Options, MatchTimeout);
        // Opaque token-like runs: 32 or more URL-safe characters mixing upper case, lower case and digits (an API token is
        // 43). A SHA-256 hash or a GUID (no upper case) is left as it is.
        private static readonly Regex OpaqueToken = new Regex(@"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{32,}(?![A-Za-z0-9_-])", RegexOptions.CultureInvariant, MatchTimeout);

        /// <summary>Removes anything shaped like an enrollment code, a token, a password, a credential in a URL or an
        /// authorization header. Defence in depth only: the wizard never hands the log a secret on purpose.</summary>
        public static string Redact(string text)
        {
            if (string.IsNullOrEmpty(text))
            {
                return text ?? "";
            }
            text = AuthorizationHeader.Replace(text, "$1: [redacted]");
            text = Scheme.Replace(text, "$1 [redacted]");
            text = UrlCredentials.Replace(text, "$1[redacted]@");
            text = SecretValue.Replace(text, "$1[redacted]");
            text = EnrollmentCode.Replace(text, "[redacted enrollment code]");
            text = OpaqueToken.Replace(text, m => m.Value.Any(char.IsUpper) && m.Value.Any(char.IsLower) && m.Value.Any(char.IsDigit) ? "[redacted]" : m.Value);
            return text;
        }

        // ---- saving a copy and closing ---------------------------------------------------------------------------------

        /// <summary>Copies the log to <paramref name="destination"/>, a NEW file the person chose on a local drive. The file
        /// is created only through folders checked to be plain folders (never a junction, symbolic link or other reparse
        /// point), and only if nothing of that name exists, before anything is written (see <see cref="SafeNewFile"/>). The
        /// log itself is only read. Never throws.</summary>
        public SaveCopyResult SaveCopyTo(string destination)
        {
            if (!WasCreated)
            {
                return SaveCopyResult.NoLog;
            }
            FileStream target = null;
            try
            {
                if (policy.InjectedFault == SetupLogFault.SaveCopy)
                {
                    throw new IOException("injected test fault");
                }
                lock (gate)
                {
                    using (var source = new FileStream(FilePath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                    {
                        target = SafeNewFile.Create(destination, policy.BeforeCopyCreated, out SaveCopyResult outcome);
                        if (target == null)
                        {
                            return outcome;
                        }
                        source.CopyTo(target);
                        target.Flush(true);
                        target.Dispose();
                        target = null;
                    }
                }
                return SaveCopyResult.Saved;
            }
            catch (Exception)
            {
                if (target != null)
                {
                    SafeNewFile.RemoveOnClose(target); // a partial copy is removed (by its handle, never by its path)
                    try
                    {
                        target.Dispose();
                    }
                    catch (Exception)
                    {
                        // nothing more to do
                    }
                }
                return SaveCopyResult.Failed;
            }
        }

        private void CloseStream()
        {
            try
            {
                stream?.Dispose();
            }
            catch (Exception)
            {
                // Already written and flushed entry by entry; nothing more to save.
            }
            stream = null;
        }

        /// <summary>Closes the file. Nothing more is written; the file stays.</summary>
        public void Dispose()
        {
            lock (gate)
            {
                CloseStream();
            }
        }
    }
}
