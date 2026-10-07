import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router'

import { ResetPasswordPage } from './account/ResetPasswordPage.tsx'
import { useAuth } from './auth/useAuth.ts'
import { ErrorMessage } from './components/ErrorMessage.tsx'
import { LoginForm } from './components/LoginForm.tsx'
import { UserMenu } from './components/UserMenu.tsx'
import { createQueryClient } from './query/queryClient.ts'
import { AppRouter } from './router/AppRouter.tsx'
import { RESET_PASSWORD_PATH } from './router/paths.ts'

type QueryClientFactory = (onSessionExpired: () => void) => QueryClient

/**
 * Everything a signed-in user sees. It owns the session's query client: made
 * when the user signs in and gone, with all it holds, when this unmounts at
 * sign-out or session expiry -- so the next person to sign in starts with
 * nothing of the last one's.
 */
function SignedInApp({ createClient }: { createClient: QueryClientFactory }) {
  const { sessionExpired } = useAuth()
  const [queryClient] = useState(() => createClient(sessionExpired))

  return (
    <QueryClientProvider client={queryClient}>
      <AppRouter />
    </QueryClientProvider>
  )
}

function AuthView({ createClient }: { createClient: QueryClientFactory }) {
  const { state, retryRestore } = useAuth()

  switch (state.status) {
    case 'restoring':
      return <p role="status">Checking your session…</p>
    case 'unauthenticated':
      return <LoginForm />
    case 'authenticated':
      // The only place the pages exist: nothing below here renders, or asks the API anything, until sign-in.
      return <SignedInApp createClient={createClient} />
    case 'error':
      return (
        <section aria-labelledby="restore-error-heading">
          <h2 id="restore-error-heading">Something went wrong</h2>
          <ErrorMessage message={state.message} />
          <button type="button" onClick={retryRestore}>
            Try again
          </button>
        </section>
      )
  }
}

/** `createClient` exists for tests, which need a query client that does not wait between retries. */
function App({ createClient = createQueryClient }: { createClient?: QueryClientFactory }) {
  const { state } = useAuth()
  // The one page that does not wait for sign-in: someone following a password reset link cannot sign in, and
  // the link must be read whether or not this browser happens to hold a session. It is shown in place of the
  // session check, the sign-in form and the signed-in pages alike.
  const resettingPassword = useLocation().pathname.replace(/\/+$/, '') === RESET_PASSWORD_PATH
  const main = useRef<HTMLElement>(null)
  const previousStatus = useRef(state.status)

  // Signing in, signing out and an expired session each replace everything in <main>, including whatever had
  // focus. Focus moves to <main> so the next Tab reaches what replaced it -- the sign-in form, or the page.
  // Checking the session on first load is not one of these, and moves nothing.
  useEffect(() => {
    const before = previousStatus.current
    previousStatus.current = state.status
    const signedIn = before === 'unauthenticated' && state.status === 'authenticated'
    const signedOut = before === 'authenticated' && state.status === 'unauthenticated'
    // The reset page moves focus itself, to the heading that says what happened.
    if ((signedIn || signedOut) && !resettingPassword) {
      main.current?.focus()
    }
  }, [state.status, resettingPassword])

  return (
    <div className="app">
      <header className="app-header">
        <h1>SortView</h1>
        {state.status === 'authenticated' && <UserMenu user={state.user} />}
      </header>
      <main ref={main} tabIndex={-1}>
        {resettingPassword ? <ResetPasswordPage /> : <AuthView createClient={createClient} />}
      </main>
    </div>
  )
}

export default App
