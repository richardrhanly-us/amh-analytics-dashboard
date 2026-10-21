using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Linq;
using SortView.Setup.Core;

namespace SortView.Setup.Wizard
{
    /// <summary>What a customer step shows.</summary>
    public enum ProgressStepState
    {
        Pending,
        Working,
        WaitingForAnswer,
        Done,
        DoneEarlier,
        NotNeeded,
        StoppedHere,
        NotReached,
        LeftDisabled,
    }

    /// <summary>How a run ended, as far as its events say.</summary>
    public enum SetupEnding
    {
        None,
        Completed,
        Cancelled,
        Interrupted,
        Stopped,
        UnexpectedError,
        CouldNotRun,
    }

    /// <summary>One of the seven customer steps.</summary>
    public sealed class ProgressStep : INotifyPropertyChanged
    {
        internal ProgressStep(int number, string title, string activity)
        {
            Number = number;
            Title = title;
            Activity = activity;
        }

        public int Number { get; }

        public string Title { get; }

        /// <summary>What setup is doing while this step is working.</summary>
        public string Activity { get; }

        public ProgressStepState State { get; private set; }

        public string StateLabel => Label(State);

        public string AccessibleName => "Step " + Number + " of " + SetupProgress.StepCount + ": " + Title + ", " + StateLabel;

        public event PropertyChangedEventHandler PropertyChanged;

        internal bool SetState(ProgressStepState state)
        {
            if (state == State)
            {
                return false;
            }
            State = state;
            foreach (string name in new[] { nameof(State), nameof(StateLabel), nameof(AccessibleName) })
            {
                PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
            }
            return true;
        }

        public static string Label(ProgressStepState state)
        {
            switch (state)
            {
                case ProgressStepState.Working: return "Working";
                case ProgressStepState.WaitingForAnswer: return "Waiting for your answer";
                case ProgressStepState.Done: return "Done";
                case ProgressStepState.DoneEarlier: return "Done earlier";
                case ProgressStepState.NotNeeded: return "Not needed";
                case ProgressStepState.StoppedHere: return "Stopped here";
                case ProgressStepState.NotReached: return "Not reached";
                case ProgressStepState.LeftDisabled: return "Left disabled";
                default: return "Pending";
            }
        }
    }

    /// <summary>
    /// The setup run's progress for a customer, worked out ONLY from the engine's structured events (and whether one of
    /// its questions is on screen). It groups the engine's fifteen stages into seven steps, and says a step is done only
    /// when the events of every stage in it say so. It decides nothing: what runs, resume, enrollment, cancellation and
    /// recovery are the engine's, and its stop text is shown elsewhere word for word. No percentages: the engine reports
    /// none.
    /// </summary>
    public sealed class SetupProgress
    {
        public const int StepCount = 7;

        /// <summary>Every engine stage (setup-collector.ps1 $script:SetupStageIds) and the step (1-7) it belongs to.
        /// token_store and enrollment_save run in either order (Contract v2 saves first), so they share step 4.</summary>
        public static readonly IReadOnlyDictionary<string, int> StageSteps = new Dictionary<string, int>
        {
            { "elevation", 1 }, { "bundle_verification", 1 }, { "bundle", 1 }, { "machine", 1 },
            { "sources", 2 },
            { "connection", 3 },
            { "setup_folder", 4 }, { "enrollment", 4 }, { "enrollment_save", 4 }, { "token_store", 4 },
            { "install", 5 }, { "rules_install", 5 },
            { "verification", 6 }, { "record_cleanup", 6 },
            { "task_enable", 7 },
        };

        private enum StageStatus
        {
            NotSeen,
            Started,
            Completed,
            Skipped,
        }

        private sealed class StageRecord
        {
            public StageStatus Status;
            public string Detail; // a completion's outcome or a skip's reason
        }

        private readonly Dictionary<string, StageRecord> stages = new Dictionary<string, StageRecord>();
        private string mode;
        private bool resumedWorkSeen;
        private bool waiting;
        private bool stopped;
        private bool failed;
        private bool couldNotRun;
        private bool recoveryOnDisk;
        private string cancelledPrompt;
        private string finishedResult;
        private string stopStage;

