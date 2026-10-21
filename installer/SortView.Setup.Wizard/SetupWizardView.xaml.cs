using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.IO;
using System.Security;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Media;
using Microsoft.Win32;
using SortView.Setup.Core;
using SortView.Setup.Wpf;

namespace SortView.Setup.Wizard
{
    /// <summary>
    /// The setup wizard's screen, shared by the production program and the simulation host. It shows the engine's
    /// questions (it is the <see cref="IPromptView"/>), its structured events and its console text, and runs setup through
    /// <see cref="SetupRunSession"/> -- off the UI thread, cancellable only at its questions, never interrupted once an
    /// enrollment code has been handed over.
    ///
    /// It never decides WHAT runs: a host calls <see cref="BeginRun"/> with a runner. Installation is DISABLED unless the
    /// host turns it on (<see cref="IsInstallationEnabled"/>); while disabled, Start does nothing and BeginRun refuses.
    /// The host window's closing is handled here: while setup runs it is deferred until the engine has returned.
    /// Nothing typed here is ever shown, logged or written; the enrollment code goes from the PasswordBox to the engine as
    /// a SecureString owned and disposed by the run.
    /// </summary>
    public partial class SetupWizardView : UserControl, IPromptView
    {
        private readonly ObservableCollection<string> events = new ObservableCollection<string>();
        private readonly ObservableCollection<string> consoleText = new ObservableCollection<string>();
        private bool installationEnabled;
        private string disabledNotice = "Installation is not enabled.";
        private SetupRunSession session;
        private PromptRequest current;
        private bool closeWhenFinished;
        private Window hostWindow;
        // The run's final events (non-secret), for the outcome view.
        private SetupEvent stopEvent;
        private SetupEvent finishEvent;

        public SetupWizardView()
        {
            InitializeComponent();
            EventList.ItemsSource = events;
            DiagnosticList.ItemsSource = consoleText;
            Loaded += OnLoaded;
            UpdateControls();
        }

        // ---- what the host sets -----------------------------------------------------------------------------------

        /// <summary>Whether this wizard may run setup at all. False (the default): Start does nothing and
        /// <see cref="BeginRun"/> refuses, and <see cref="DisabledNotice"/> is shown.</summary>
        public bool IsInstallationEnabled
        {
            get => installationEnabled;
            set
            {
                installationEnabled = value;
                UpdateControls();
            }
        }

        /// <summary>Shown while installation is disabled.</summary>
        public string DisabledNotice
        {
            get => disabledNotice;
            set
            {
                disabledNotice = value ?? "";
                UpdateControls();
            }
        }

        public object StartButtonText
        {
            get => StartButton.Content;
            set => StartButton.Content = value;
        }

        public string EnrollmentCodeTitle { get; set; } = "Enter the one-time enrollment code from your SortView administrator";

        public string EnrollmentCodeDetails { get; set; } = "The code is used once and is never shown or stored.";

        /// <summary>The code box's accessible name.</summary>
        public string EnrollmentCodeLabel
        {
            get => AutomationProperties.GetName(CodeBox);
            set => AutomationProperties.SetName(CodeBox, value);
        }

        /// <summary>Where the file picker starts when a source file is asked for.</summary>
        public string SourceBrowseFolder { get; set; } = @"C:\TLCFinalDlls";

        // ---- what the host sees -----------------------------------------------------------------------------------

        public bool IsRunning => session != null && session.IsRunning;

        public bool EnrollmentMayHaveBegun => IsRunning && session.EnrollmentMayHaveBegun;

        /// <summary>Start was pressed while installation is enabled and nothing is running: the host prepares and calls
        /// <see cref="BeginRun"/>.</summary>
        public event EventHandler StartRequested;

        /// <summary>A run started or finished (or what it allows changed): hosts update their own controls.</summary>
        public event EventHandler RunStateChanged;

        /// <summary>Every structured event, on the UI thread, after it is shown.</summary>
        public event Action<SetupEvent> SetupEventReceived;

        public void ShowStatus(string text) => StatusText.Text = text;

        public void ClearOutput()
        {
            events.Clear();
            consoleText.Clear();
        }

