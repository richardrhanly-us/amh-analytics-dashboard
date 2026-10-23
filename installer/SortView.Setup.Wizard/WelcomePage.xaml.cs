using System.Windows.Controls;

namespace SortView.Setup.Wizard
{
    /// <summary>The first screen: what SortView Setup does. Its host window owns the Next and Exit buttons.</summary>
    public partial class WelcomePage : UserControl
    {
        /// <summary>The notice in a build that may install: a test build, never presented as a released one.</summary>
        public const string EnabledNotice = "Test build: this version can install SortView Collector, but it is not a released version of SortView Setup. Use it only on a computer where you have been asked to test it. To check the package, setup copies it to a protected folder and removes that copy when you go back or close setup.";

        private readonly string disabledNotice;
        private bool installationEnabled;

        public WelcomePage()
        {
            InitializeComponent();
            disabledNotice = DevelopmentNotice.Text; // the XAML's: installation is disabled
        }

        /// <summary>Whether this build may install (its host decides, from the build's own gate and pin). Only the notice
        /// changes: it no longer says installation is disabled. False, the default, keeps the disabled notice.</summary>
        public bool IsInstallationEnabled
        {
            get => installationEnabled;
            set
            {
                installationEnabled = value;
                DevelopmentNotice.Text = value ? EnabledNotice : disabledNotice;
            }
        }
    }
}
