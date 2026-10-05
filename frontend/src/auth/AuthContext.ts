import { createContext } from 'react'

import type { User } from '../api/auth.ts'

/**
 * Where the app stands with the API's session.
 *
 *   restoring        asking the API whether a session exists (first load, or a retry)
 *   unauthenticated  the API said nobody is signed in
 *   authenticated    the API returned a user
 *   error            the question could not be answered -- NOT the same as signed out
 */
export type AuthState =
  | { status: 'restoring' }
  | { status: 'unauthenticated' }
  | { status: 'authenticated'; user: User }
  | { status: 'error'; message: string }

export interface AuthContextValue {
  state: AuthState
  /** Signs in. Resolves once authenticated; rejects with an ApiError and leaves the state unauthenticated. */
  login: (email: string, password: string) => Promise<void>
  /** Signs out. Resolves once unauthenticated; rejects with an ApiError and leaves the user signed in. */
  logout: () => Promise<void>
  /** Asks the API again after a failed restore. */
  retryRestore: () => void
}

export const AuthContext = createContext<AuthContextValue | null>(null)