        /// <summary>Runs <paramref name="runner"/> (off the UI thread). Refused while installation is disabled or a run is
        /// already going.</summary>
        public void BeginRun(SetupRunner runner, string runningMessage)
        {
            if (!installationEnabled)
            {
                throw new InvalidOperationException("Installation is not enabled in this build.");
            }
            if (runner == null)
            {
                throw new ArgumentNullException(nameof(runner));
            }
            if (IsRunning)
            {
                throw new InvalidOperationException("Setup is already running.");
            }
            ClearOutput();
            ClearPrompt();
            ClearOutcome();
            stopEvent = null;
            finishEvent = null;
            closeWhenFinished = false;
            session = SetupRunSession.Start(runner, Dispatcher, this, OnSetupEvent, text => consoleText.Add(text));
            SetupRunSession started = session;
            session.Completion.ContinueWith(task => Dispatcher.BeginInvoke(new Action(() => OnFinished(started, task))),
                TaskScheduler.Default);
            StatusText.Text = string.IsNullOrEmpty(runningMessage) ? "Running setup…" : runningMessage;
            UpdateControls();
        }

        // ---- running -------------------------------------------------------------------------------------------

        private void OnLoaded(object sender, RoutedEventArgs e)
        {
            Window window = Window.GetWindow(this);
            if (window != null && !ReferenceEquals(window, hostWindow))
            {
                if (hostWindow != null)
                {
                    hostWindow.Closing -= OnHostClosing;
                }
                hostWindow = window;
                hostWindow.Closing += OnHostClosing;
            }
        }

        private void StartButton_Click(object sender, RoutedEventArgs e)
        {
            if (!installationEnabled)
            {
                StatusText.Text = disabledNotice;
                return;
            }
            if (!IsRunning)
            {
                StartRequested?.Invoke(this, EventArgs.Empty);
            }
        }

        private void CancelButton_Click(object sender, RoutedEventArgs e)
        {
            if (!IsRunning)
            {
                return;
            }
            switch (session.RequestCancel())
            {
                case CancelRequestOutcome.CancelledAtQuestion:
                    StatusText.Text = "Cancelled: setup stops safely at this question.";
                    break;
                case CancelRequestOutcome.WillStopAtNextQuestion:
                    StatusText.Text = "Cancelling: the step in progress finishes, then setup stops at its next question.";
                    break;
                case CancelRequestOutcome.FinishingAfterEnrollment:
                    StatusText.Text = "Setup cannot be cancelled now: the enrollment may already have been sent. It is finishing on its own.";
                    break;
            }
            UpdateControls();
        }

        private void OnHostClosing(object sender, CancelEventArgs e)
        {
            if (!IsRunning)
            {
                return;
            }
            // Never interrupt setup: keep the window open until the engine has returned, then close.
            e.Cancel = true;
            closeWhenFinished = true;
            bool enrollmentBegun = session.EnrollmentMayHaveBegun;
            session.DeferClose();
            StatusText.Text = enrollmentBegun
                ? "This window will close when setup has finished. Setup cannot be interrupted now: the enrollment may already have been sent."
                : "This window will close as soon as setup stops at its next question.";
            UpdateControls();
        }

        private void OnFinished(SetupRunSession finished, Task<SetupRunResult> task)
        {
            if (!ReferenceEquals(finished, session))
            {
                return;
            }
            ClearPrompt();
            if (task.IsFaulted)
            {
                Exception error = task.Exception.GetBaseException();
                StatusText.Text = "Setup could not run: " + error.GetType().Name + ": " + error.Message;
            }
            else
            {
                SetupRunResult result = task.Result;
                StatusText.Text = "Finished: exit code " + (result.ExitCode?.ToString() ?? "(none)") + " — " + Describe(result.ExitCode) + ".";
            }
            ShowOutcome(task);
            UpdateControls();
            if (closeWhenFinished)
            {
                hostWindow?.Close();
            }
        }

        private void UpdateControls()
        {
            bool running = IsRunning;
            StartButton.IsEnabled = installationEnabled && !running;
            InstallationNotice.Text = disabledNotice;
            InstallationNotice.Visibility = installationEnabled ? Visibility.Collapsed : Visibility.Visible;
            bool enrollmentBegun = running && session.EnrollmentMayHaveBegun;
            CancelButton.IsEnabled = running && !enrollmentBegun;
            CancelNote.Text = enrollmentBegun
                ? "Setup can no longer be cancelled safely: the enrollment may already have been sent. It will run to the end on its own; closing the window waits for it."
                : running ? "Cancelling stops setup safely at its next question; a step already running always finishes." : "";
            RunStateChanged?.Invoke(this, EventArgs.Empty);
        }

