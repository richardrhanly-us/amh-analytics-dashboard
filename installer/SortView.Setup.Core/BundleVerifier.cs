using System;
using System.Collections;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;

namespace SortView.Setup.Core
{
    /// <summary>Why a bundle was not verified. Each problem names a bundle-relative path, never any file's contents.</summary>
    public enum BundleProblemCode
    {
        /// <summary>The expected manifest SHA-256 is not 64 hexadecimal characters.</summary>
        ExpectedHashInvalid,
        /// <summary>The bundle root is not an absolute path to a directory.</summary>
        BundleRootInvalid,
        BundleRootMissing,
        /// <summary>A junction, symbolic link or other reparse point: never followed or read.</summary>
        ReparsePoint,
        AccessDenied,
        /// <summary>Any other I/O failure while reading.</summary>
        ReadError,
        /// <summary>More entries, deeper folders or a larger manifest than any real bundle has.</summary>
        LimitExceeded,
        ManifestMissing,
        /// <summary>MANIFEST.json's SHA-256 is not the expected one.</summary>
        ManifestHashMismatch,
        /// <summary>Not the manifest schema build_release.py writes.</summary>
        ManifestMalformed,
        ProductMismatch,
        VersionInvalid,
        VersionMismatch,
        /// <summary>A listed path that is rooted, traverses, names a stream or device, or is otherwise not a plain file name.</summary>
        UnsafePath,
        DuplicatePath,
        /// <summary>Two listed paths that differ only in case, or one listed both as a file and as a folder.</summary>
        PathCollision,
        FileMissing,
        /// <summary>A listed path that is a folder on disk.</summary>
        NotAFile,
        SizeMismatch,
        HashMismatch,
        UnexpectedFile,
        /// <summary>A folder no listed file is in (an empty one included).</summary>
        UnexpectedDirectory,
    }

    public sealed class BundleProblem
    {
        internal BundleProblem(BundleProblemCode code, string path)
        {
            Code = code;
            Path = path ?? "";
        }

        public BundleProblemCode Code { get; }

        /// <summary>The bundle-relative path concerned ("/" separated), or "" for the bundle or the request itself.</summary>
        public string Path { get; }

        public override string ToString() => Code + (Path.Length == 0 ? "" : ": " + Path);
    }

    /// <summary>The outcome of <see cref="BundleVerifier.Verify"/>.</summary>
    public sealed class BundleVerification
    {
        internal BundleVerification(bool isVerified, string version, int fileCount, IList<BundleProblem> problems, bool problemsTruncated)
        {
            IsVerified = isVerified;
            Version = version;
            FileCount = fileCount;
            Problems = new ReadOnlyCollection<BundleProblem>(problems);
            ProblemsTruncated = problemsTruncated;
        }

        /// <summary>True only when MANIFEST.json has exactly the expected SHA-256 and the bundle holds exactly the files it
        /// lists -- nothing more, nothing less, no link. This says the bundle is the one the expected hash names; it says
        /// nothing about who published it, which only a trusted signature on whatever supplied that hash can.</summary>
        public bool IsVerified { get; }

        /// <summary>The manifest's version, once the manifest matched the expected hash; otherwise null.</summary>
        public string Version { get; }

        /// <summary>Files the manifest lists (0 until the manifest matched the expected hash).</summary>
        public int FileCount { get; }

        public IReadOnlyList<BundleProblem> Problems { get; }

        /// <summary>More problems were found than are listed.</summary>
        public bool ProblemsTruncated { get; }
    }

