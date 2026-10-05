import { useCallback, useEffect, useState } from 'react'

import { isApiError } from '../api/client.ts'
import { useAuth } from '../auth/useAuth.ts'
import { messageFor } from '../components/errorText.ts'

/**
 * One thing loaded from the API for the page being shown.
 *
 *   loading      the request is in flight
 *   ready        the API returned it
 *   unavailable  the API said 404 or 403: it does not exist, or is not this user's to see
 *   error        anything else; `message` is safe to show and the load can be retried
 */
export type Resource<T> =
  | { status: 'loading' }
  | { status: 'ready'; data: T }
  | { status: 'unavailable' }
  | { status: 'error'; message: string }

const LOADING = { status: 'loading' } as const

/**
 * Loads `load(key)` once for each `key`, and again on `retry`. It does not
 * load again because a component re-rendered.
 *
 * A result is remembered together with the request it answers and is only
 * returned while that is still the current request. So when `key` changes the
 * state is `loading` at once, and a late answer for the previous key -- whose
 * request is also cancelled -- can never be shown under the new one.
 *
 * A 401 means the session ended: the auth state is told, and the app returns
 * to the sign-in form.
 *
 * `load` must be a stable function (declare it outside the component).
 */
export function useResource<T>(
  key: string,
  load: (key: string, signal: AbortSignal) => Promise<T>,
): { resource: Resource<T>; retry: () => void } {
  const { sessionExpired } = useAuth()
  const [attempt, setAttempt] = useState(0)
  const [settled, setSettled] = useState<{ requestId: string; resource: Resource<T> } | null>(null)
  const requestId = `${attempt}:${key}`

  useEffect(() => {
    const controller = new AbortController()
    let current = true

    load(key, controller.signal).then(
      (data) => {
        if (current) {
          setSettled({ requestId, resource: { status: 'ready', data } })
        }
      },
      (error: unknown) => {
        if (!current) {
          return
        }
        if (isApiError(error) && error.status === 401) {
          sessionExpired()
        } else if (isApiError(error) && (error.status === 404 || error.status === 403)) {
          setSettled({ requestId, resource: { status: 'unavailable' } })
        } else {
          setSettled({ requestId, resource: { status: 'error', message: messageFor(error) } })
        }
      },
    )

    return () => {
      current = false
      controller.abort()
    }
  }, [key, load, requestId, sessionExpired])

  const retry = useCallback(() => setAttempt((count) => count + 1), [])

  return { resource: settled?.requestId === requestId ? settled.resource : LOADING, retry }
}