        // ---- events and console text -------------------------------------------------------------------------

        private void OnSetupEvent(SetupEvent setupEvent)
        {
            switch (setupEvent.Type)
            {
                case "run_stopped":
                case "run_failed_unexpected":
                    stopEvent = setupEvent;
                    break;
                case "run_finished":
                    finishEvent = setupEvent;
                    break;
            }
            foreach (string line in Format(setupEvent))
            {
                events.Add(line);
            }
            SetupEventReceived?.Invoke(setupEvent);
            EventList.ScrollIntoView(events[events.Count - 1]);
            UpdateControls();
        }

        private static IEnumerable<string> Format(SetupEvent e)
        {
            string head = "#" + e.Sequence + "  ";
            switch (e.Type)
            {
                case "stage_started":
                    yield return head + "▶ " + e.Stage;
                    break;
                case "stage_completed":
                    yield return head + "✔ " + e.Stage + " (" + Field(e, "outcome") + ")";
                    break;
                case "stage_skipped":
                    yield return head + "↷ " + e.Stage + " skipped (" + Field(e, "reason") + ")";
                    break;
                case "mode_detected":
                    yield return head + "Mode: " + Field(e, "mode") + (Field(e, "contract") == null ? "" : ", contract " + Field(e, "contract"));
                    break;
                case "enrollment_result":
                    yield return head + "Enrollment (contract " + Field(e, "requested_contract") + " requested): " + Field(e, "enrollment_outcome") +
                        (Field(e, "refusal_code") == null ? "" : " [" + Field(e, "refusal_code") + "]") + "; code " + Field(e, "code_used");
                    break;
                case "input_cancelled":
                    yield return head + "✖ Question cancelled: " + Field(e, "prompt_id");
                    break;
                case "run_stopped":
                    yield return head + "■ SETUP STOPPED at " + Field(e, "step_label") + " (exit " + Field(e, "exit_code") + ")";
                    yield return "      What went wrong: " + Field(e, "problem");
                    yield return "      Left unchanged: " + Field(e, "unchanged");
                    foreach (string line in Lines(e, "advice"))
                    {
                        yield return "      " + line;
                    }
                    yield return "      What to do: " + Field(e, "fix");
                    break;
                case "run_failed_unexpected":
                    yield return head + "■ UNEXPECTED ERROR: " + Field(e, "exception_type") + ": " + Field(e, "message");
                    foreach (string line in Lines(e, "advice"))
                    {
                        yield return "      " + line;
                    }
                    break;
                case "run_finished":
                    yield return head + "■ FINISHED: " + Field(e, "result") + ", exit " + Field(e, "exit_code") +
                        (Field(e, "mode") == null ? "" : ", " + Field(e, "mode")) + (Field(e, "contract") == null ? "" : ", contract " + Field(e, "contract")) +
                        (Field(e, "task_state") == null ? "" : ", task " + Field(e, "task_state"));
                    break;
                default:
                    yield return head + e.Type + " " + e.Stage;
                    break;
            }
        }

        private static string Field(SetupEvent e, string name) =>
            e.Fields.TryGetValue(name, out object value) && value != null ? Convert.ToString(value) : null;

        private static IEnumerable<string> Lines(SetupEvent e, string name) =>
            e.Fields.TryGetValue(name, out object value) && value is string[] lines ? lines : new string[0];

        private static string Describe(int? exitCode)
        {
            switch (exitCode)
            {
                case 0: return "setup completed";
                case 1: return "a step failed or setup was cancelled";
                case 2: return "refused: the machine's state is not safe to change";
                default: return "a step failed (its own exit code)";
            }
        }

        // ---- prompts (IPromptView: called on this control's thread) ---------------------------------------------
        // Each question has its own view. The views only collect an answer: the engine decides when it asks, whether a
        // path is acceptable (it says why when it asks again), what an answer means and what happens next.

