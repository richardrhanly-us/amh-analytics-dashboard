using System;
using System.Linq;
using System.Reflection;
using System.Text.RegularExpressions;

namespace SortView.Setup
{
    /// <summary>Why a pin cannot be used.</summary>
    public enum TrustedBundlePinState
    {
        /// <summary>A hash and a version, both well formed.</summary>
        Configured,
        /// <summary>Both empty: this build trusts no bundle (the default).</summary>
        NotConfigured,
        /// <summary>Missing, repeated, half set or malformed: never used.</summary>
        Invalid,
    }

    /// <summary>
    /// The Collector release bundle this installer build trusts: the SHA-256 of its MANIFEST.json and its version, embedded
    /// at build time from installer\bundle-pin.props (AssemblyMetadata on this program, see SortView.Setup.csproj). Read
    /// only from this program's own assembly -- never from the bundle, the command line, the environment, the registry or
    /// a file beside the program. Anything but exactly one well-formed hash and version is not a pin.
    /// </summary>
    public sealed class TrustedBundlePin
    {
        internal const string ManifestSha256Key = "SortView.BundleManifestSha256";
        internal const string VersionKey = "SortView.BundleVersion";
        internal const string ValuePrefix = "pin:";

        private static readonly Regex Sha256Pattern = new Regex("^[0-9a-f]{64}$", RegexOptions.CultureInvariant);
        // The same version rule as BundleVerifier's (and the build's).
        private static readonly Regex VersionPattern = new Regex(@"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+.][0-9A-Za-z][0-9A-Za-z.+-]*)?$", RegexOptions.CultureInvariant);
        private const int MaxVersionLength = 64;

        /// <summary>For tests (internal): a pin from given values, checked exactly as an embedded one is.</summary>
        internal TrustedBundlePin(string manifestSha256, string version)
        {
            manifestSha256 = manifestSha256 ?? "";
            version = version ?? "";
            if (manifestSha256.Length == 0 && version.Length == 0)
            {
                State = TrustedBundlePinState.NotConfigured;
            }
            else if (Sha256Pattern.IsMatch(manifestSha256) && version.Length <= MaxVersionLength && VersionPattern.IsMatch(version))
            {
                State = TrustedBundlePinState.Configured;
                ManifestSha256 = manifestSha256;
                Version = version;
            }
            else
            {
                State = TrustedBundlePinState.Invalid;
            }
        }

        public TrustedBundlePinState State { get; }

        public bool IsConfigured => State == TrustedBundlePinState.Configured;

        /// <summary>The trusted manifest SHA-256 (lowercase), or null unless configured.</summary>
        public string ManifestSha256 { get; }

        /// <summary>The trusted release version, or null unless configured.</summary>
        public string Version { get; }

        /// <summary>The pin embedded in this program.</summary>
        public static TrustedBundlePin ForThisInstaller() => Read(typeof(TrustedBundlePin).Assembly);

        internal static TrustedBundlePin Read(Assembly assembly)
        {
            AssemblyMetadataAttribute[] metadata = assembly.GetCustomAttributes(typeof(AssemblyMetadataAttribute), false)
                .Cast<AssemblyMetadataAttribute>().ToArray();
            string sha = Single(metadata, ManifestSha256Key);
            string version = Single(metadata, VersionKey);
            if (sha == null || version == null)
            {
                return Invalid();
            }
            return new TrustedBundlePin(sha, version);
        }

        /// <summary>The value of exactly one attribute named <paramref name="key"/>, without its prefix; null otherwise.</summary>
        private static string Single(AssemblyMetadataAttribute[] metadata, string key)
        {
            AssemblyMetadataAttribute[] matches = metadata.Where(m => string.Equals(m.Key, key, StringComparison.Ordinal)).ToArray();
            if (matches.Length != 1 || matches[0].Value == null || !matches[0].Value.StartsWith(ValuePrefix, StringComparison.Ordinal))
            {
                return null;
            }
            return matches[0].Value.Substring(ValuePrefix.Length);
        }

        private static TrustedBundlePin Invalid() => new TrustedBundlePin("invalid", "invalid");
    }
}