    /// <summary>
    /// Verifies a frozen SortView Collector release bundle against the SHA-256 of its MANIFEST.json, supplied by the caller
    /// from somewhere other than the bundle (eventually: the signed installer). Read-only: it lists folders, reads file
    /// attributes and file bytes, and nothing else -- it never writes, runs anything, follows a link or uses the network.
    ///
    /// Order matters, so nothing untrusted is read before it is checked: (1) the request; (2) the bundle folder and every
    /// entry under it, hidden and system ones included, walked folder by folder and never into a reparse point -- any
    /// reparse point, the bundle folder itself included, stops verification before a single file is read; (3) MANIFEST.json,
    /// read once, whose bytes must hash to the expected value before they are parsed; (4) the manifest's schema and every
    /// listed path, each validated as a plain relative file name before it is ever combined with the bundle folder; (5) the
    /// inventory: every listed file present with its size and SHA-256, and no other file or folder. Any error fails closed.
    ///
    /// The manifest is the one collector/build_release.py writes: {"product", "version", "built_at", "files": [{"path",
    /// "sha256", "size_bytes"}]}, paths relative with "/" separators, SHA-256 in hexadecimal. MANIFEST.json itself is not
    /// listed. Every folder in a bundle holds at least one listed file, so any other folder -- even an empty one -- is refused.
    /// </summary>
    public static class BundleVerifier
    {
        public const string ManifestFileName = "MANIFEST.json";
        public const string ProductName = "SortView Collector";

        // Far beyond a real bundle (1.0.10: 1,350 files, 99 folders, a 253 KB manifest, paths up to 97 characters).
        internal const int MaxEntries = 100000;
        internal const int MaxDepth = 32;
        internal const int MaxManifestBytes = 16 * 1024 * 1024;
        internal const int MaxPathLength = 400;
        internal const int MaxVersionLength = 64;
        internal const int MaxReportedProblems = 500;

        private static readonly Regex Sha256Pattern = new Regex("^[0-9a-fA-F]{64}$", RegexOptions.CultureInvariant);
        private static readonly Regex VersionPattern = new Regex(@"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+.][0-9A-Za-z][0-9A-Za-z.+-]*)?$", RegexOptions.CultureInvariant);
        private static readonly HashSet<string> ReservedNames = new HashSet<string>(
            new[] { "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$" }
                .Concat(Enumerable.Range(1, 9).Select(n => "COM" + n))
                .Concat(Enumerable.Range(1, 9).Select(n => "LPT" + n)),
            StringComparer.OrdinalIgnoreCase);
        private static readonly char[] ForbiddenPathChars = { '\\', ':', '*', '?', '"', '<', '>', '|' };
        private static readonly string[] TopLevelKeys = { "product", "version", "built_at", "files" };
        private static readonly string[] EntryKeys = { "path", "sha256", "size_bytes" };

        /// <summary>Verifies the bundle at <paramref name="bundleRoot"/> (an absolute path) against
        /// <paramref name="expectedManifestSha256"/> and, when given, <paramref name="expectedVersion"/>.</summary>
        public static BundleVerification Verify(string bundleRoot, string expectedManifestSha256, string expectedVersion = null)
        {
            var report = new Report();
            try
            {
                return VerifyCore(bundleRoot, expectedManifestSha256, expectedVersion, report);
            }
            catch (UnauthorizedAccessException)
            {
                report.Add(BundleProblemCode.AccessDenied, "");
            }
            catch (Exception)
            {
                report.Add(BundleProblemCode.ReadError, "");
            }
            return report.Result(null, 0);
        }

