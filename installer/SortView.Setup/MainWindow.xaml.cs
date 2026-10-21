using System.Windows;

namespace SortView.Setup
{
    /// <summary>
    /// The production setup window: Welcome, then Prerequisites (read-only checks), then the shared wizard with
    /// installation disabled. There is no setup runner, so nothing can be installed, enrolled or changed. Enabling
    /// installation means giving the wizard a runner over the real engine (SetupEngineHost) and a verified bundle -- a
    /// later, separate milestone.
    /// </summary>
    public partial class MainWindow : Window
    {
        public enum WizardPage
        {
            Welcome,
            Prerequisites,
            Setup,
        }

        private bool prerequisitesChecked;

        public MainWindow()
        {
            InitializeComponent();
            PrerequisitesView.StateChanged += (s, e) => UpdateButtons();
            ShowPage(WizardPage.Welcome);
        }

        public WizardPage CurrentPage { get; private set; }

        private void ShowPage(WizardPage page)
        {
            CurrentPage = page;
            WelcomeView.Visibility = page == WizardPage.Welcome ? Visibility.Visible : Visibility.Collapsed;
            PrerequisitesView.Visibility = page == WizardPage.Prerequisites ? Visibility.Visible : Visibility.Collapsed;
            Wizard.Visibility = page == WizardPage.Setup ? Visibility.Visible : Visibility.Collapsed;
            if (page == WizardPage.Prerequisites && !prerequisitesChecked)
            {
                prerequisitesChecked = true;
                PrerequisitesView.CheckAgain();
            }
            UpdateButtons();
        }

        private void UpdateButtons()
        {
            BackButton.IsEnabled = CurrentPage != WizardPage.Welcome;
            NextButton.IsEnabled = CurrentPage == WizardPage.Welcome ||
                                   (CurrentPage == WizardPage.Prerequisites && PrerequisitesView.CanContinue);
        }

        private void BackButton_Click(object sender, RoutedEventArgs e)
        {
            if (CurrentPage != WizardPage.Welcome)
            {
                ShowPage(CurrentPage - 1);
            }
        }

        private void NextButton_Click(object sender, RoutedEventArgs e)
        {
            if (CurrentPage == WizardPage.Welcome)
            {
                ShowPage(WizardPage.Prerequisites);
            }
            else if (CurrentPage == WizardPage.Prerequisites && PrerequisitesView.CanContinue)
            {
                ShowPage(WizardPage.Setup);
            }
        }

        private void ExitButton_Click(object sender, RoutedEventArgs e) => Close();
    }
}
