import { useState, type FormEvent } from 'react'

import { requestPasswordReset } from '../api/account.ts'
import { ArrivalHeading } from '../components/ArrivalHeading.tsx'
import { ErrorMessage } from '../components/ErrorMessage.tsx'
import { messageFor } from '../components/errorText.ts'

// The API's own sentence. It is the same for an address with an account and one without, and so is this.
const REQUESTED = 'If an active account exists for that email address, password reset instructions will be sent.'

/**
 * Asks for a password reset link by email. Reached from the sign-in form and
 * shown in its place.
 *
 * It never says whether an address has an account: the API answers every
 * address the same way, and what is shown afterwards is one fixed sentence
 * that does not repeat the address. A failure is about the request -- too
 * many, or the service being unavailable -- and never about the address.
 */
export function ForgotPasswordForm({ initialEmail, onBack }: { initialEmail: string; onBack: () => void }) {
  const [email, setEmail] = useState(initialEmail)
  const [sending, setSending] = useState(false)
  const [requested, setRequested] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (sending) {
      return
    }
    const trimmed = email.trim()
    if (trimmed === '') {
      return
    }

    setSending(true)
    setError(null)
    try {
      await requestPasswordReset(trimmed)
      setRequested(true)
    } catch (caught) {
      setError(messageFor(caught))
    } finally {
      setSending(false)
    }
  }

  if (requested) {
    return (
      <section className="auth-form" aria-labelledby="forgot-heading">
        <ArrivalHeading id="forgot-heading" arrive>
          Check your email
        </ArrivalHeading>
        <p role="status">{REQUESTED}</p>
        <p className="quiet">The link in the email works once and expires after 30 minutes.</p>
        <button type="button" className="button-secondary" onClick={onBack}>
          Back to sign in
        </button>
      </section>
    )
  }

  return (
    <form className="auth-form" onSubmit={handleSubmit} aria-labelledby="forgot-heading">
      <ArrivalHeading id="forgot-heading" arrive>
        Reset your password
      </ArrivalHeading>
      <p>Enter the email address you sign in with and we will send a link to reset your password.</p>

      <ErrorMessage message={error} />

      <div className="field">
        <label htmlFor="forgot-email">Email</label>
        <input
          id="forgot-email"
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

      <div className="form-actions">
        {/* Unavailable while sending, but not `disabled`: a disabled button drops keyboard focus. */}
        <button type="submit" aria-disabled={sending}>
          {sending ? 'Sending…' : 'Send reset link'}
        </button>
        <button type="button" className="button-secondary" onClick={onBack}>
          Back to sign in
        </button>
      </div>
    </form>
  )
}