        public SetupProgress()
        {
            Steps = new[]
            {
                new ProgressStep(1, "Check the computer and installation package", "Checking the installation files and this computer…"),
                new ProgressStep(2, "Find Tech Logic files", "Confirming the Tech Logic files…"),
                new ProgressStep(3, "Connect to SortView", "Checking the connection to SortView…"),
                new ProgressStep(4, "Enroll this computer", "Enrolling this computer and saving its enrollment securely…"),
                new ProgressStep(5, "Install SortView Collector", "Installing SortView Collector…"),
                new ProgressStep(6, "Verify installation", "Verifying the installation…"),
                new ProgressStep(7, "Configure the Scheduled Task", "Setting up the Collector's Scheduled Task…"),
            };
        }

        public IReadOnlyList<ProgressStep> Steps { get; }

        public SetupEnding Ending
        {
            get
            {
                if (couldNotRun)
                {
                    return SetupEnding.CouldNotRun;
                }
                if (failed)
                {
                    return SetupEnding.UnexpectedError;
                }
                if (stopped)
                {
                    return cancelledPrompt != null ? SetupEnding.Cancelled : recoveryOnDisk ? SetupEnding.Interrupted : SetupEnding.Stopped;
                }
                switch (finishedResult)
                {
                    case null: return SetupEnding.None;
                    case "completed": return SetupEnding.Completed;
                    case "failed": return SetupEnding.UnexpectedError;
                    default: return SetupEnding.Stopped;
                }
            }
        }

        /// <summary>Set once the engine has said it found an unfinished earlier setup (mode_detected), and changed once
        /// the run actually skips work an earlier run did. Null otherwise.</summary>
        public string ResumeNotice
        {
            get
            {
                if (mode != "resume_install" && mode != "resume_finish")
                {
                    return null;
                }
                return resumedWorkSeen
                    ? "Resuming an earlier setup. No new enrollment code is needed."
                    : "An unfinished earlier setup was found on this computer.";
            }
        }

        /// <summary>The step being worked on, in words, or null when none is.</summary>
        public string CurrentActivity
        {
            get
            {
                ProgressStep step = Steps.FirstOrDefault(s => s.State == ProgressStepState.Working || s.State == ProgressStepState.WaitingForAnswer);
                if (step == null)
                {
                    return null;
                }
                return "Step " + step.Number + " of " + StepCount + ": " + step.Title + ". " +
                    (step.State == ProgressStepState.WaitingForAnswer ? "Waiting for your answer." : step.Activity);
            }
        }

        /// <summary>A new run: every step pending.</summary>
        public IReadOnlyList<ProgressStep> Reset()
        {
            stages.Clear();
            mode = null;
            resumedWorkSeen = false;
            waiting = false;
            stopped = false;
            failed = false;
            couldNotRun = false;
            recoveryOnDisk = false;
            cancelledPrompt = null;
            finishedResult = null;
            stopStage = null;
            return Update();
        }

        public IReadOnlyList<ProgressStep> Apply(SetupEvent setupEvent) =>
            setupEvent == null ? new ProgressStep[0] : Apply(setupEvent.Type, setupEvent.Stage, setupEvent.Fields);

        /// <summary>Takes one engine event into account. Returns the steps whose state changed. Unknown event types and
        /// stages change nothing.</summary>
        public IReadOnlyList<ProgressStep> Apply(string type, string stage, IReadOnlyDictionary<string, object> fields)
        {
            switch (type)
            {
                case "stage_started":
                    Mark(stage, StageStatus.Started, null);
                    break;
                case "stage_completed":
                    Mark(stage, StageStatus.Completed, Field(fields, "outcome"));
                    break;
                case "stage_skipped":
                    string reason = Field(fields, "reason");
                    Mark(stage, StageStatus.Skipped, reason);
                    if (reason == "resume" || reason == "already_installed")
                    {
                        resumedWorkSeen = true;
                    }
                    break;
                case "mode_detected":
                    mode = Field(fields, "mode");
                    break;
                case "input_cancelled":
                    // Cancelling the last question (enable the task) is not cancelling setup: it completes, task disabled.
                    string prompt = Field(fields, "prompt_id");
                    if (prompt != "legacy_task_enable")
                    {
                        cancelledPrompt = prompt;
                    }
                    break;
                case "run_stopped":
                    stopped = true;
                    stopStage = stage;
                    recoveryOnDisk = fields != null && fields.TryGetValue("recovery_on_disk", out object onDisk) && onDisk is bool b && b;
                    break;
                case "run_failed_unexpected":
                    failed = true;
                    stopStage = stage;
                    break;
                case "run_finished":
                    finishedResult = Field(fields, "result");
                    break;
                // enrollment_result: the engine's own text and the outcome view say what it means; no step changes.
            }
            return Update();
        }

