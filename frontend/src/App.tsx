import { QueryClientProvider, type QueryClient } from '@tanstack/react-query'
import { useState } from 'react'

import { useAuth } from './auth/useAuth.ts'
import { ErrorMessage } from './components/ErrorMessage.tsx'
import { LoginForm } from './components/LoginForm.tsx'
import { UserMenu } from './components/UserMenu.tsx'
import { createQueryClient } from './query/queryClient.ts'
import { AppRouter } from './router/AppRouter.tsx'

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

  return (
    <div className="app">
      <header className="app-header">
        <h1>SortView</h1>
        {state.status === 'authenticated' && <UserMenu user={state.user} />}
      </header>
      <main>
        <AuthView createClient={createClient} />
      </main>
    </div>
  )
}

export default App
