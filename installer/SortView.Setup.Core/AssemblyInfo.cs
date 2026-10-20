using System.Runtime.CompilerServices;

// The ONLY assembly that may reach the internal pre-run seam of SetupEngineHost (RunCore): the simulation harness in
// Testing/, which is never shipped. The public API has no way to run additional PowerShell. (This is API-surface
// isolation, not a security boundary against code already running in the process.)
[assembly: InternalsVisibleTo("SortView.Setup.Core.Testing")]
