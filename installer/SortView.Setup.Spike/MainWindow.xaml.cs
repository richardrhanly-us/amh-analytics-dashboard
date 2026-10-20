using System;
using System.Windows;
using SortView.Setup.Core;
using SortView.Setup.Core.Testing;
using SortView.Setup.Spike.Simulation;

namespace SortView.Setup.Spike
{
    /// <summary>
    /// Phase 3C developer test window -- SIMULATION ONLY. Hosts the shared setup wizard (SortView.Setup.Wizard) and
    /// supplies the one thing the wizard never chooses: what runs. Here that is the real setup engine (Invoke-SetupMain)
    /// through the Core simulation harness, against a generated simulated machine (SimulationWorkspace); there is no way
    /// to run anything else. Everything else -- the questions, progress, cancellation and closing -- is the wizard's.
    /// </summary>
    public partial class MainWindow : Window
    {
        private string repositoryRoot;
        private SimulationWorkspace workspace;

        public MainWindow()
        {
            InitializeComponent();
            Wizard.StartRequested += OnStartRequested;
            Wizard.RunStateChanged += (sender, e) => UpdateSimulationControls();
            Wizard.SetupEventReceived += OnSetupEvent;
            try
            {
                repositoryRoot = SimulationWorkspace.FindRepositoryRoot();
                NewWorkspace();
            }
            catch (Exception ex)
            {
                Wizard.IsInstallationEnabled = false;
                Wizard.ShowStatus("Cannot simulate: " + ex.Message);
                NewMachineButton.IsEnabled = false;
            }
        }

        private void OnStartRequested(object sender, EventArgs e)
        {
            SimulatedContract contract = ScenarioV2.IsChecked == true ? SimulatedContract.ContractV2 : SimulatedContract.Legacy;
            SimulatedFault fault = FaultInstall.IsChecked == true ? SimulatedFault.DuringInstall
                : FaultVerification.IsChecked == true ? SimulatedFault.DuringVerification : SimulatedFault.None;
            try
            {
                workspace.PrepareRun(contract, fault, SlowSteps.IsChecked == true, StandardFilesPresent.IsChecked == true);
            }
            catch (Exception ex)
            {
                Wizard.ShowStatus("Cannot prepare the simulated machine: " + ex.Message);
                return;
            }
            if (fault != SimulatedFault.None)
            {
                // The failure is set up for THIS run only ("make the next run fail"): the selection goes back to
                // "Do not fail" so it always shows what the next Start will do, and a resume needs no extra click.
                FaultNone.IsChecked = true;
            }

            SimulationRequest request = workspace.Request;
            Wizard.BeginRun(
                (presenter, onEvent, onConsoleText, token) => SimulatedSetupHarness.Run(request, presenter, onEvent, onConsoleText, token),
                fault == SimulatedFault.None
                    ? "Running the simulated setup…"
                    : "Running the simulated setup… (it will fail " + (fault == SimulatedFault.DuringInstall ? "during install" : "during verification") +
                      "; the failure applies to this run only)");
        }

        private void OnSetupEvent(SetupEvent setupEvent)
        {
            if (setupEvent.Type == "enrollment_result" && setupEvent.Fields.TryGetValue("enrollment_outcome", out object outcome) &&
                (outcome as string) == "Enrolled")
            {
                workspace.MarkCodeUsed(); // the simulated server will refuse this code from now on, as the real one does
            }
        }

        private void NewMachineButton_Click(object sender, RoutedEventArgs e)
        {
            if (Wizard.IsRunning)
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
            Wizard.ClearOutput();
            Wizard.ShowStatus("A new simulated machine is ready.");
        }

        private void NewWorkspace()
        {
            workspace = SimulationWorkspace.Create(repositoryRoot);
            WorkspacePath.Text = "Simulated machine: " + workspace.Root;
            Wizard.SourceBrowseFolder = workspace.SourceDir;
            UpdateSimulationControls();
        }

        private void UpdateSimulationControls()
        {
            bool running = Wizard.IsRunning;
            NewMachineButton.IsEnabled = !running && workspace != null;
            foreach (UIElement option in new UIElement[] { ScenarioLegacy, ScenarioV2, FaultNone, FaultInstall, FaultVerification, StandardFilesPresent, SlowSteps })
            {
                option.IsEnabled = !running;
            }
        }
    }
}
