using System;
using System.Collections.Generic;
using System.Management.Automation;
using System.Management.Automation.Runspaces;
using System.Security;

namespace SortView.Setup.Spike
{
    // One structured progress record, built from a PowerShell Information-stream object -- never from text output.
    public sealed class SpikeEvent
    {
        public string Schema { get; set; }
        public string Stage { get; set; }
        public string Status { get; set; }
        public string Detail { get; set; }
    }

    public sealed class SpikeResult
    {
        public bool Succeeded { get; set; }
        public string PSVersion { get; set; }
        public string PSEdition { get; set; }
        public int ChildProcessCount { get; set; }
        public string Error { get; set; }
        public List<SpikeEvent> Events { get; } = new List<SpikeEvent>();
    }

    // Phase 0B: hosts Windows PowerShell 5.1 in-process and runs SYNTHETIC functions only. Nothing here touches the
    // network, Scheduled Tasks, credentials, files, or any SortView script.
    public static class SpikeRunner
    {
        public const string EventTag = "SortView.Event";

        // Defined in memory. The enrollment code arrives as a SecureString object parameter and is never read:
        // the simulation only checks its type and that it is non-empty.
        private const string SyntheticScript = @"
function Send-SpikeEvent {
    param([string]$Stage, [string]$Status, [string]$Detail)
    Write-Information -Tags 'SortView.Event' -MessageData ([pscustomobject]@{
        schema = 'sortview.spike.event/1'; stage = $Stage; status = $Status; detail = $Detail
    })
}

function Invoke-SpikeSimulation {
    param([Parameter(Mandatory)][SecureString]$EnrollmentCode)

    Send-SpikeEvent -Stage 'prerequisites' -Status 'started' -Detail 'Simulated prerequisite checks'
    $version = $PSVersionTable.PSVersion.ToString()
    $edition = [string]$PSVersionTable.PSEdition
    Start-Sleep -Milliseconds 800
    Send-SpikeEvent -Stage 'prerequisites' -Status 'ok' -Detail ""Windows PowerShell $version ($edition)""

    Send-SpikeEvent -Stage 'enrollment' -Status 'started' -Detail 'Simulated enrollment (no network request)'
    Start-Sleep -Milliseconds 800
    $received = ($EnrollmentCode -is [System.Security.SecureString]) -and $EnrollmentCode.Length -gt 0
    Send-SpikeEvent -Stage 'enrollment' -Status $(if ($received) { 'ok' } else { 'failed' }) `
        -Detail $(if ($received) { 'SecureString received as an object parameter (value not read)' } else { 'No SecureString was received' })

    Send-SpikeEvent -Stage 'completion' -Status 'started' -Detail 'Checking for child processes'
    $children = @(Get-CimInstance -ClassName Win32_Process -Filter ""ParentProcessId = $PID"").Count
    Start-Sleep -Milliseconds 400
    Send-SpikeEvent -Stage 'completion' -Status $(if ($received) { 'ok' } else { 'failed' }) -Detail ""Child processes of this process: $children""

    [pscustomobject]@{ Succeeded = $received; PSVersion = $version; PSEdition = $edition; ChildProcessCount = $children }
}
";

        public static SpikeResult Run(SecureString enrollmentCode, Action<SpikeEvent> onEvent)
        {
            var result = new SpikeResult();
            using (Runspace runspace = RunspaceFactory.CreateRunspace(InitialSessionState.CreateDefault()))
            {
                runspace.Open();
                using (PowerShell ps = PowerShell.Create())
                {
                    ps.Runspace = runspace;
                    ps.AddScript(SyntheticScript).Invoke();
                    if (ps.HadErrors) { result.Error = FirstError(ps); return result; }

                    ps.Commands.Clear();
                    ps.Streams.Information.DataAdded += (sender, e) =>
                    {
                        SpikeEvent ev = ToEvent(ps.Streams.Information[e.Index]);
                        if (ev == null) return;
                        result.Events.Add(ev);
                        onEvent?.Invoke(ev);
                    };

                    ps.AddCommand("Invoke-SpikeSimulation").AddParameter("EnrollmentCode", enrollmentCode);
                    var output = ps.Invoke();
                    if (ps.HadErrors) { result.Error = FirstError(ps); return result; }
                    if (output.Count != 1) { result.Error = "Expected one result object, got " + output.Count + "."; return result; }

                    PSObject summary = output[0];
                    result.Succeeded = (bool)summary.Properties["Succeeded"].Value;
                    result.PSVersion = (string)summary.Properties["PSVersion"].Value;
                    result.PSEdition = (string)summary.Properties["PSEdition"].Value;
                    result.ChildProcessCount = Convert.ToInt32(summary.Properties["ChildProcessCount"].Value);
                }
            }
            return result;
        }

        private static SpikeEvent ToEvent(InformationRecord record)
        {
            if (record == null || !record.Tags.Contains(EventTag)) return null;
            var data = record.MessageData as PSObject;
            if (data == null) return null;
            return new SpikeEvent
            {
                Schema = data.Properties["schema"]?.Value as string,
                Stage = data.Properties["stage"]?.Value as string,
                Status = data.Properties["status"]?.Value as string,
                Detail = data.Properties["detail"]?.Value as string,
            };
        }

        private static string FirstError(PowerShell ps)
        {
            return ps.Streams.Error.Count > 0 ? ps.Streams.Error[0].ToString() : "PowerShell reported an error.";
        }
    }
}
