using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;

namespace SortView.Setup.Wizard
{
    /// <summary>
    /// The prerequisites screen. Reads this machine (read-only, <see cref="MachineFacts.Read"/>) on a background thread so
    /// the window stays responsive, then shows each result. A read that throws shows every check as Not checked, which
    /// blocks Next. Its host window owns Back/Next/Exit and enables Next from <see cref="CanContinue"/>.
    /// </summary>
    public partial class PrerequisitesPage : UserControl
    {
        public PrerequisitesPage()
        {
            InitializeComponent();
        }

        /// <summary>Where machine facts come from; tests substitute their own. Called on a background thread.</summary>
        public Func<MachineFacts> ReadFacts { get; set; } = MachineFacts.Read;

        /// <summary>The latest results, or null before the first check finishes.</summary>
        public IReadOnlyList<PrerequisiteResult> Results { get; private set; }

        public bool IsChecking { get; private set; }

        /// <summary>True only once results exist, no check is running, and no result blocks (any Failed, or a
        /// required check Not checked).</summary>
        public bool CanContinue => !IsChecking && Results != null && !Results.Any(r => r.BlocksNext);

        /// <summary>Raised on the UI thread whenever checking starts or results arrive.</summary>
        public event EventHandler StateChanged;

        /// <summary>Starts a check unless one is already running.</summary>
        public void CheckAgain()
        {
            if (IsChecking)
            {
                return;
            }
            IsChecking = true;
            CheckAgainButton.IsEnabled = false;
            SummaryText.Text = "Checking this computer…";
            StateChanged?.Invoke(this, EventArgs.Empty);

            Func<MachineFacts> read = ReadFacts;
            Task.Run(() =>
            {
                MachineFacts facts;
                try
                {
                    facts = read();
                }
                catch (Exception)
                {
                    facts = null; // Evaluate treats it as unknown: every check Not checked.
                }
                IReadOnlyList<PrerequisiteResult> results = PrerequisiteRules.Evaluate(facts);
                Dispatcher.BeginInvoke(new Action(() => ShowResults(results)));
            });
        }

        private void ShowResults(IReadOnlyList<PrerequisiteResult> results)
        {
            Results = results;
            IsChecking = false;
            ResultsList.ItemsSource = results;
            CheckAgainButton.IsEnabled = true;
            int blocking = results.Count(r => r.BlocksNext);
            int warnings = results.Count(r => r.Status == PrerequisiteStatus.Warning);
            SummaryText.Text = blocking > 0
                ? blocking + (blocking == 1 ? " required check needs" : " required checks need") + " attention. Resolve them, then select Check again."
                : warnings > 0
                    ? "Ready to continue, with " + warnings + (warnings == 1 ? " warning" : " warnings") + "."
                    : "Ready to continue.";
            StateChanged?.Invoke(this, EventArgs.Empty);
        }

        private void CheckAgainButton_Click(object sender, RoutedEventArgs e) => CheckAgain();
    }
}
