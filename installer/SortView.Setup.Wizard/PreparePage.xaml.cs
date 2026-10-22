using System;
using System.Collections.Generic;
using System.Linq;
using System.Windows;
using System.Windows.Automation.Peers;
using System.Windows.Controls;

namespace SortView.Setup.Wizard
{
    /// <summary>
    /// The Prepare step's screen: what the host's preparation is doing, how it ended, and whether it can be tried again. It
    /// decides nothing and holds nothing: its host (the production window) owns the preparation and its staged bundle and
    /// tells this page what to show. The stage line is announced to screen readers only when the stage changes -- never
    /// for each file copied.
    /// </summary>
    public partial class PreparePage : UserControl
    {
        public PreparePage()
        {
            InitializeComponent();
        }

        /// <summary>The person asked to stop the preparation.</summary>
        public event EventHandler CancelRequested;

        /// <summary>The person asked to prepare again (offered only after a failure or cancellation that may be retried).</summary>
        public event EventHandler RetryRequested;

        /// <summary>The stage line as shown (for the host and tests).</summary>
        public string Stage => StageText.Text;

        /// <summary>Back to a blank page (the host left the step).</summary>
        public void Reset()
        {
            StageText.Text = "";
            CountText.Text = "";
            DetailText.Text = "";
            BusyIndicator.IsIndeterminate = false;
            BusyIndicator.Visibility = Visibility.Collapsed;
            CancelPrepareButton.Visibility = Visibility.Collapsed;
            RetryButton.Visibility = Visibility.Collapsed;
            ShowEarlierRuns(null);
            ShowDetails(null);
        }

        /// <summary>A new stage of work is under way: announced once.</summary>
        public void ShowWorking(string stage)
        {
            DetailText.Text = "";
            RetryButton.Visibility = Visibility.Collapsed;
            CancelPrepareButton.Visibility = Visibility.Visible;
            CancelPrepareButton.IsEnabled = true;
            BusyIndicator.Visibility = Visibility.Visible;
            BusyIndicator.IsIndeterminate = true;
            ShowDetails(null);
            SetStage(stage);
        }

        /// <summary>Files copied so far (not announced: it changes for every file).</summary>
        public void ShowCount(int done, int total) =>
            CountText.Text = total > 0 ? "Copied " + done + " of " + total + " files." : "";

        /// <summary>Cancellation was requested: the button is off until the preparation stops.</summary>
        public void ShowCancelling()
        {
            CancelPrepareButton.IsEnabled = false;
            DetailText.Text = "Cancelling: setup stops at its next check and removes the files it copied.";
        }

        /// <summary>The preparation ended: announced once.</summary>
        public void ShowOutcome(string stage, string detail, IEnumerable<string> details, bool canRetry)
        {
            BusyIndicator.IsIndeterminate = false;
            BusyIndicator.Visibility = Visibility.Collapsed;
            CancelPrepareButton.Visibility = Visibility.Collapsed;
            RetryButton.Visibility = canRetry ? Visibility.Visible : Visibility.Collapsed;
            DetailText.Text = detail ?? "";
            ShowDetails(details);
            SetStage(stage);
        }

        /// <summary>Information about earlier setup attempts' folders (null hides it). Never blocks anything.</summary>
        public void ShowEarlierRuns(string text)
        {
            EarlierRunsText.Text = text ?? "";
            EarlierRunsNotice.Visibility = string.IsNullOrEmpty(text) ? Visibility.Collapsed : Visibility.Visible;
        }

        private void ShowDetails(IEnumerable<string> details)
        {
            List<string> lines = details?.Where(d => !string.IsNullOrEmpty(d)).ToList() ?? new List<string>();
            DetailsList.ItemsSource = lines;
            DetailsExpander.Visibility = lines.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
        }

        private void SetStage(string stage)
        {
            if (StageText.Text == (stage ?? ""))
            {
                return;
            }
            StageText.Text = stage ?? "";
            if (AutomationPeer.ListenerExists(AutomationEvents.LiveRegionChanged))
            {
                UIElementAutomationPeer.CreatePeerForElement(StageText)?.RaiseAutomationEvent(AutomationEvents.LiveRegionChanged);
            }
        }

        private void CancelPrepareButton_Click(object sender, RoutedEventArgs e) => CancelRequested?.Invoke(this, EventArgs.Empty);

        private void RetryButton_Click(object sender, RoutedEventArgs e) => RetryRequested?.Invoke(this, EventArgs.Empty);
    }
}
