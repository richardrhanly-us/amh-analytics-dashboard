import { useState } from 'react'
import { useNavigate } from 'react-router'

import type { User } from '../api/auth.ts'
import { useAuth } from '../auth/useAuth.ts'
import { ErrorMessage } from './ErrorMessage.tsx'
import { messageFor } from './errorText.ts'

/** Who is signed in, and the way out. Shown in the header on every signed-in page. */
export function UserMenu({ user }: { user: User }) {
  const { logout } = useAuth()
  const navigate = useNavigate()
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
      // Signed out on purpose: leave the page too, so whoever signs in next on this browser starts at the
      // beginning and not at this user's organization or branch. (A session that merely expired keeps its
      // address, so signing back in returns to the same page.)
      navigate('/', { replace: true })
    } catch (caught) {
      // Still signed in. Say so and let the person try again.
      setError(messageFor(caught))
      setSigningOut(false)
    }
  }

  const name = user.full_name.trim()

  return (
    <div className="user-menu">
      <p className="identity">
        {name !== '' && <span className="identity-name">{name}</span>}
        <span className="identity-email">{user.email}</span>
      </p>

      <button type="button" onClick={handleLogout} disabled={signingOut}>
        {signingOut ? 'Signing out…' : 'Sign out'}
      </button>

      <ErrorMessage message={error} />
    </div>
  )
}
