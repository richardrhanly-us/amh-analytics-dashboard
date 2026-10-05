import { useAuth } from './auth/useAuth.ts'
import { ErrorMessage } from './components/ErrorMessage.tsx'
import { LoginForm } from './components/LoginForm.tsx'
import { UserMenu } from './components/UserMenu.tsx'
import { AppRouter } from './router/AppRouter.tsx'

function AuthView() {
  const { state, retryRestore } = useAuth()

  switch (state.status) {
    case 'restoring':
      return <p role="status">Checking your session…</p>
    case 'unauthenticated':
      return <LoginForm />
    case 'authenticated':
      // The only place the pages exist: nothing below here renders, or asks the API anything, until sign-in.
      return <AppRouter />
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
  const { state } = useAuth()

  return (
    <div className="app">
      <header className="app-header">
        <h1>SortView</h1>
        {state.status === 'authenticated' && <UserMenu user={state.user} />}
      </header>
      <main>
        <AuthView />
      </main>
    </div>
  )
}

export default App
