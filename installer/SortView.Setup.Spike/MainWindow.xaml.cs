using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Security;
using System.Threading.Tasks;
using System.Windows;
using Microsoft.Win32;
using SortView.Setup.Core;
using SortView.Setup.Core.Testing;
using SortView.Setup.Spike.Simulation;
using SortView.Setup.Wpf;

namespace SortView.Setup.Spike
{
    /// <summary>
    /// Phase 3C developer test window -- SIMULATION ONLY. Runs the real setup engine (Invoke-SetupMain) through
    /// SetupRunSession and the Core simulation harness, against a generated simulated machine (SimulationWorkspace);
    /// there is no way to run anything else. Answers the engine's questions itself (IPromptView). Nothing a person types
    /// here is ever shown, logged or written: the enrollment code goes straight from the PasswordBox to the engine as a
    /// SecureString, owned and disposed by the run.
    /// </summary>
    public partial class MainWindow : Window, IPromptView
    {
        private readonly ObservableCollection<string> events = new ObservableCollection<string>();
        private readonly ObservableCollection<string> consoleText = new ObservableCollection<string>();
        private string repositoryRoot;
        private SimulationWorkspace workspace;
        private SetupRunSession session;
        private PromptRequest current;
        private bool closeWhenFinished;

        public MainWindow()
        {
            InitializeComponent();
            EventList.ItemsSource = events;
            DiagnosticList.ItemsSource = consoleText;
            try
            {
                repositoryRoot = SimulationWorkspace.FindRepositoryRoot();
                NewWorkspace();
            }
            catch (Exception ex)
            {
                StatusText.Text = "Cannot simulate: " + ex.Message;
                StartButton.IsEnabled = false;
                NewMachineButton.IsEnabled = false;
            }
        }

        private bool IsRunning => session != null && session.IsRunning;

        // ---- running -------------------------------------------------------------------------------------------

        private void StartButton_Click(object sender, RoutedEventArgs e)
        {
            if (IsRunning)
            {
                return;
            }
            events.Clear();
            consoleText.Clear();
            ClearPrompt();
            closeWhenFinished = false;
            SimulatedContract contract = ScenarioV2.IsChecked == true ? SimulatedContract.ContractV2 : SimulatedContract.Legacy;
            SimulatedFault fault = FaultInstall.IsChecked == true ? SimulatedFault.DuringInstall
                : FaultVerification.IsChecked == true ? SimulatedFault.DuringVerification : SimulatedFault.None;
            try
            {
                workspace.PrepareRun(contract, fault, SlowSteps.IsChecked == true, StandardFilesPresent.IsChecked == true);
            }
            catch (Exception ex)
            {
                StatusText.Text = "Cannot prepare the simulated machine: " + ex.Message;
                return;
            }
            if (fault != SimulatedFault.None)
            {
                // The failure is set up for THIS run only ("make the next run fail"): the selection goes back to
                // "Do not fail" so it always shows what the next Start will do, and a resume needs no extra click.
                FaultNone.IsChecked = true;
            }

            SimulationRequest request = workspace.Request;
            session = SetupRunSession.Start(
                (presenter, onEvent, onConsoleText, token) => SimulatedSetupHarness.Run(request, presenter, onEvent, onConsoleText, token),
                Dispatcher, this, OnSetupEvent, text => consoleText.Add(text));
            SetupRunSession started = session;
            session.Completion.ContinueWith(task => Dispatcher.BeginInvoke(new Action(() => OnFinished(started, task))),
                TaskScheduler.Default);
            StatusText.Text = fault == SimulatedFault.None
                ? "Running the simulated setup…"
                : "Running the simulated setup… (it will fail " + (fault == SimulatedFault.DuringInstall ? "during install" : "during verification") +
                  "; the failure applies to this run only)";
            UpdateControls();
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

        private void Window_Closing(object sender, CancelEventArgs e)
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
                StatusText.Text = "The simulation did not run: " + error.GetType().Name + ": " + error.Message;
            }
            else
            {
                SetupRunResult result = task.Result;
                StatusText.Text = "Finished: exit code " + (result.ExitCode?.ToString() ?? "(none)") + " — " + Describe(result.ExitCode) + ".";
            }
            UpdateControls();
            if (closeWhenFinished)
            {
                Close();
            }
        }

        private void NewMachineButton_Click(object sender, RoutedEventArgs e)
        {
            if (IsRunning)
            {
                return;
            }
            try
            {
                workspace?.Delete();
            }
            catch (Exception)
            {
                // A file still in use: the old scratch folder is simply left behind in %TEMP%.
            }
            NewWorkspace();
            events.Clear();
            consoleText.Clear();
            StatusText.Text = "A new simulated machine is ready.";
        }

        private void NewWorkspace()
        {
            workspace = SimulationWorkspace.Create(repositoryRoot);
            WorkspacePath.Text = "Simulated machine: " + workspace.Root;
            UpdateControls();
        }

        private void UpdateControls()
        {
            bool running = IsRunning;
            StartButton.IsEnabled = !running && workspace != null;
            NewMachineButton.IsEnabled = !running && workspace != null;
            foreach (UIElement option in new UIElement[] { ScenarioLegacy, ScenarioV2, FaultNone, FaultInstall, FaultVerification, StandardFilesPresent, SlowSteps })
            {
                option.IsEnabled = !running;
            }
            bool enrollmentBegun = running && session.EnrollmentMayHaveBegun;
            CancelButton.IsEnabled = running && !enrollmentBegun;
            CancelNote.Text = enrollmentBegun
                ? "Setup can no longer be cancelled safely: the enrollment may already have been sent. It will run to the end on its own; closing the window waits for it."
                : running ? "Cancelling stops setup safely at its next question; a step already running always finishes." : "";
        }

        // ---- events and console text -------------------------------------------------------------------------

        private void OnSetupEvent(SetupEvent setupEvent)
        {
            foreach (string line in Format(setupEvent))
            {
                events.Add(line);
            }
            if (setupEvent.Type == "enrollment_result" && Field(setupEvent, "enrollment_outcome") == "Enrolled")
            {
                workspace.MarkCodeUsed(); // the simulated server will refuse this code from now on, as the real one does
            }
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

        // ---- prompts (IPromptView: called on this window's thread) ----------------------------------------------

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
                    PromptTitle.Text = "Enter a DUMMY enrollment code (simulation only — never a real one)";
                    PromptDetails.Text = "Any text works: the simulated server accepts it and nothing is sent anywhere. The code is never shown or stored.";
                    ChoiceRow.Visibility = Visibility.Collapsed;
                    CodeRow.Visibility = Visibility.Visible;
                    break;
                case "legacy_task_enable":
                    PromptTitle.Text = "Setup is complete. Enable the Scheduled Task now?";
                    PromptDetails.Text = "Enabling starts the recurring runs (simulated here). Leaving it disabled, or cancelling, keeps it disabled.";
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

        private void PathBox_TextChanged(object sender, System.Windows.Controls.TextChangedEventArgs e) =>
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
                InitialDirectory = workspace?.SourceDir,
            };
            if (dialog.ShowDialog(this) == true && current != null && !current.IsCompleted)
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
            StatusText.Text = "Running the simulated setup…";
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
