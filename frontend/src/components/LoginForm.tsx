import { useState, type FormEvent } from 'react'

import { useAuth } from '../auth/useAuth.ts'
import { ErrorMessage } from './ErrorMessage.tsx'
import { messageFor } from './errorText.ts'

export function LoginForm() {
  const { login } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (submitting) {
      return
    }
    // An email address has no meaningful surrounding space. A password is sent exactly as it was typed.
    const trimmedEmail = email.trim()
    if (trimmedEmail === '' || password === '') {
      return
    }

    setSubmitting(true)
    setError(null)
    try {
      await login(trimmedEmail, password)
      // Signed in: this form is about to be replaced. The password does not outlive the attempt.
      setPassword('')
    } catch (caught) {
      setError(messageFor(caught))
      setSubmitting(false)
    }
  }

  return (
    <form className="auth-form" onSubmit={handleSubmit} aria-labelledby="login-heading">
      <h2 id="login-heading">Sign in</h2>

      <ErrorMessage message={error} />

      <div className="field">
        <label htmlFor="login-email">Email</label>
        <input
          id="login-email"
          name="email"
          type="email"
          autoComplete="username"
          autoCapitalize="none"
          spellCheck={false}
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
      </div>

      <div className="field">
        <label htmlFor="login-password">Password</label>
        <input
          id="login-password"
          name="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(event) => setPassword(event.target.value)}
        />
      </div>

      {/* Unavailable while signing in, but not `disabled`: a disabled button drops keyboard focus. */}
      <button type="submit" aria-disabled={submitting}>
        {submitting ? 'Signing in…' : 'Sign in'}
      </button>
    </form>
  )
}