        private static BundleVerification VerifyCore(string bundleRoot, string expectedManifestSha256, string expectedVersion, Report report)
        {
            // (1) The request. The expected hash comes from the caller, never from the bundle.
            string expectedHash = (expectedManifestSha256 ?? "").Trim();
            if (!Sha256Pattern.IsMatch(expectedHash))
            {
                report.Add(BundleProblemCode.ExpectedHashInvalid, "");
            }
            if (expectedVersion != null && !IsValidVersion(expectedVersion))
            {
                report.Add(BundleProblemCode.VersionInvalid, "");
            }
            string root = NormalizeRoot(bundleRoot);
            if (root == null)
            {
                report.Add(BundleProblemCode.BundleRootInvalid, "");
            }
            if (report.Any)
            {
                return report.Result(null, 0);
            }

            // (2) The tree, before anything in it is read.
            var tree = Walk(root, report);
            if (report.Any)
            {
                return report.Result(null, 0);
            }

            // (3) MANIFEST.json: read once, hashed before it is parsed.
            FileInfo manifestFile;
            if (!tree.Files.TryGetValue(ManifestFileName, out manifestFile) || !string.Equals(manifestFile.Name, ManifestFileName, StringComparison.Ordinal))
            {
                report.Add(BundleProblemCode.ManifestMissing, ManifestFileName);
                return report.Result(null, 0);
            }
            if (manifestFile.Length > MaxManifestBytes)
            {
                report.Add(BundleProblemCode.LimitExceeded, ManifestFileName);
                return report.Result(null, 0);
            }
            byte[] manifestBytes = ReadAll(manifestFile, ManifestFileName, report);
            if (manifestBytes == null)
            {
                return report.Result(null, 0);
            }
            if (!string.Equals(Sha256Hex(manifestBytes), expectedHash, StringComparison.OrdinalIgnoreCase))
            {
                report.Add(BundleProblemCode.ManifestHashMismatch, ManifestFileName);
                return report.Result(null, 0);
            }

            // (4) Schema and listed paths.
            Manifest manifest = Parse(manifestBytes, report);
            if (manifest == null)
            {
                return report.Result(null, 0);
            }
            if (!string.Equals(manifest.Product, ProductName, StringComparison.Ordinal))
            {
                report.Add(BundleProblemCode.ProductMismatch, "");
            }
            if (!IsValidVersion(manifest.Version))
            {
                report.Add(BundleProblemCode.VersionInvalid, "");
            }
            else if (expectedVersion != null && !string.Equals(manifest.Version, expectedVersion, StringComparison.Ordinal))
            {
                report.Add(BundleProblemCode.VersionMismatch, "");
            }
            var listed = new Dictionary<string, ManifestFile>(StringComparer.OrdinalIgnoreCase);
            var exact = new HashSet<string>(StringComparer.Ordinal);
            foreach (ManifestFile entry in manifest.Files)
            {
                if (!IsSafeRelativePath(entry.Path) || string.Equals(entry.Path, ManifestFileName, StringComparison.OrdinalIgnoreCase))
                {
                    report.Add(BundleProblemCode.UnsafePath, Printable(entry.Path));
                    continue;
                }
                if (!exact.Add(entry.Path))
                {
                    report.Add(BundleProblemCode.DuplicatePath, entry.Path);
                    continue;
                }
                if (listed.ContainsKey(entry.Path))
                {
                    report.Add(BundleProblemCode.PathCollision, entry.Path);
                    continue;
                }
                listed.Add(entry.Path, entry);
            }
            var listedFolders = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (string path in listed.Keys)
            {
                for (int slash = path.IndexOf('/'); slash >= 0; slash = path.IndexOf('/', slash + 1))
                {
                    listedFolders.Add(path.Substring(0, slash));
                }
            }
            foreach (string path in listed.Keys.Where(listedFolders.Contains).ToList())
            {
                report.Add(BundleProblemCode.PathCollision, path); // listed as a file, and as the folder of another file
            }
            if (report.Any)
            {
                return report.Result(manifest.Version, manifest.Files.Count);
            }

            // (5) The inventory: exactly the listed files, at their sizes and hashes; no other file or folder.
            foreach (ManifestFile entry in listed.Values)
            {
                FileInfo file;
                if (!tree.Files.TryGetValue(entry.Path, out file))
                {
                    report.Add(tree.Directories.Contains(entry.Path) ? BundleProblemCode.NotAFile : BundleProblemCode.FileMissing, entry.Path);
                    continue;
                }
                if (file.Length != entry.SizeBytes)
                {
                    report.Add(BundleProblemCode.SizeMismatch, entry.Path);
                    continue;
                }
                string hash = HashFile(file, entry.Path, report);
                if (hash != null && !string.Equals(hash, entry.Sha256, StringComparison.OrdinalIgnoreCase))
                {
                    report.Add(BundleProblemCode.HashMismatch, entry.Path);
                }
            }
            foreach (string path in tree.Files.Keys)
            {
                if (!string.Equals(path, ManifestFileName, StringComparison.OrdinalIgnoreCase) && !listed.ContainsKey(path))
                {
                    report.Add(BundleProblemCode.UnexpectedFile, Printable(path));
                }
            }
            foreach (string path in tree.Directories)
            {
                if (!listedFolders.Contains(path))
                {
                    report.Add(BundleProblemCode.UnexpectedDirectory, Printable(path));
                }
            }
            return report.Result(manifest.Version, manifest.Files.Count);
        }