        public void ShowPrompt(PromptRequest request)
        {
            current = request;
            IReadOnlyDictionary<string, object> context = request.Prompt.Context;
            PromptId.Text = request.Prompt.PromptId;
            PathBox.Text = "";
            CodeBox.Clear();
            ShowView(null);
            PromptCancel.Content = "_Cancel setup";
            switch (request.Prompt.PromptId)
            {
                case "sources_confirm":
                    PromptTitle.Text = "Use these Tech Logic files?";
                    StandardCheckinsPath.Text = Context(context, "checkins");
                    StandardRejectsPath.Text = Context(context, "rejects");
                    StandardAcsPath.Text = Context(context, "acs");
                    SourcesNetworkWarning.Text = NetworkWarning(Context(context, "checkins"), Context(context, "rejects"), Context(context, "acs"));
                    ShowView(SourcesConfirmView);
                    break;
                case "source_path":
                    string source = SourceLabel(Context(context, "source"));
                    string problem = Context(context, "problem");
                    string rejected = Context(context, "rejected_path");
                    PromptTitle.Text = "Choose the Tech Logic " + source + " file";
                    SourceDescription.Text = "Select the " + source + " file the Collector should read. It can have any name and be in any folder.";
                    SourceAttempt.Text = "Attempt " + Context(context, "attempt") + " of " + Context(context, "max_attempts") +
                        (Context(context, "attempt") != null && Context(context, "attempt") == Context(context, "max_attempts")
                            ? ". If this file is not accepted either, setup stops."
                            : ".");
                    SourceProblem.Text = problem == "not_found" ? "Setup could not find a file at: " + rejected
                        : problem == "not_absolute" ? "That is not a full path (like C:\\folder\\file.txt): " + rejected
                        : problem != null ? "The previous path was not accepted (" + problem + "): " + rejected
                        : "";
                    PathBox.Text = rejected ?? ""; // to correct; nothing is sent until "Use this file"
                    ShowView(SourcePathView);
                    break;
                case "resume_confirm":
                    PromptTitle.Text = "Resume the unfinished SortView setup?";
                    ResumeExplanation.Text = (Context(context, "mode") == "resume_finish"
                            ? "An earlier setup on this computer installed the Collector but did not finish. Setup can complete the remaining steps"
                            : "An earlier setup on this computer enrolled but did not finish installing. Setup can finish installing")
                        + " with the saved enrollment. No new enrollment code is needed. If you do not resume, setup stops and leaves everything as it is.";
                    ResumeDetails.Text = "Installation " + Context(context, "installation_id") + " · release " + Context(context, "release_version") +
                        (Context(context, "contract") == null ? "" : " · Contract v" + Context(context, "contract"));
                    ShowView(ResumeView);
                    break;
                case "enrollment_code":
                    PromptTitle.Text = EnrollmentCodeTitle;
                    EnrollmentExplanation.Text = EnrollmentCodeDetails;
                    PromptCancel.Content = "_Cancel setup (the code is not sent)";
                    ShowView(EnrollmentView);
                    break;
                case "legacy_task_enable":
                    PromptTitle.Text = "Setup is complete. Enable the Scheduled Task now?";
                    PromptCancel.Content = "_Cancel (the task stays disabled)";
                    ShowView(TaskEnableView);
                    break;
                default:
                    PromptTitle.Text = "An unknown question: " + request.Prompt.PromptId;
                    break;
            }
            UpdatePromptButtons();
            PromptPanel.Visibility = Visibility.Visible;
            StatusText.Text = "Waiting for your answer.";
        }

        public void DismissPrompt(PromptRequest request)
        {
            if (ReferenceEquals(request, current))
            {
                ClearPrompt();
            }
        }

        private void ShowView(FrameworkElement view)
        {
            foreach (FrameworkElement each in new FrameworkElement[] { SourcesConfirmView, SourcePathView, ResumeView, EnrollmentView, TaskEnableView })
            {
                each.Visibility = ReferenceEquals(each, view) ? Visibility.Visible : Visibility.Collapsed;
            }
        }

        private void UseStandardFilesButton_Click(object sender, RoutedEventArgs e) => Answer(PromptAnswers.Yes);