        /// <summary>Whether one of the engine's questions is on screen (the working step then waits for an answer).</summary>
        public IReadOnlyList<ProgressStep> SetWaiting(bool isWaiting)
        {
            waiting = isWaiting;
            return Update();
        }

        /// <summary>The run has returned. <paramref name="couldNotRunAtAll"/>: the engine could not even be loaded.</summary>
        public IReadOnlyList<ProgressStep> Finish(bool couldNotRunAtAll)
        {
            waiting = false;
            couldNotRun = couldNotRunAtAll;
            if (finishedResult == null && !stopped && !failed && !couldNotRun)
            {
                failed = true; // returned with no final event: never shown as complete
            }
            return Update();
        }

        private void Mark(string stage, StageStatus status, string detail)
        {
            if (stage == null || !StageSteps.ContainsKey(stage))
            {
                return;
            }
            stages[stage] = new StageRecord { Status = status, Detail = detail };
        }

        private IReadOnlyList<ProgressStep> Update()
        {
            var changed = new List<ProgressStep>();
            foreach (ProgressStep step in Steps)
            {
                if (step.SetState(StateOf(step.Number)))
                {
                    changed.Add(step);
                }
            }
            return changed;
        }

        private ProgressStepState StateOf(int number)
        {
            SetupEnding ending = Ending;
            bool ended = ending != SetupEnding.None;
            bool endedEarly = ended && ending != SetupEnding.Completed;
            List<StageRecord> seen = StageSteps.Where(pair => pair.Value == number && stages.ContainsKey(pair.Key))
                .Select(pair => stages[pair.Key]).ToList();
            int total = StageSteps.Count(pair => pair.Value == number);

            if (endedEarly && stopStage != null && StageSteps.TryGetValue(stopStage, out int stopStep) && stopStep == number)
            {
                return ProgressStepState.StoppedHere;
            }
            if (seen.Any(r => r.Status == StageStatus.Started))
            {
                if (endedEarly)
                {
                    return ProgressStepState.StoppedHere; // the run ended while this step was in progress
                }
                return waiting && !ended ? ProgressStepState.WaitingForAnswer : ProgressStepState.Working;
            }
            if (seen.Count == 0)
            {
                return ended ? ProgressStepState.NotReached : ProgressStepState.Pending;
            }
            if (seen.Count < total)
            {
                // Some of its stages are finished, the rest not yet seen: still in progress, or cut short.
                return !ended ? ProgressStepState.Working : endedEarly ? ProgressStepState.StoppedHere : Final(seen);
            }
            return Final(seen);
        }

        private static ProgressStepState Final(List<StageRecord> records)
        {
            if (records.Any(r => r.Status == StageStatus.Completed && r.Detail == "left_disabled"))
            {
                return ProgressStepState.LeftDisabled;
            }
            if (records.Any(r => r.Status == StageStatus.Completed))
            {
                return ProgressStepState.Done;
            }
            if (records.All(r => r.Detail == "not_applicable"))
            {
                return ProgressStepState.NotNeeded;
            }
            return ProgressStepState.DoneEarlier;
        }

        private static string Field(IReadOnlyDictionary<string, object> fields, string name) =>
            fields != null && fields.TryGetValue(name, out object value) && value != null ? Convert.ToString(value) : null;
    }
}
