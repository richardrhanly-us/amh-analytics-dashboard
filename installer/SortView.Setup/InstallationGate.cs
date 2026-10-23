using System;
using System.Linq;
using System.Reflection;

namespace SortView.Setup
{
    /// <summary>What an installation gate says.</summary>
    public enum InstallationGateState
    {
        /// <summary>Exactly "false": this build cannot install (the default).</summary>
        Disabled,
        /// <summary>Exactly "true": this reviewed build may install (with its trusted bundle pin).</summary>
        Enabled,
        /// <summary>Missing, repeated or anything else: treated as disabled.</summary>
        Invalid,
    }

    /// <summary>
    /// Whether this installer build may install SortView Collector, embedded at build time from installer\installation-gate.props
    /// (AssemblyMetadata on this program, see SortView.Setup.csproj). Read only from this program's own assembly -- never from
    /// the command line, the environment, the registry or a file beside the program. Fails closed: anything but exactly one
    /// well-formed "true" is not enabled.
    /// </summary>
    public sealed class InstallationGate
    {
        internal const string Key = "SortView.InstallationEnabled";
        internal const string ValuePrefix = "gate:";

        /// <summary>For tests (internal): a gate from the values of the metadata entries named <see cref="Key"/>, checked
        /// exactly as an embedded one is.</summary>
        internal InstallationGate(string[] values)
        {
            if (values == null || values.Length != 1 || values[0] == null)
            {
                State = InstallationGateState.Invalid;
            }
            else if (string.Equals(values[0], ValuePrefix + "true", StringComparison.Ordinal))
            {
                State = InstallationGateState.Enabled;
            }
            else if (string.Equals(values[0], ValuePrefix + "false", StringComparison.Ordinal))
            {
                State = InstallationGateState.Disabled;
            }
            else
            {
                State = InstallationGateState.Invalid;
            }
        }

        public InstallationGateState State { get; }

        public bool IsEnabled => State == InstallationGateState.Enabled;

        /// <summary>The gate embedded in this program.</summary>
        public static InstallationGate ForThisInstaller() => Read(typeof(InstallationGate).Assembly);

        internal static InstallationGate Read(Assembly assembly) =>
            new InstallationGate(assembly.GetCustomAttributes(typeof(AssemblyMetadataAttribute), false)
                .Cast<AssemblyMetadataAttribute>()
                .Where(m => string.Equals(m.Key, Key, StringComparison.Ordinal))
                .Select(m => m.Value)
                .ToArray());
    }
}
