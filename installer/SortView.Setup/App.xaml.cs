using System.Windows;

namespace SortView.Setup
{
    /// <summary>
    /// Starts SortView Setup only once the single-instance lock is held (<see cref="SingleInstanceLock"/>), and keeps it
    /// until the program exits -- which, while setup runs, the setup window defers until the engine has returned.
    /// </summary>
    public partial class App : Application
    {
        public const string AlreadyRunningMessage =
            "SortView Setup is already running on this computer. Use the setup window that is already open, or wait for it to finish.";

        public const string NotVerifiedMessage =
            "SortView Setup could not verify that it is the only setup running on this computer, so it did not start. " +
            "Close any other SortView Setup window and try again. If this keeps happening, restart the computer.";

        private SingleInstanceLock instanceLock;

        protected override void OnStartup(StartupEventArgs e)
        {
            base.OnStartup(e);
            SingleInstanceOutcome outcome = SingleInstanceLock.TryAcquireInstaller(out instanceLock);
            if (instanceLock == null)
            {
                MessageBox.Show(outcome == SingleInstanceOutcome.HeldElsewhere ? AlreadyRunningMessage : NotVerifiedMessage,
                    "SortView Setup", MessageBoxButton.OK, MessageBoxImage.Warning);
                Shutdown(1);
                return;
            }
            try
            {
                new MainWindow().Show();
            }
            catch
            {
                ReleaseLock();
                throw;
            }
        }

        protected override void OnExit(ExitEventArgs e)
        {
            // The staged bundle goes first (normally already released when the window closed), then the lock.
            (MainWindow as SortView.Setup.MainWindow)?.DisposePreparation();
            ReleaseLock();
            base.OnExit(e);
        }

        private void ReleaseLock()
        {
            instanceLock?.Dispose();
            instanceLock = null;
        }
    }
}
