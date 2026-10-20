using System.Windows;

namespace SortView.Setup
{
    /// <summary>
    /// The production setup window. In this build it only shows the shared wizard with installation disabled: there is
    /// no setup runner, so nothing can be installed, enrolled or changed. Enabling installation means giving the wizard a
    /// runner over the real engine (SetupEngineHost) and a verified bundle -- a later, separate milestone.
    /// </summary>
    public partial class MainWindow : Window
    {
        public MainWindow()
        {
            InitializeComponent();
        }
    }
}
