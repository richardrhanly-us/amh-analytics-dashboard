using System.Windows.Controls;

namespace SortView.Setup.Wizard
{
    /// <summary>The screen before setup: what setup will do. Its host window owns the Back, Next and Exit buttons.</summary>
    public partial class ReadyPage : UserControl
    {
        /// <summary>The notice in a build that may install: a test build, never presented as a released one.</summary>
        public const string EnabledNotice = "Test build: installation is enabled in this version, but it is not a released version of SortView Setup. Select Next to check the installation package and copy it to a protected folder; that copy is removed when you go back or close setup. Nothing is installed or enrolled until you select Start.";

        private readonly string disabledNotice;
        private bool installationEnabled;

        public ReadyPage()
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
