using System;
using System.ComponentModel;
using System.Windows;

namespace SortView.Setup
{
    /// <summary>
    /// The production setup window: Welcome, then Prerequisites (read-only checks), then Ready (what setup will do), then
    /// Prepare (the trusted installation package verified and copied to a protected folder), then the shared wizard with
    /// installation disabled. There is no setup runner, so nothing can be installed, enrolled or changed. Enabling
    /// installation means giving the wizard a runner over the real engine (SetupEngineHost) and the prepared bundle -- a
    /// later, separate milestone.
    ///
    /// While the wizard runs setup or the package is being prepared, Back and Next are off (and refuse even a forced
    /// click); Exit stays on, and the window waits for the work to stop and be cleaned up before it closes. Next leaves
    /// Prepare only once a verified bundle is staged; Back from Prepare releases it.
    /// </summary>
    public partial class MainWindow : Window
    {
        public enum WizardPage
        {
            Welcome,
            Prerequisites,
            Ready,
            Prepare,
            Setup,
        }

        private bool prerequisitesChecked;
        private readonly PreparationStep preparation;

        public MainWindow()
        {
            InitializeComponent();
            preparation = new PreparationStep(PrepareView, Dispatcher, () => PreparationFactory());
            PrerequisitesView.StateChanged += (s, e) => UpdateButtons();
            Wizard.RunStateChanged += (s, e) => UpdateButtons();
            preparation.StateChanged += (s, e) => UpdateButtons();
            Closing += OnClosing;
            Closed += (s, e) => preparation.DisposePreparation();
            ShowPage(WizardPage.Welcome);
        }

        public WizardPage CurrentPage { get; private set; }

        /// <summary>TEST SEAM (internal): how the Prepare step gets its preparation. Always the installer's own (embedded pin,
        /// bundle folder beside the program, production staging policy) unless a test replaces it.</summary>
        internal Func<BundlePreparation> PreparationFactory { get; set; } = BundlePreparation.CreateForThisInstaller;

        /// <summary>Releases the Prepare step's staged bundle (for the application's exit, before its lock is released).</summary>
        internal void DisposePreparation() => preparation.DisposePreparation();

        private bool IsBusy => Wizard.IsRunning || preparation.IsBusy;

        private void ShowPage(WizardPage page)
        {
            WizardPage previous = CurrentPage;
            CurrentPage = page;
            WelcomeView.Visibility = page == WizardPage.Welcome ? Visibility.Visible : Visibility.Collapsed;
            PrerequisitesView.Visibility = page == WizardPage.Prerequisites ? Visibility.Visible : Visibility.Collapsed;
            ReadyView.Visibility = page == WizardPage.Ready ? Visibility.Visible : Visibility.Collapsed;
            PrepareView.Visibility = page == WizardPage.Prepare ? Visibility.Visible : Visibility.Collapsed;
            Wizard.Visibility = page == WizardPage.Setup ? Visibility.Visible : Visibility.Collapsed;
            if (page == WizardPage.Prerequisites && !prerequisitesChecked)
            {
                prerequisitesChecked = true;
                PrerequisitesView.CheckAgain();
            }
            if (previous == WizardPage.Prepare && page == WizardPage.Ready)
            {
                preparation.Leave(); // going back: the staged bundle is released
            }
            if (page == WizardPage.Prepare)
            {
                preparation.Enter(); // from Ready: prepares afresh; back from Setup: keeps the prepared bundle
            }
            UpdateButtons();
        }

        private void UpdateButtons()
        {
            bool busy = IsBusy;
            BackButton.IsEnabled = !busy && CurrentPage != WizardPage.Welcome;
            NextButton.IsEnabled = !busy && (CurrentPage == WizardPage.Welcome || CurrentPage == WizardPage.Ready ||
                                   (CurrentPage == WizardPage.Prerequisites && PrerequisitesView.CanContinue) ||
                                   (CurrentPage == WizardPage.Prepare && preparation.IsPrepared));
        }

        private void BackButton_Click(object sender, RoutedEventArgs e)
        {
            if (!IsBusy && CurrentPage != WizardPage.Welcome)
            {
                ShowPage(CurrentPage - 1);
            }
        }

        private void NextButton_Click(object sender, RoutedEventArgs e)
        {
            if (IsBusy)
            {
                return;
            }
            if (CurrentPage == WizardPage.Welcome)
            {
                ShowPage(WizardPage.Prerequisites);
            }
            else if (CurrentPage == WizardPage.Prerequisites && PrerequisitesView.CanContinue)
            {
                ShowPage(WizardPage.Ready);
            }
            else if (CurrentPage == WizardPage.Ready)
            {
                ShowPage(WizardPage.Prepare);
            }
            else if (CurrentPage == WizardPage.Prepare && preparation.IsPrepared)
            {
                ShowPage(WizardPage.Setup);
            }
        }

        private void ExitButton_Click(object sender, RoutedEventArgs e) => Close();

        /// <summary>While the package is being prepared, closing waits: the preparation is cancelled, and the window closes
        /// once it has stopped and its files are removed. The UI thread never blocks on the worker.</summary>
        private void OnClosing(object sender, CancelEventArgs e)
        {
            if (Wizard.IsRunning)
            {
                return; // the wizard defers this close until setup has returned; the bundle is released when the window closes
            }
            if (preparation.DeferClose(Close))
            {
                e.Cancel = true;
            }
        }
    }
}
