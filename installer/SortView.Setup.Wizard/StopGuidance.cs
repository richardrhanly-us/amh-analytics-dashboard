using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using SortView.Setup.Core;

namespace SortView.Setup.Wizard
{
    /// <summary>Who can act on a run that did not complete.</summary>
    public enum StopCategory
    {
        None,
        TryAgain,
        NeedsAdministrator,
        ContactSupport,
    }

    /// <summary>
    /// What a customer should do after a setup run that did not complete, worked out ONLY from the engine's structured
    /// events (and whether this window declined to resume). It never reads the engine's own text, which the outcome view
    /// still shows word for word. It decides nothing about the run.
    ///
    /// It offers Install again only where the engine's own state says that is safe: nothing was sent yet, the code was
    /// certainly not used, or an enrollment saved on this computer with its stored token lets the next run resume. Every
    /// other stop -- an unknown enrollment result, a failure after enrolling, a refusal, an unknown stage or exit code --
    /// points to an administrator or to SortView support, never to a retry. Its diagnostic summary holds only fixed text and
    /// values checked against the engine's closed vocabularies: never an engine message, a path, a code or a token.
    /// </summary>
    public sealed class StopGuidance
    {
        internal const string CancelledAction =
            "Nothing more will happen until you start again. When you are ready, select Install to run setup again.";
        internal const string ResumeAction =
            "Setup left everything on this computer as it was. When you are ready, select Install and choose Resume setup to finish the earlier setup.";
        internal const string ElevationAction =
            "SortView Setup must run with administrator rights. Ask your IT staff to open SortView Setup with an administrator account.";
        internal const string PackageAction =
            "The installation package could not be used. Do not use this copy of SortView Setup: ask your IT staff to download SortView Setup again from SortView.";
        internal const string MachineAction =
            "This computer already has SortView Collector files, or an earlier setup that cannot be continued safely, and setup stopped without changing them. " +
            "Do not delete or move any SortView files. Select Copy details and send the copied text to SortView support.";
        internal const string SourcesAction =
            "Setup could not find the Tech Logic files it needs. Make sure the Tech Logic files are on this computer (ask your IT staff if you are not sure where they are), then select Install to try again.";
        internal const string ConnectionAction =
            "This computer could not connect to SortView. Ask your IT staff to check its internet connection, proxy and firewall settings for SortView, then select Install to try again.";
        internal const string SetupFolderAction =
            "Setup could not prepare its protected folder on this computer. Ask your IT staff to make sure administrators can create and write folders in the Windows ProgramData folder, then select Install to try again.";
        internal const string UpgradeRequiredAction =
            "This enrollment code needs a newer version of SortView Setup. Ask your SortView administrator for the current SortView Setup, then use it with the same enrollment code.";
        internal const string NotReadyAction =
            "SortView is not ready to enroll this computer yet. Contact your SortView administrator before trying again. " +
            "If an earlier setup on this computer ended without a clear result, contact SortView support instead.";
        internal const string RejectedAction =
            "SortView did not accept the enrollment code. Check the code with your SortView administrator, then select Install and enter it again. " +
            "If an earlier setup on this computer ended without a clear result, contact SortView support instead of trying again.";
        internal const string RateLimitedAction =
            "SortView is receiving too many requests from this computer. Wait a minute, then select Install and enter the same enrollment code again.";
        internal const string EnrollmentUnknownAction =
            "The result of the enrollment is not known: SortView may already have enrolled this computer. Do not select Install again until SortView support has looked at it, " +
            "and do not ask for a different enrollment code. Select Copy details and send the copied text to SortView support.";
        internal const string AfterEnrollmentAction =
            "This computer was enrolled with SortView, but setup could not finish saving the enrollment safely. Do not select Install again until SortView support tells you to, " +
            "and do not delete any SortView files. Select Copy details and send the copied text to SortView support.";
        internal const string ResumeInstallAction =
            "Setup stopped before the installation was finished. The cause is listed in the setup log under Show details. Select Install to try again: setup continues from " +
            "where it stopped, and the enrollment code is not needed again. If it stops at the same step again, select Copy details and send the copied text to SortView support.";
        internal const string InstallSupportAction =
            "Setup stopped before the installation was finished, and it cannot safely continue on its own. The cause is listed in the setup log under Show details. " +
            "Do not select Install again and do not delete any SortView files. Select Copy details and send the copied text to SortView support.";
        internal const string TaskEnableAction =
            "SortView Collector is installed, but its Scheduled Task could not be turned on, so the Collector will not run yet. Ask your IT staff to enable the " +
            "\"SortView Collector\" task in Windows Task Scheduler. Do not select Install again: it cannot fix this.";
        internal const string CouldNotStartAction =
            "SortView Setup could not start the installation. Close SortView Setup, open it again and try once more. If this happens again, select Copy details and send the copied text to SortView support.";
        internal const string SupportAction =
            "Do not select Install again and do not delete any SortView files. Select Copy details and send the copied text to SortView support.";

