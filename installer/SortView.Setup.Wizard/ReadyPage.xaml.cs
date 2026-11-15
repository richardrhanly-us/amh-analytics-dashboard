using System.Windows.Controls;

namespace SortView.Setup.Wizard
{
    /// <summary>The screen before setup: what setup will do. Its host window owns the Back, Next and Exit buttons.</summary>
    public partial class ReadyPage : UserControl
    {
        /// <summary>The notice in a build that may install: a supervised-pilot version, never presented as signed, generally
        /// released or approved. It names the Setup page's own Install button.</summary>
        public const string EnabledNotice = "Supervised pilot version: installation is enabled in this version, which is meant to be used with help from SortView. It is not digitally signed and is not a generally released version of SortView Setup. Select Next to check the installation package and copy it to a protected folder; that copy is removed when you go back or close setup. Nothing is installed or enrolled until you select Install.";

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