        // ---- the bundle folder ---------------------------------------------------------------------------------------

        private static string NormalizeRoot(string bundleRoot)
        {
            if (string.IsNullOrWhiteSpace(bundleRoot) || bundleRoot.StartsWith(@"\\?\", StringComparison.Ordinal) ||
                bundleRoot.StartsWith(@"\\.\", StringComparison.Ordinal) || !Path.IsPathRooted(bundleRoot))
            {
                return null;
            }
            try
            {
                string full = Path.GetFullPath(bundleRoot).TrimEnd('\\', '/');
                // A drive or share root is never a bundle (and "C:" alone would mean the current folder on C:).
                if (string.Equals(Path.GetPathRoot(full + "\\").TrimEnd('\\', '/'), full, StringComparison.OrdinalIgnoreCase))
                {
                    return null;
                }
                return full;
            }
            catch (Exception)
            {
                return null;
            }
        }

        private sealed class Tree
        {
            // Bundle-relative, "/"-separated, case-insensitive (as NTFS is).
            public readonly Dictionary<string, FileInfo> Files = new Dictionary<string, FileInfo>(StringComparer.OrdinalIgnoreCase);
            public readonly HashSet<string> Directories = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        }

        /// <summary>Lists every entry, hidden and system ones included, one folder at a time. A reparse point is reported
        /// and never entered or read; listing it at all is how it is found.</summary>
        private static Tree Walk(string root, Report report)
        {
            var tree = new Tree();
            FileAttributes rootAttributes;
            try
            {
                rootAttributes = File.GetAttributes(root); // the entry itself: a junction reports ReparsePoint here
            }
            catch (FileNotFoundException)
            {
                report.Add(BundleProblemCode.BundleRootMissing, "");
                return tree;
            }
            catch (DirectoryNotFoundException)
            {
                report.Add(BundleProblemCode.BundleRootMissing, "");
                return tree;
            }
            if ((rootAttributes & FileAttributes.ReparsePoint) != 0)
            {
                report.Add(BundleProblemCode.ReparsePoint, "");
                return tree;
            }
            if ((rootAttributes & FileAttributes.Directory) == 0)
            {
                report.Add(BundleProblemCode.BundleRootInvalid, "");
                return tree;
            }

            var pending = new Stack<KeyValuePair<DirectoryInfo, string>>();
            pending.Push(new KeyValuePair<DirectoryInfo, string>(new DirectoryInfo(root), ""));
            int entries = 0;
            while (pending.Count > 0)
            {
                KeyValuePair<DirectoryInfo, string> folder = pending.Pop();
                if (folder.Value.Count(c => c == '/') >= MaxDepth)
                {
                    report.Add(BundleProblemCode.LimitExceeded, Printable(folder.Value));
                    return tree;
                }
                IEnumerable<FileSystemInfo> children;
                try
                {
                    children = folder.Key.EnumerateFileSystemInfos().ToList();
                }
                catch (UnauthorizedAccessException)
                {
                    report.Add(BundleProblemCode.AccessDenied, Printable(folder.Value));
                    return tree;
                }
                catch (IOException)
                {
                    report.Add(BundleProblemCode.ReadError, Printable(folder.Value));
                    return tree;
                }
                foreach (FileSystemInfo child in children)
                {
                    if (++entries > MaxEntries)
                    {
                        report.Add(BundleProblemCode.LimitExceeded, "");
                        return tree;
                    }
                    string relative = folder.Value.Length == 0 ? child.Name : folder.Value + "/" + child.Name;
                    if ((child.Attributes & FileAttributes.ReparsePoint) != 0)
                    {
                        report.Add(BundleProblemCode.ReparsePoint, Printable(relative));
                        continue;
                    }
                    // Two entries whose paths differ only in case (a case-sensitive folder): one must never hide the other.
                    if (tree.Files.ContainsKey(relative) || tree.Directories.Contains(relative))
                    {
                        report.Add(BundleProblemCode.PathCollision, Printable(relative));
                        return tree;
                    }
                    var directory = child as DirectoryInfo;
                    if (directory != null)
                    {
                        tree.Directories.Add(relative);
                        pending.Push(new KeyValuePair<DirectoryInfo, string>(directory, relative));
                    }
                    else
                    {
                        tree.Files.Add(relative, (FileInfo)child);
                    }
                }
            }
            return tree;
        }

        // ---- reading -------------------------------------------------------------------------------------------------

        private static byte[] ReadAll(FileInfo file, string relative, Report report)
        {
            try
            {
                using (var stream = new FileStream(file.FullName, FileMode.Open, FileAccess.Read, FileShare.Read))
                {
                    if (stream.Length > MaxManifestBytes)
                    {
                        report.Add(BundleProblemCode.LimitExceeded, relative);
                        return null;
                    }
                    var bytes = new byte[stream.Length];
                    int read = 0;
                    while (read < bytes.Length)
                    {
                        int n = stream.Read(bytes, read, bytes.Length - read);
                        if (n == 0)
                        {
                            report.Add(BundleProblemCode.ReadError, relative);
                            return null;
                        }
                        read += n;
                    }
                    return bytes;
                }
            }
            catch (UnauthorizedAccessException)
            {
                report.Add(BundleProblemCode.AccessDenied, relative);
            }
            catch (IOException)
            {
                report.Add(BundleProblemCode.ReadError, relative);
            }
            return null;
        }

        private static string HashFile(FileInfo file, string relative, Report report)
        {
            try
            {
                using (var stream = new FileStream(file.FullName, FileMode.Open, FileAccess.Read, FileShare.Read, 65536, FileOptions.SequentialScan))
                using (SHA256 sha = SHA256.Create())
                {
                    return Hex(sha.ComputeHash(stream));
                }
            }
            catch (UnauthorizedAccessException)
            {
                report.Add(BundleProblemCode.AccessDenied, relative);
            }
            catch (IOException)
            {
                report.Add(BundleProblemCode.ReadError, relative);
            }
            return null;
        }

        private static string Sha256Hex(byte[] bytes)
        {
            using (SHA256 sha = SHA256.Create())
            {
                return Hex(sha.ComputeHash(bytes));
            }
        }

        private static string Hex(byte[] bytes)
        {
            var text = new StringBuilder(bytes.Length * 2);
            foreach (byte b in bytes)
            {
                text.Append(b.ToString("x2", System.Globalization.CultureInfo.InvariantCulture));
            }
            return text.ToString();
        }

        // ---- the manifest --------------------------------------------------------------------------------------------

        private sealed class Manifest
        {
            public string Product;
            public string Version;
            public List<ManifestFile> Files;
        }

        private sealed class ManifestFile
        {
            public string Path;
            public string Sha256;
            public long SizeBytes;
        }

        /// <summary>Parses the (already hash-matched) manifest, accepting exactly build_release.py's schema.</summary>
        private static Manifest Parse(byte[] bytes, Report report)
        {
            object root;
            try
            {
                string text = new UTF8Encoding(false, true).GetString(bytes);
                var serializer = new JavaScriptSerializer { MaxJsonLength = MaxManifestBytes, RecursionLimit = 8 };
                root = serializer.DeserializeObject(text);
            }
            catch (Exception)
            {
                report.Add(BundleProblemCode.ManifestMalformed, ManifestFileName);
                return null;
            }
            var top = root as IDictionary<string, object>;
            string product, version, builtAt;
            var files = top == null ? null : Value(top, "files") as IEnumerable;
            if (top == null || !HasExactly(top, TopLevelKeys) || !TryString(top, "product", out product) ||
                !TryString(top, "version", out version) || !TryString(top, "built_at", out builtAt) ||
                files == null || files is string || files is IDictionary)
            {
                report.Add(BundleProblemCode.ManifestMalformed, ManifestFileName);
                return null;
            }
            var manifest = new Manifest { Product = product, Version = version, Files = new List<ManifestFile>() };
            foreach (object item in files)
            {
                if (manifest.Files.Count >= MaxEntries)
                {
                    report.Add(BundleProblemCode.LimitExceeded, ManifestFileName);
                    return null;
                }
                var entry = item as IDictionary<string, object>;
                string path, sha256;
                long size;
                if (entry == null || !HasExactly(entry, EntryKeys) || !TryString(entry, "path", out path) ||
                    !TryString(entry, "sha256", out sha256) || !Sha256Pattern.IsMatch(sha256) || !TryNonNegativeInteger(entry, "size_bytes", out size))
                {
                    report.Add(BundleProblemCode.ManifestMalformed, ManifestFileName);
                    return null;
                }
                manifest.Files.Add(new ManifestFile { Path = path, Sha256 = sha256, SizeBytes = size });
            }
            return manifest;
        }

        private static object Value(IDictionary<string, object> map, string key)
        {
            object value;
            return map.TryGetValue(key, out value) ? value : null;
        }

        private static bool HasExactly(IDictionary<string, object> map, string[] keys) =>
            map.Count == keys.Length && keys.All(map.ContainsKey);

        private static bool TryString(IDictionary<string, object> map, string key, out string value)
        {
            value = Value(map, key) as string;
            return value != null;
        }

        private static bool TryNonNegativeInteger(IDictionary<string, object> map, string key, out long value)
        {
            object raw = Value(map, key);
            value = -1;
            if (raw is int)
            {
                value = (int)raw;
            }
            else if (raw is long)
            {
                value = (long)raw;
            }
            return value >= 0;
        }

        private static bool IsValidVersion(string version) =>
            version != null && version.Length <= MaxVersionLength && VersionPattern.IsMatch(version);

        /// <summary>A listed path is a plain relative file path: "/" separated, no empty, "." or ".." segment, no
        /// separator or drive at the start, no backslash, colon (drive or alternate data stream), wildcard or other
        /// character Windows forbids in a name, no control character, no segment ending in a dot or a space, and no
        /// reserved device name (CON, NUL, COM1, ... with or without an extension).</summary>
        internal static bool IsSafeRelativePath(string path)
        {
            if (string.IsNullOrEmpty(path) || path.Length > MaxPathLength || path.IndexOfAny(ForbiddenPathChars) >= 0 ||
                path.Any(c => c < 0x20 || c == 0x7F))
            {
                return false;
            }
            foreach (string segment in path.Split('/'))
            {
                if (segment.Length == 0 || segment == "." || segment == ".." || segment.EndsWith(".", StringComparison.Ordinal) ||
                    segment.EndsWith(" ", StringComparison.Ordinal))
                {
                    return false;
                }
                int dot = segment.IndexOf('.');
                string stem = (dot < 0 ? segment : segment.Substring(0, dot)).TrimEnd(' ');
                if (ReservedNames.Contains(stem))
                {
                    return false;
                }
            }
            return true;
        }

        /// <summary>A path for a report: control characters replaced, length bounded.</summary>
        private static string Printable(string path)
        {
            if (path == null)
            {
                return "";
            }
            var text = new string(path.Select(c => c < 0x20 || c == 0x7F ? '?' : c).ToArray());
            return text.Length > MaxPathLength ? text.Substring(0, MaxPathLength) + "..." : text;
        }

        private sealed class Report
        {
            private readonly List<BundleProblem> problems = new List<BundleProblem>();
            private bool truncated;

            public bool Any => problems.Count > 0;

            public void Add(BundleProblemCode code, string path)
            {
                if (problems.Count >= MaxReportedProblems)
                {
                    truncated = true;
                    return;
                }
                problems.Add(new BundleProblem(code, path));
            }

            public BundleVerification Result(string version, int fileCount)
            {
                List<BundleProblem> sorted = problems.OrderBy(p => p.Code).ThenBy(p => p.Path, StringComparer.Ordinal).ToList();
                return new BundleVerification(sorted.Count == 0 && !truncated, IsValidVersion(version) ? version : null, fileCount, sorted, truncated);
            }
        }
    }
}
