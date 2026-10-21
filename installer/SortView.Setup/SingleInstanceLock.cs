using System;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Threading;

namespace SortView.Setup
{
    /// <summary>How an attempt to take the installer's lock ended.</summary>
    public enum SingleInstanceOutcome
    {
        /// <summary>This process now holds the lock.</summary>
        Acquired,

        /// <summary>The previous holder ended without releasing it; Windows handed it to this process, which now holds
        /// it. (That holder's setup ran inside its own process, so it ended too.)</summary>
        AcquiredAbandoned,

        /// <summary>Another process holds it: another SortView Setup is running.</summary>
        HeldElsewhere,

        /// <summary>The lock exists but this process may not use it, so it cannot tell whether setup is running.</summary>
        AccessDenied,

        /// <summary>Any other failure: exclusive ownership could not be confirmed.</summary>
        Uncertain,
    }

    /// <summary>
    /// Lets only one SortView Setup run on this computer at a time, in any logon session: a named mutex in the Global
    /// namespace, held from before the window opens until the program exits. Only SYSTEM and Administrators may use it.
    /// Anything other than holding it -- held by another process, access denied, any error -- means setup must not run.
    ///
    /// It covers the graphical installer only: setup.ps1 run from a command line does not take it. A local user can create
    /// the name first and so stop setup from starting (never make two run at once); hardening against that (a private
    /// namespace) is for before customer release.
    /// </summary>
    public sealed class SingleInstanceLock : IDisposable
    {
        public const string InstallerName = @"Global\SortView.Setup.Installer";

        private Mutex mutex;

        private SingleInstanceLock(Mutex mutex)
        {
            this.mutex = mutex;
        }

        /// <summary>The installer's lock, with <see cref="ProductionSecurity"/>. <paramref name="held"/> is set only when
        /// the outcome is Acquired or AcquiredAbandoned.</summary>
        public static SingleInstanceOutcome TryAcquireInstaller(out SingleInstanceLock held) =>
            TryAcquire(InstallerName, ProductionSecurity(), out held);

        /// <summary>Who may use the installer's lock: SYSTEM and Administrators, nobody else.</summary>
        public static MutexSecurity ProductionSecurity()
        {
            var security = new MutexSecurity();
            foreach (WellKnownSidType sid in new[] { WellKnownSidType.LocalSystemSid, WellKnownSidType.BuiltinAdministratorsSid })
            {
                security.AddAccessRule(new MutexAccessRule(new SecurityIdentifier(sid, null), MutexRights.FullControl, AccessControlType.Allow));
            }
            return security;
        }

        /// <summary>Takes the lock named <paramref name="name"/> without waiting. <paramref name="security"/> applies only
        /// if this call creates it. Tests use their own names (and may pass their own security); the program uses
        /// <see cref="TryAcquireInstaller"/>.</summary>
        public static SingleInstanceOutcome TryAcquire(string name, MutexSecurity security, out SingleInstanceLock held)
        {
            held = null;
            Mutex candidate = null;
            try
            {
                candidate = new Mutex(false, name, out bool _, security);
                SingleInstanceOutcome outcome = SingleInstanceOutcome.Acquired;
                bool owned;
                try
                {
                    owned = candidate.WaitOne(0);
                }
                catch (AbandonedMutexException)
                {
                    owned = true; // ownership passes to this thread
                    outcome = SingleInstanceOutcome.AcquiredAbandoned;
                }
                if (!owned)
                {
                    candidate.Dispose();
                    return SingleInstanceOutcome.HeldElsewhere;
                }
                held = new SingleInstanceLock(candidate);
                return outcome;
            }
            catch (UnauthorizedAccessException)
            {
                candidate?.Dispose();
                return SingleInstanceOutcome.AccessDenied;
            }
            catch (Exception)
            {
                candidate?.Dispose();
                return SingleInstanceOutcome.Uncertain;
            }
        }

        /// <summary>Releases the lock. Call it on the thread that took it (a mutex belongs to that thread); if that is
        /// not possible, Windows releases it when the thread or process ends, and the next taker sees it abandoned.</summary>
        public void Dispose()
        {
            Mutex held = mutex;
            mutex = null;
            if (held == null)
            {
                return;
            }
            try
            {
                held.ReleaseMutex();
            }
            catch (ApplicationException)
            {
                // Not the owning thread: Windows releases it when that thread ends.
            }
            finally
            {
                held.Dispose();
            }
        }
    }
}
