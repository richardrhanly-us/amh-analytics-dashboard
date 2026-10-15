import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'

import { login, logout, type User } from '../../src/api/auth.ts'
import { isApiError } from '../../src/api/client.ts'
import { getAdminSession } from './adminSession.ts'

const CHECK_FAILED = 'We could not check your administration access. Please try again.'
const SIGN_IN_FAILED = 'Sign-in failed. Please try again.'
const SIGN_OUT_FAILED = 'Sign-out failed. Please try again.'
const NO_ACCESS = 'This account does not have SortView administration access.'

/**
 * What the admin app knows, which is only what GET /api/admin/session last said:
 *
 *   checking   asking
 *   signedOut  401: nobody is signed in
 *   forbidden  403: someone is signed in, and is not a platform administrator
 *   admin      200: a platform administrator
 *   error      any other answer, or none
 *
 * Signing in never decides this by itself: after a successful sign-in the admin session is asked again.
 */
type State =
  | { status: 'checking' }
  | { status: 'signedOut' }
  | { status: 'forbidden' }
  | { status: 'admin'; user: User }
  | { status: 'error' }

function SignInForm({ onSignedIn }: { onSignedIn: () => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const trimmedEmail = email.trim()
    if (submitting || trimmedEmail === '' || password === '') {
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await login(trimmedEmail, password)
      setPassword('')
      onSignedIn()
    } catch (caught) {
      setError(isApiError(caught) ? caught.message : SIGN_IN_FAILED)
      setSubmitting(false)
    }
  }

  return (
    <section aria-labelledby="sign-in-heading">
      <h2 id="sign-in-heading">Sign in</h2>
      <form onSubmit={(event) => void handleSubmit(event)} noValidate>
        <p>
          <label htmlFor="admin-email">Email</label>
          <input
            id="admin-email"
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </p>
        <p>
          <label htmlFor="admin-password">Password</label>
          <input
            id="admin-password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </p>
        {error !== null && (
          <p className="error-message" role="alert">
            {error}
          </p>
        )}
        <button type="submit" aria-disabled={submitting}>
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </section>
  )
}

function SignOutButton({ onSignedOut }: { onSignedOut: () => void }) {
  const [signingOut, setSigningOut] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleClick() {
    if (signingOut) {
      return
    }
    setSigningOut(true)
    setError(null)
    try {
      // Local state changes only once the server has ended the session.
      await logout()
      onSignedOut()
    } catch {
      setError(SIGN_OUT_FAILED)
      setSigningOut(false)
    }
  }

  return (
    <>
      <button type="button" className="button-secondary" aria-disabled={signingOut} onClick={() => void handleClick()}>
        {signingOut ? 'Logging out…' : 'Log out'}
      </button>
      {error !== null && (
        <p className="error-message" role="alert">
          {error}
        </p>
      )}
    </>
  )
}

/** The separate SortView administration app (A2 item 1): a session check and an empty home, nothing more yet. */
export function AdminApp() {
  const [state, setState] = useState<State>({ status: 'checking' })
  // React StrictMode runs an effect twice in development; the session is still asked once.
  const checkStarted = useRef(false)

  const check = useCallback(() => {
    setState({ status: 'checking' })
    getAdminSession().then(
      (user) => setState({ status: 'admin', user }),
      (error: unknown) => {
        if (isApiError(error) && error.status === 401) {
          setState({ status: 'signedOut' })
        } else if (isApiError(error) && error.status === 403) {
          setState({ status: 'forbidden' })
        } else {
          setState({ status: 'error' })
        }
      },
    )
  }, [])

  useEffect(() => {
    if (checkStarted.current) {
      return
    }
    checkStarted.current = true
    check()
  }, [check])

  const signedOut = useCallback(() => setState({ status: 'signedOut' }), [])

  return (
    <div className="app">
      <header className="app-header">
        <h1>SortView Administration</h1>
        {state.status === 'admin' && (
          <p>
            Signed in as {state.user.email} <SignOutButton onSignedOut={signedOut} />
          </p>
        )}
      </header>
      <main>
        {state.status === 'checking' && <p role="status">Checking your administration access…</p>}
        {state.status === 'signedOut' && <SignInForm onSignedIn={check} />}
        {state.status === 'forbidden' && (
          <section aria-labelledby="no-access-heading">
            <h2 id="no-access-heading">No access</h2>
            <p>{NO_ACCESS}</p>
            <SignOutButton onSignedOut={signedOut} />
          </section>
        )}
        {state.status === 'admin' && <p>No administration features yet.</p>}
        {state.status === 'error' && (
          <section aria-labelledby="check-error-heading">
            <h2 id="check-error-heading">Something went wrong</h2>
            <p className="error-message" role="alert">
              {CHECK_FAILED}
            </p>
            <button type="button" onClick={check}>
              Try again
            </button>
          </section>
        )}
      </main>
    </div>
  )
}
