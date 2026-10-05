import { useState } from 'react'

import type { User } from '../api/auth.ts'
import { useAuth } from '../auth/useAuth.ts'
import { ErrorMessage } from './ErrorMessage.tsx'
import { messageFor } from './errorText.ts'

export function AuthenticatedShell({ user }: { user: User }) {
  const { logout } = useAuth()
  const [signingOut, setSigningOut] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function handleLogout() {
    if (signingOut) {
      return
    }
    setSigningOut(true)
    setError(null)
    try {
      await logout()
      // Signed out: this component is about to be replaced by the sign-in form.
    } catch (caught) {
      // Still signed in. Say so and let the person try again.
      setError(messageFor(caught))
      setSigningOut(false)
    }
  }

  const name = user.full_name.trim()

  return (
    <section className="auth-shell" aria-labelledby="signed-in-heading">
      <h2 id="signed-in-heading">Signed in</h2>
      <p className="identity">
        {name !== '' && <span className="identity-name">{name}</span>}
        <span className="identity-email">{user.email}</span>
      </p>

      <p>Dashboard migration in progress.</p>

      <ErrorMessage message={error} />

      <button type="button" onClick={handleLogout} disabled={signingOut}>
        {signingOut ? 'Signing out…' : 'Sign out'}
      </button>
    </section>
  )
}
