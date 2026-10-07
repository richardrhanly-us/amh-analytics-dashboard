import { useState, type FormEvent } from 'react'

import { ForgotPasswordForm } from '../account/ForgotPasswordForm.tsx'
import type { SignedOutNotice } from '../auth/AuthContext.ts'
import { useAuth } from '../auth/useAuth.ts'
import { ArrivalHeading } from './ArrivalHeading.tsx'
import { ErrorMessage } from './ErrorMessage.tsx'
import { messageFor } from './errorText.ts'

const NOTICES: Record<SignedOutNotice, string> = {
  password_changed: 'Password changed. Sign in again with your new password.',
}

export function LoginForm() {
  const { login, state } = useAuth()
  // "Forgot password?" shows the request form in this one's place, and "Back to sign in" brings this back.
  const [forgot, setForgot] = useState(false)
  const [returned, setReturned] = useState(false)
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

  if (forgot) {
    return (
      <ForgotPasswordForm
        initialEmail={email.trim()}
        onBack={() => {
          setForgot(false)
          setReturned(true)
        }}
      />
    )
  }

  const notice = state.status === 'unauthenticated' ? state.notice : undefined

  return (
    <form className="auth-form" onSubmit={handleSubmit} aria-labelledby="login-heading">
      <ArrivalHeading id="login-heading" arrive={returned}>
        Sign in
      </ArrivalHeading>

      {/* Why the person is here, when this app signed them out itself. */}
      {notice !== undefined && (
        <p className="notice" role="status">
          {NOTICES[notice]}
        </p>
      )}

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
      <div className="form-actions">
        <button type="submit" aria-disabled={submitting}>
          {submitting ? 'Signing in…' : 'Sign in'}
        </button>
        <button
          type="button"
          className="button-link"
          onClick={() => {
            // The password is not carried to the other form, or kept for the way back.
            setPassword('')
            setError(null)
            setForgot(true)
          }}
        >
          Forgot password?
        </button>
      </div>
    </form>
  )
}
