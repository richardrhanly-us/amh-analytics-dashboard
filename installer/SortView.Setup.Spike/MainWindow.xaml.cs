using System;
using System.Runtime.InteropServices;
using System.Security;
using System.Threading.Tasks;
using System.Windows;

namespace SortView.Setup.Spike
{
    public partial class MainWindow : Window
    {
        [DllImport("kernel32.dll")]
        private static extern IntPtr GetConsoleWindow();

        public MainWindow()
        {
            InitializeComponent();
        }

        private async void StartButton_Click(object sender, RoutedEventArgs e)
        {
            // SecurePassword returns a COPY; the box is cleared at once. The value is never converted to text here.
            SecureString code = CodeBox.SecurePassword;
            CodeBox.Clear();
            if (code.Length == 0)
            {
                code.Dispose();
                StatusText.Text = "Enter a dummy code first.";
                return;
            }
            code.MakeReadOnly();

            StartButton.IsEnabled = false;
            CodeBox.IsEnabled = false;
            EventList.Items.Clear();
            Progress.Value = 0;
            StatusText.Text = "Running…";
            try
            {
                SpikeResult result = await Task.Run(() =>
                    SpikeRunner.Run(code, ev => Dispatcher.BeginInvoke(new Action(() => ShowEvent(ev)))));

                bool consoleAttached = GetConsoleWindow() != IntPtr.Zero;
                StatusText.Text = result.Error != null
                    ? "Failed: " + result.Error
                    : (result.Succeeded ? "Simulation complete." : "Simulation failed.")
                      + " Engine: Windows PowerShell " + result.PSVersion + " (" + result.PSEdition + ")."
                      + " Child processes: " + result.ChildProcessCount + "."
                      + " Console attached: " + (consoleAttached ? "yes" : "no") + ".";
            }
            catch (Exception ex)
            {
                StatusText.Text = "Failed: " + ex.GetType().Name + ": " + ex.Message;
            }
            finally
            {
                code.Dispose();
                StartButton.IsEnabled = true;
                CodeBox.IsEnabled = true;
            }
        }

        private void ShowEvent(SpikeEvent ev)
        {
            EventList.Items.Add("[" + ev.Stage + "] " + ev.Status + " — " + ev.Detail);
            if (ev.Status == "ok" || ev.Status == "failed") Progress.Value += 1;
        }
    }
}