        private void ChooseOtherFilesButton_Click(object sender, RoutedEventArgs e) => Answer(PromptAnswers.No);

        private void ResumeButton_Click(object sender, RoutedEventArgs e) => Answer(PromptAnswers.Yes);

        private void DontResumeButton_Click(object sender, RoutedEventArgs e) => Answer(PromptAnswers.No);

        private void EnableTaskButton_Click(object sender, RoutedEventArgs e) => Answer(PromptAnswers.Yes);

        private void LeaveTaskDisabledButton_Click(object sender, RoutedEventArgs e) => Answer(PromptAnswers.No);

        private void PromptCancel_Click(object sender, RoutedEventArgs e)
        {
            current?.TryCancel();
            ClearPrompt();
        }

        private void PathBox_TextChanged(object sender, TextChangedEventArgs e)
        {
            PathNetworkWarning.Text = NetworkWarning(PathBox.Text);
            UpdatePromptButtons();
        }

        private void UsePathButton_Click(object sender, RoutedEventArgs e)
        {
            if (!string.IsNullOrWhiteSpace(PathBox.Text))
            {
                Answer(PathBox.Text.Trim()); // setup itself decides whether the path is acceptable
            }
        }

        private void BrowseButton_Click(object sender, RoutedEventArgs e)
        {
            var dialog = new OpenFileDialog
            {
                Title = PromptTitle.Text,
                Filter = "Text files (*.txt)|*.txt|All files (*.*)|*.*",
                CheckFileExists = true,
                InitialDirectory = Directory.Exists(SourceBrowseFolder) ? SourceBrowseFolder : null,
            };
            if (dialog.ShowDialog(Window.GetWindow(this)) == true && current != null && !current.IsCompleted)
            {
                PathBox.Text = dialog.FileName; // shown for confirmation: "Use this file" sends it
            }
        }

        private void CodeBox_PasswordChanged(object sender, RoutedEventArgs e) => UpdatePromptButtons();

        private void EnrollButton_Click(object sender, RoutedEventArgs e)
        {
            if (current == null || current.Prompt.PromptId != "enrollment_code")
            {
                return;
            }
            // A copy, read-only, handed straight to the run (which disposes it); the box is cleared at once, and the
            // button stays disabled until another code question is asked (a run asks for the code at most once).
            SecureString code = CodeBox.SecurePassword;
            if (code.Length == 0)
            {
                code.Dispose();
                return;
            }
            code.MakeReadOnly();
            CodeBox.Clear();
            EnrollButton.IsEnabled = false;
            Answer(code);
            StatusText.Text = "Enrolling this computer with SortView… This cannot be interrupted.";
        }

        private void UpdatePromptButtons()
        {
            UsePathButton.IsEnabled = current != null && !string.IsNullOrWhiteSpace(PathBox.Text);
            using (SecureString typed = CodeBox.SecurePassword)
            {
                EnrollButton.IsEnabled = current != null && current.Prompt.PromptId == "enrollment_code" && typed.Length > 0;
            }
        }

        private void Answer(object answer)
        {
            PromptRequest request = current;
            ClearPrompt();
            if (request == null || !request.TrySubmit(answer))
            {
                StatusText.Text = "That question had already been settled; the answer was not used.";
                return;
            }
            StatusText.Text = "Running setup…";
            UpdateControls();
        }

        private void ClearPrompt()
        {
            current = null;
            CodeBox.Clear();
            PathBox.Text = "";
            PromptId.Text = "none";
            PromptPanel.Visibility = Visibility.Collapsed;
            UpdatePromptButtons();
        }

        private static string SourceLabel(string source)
        {
            switch (source)
            {
                case "checkins": return "Checkins";
                case "rejects": return "Rejects";
                case "acs": return "ACS";
                default: return source ?? "source";
            }
        }

        /// <summary>A warning, never a refusal, when a path is a network share or a mapped drive: the Collector runs as
        /// SYSTEM, which usually cannot see a user's mapped drives. Setup's own checks decide.</summary>
        private static string NetworkWarning(params string[] paths)
        {
            foreach (string path in paths)
            {
                if (IsNetworkLocation(path))
                {
                    return "Note: " + path.Trim() + " is on a network location. The Collector runs as the Windows SYSTEM account, " +
                        "which often cannot reach mapped drives or network shares. Setup checks this before it finishes; " +
                        "files on this computer's own drive are more reliable.";
                }
            }
            return "";
        }

