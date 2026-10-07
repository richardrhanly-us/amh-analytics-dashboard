import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

import * as authApi from '../api/auth.ts'
import { isApiError } from '../api/client.ts'
import { AuthContext, type AuthContextValue, type AuthState } from './AuthContext.ts'

const RESTORE_FAILED = 'We could not check whether you are signed in. Please try again.'

/**
 * Holds the signed-in user, and nothing else about the session: the session
 * itself is an HttpOnly cookie this code never sees. The user is kept in
 * memory only, so a reload asks the API again.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: 'restoring' })
  // In development React StrictMode runs an effect, undoes it and runs it again. This keeps "ask the API once"
  // true there as well: the first run starts the request and the repeat finds it already started.
  const restoreStarted = useRef(false)

  const restore = useCallback(() => {
    setState({ status: 'restoring' })
    authApi.getSession().then(
      (user) => setState({ status: 'authenticated', user }),
      (error: unknown) => {
        if (isApiError(error) && error.status === 401) {
          setState({ status: 'unauthenticated' })
        } else {
          // Not an answer to "is anyone signed in?": say so, rather than showing a login form to someone who
          // may well be signed in.
          setState({ status: 'error', message: isApiError(error) ? error.message : RESTORE_FAILED })
        }
      },
    )
  }, [])

  useEffect(() => {
    if (restoreStarted.current) {
      return
    }
    restoreStarted.current = true
    restore()
  }, [restore])

  const login = useCallback(async (email: string, password: string) => {
    // The API's answer IS the user: there is no second request to learn it again.
    const user = await authApi.login(email, password)
    setState({ status: 'authenticated', user })
  }, [])

  const logout = useCallback(async () => {
    // The local state only changes once the server has ended the session. If this rejects, the user is still
    // signed in on the server and stays signed in here.
    await authApi.logout()
    setState({ status: 'unauthenticated' })
  }, [])

  const sessionExpired = useCallback(() => {
    setState({ status: 'unauthenticated' })
  }, [])

  const passwordChanged = useCallback(() => {
    setState({ status: 'unauthenticated', notice: 'password_changed' })
  }, [])

  const updateUserName = useCallback((fullName: string) => {
    setState((current) => (current.status === 'authenticated' ? { ...current, user: { ...current.user, full_name: fullName } } : current))
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({ state, login, logout, retryRestore: restore, sessionExpired, passwordChanged, updateUserName }),
    [state, login, logout, restore, sessionExpired, passwordChanged, updateUserName],
  )

  return <AuthContext value={value}>{children}</AuthContext>
}
