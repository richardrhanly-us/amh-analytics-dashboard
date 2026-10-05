import { useAuth } from './auth/useAuth.ts'
import { AuthenticatedShell } from './components/AuthenticatedShell.tsx'
import { ErrorMessage } from './components/ErrorMessage.tsx'
import { LoginForm } from './components/LoginForm.tsx'

function AuthView() {
  const { state, retryRestore } = useAuth()

  switch (state.status) {
    case 'restoring':
      return <p role="status">Checking your session…</p>
    case 'unauthenticated':
      return <LoginForm />
    case 'authenticated':
      return <AuthenticatedShell user={state.user} />
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

function App() {
  return (
    <main className="app">
      <h1>SortView</h1>
      <AuthView />
    </main>
  )
}

export default App