        private static bool IsNetworkLocation(string path)
        {
            if (string.IsNullOrWhiteSpace(path))
            {
                return false;
            }
            path = path.Trim();
            if (path.StartsWith(@"\\", StringComparison.Ordinal) || path.StartsWith("//", StringComparison.Ordinal))
            {
                return true;
            }
            try
            {
                return path.Length >= 2 && path[1] == ':' && new DriveInfo(path.Substring(0, 1)).DriveType == DriveType.Network;
            }
            catch (Exception)
            {
                return false;
            }
        }

        private static string Context(IReadOnlyDictionary<string, object> context, string key) =>
            context.TryGetValue(key, out object value) && value != null ? Convert.ToString(value) : null;

        // ---- outcome ------------------------------------------------------------------------------------------
        // How the run ended, from its final events. A stop is shown in the engine's exact words (what went wrong, what
        // was left unchanged, its advice about the code and the saved enrollment, and what to do). No retry is offered.

        private void ShowOutcome(Task<SetupRunResult> task)
        {
            ClearOutcome();
            bool completed = !task.IsFaulted && finishEvent != null && Field(finishEvent, "result") == "completed" && task.Result.ExitCode == 0;
            if (task.IsFaulted)
            {
                OutcomeTitle.Text = "Setup could not run";
                OutcomeSummary.Text = task.Exception.GetBaseException().Message;
            }
            else if (completed)
            {
                OutcomeTitle.Text = "SortView setup is complete";
                string id = Field(finishEvent, "installation_id");
                OutcomeSummary.Text = (Field(finishEvent, "task_state") == "Enabled"
                        ? "The Collector is installed and its Scheduled Task is enabled: it runs every 15 minutes."
                        : "The Collector is installed. Its Scheduled Task was left disabled, so nothing is uploaded until it is enabled (the setup log says how).")
                    + (id == null ? "" : " Installation " + id + (Field(finishEvent, "release_version") == null ? "" : ", release " + Field(finishEvent, "release_version")) + ".");
            }
            else if (stopEvent != null && stopEvent.Type == "run_stopped")
            {
                OutcomeTitle.Text = "Setup stopped before it finished";
                OutcomeSummary.Text = "Setup stopped at " + Field(stopEvent, "step_label") + ".";
                OutcomeProblem.Text = "What went wrong: " + Field(stopEvent, "problem");
                OutcomeUnchanged.Text = "Left unchanged: " + Field(stopEvent, "unchanged");
                OutcomeAdvice.Text = string.Join("\n", Lines(stopEvent, "advice"));
                OutcomeFix.Text = "What to do: " + Field(stopEvent, "fix");
            }
            else if (stopEvent != null)
            {
                OutcomeTitle.Text = "Setup stopped because of an unexpected error";
                OutcomeProblem.Text = "What went wrong: " + Field(stopEvent, "exception_type") + ": " + Field(stopEvent, "message");
                OutcomeAdvice.Text = string.Join("\n", Lines(stopEvent, "advice"));
            }
            else
            {
                OutcomeTitle.Text = "Setup did not finish";
                OutcomeSummary.Text = "Exit code " + (task.Result.ExitCode?.ToString() ?? "(none)") + ": " + Describe(task.Result.ExitCode) + ". See the details below.";
            }
            OutcomePanel.BorderBrush = completed ? Brushes.SeaGreen : Brushes.Firebrick;
            foreach (TextBlock text in new[] { OutcomeSummary, OutcomeProblem, OutcomeUnchanged, OutcomeAdvice, OutcomeFix })
            {
                text.Visibility = string.IsNullOrEmpty(text.Text) ? Visibility.Collapsed : Visibility.Visible;
            }
            OutcomePanel.Visibility = Visibility.Visible;
        }

        private void ClearOutcome()
        {
            foreach (TextBlock text in new[] { OutcomeTitle, OutcomeSummary, OutcomeProblem, OutcomeUnchanged, OutcomeAdvice, OutcomeFix })
            {
                text.Text = "";
            }
            OutcomePanel.Visibility = Visibility.Collapsed;
        }
    }
}
