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

        public void ShowPrompt(PromptRequest request)
        {
            current = request;
            IReadOnlyDictionary<string, object> context = request.Prompt.Context;
            PromptId.Text = request.Prompt.PromptId;
            PromptDetails.Text = "";
            PathBox.Text = "";
            CodeBox.Clear();
            ChoiceRow.Visibility = Visibility.Visible;
            PathRow.Visibility = Visibility.Collapsed;
            CodeRow.Visibility = Visibility.Collapsed;
            PromptCancel.Content = "Cancel";
            switch (request.Prompt.PromptId)
            {
                case "sources_confirm":
                    PromptTitle.Text = "Use the Tech Logic files found on this machine?";
                    PromptDetails.Text = "checkins: " + Context(context, "checkins") + "\nrejects: " + Context(context, "rejects") + "\nacs: " + Context(context, "acs");
                    PromptPrimary.Content = "Use these files";
                    PromptSecondary.Content = "Choose other files";
                    break;
                case "source_path":
                    PromptTitle.Text = "Select the " + Context(context, "source") + " file";
                    string problem = Context(context, "problem");
                    PromptDetails.Text = "Attempt " + Context(context, "attempt") + " of " + Context(context, "max_attempts") +
                        (problem == null ? "" : "\nThe previous path was refused (" + (problem == "not_found" ? "file not found" : "not an absolute path") + "): " + Context(context, "rejected_path"));
                    ChoiceRow.Visibility = Visibility.Collapsed;
                    PathRow.Visibility = Visibility.Visible;
                    UsePathButton.IsEnabled = false;
                    break;
                case "resume_confirm":
                    PromptTitle.Text = "Resume the unfinished setup?";
                    PromptDetails.Text = "Installation " + Context(context, "installation_id") + ", release " + Context(context, "release_version") +
                        ", " + Context(context, "mode") + ", contract " + Context(context, "contract") + ". No new enrollment code is needed.";
                    PromptPrimary.Content = "Resume";
                    PromptSecondary.Content = "Don't resume";
                    break;
                case "enrollment_code":
                    PromptTitle.Text = EnrollmentCodeTitle;
                    PromptDetails.Text = EnrollmentCodeDetails;
                    ChoiceRow.Visibility = Visibility.Collapsed;
                    CodeRow.Visibility = Visibility.Visible;
                    break;
                case "legacy_task_enable":
                    PromptTitle.Text = "Setup is complete. Enable the Scheduled Task now?";
                    PromptDetails.Text = "Enabling starts the recurring 15-minute runs. Leaving it disabled, or cancelling, keeps it disabled.";
                    PromptPrimary.Content = "Enable";
                    PromptSecondary.Content = "Leave disabled";
                    PromptCancel.Content = "Cancel (the task stays disabled)";
                    break;
                default:
                    PromptTitle.Text = "An unknown question: " + request.Prompt.PromptId;
                    ChoiceRow.Visibility = Visibility.Collapsed;
                    break;
            }
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

        private void PromptPrimary_Click(object sender, RoutedEventArgs e) => Answer(PromptAnswers.Yes);

        private void PromptSecondary_Click(object sender, RoutedEventArgs e) => Answer(PromptAnswers.No);

        private void PromptCancel_Click(object sender, RoutedEventArgs e)
        {
            current?.TryCancel();
            ClearPrompt();
        }

        private void PathBox_TextChanged(object sender, TextChangedEventArgs e) =>
            UsePathButton.IsEnabled = !string.IsNullOrWhiteSpace(PathBox.Text);

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
                Answer(dialog.FileName);
            }
        }

        private void EnrollButton_Click(object sender, RoutedEventArgs e)
        {
            if (current == null || CodeBox.SecurePassword.Length == 0)
            {
                return;
            }
            // A copy, read-only, handed straight to the run (which disposes it); the box is cleared at once.
            SecureString code = CodeBox.SecurePassword;
            code.MakeReadOnly();
            CodeBox.Clear();
            Answer(code);
            UpdateControls();
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
        }

        private static string Context(IReadOnlyDictionary<string, object> context, string key) =>
            context.TryGetValue(key, out object value) && value != null ? Convert.ToString(value) : null;
    }
}