        // The engine's closed vocabularies (setup-collector.ps1 $script:SetupEventFieldRules): a value outside them is never shown.
        private static readonly string[] Modes = { "fresh", "resume_install", "resume_finish" };
        private static readonly string[] EnrollmentOutcomes =
            { "Enrolled", "Refused", "Rejected", "RateLimited", "Unreachable", "Redirected", "BadResponse", "ServerError" };
        private static readonly string[] CodeUses = { "used", "not_used", "not_used_by_this_request", "unknown" };
        private static readonly string[] PromptIds = { "enrollment_code", "sources_confirm", "source_path", "resume_confirm", "legacy_task_enable" };
        private static readonly Regex RefusalCodePattern = new Regex("^[a-z][a-z0-9_]{0,63}$");
        private static readonly Regex VersionPattern = new Regex("^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$");
        private static readonly Regex TypeNamePattern = new Regex("^[A-Za-z0-9_.`+]{1,256}$");
        private static readonly IReadOnlyList<ProgressStep> StepNames = new SetupProgress().Steps;

        private string stopType;
        private string stopStage;
        private IReadOnlyDictionary<string, object> stopFields;
        private IReadOnlyDictionary<string, object> enrollmentFields;
        private IReadOnlyDictionary<string, object> finishFields;
        private string mode;
        private string cancelledPrompt;
        private bool resumeDeclined;
        private bool anyEvent;
        private bool ended;
        private bool couldNotRun;

        public StopGuidance()
        {
            Reset();
        }

        /// <summary>None until the run has ended without completing.</summary>
        public StopCategory Category { get; private set; }

        /// <summary>The one thing to do next, in words, or null.</summary>
        public string Action { get; private set; }

        /// <summary>Whether Action tells the person to select Install again.</summary>
        public bool MayRetryInstall { get; private set; }

        /// <summary>Whether the cause is only in the setup log (Show details), so the details should be opened.</summary>
        public bool ShowDetails { get; private set; }

        /// <summary>The customer step (1-7) the run stopped in, or null.</summary>
        public int? StepNumber => stopStage != null && SetupProgress.StageSteps.TryGetValue(stopStage, out int step) ? step : (int?)null;

        /// <summary>"Step 4 of 7: Enroll this computer", as the step list says it, or null.</summary>
        public string StepText => StepNumber == null ? null
            : "Step " + StepNumber + " of " + SetupProgress.StepCount + ": " + StepNames[StepNumber.Value - 1].Title;

        public string CategoryLabel => Label(Category);

        public void Reset()
        {
            stopType = null;
            stopStage = null;
            stopFields = null;
            enrollmentFields = null;
            finishFields = null;
            mode = null;
            cancelledPrompt = null;
            resumeDeclined = false;
            anyEvent = false;
            ended = false;
            couldNotRun = false;
            Category = StopCategory.None;
            Action = null;
            MayRetryInstall = false;
            ShowDetails = false;
        }

