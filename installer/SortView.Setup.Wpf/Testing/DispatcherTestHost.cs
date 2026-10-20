using System;
using System.Diagnostics;
using System.Threading;
using System.Windows.Threading;

namespace SortView.Setup.Wpf.Testing
{
    /// <summary>TEST-ONLY: a real WPF Dispatcher running its message loop on its own STA thread -- the UI thread.</summary>
    public sealed class DispatcherTestHost : IDisposable
    {
        private readonly Thread thread;

        public DispatcherTestHost()
        {
            using (var ready = new ManualResetEventSlim(false))
            {
                thread = new Thread(() =>
                {
                    Dispatcher = Dispatcher.CurrentDispatcher;
                    ThreadId = Thread.CurrentThread.ManagedThreadId;
                    ready.Set();
                    Dispatcher.Run();
                });
                thread.SetApartmentState(ApartmentState.STA);
                thread.IsBackground = true;
                thread.Name = "Test UI thread";
                thread.Start();
                ready.Wait();
            }
        }

        public Dispatcher Dispatcher { get; private set; }

        public int ThreadId { get; private set; }

        /// <summary>Milliseconds the UI thread took to run a trivial posted operation, or -1 if it did not within the limit.</summary>
        public long MeasureResponsiveness(int limitMilliseconds)
        {
            var watch = Stopwatch.StartNew();
            DispatcherOperation<int> operation = Dispatcher.InvokeAsync(() => 0);
            return operation.Task.Wait(limitMilliseconds) ? watch.ElapsedMilliseconds : -1;
        }

        public void Shutdown()
        {
            if (!Dispatcher.HasShutdownStarted)
            {
                Dispatcher.BeginInvokeShutdown(DispatcherPriority.Send);
            }
            thread.Join(10000);
        }

        public void Dispose() => Shutdown();
    }
}
