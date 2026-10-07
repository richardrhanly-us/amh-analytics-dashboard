import { createContext } from 'react'

import type { User } from '../api/auth.ts'

/**
 * Where the app stands with the API's session.
 *
 *   restoring        asking the API whether a session exists (first load, or a retry)
 *   unauthenticated  the API said nobody is signed in. `notice` says why, when this app itself signed the
 *                    person out for a reason they should be told: it is shown above the sign-in form.
 *   authenticated    the API returned a user
 *   error            the question could not be answered -- NOT the same as signed out
 */
export type AuthState =
  | { status: 'restoring' }
  | { status: 'unauthenticated'; notice?: SignedOutNotice }
  | { status: 'authenticated'; user: User }
  | { status: 'error'; message: string }

/** Why the person is looking at the sign-in form, when it is not simply that they have yet to sign in. */
export type SignedOutNotice = 'password_changed'

export interface AuthContextValue {
  state: AuthState
  /** Signs in. Resolves once authenticated; rejects with an ApiError and leaves the state unauthenticated. */
  login: (email: string, password: string) => Promise<void>
  /** Signs out. Resolves once unauthenticated; rejects with an ApiError and leaves the user signed in. */
  logout: () => Promise<void>
  /** Asks the API again after a failed restore. */
  retryRestore: () => void
  /**
   * For when the API answers 401 to a signed-in user, or a completed password reset has ended every session
   * of the account: the session is gone. Drops the user and returns to unauthenticated, with no request --
   * there is no session left to end and none to refresh.
   */
  sessionExpired: () => void
  /**
   * For when the person has just changed their password: the API ended every session they had, this one
   * included. Returns to unauthenticated with no request, and with a notice saying to sign in again.
   */
  passwordChanged: () => void
  /** The signed-in person changed their name: what is shown of them follows. Does nothing when signed out. */
  updateUserName: (fullName: string) => void
}

export const AuthContext = createContext<AuthContextValue | null>(null)