        public void Apply(SetupEvent setupEvent)
        {
            if (setupEvent != null)
            {
                Apply(setupEvent.Type, setupEvent.Stage, setupEvent.Fields);
            }
        }

        /// <summary>Takes one engine event into account. Unknown event types change nothing.</summary>
        public void Apply(string type, string stage, IReadOnlyDictionary<string, object> fields)
        {
            anyEvent = true;
            fields = fields ?? new Dictionary<string, object>();
            switch (type)
            {
                case "mode_detected":
                    mode = Text(fields, "mode");
                    break;
                case "enrollment_result":
                    enrollmentFields = fields; // the last one: after the one legacy retry, that request's result
                    break;
                case "input_cancelled":
                    // Cancelling the last question (enable the task) is not cancelling setup: it completes, task disabled.
                    string prompt = Text(fields, "prompt_id");
                    if (prompt != "legacy_task_enable")
                    {
                        cancelledPrompt = prompt;
                    }
                    break;
                case "run_stopped":
                case "run_failed_unexpected":
                    stopType = type;
                    stopStage = stage;
                    stopFields = fields;
                    break;
                case "run_finished":
                    finishFields = fields;
                    break;
            }
        }

        /// <summary>This window answered "Do not resume" to the engine's resume question (and the engine took it).</summary>
        public void NoteResumeDeclined() => resumeDeclined = true;

        /// <summary>The run has returned. <paramref name="couldNotRunAtAll"/>: the engine could not even be loaded.</summary>
        public void Finish(bool couldNotRunAtAll)
        {
            ended = true;
            couldNotRun = couldNotRunAtAll;
            Decide();
        }

        private void Decide()
        {
            Category = StopCategory.None;
            Action = null;
            MayRetryInstall = false;
            ShowDetails = false;
            if (!ended)
            {
                return;
            }
            if (couldNotRun)
            {
                // No event at all: the engine never began, so opening setup again is safe. Otherwise it did begin.
                if (anyEvent)
                {
                    Set(StopCategory.ContactSupport, SupportAction);
                }
                else
                {
                    Set(StopCategory.TryAgain, CouldNotStartAction);
                }
                return;
            }
            if (stopType == "run_stopped")
            {
                Stopped();
                return;
            }
            if (stopType == null && Text(finishFields, "result") == "completed" && Int(finishFields, "exit_code") == 0)
            {
                return; // complete: nothing to do
            }
            // An unexpected error, or a run that returned without saying how it ended.
            Set(StopCategory.ContactSupport, SupportAction);
        }

