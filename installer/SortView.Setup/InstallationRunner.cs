using System;
using SortView.Setup.Core;
using SortView.Setup.Wpf;

namespace SortView.Setup
{
    /// <summary>
    /// The production setup runner: the existing setup engine (<see cref="SetupEngineHost"/>, running the bundle's own
    /// setup.ps1) over the PROTECTED staged copy of the trusted bundle -- never the original bundle folder, and never a
    /// path anyone else supplies. A runner exists only when this build may install (its installation gate is enabled and
    /// its trusted pin configured, both embedded at build time) and a verified bundle of the pinned version is staged and
    /// held. Otherwise there is none, and nothing runs.
    /// </summary>
    internal static class InstallationRunner
    {
        /// <summary>Where the Collector is installed, and where its data lives: the setup engine's own defaults.</summary>
        internal const string InstallRoot = @"C:\SortView\Collector";
        internal const string DataRoot = @"C:\ProgramData\SortViewCollector";

        /// <summary>Whether this build may install at all: an enabled gate and a configured pin.</summary>
        internal static bool MayInstall(InstallationGate gate, TrustedBundlePin pin) =>
            gate != null && gate.IsEnabled && pin != null && pin.IsConfigured;

        /// <summary>
        /// The runner for one setup run over <paramref name="staged"/>, or null: this build may not install, or no verified
        /// bundle of the pinned version is staged and held. <paramref name="engine"/> turns the request into a runner (the
        /// real engine, <see cref="RealEngine"/>, except in tests); it is never called when the answer is null. The runner
        /// returned verifies the staged copy against the pin again before it runs <paramref name="engine"/>'s runner, and
        /// throws instead of running it unless that verification succeeds.
        /// </summary>
        internal static SetupRunner Create(InstallationGate gate, TrustedBundlePin pin, StagedBundle staged, Func<SetupRunRequest, SetupRunner> engine)
        {
            if (!MayInstall(gate, pin) || engine == null)
            {
                return null;
            }
            if (staged == null || staged.IsDisposed || !staged.IsStaged || string.IsNullOrEmpty(staged.BundlePath) ||
                !string.Equals(staged.Version, pin.Version, StringComparison.Ordinal))
            {
                return null;
            }
            var request = new SetupRunRequest { BundleRoot = staged.BundlePath, InstallRoot = InstallRoot, DataRoot = DataRoot };
            SetupRunner run = engine(request);
            if (run == null)
            {
                return null;
            }
            string expectedSha256 = pin.ManifestSha256;
            string expectedVersion = pin.Version;
            // Immediately before the engine runs (on the run's own thread, never the UI's): the staged copy is verified again
            // against the pin -- it was verified when it was prepared, but setup may start much later. Only an explicit
            // success runs the engine; anything else (a changed, added or missing file, an unreadable bundle) stops here.
            return (presenter, onEvent, onConsoleText, cancellationToken) =>
            {
                BundleVerification verification = BundleVerifier.Verify(request.BundleRoot, expectedSha256, expectedVersion);
                if (verification == null || !verification.IsVerified)
                {
                    throw new InvalidOperationException("The checked installation package changed after it was prepared, so setup " +
                        "did not start. Nothing was installed or changed. Select Back until the Ready page is shown, then select Next to prepare it again.");
                }
                return run(presenter, onEvent, onConsoleText, cancellationToken);
            };
        }

        /// <summary>The real engine: one <see cref="SetupEngineHost"/> run of the staged bundle's setup.ps1.</summary>
        internal static SetupRunner RealEngine(SetupRunRequest request) =>
            (presenter, onEvent, onConsoleText, cancellationToken) =>
                new SetupEngineHost().Run(request, presenter, onEvent, onConsoleText, cancellationToken);
    }
}
