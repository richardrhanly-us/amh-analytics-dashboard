using System.Windows.Controls;

namespace SortView.Setup.Wizard
{
    /// <summary>The first screen: what SortView Setup does. Its host window owns the Next and Exit buttons.</summary>
    public partial class WelcomePage : UserControl
    {
        /// <summary>The notice in a build that may install: a supervised-pilot version, never presented as signed, generally
        /// released or approved.</summary>
        public const string EnabledNotice = "Supervised pilot version: this version can install SortView Collector, and is meant to be used with help from SortView. It is not digitally signed and is not a generally released version of SortView Setup. To check the package, setup copies it to a protected folder and removes that copy when you go back or close setup.";

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