        private void Stopped()
        {
            int? exit = Int(stopFields, "exit_code");
            bool consumed = Bool(stopFields, "code_consumed");
            // The engine's own state says the next run resumes with no code (Get-EnrollmentStateLines).
            bool resumable = consumed && Bool(stopFields, "token_stored") && Bool(stopFields, "recovery_on_disk");

            if (cancelledPrompt == "resume_confirm" || (resumeDeclined && stopStage == "machine"))
            {
                Set(StopCategory.TryAgain, ResumeAction, retry: true);
                return;
            }
            if (cancelledPrompt != null && PromptIds.Contains(cancelledPrompt))
            {
                Set(StopCategory.TryAgain, CancelledAction, retry: true);
                return;
            }
            switch (stopStage)
            {
                case "elevation":
                    Set(StopCategory.NeedsAdministrator, ElevationAction);
                    return;
                case "bundle_verification":
                case "bundle":
                    Set(StopCategory.NeedsAdministrator, PackageAction);
                    return;
                case "machine":
                    Set(StopCategory.ContactSupport, MachineAction);
                    return;
                case "sources":
                    // Before any enrollment request (fresh), or a resume that needs no code.
                    if (exit == 1 && (!consumed || resumable))
                    {
                        Set(StopCategory.TryAgain, SourcesAction, retry: true);
                        return;
                    }
                    break;
                case "connection":
                    if (exit == 1 && (!consumed || resumable))
                    {
                        Set(StopCategory.NeedsAdministrator, ConnectionAction, retry: true);
                        return;
                    }
                    break;
                case "setup_folder":
                    if (exit == 1 && !consumed && !Bool(stopFields, "code_attempted"))
                    {
                        Set(StopCategory.NeedsAdministrator, SetupFolderAction, retry: true);
                        return;
                    }
                    break;
                case "enrollment":
                    if (exit == 1 && !consumed)
                    {
                        Enrollment();
                        return;
                    }
                    break;
                case "enrollment_save":
                case "token_store":
                    if (consumed)
                    {
                        Set(StopCategory.ContactSupport, AfterEnrollmentAction);
                        return;
                    }
                    break;
                case "install":
                case "rules_install":
                    Set(resumable ? StopCategory.TryAgain : StopCategory.ContactSupport, resumable ? ResumeInstallAction : InstallSupportAction,
                        retry: resumable, details: true);
                    return;
                case "verification":
                    // Exit 1 is a step that did not pass; anything else (2: a refusal) is never retried.
                    bool retry = resumable && exit == 1;
                    Set(retry ? StopCategory.TryAgain : StopCategory.ContactSupport, retry ? ResumeInstallAction : InstallSupportAction,
                        retry: retry, details: true);
                    return;
                case "task_enable":
                    Set(StopCategory.NeedsAdministrator, TaskEnableAction);
                    return;
            }
            Set(StopCategory.ContactSupport, SupportAction);
        }

        private void Enrollment()
        {
            bool attempted = Bool(stopFields, "code_attempted");
            bool rejected = Bool(stopFields, "code_rejected");
            string outcome = Text(enrollmentFields, "enrollment_outcome");
            string refusal = Text(enrollmentFields, "refusal_code");
            switch (outcome)
            {
                case "Refused" when !attempted:
                    if (refusal == "collector_upgrade_required")
                    {
                        Set(StopCategory.NeedsAdministrator, UpgradeRequiredAction);
                    }
                    else if (refusal == "enrollment_not_ready")
                    {
                        Set(StopCategory.NeedsAdministrator, NotReadyAction);
                    }
                    else
                    {
                        Set(StopCategory.ContactSupport, SupportAction);
                    }
                    return;
                case "Rejected" when rejected && !attempted:
                    Set(StopCategory.TryAgain, RejectedAction, retry: true);
                    return;
                case "RateLimited" when !attempted:
                    Set(StopCategory.TryAgain, RateLimitedAction, retry: true);
                    return;
                case "Unreachable":
                case "ServerError":
                case "BadResponse":
                    Set(StopCategory.ContactSupport, EnrollmentUnknownAction);
                    return;
            }
            // Redirected, a missing result, or flags that do not agree with it.
            Set(StopCategory.ContactSupport, SupportAction);
        }

        private void Set(StopCategory category, string action, bool retry = false, bool details = false)
        {
            Category = category;
            Action = action;
            MayRetryInstall = retry;
            ShowDetails = details;
        }

        public static string Label(StopCategory category)
        {
            switch (category)
            {
                case StopCategory.TryAgain: return "You can try again";
                case StopCategory.NeedsAdministrator: return "Your IT staff or SortView administrator needs to help";
                case StopCategory.ContactSupport: return "Contact SortView support";
                default: return "";
            }
        }

        /// <summary>
        /// A summary safe to send to SortView support: this model's own fixed text, and engine values only where they are in
        /// the engine's closed vocabularies or of its checked kinds (numbers, yes/no, an installation number, a version, an
        /// error's type name). Never an engine message, a path, the setup log, a code, a token or a secret.
        /// </summary>
        public string DiagnosticSummary(DateTimeOffset at)
        {
            var text = new StringBuilder();
            text.AppendLine("SortView Setup: diagnostic summary");
            text.AppendLine("Time: " + at.ToString("yyyy-MM-dd HH:mm:ss zzz", CultureInfo.InvariantCulture));
            text.AppendLine("Result: " + (couldNotRun ? "could not run" : stopType == "run_stopped" ? "stopped" : Category == StopCategory.None ? "completed" : "failed"));
            Line(text, "Stopped at", StepText);
            Line(text, "What to do", Category == StopCategory.None ? null : CategoryLabel);
            Line(text, "Action", Action);
            Line(text, "Setup stage", StepNumber == null ? null : stopStage);
            Line(text, "Exit code", Int(stopFields, "exit_code")?.ToString(CultureInfo.InvariantCulture));
            Line(text, "Mode", OneOf(mode, Modes));
            int? contract = Int(stopFields, "contract") ?? Int(finishFields, "contract");
            Line(text, "Contract", contract == 1 || contract == 2 ? contract.Value.ToString(CultureInfo.InvariantCulture) : null);
            Line(text, "Cancelled question", OneOf(cancelledPrompt, PromptIds));
            Line(text, "Resume declined", resumeDeclined ? "yes" : null);
            string outcome = OneOf(Text(enrollmentFields, "enrollment_outcome"), EnrollmentOutcomes);
            string refusal = Text(enrollmentFields, "refusal_code");
            Line(text, "Enrollment result", outcome == null ? null
                : outcome + (refusal != null && RefusalCodePattern.IsMatch(refusal) ? " (" + refusal + ")" : ""));
            Line(text, "Enrollment code used", OneOf(Text(enrollmentFields, "code_used"), CodeUses));
            if (stopFields != null)
            {
                Line(text, "Enrollment code sent, result unknown", YesNo(stopFields, "code_attempted"));
                Line(text, "Enrollment completed", YesNo(stopFields, "code_consumed"));
                Line(text, "Enrollment code not accepted", YesNo(stopFields, "code_rejected"));
                Line(text, "API token stored", YesNo(stopFields, "token_stored"));
                Line(text, "Saved enrollment on this computer", YesNo(stopFields, "recovery_on_disk"));
            }
            int? installation = Int(finishFields, "installation_id");
            Line(text, "Installation", installation >= 1 ? installation.Value.ToString(CultureInfo.InvariantCulture) : null);
            string release = Text(finishFields, "release_version");
            Line(text, "Collector release", release != null && VersionPattern.IsMatch(release) ? release : null);
            string errorType = stopType == "run_failed_unexpected" ? Text(stopFields, "exception_type") : null;
            Line(text, "Unexpected error type", errorType != null && TypeNamePattern.IsMatch(errorType) ? errorType : null);
            text.Append("This summary holds no enrollment code, API token, secret, file path or setup log.");
            return text.ToString();
        }

        private static void Line(StringBuilder text, string name, string value)
        {
            if (!string.IsNullOrEmpty(value))
            {
                text.AppendLine(name + ": " + value);
            }
        }

        private static string OneOf(string value, string[] allowed) => value != null && allowed.Contains(value) ? value : null;

        private static string YesNo(IReadOnlyDictionary<string, object> fields, string name) =>
            fields.TryGetValue(name, out object value) && value is bool b ? (b ? "yes" : "no") : null;

        private static string Text(IReadOnlyDictionary<string, object> fields, string name) =>
            fields != null && fields.TryGetValue(name, out object value) ? value as string : null;

        private static bool Bool(IReadOnlyDictionary<string, object> fields, string name) =>
            fields != null && fields.TryGetValue(name, out object value) && value is bool b && b;

        private static int? Int(IReadOnlyDictionary<string, object> fields, string name)
        {
            if (fields == null || !fields.TryGetValue(name, out object value))
            {
                return null;
            }
            if (value is int i)
            {
                return i;
            }
            if (value is long l && l >= int.MinValue && l <= int.MaxValue)
            {
                return (int)l;
            }
            return null;
        }
    }
}
