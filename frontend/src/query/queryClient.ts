import { QueryCache, QueryClient } from '@tanstack/react-query'

import { isApiError } from '../api/client.ts'

/** How many times a failed read is tried again before it is reported as failed. */
export const MAX_RETRIES = 2

/**
 * Whether a failed read is worth trying again by itself.
 *
 * Only for a failure that may pass on its own: no response at all (a network
 * blip) or a 5xx. Never for an answer -- 401 (the session ended), 403, 404
 * (not there, or not available to this user), 422 (the request itself is
 * wrong) -- which would be the same answer again. And never for 429: asking
 * again is what the server just said to stop doing.
 */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  if (failureCount >= MAX_RETRIES || !isApiError(error)) {
    return false
  }
  return error.status === null || error.status >= 500
}

/** 1 second, then 2: long enough to outlast a blip, short enough that a real outage is reported promptly. */
export function retryDelay(attemptIndex: number): number {
  return Math.min(1000 * 2 ** attemptIndex, 4000)
}

/**
 * The query client for one signed-in session. It lives in memory only --
 * nothing is persisted -- and is discarded at sign-out, so no user is ever
 * shown what was loaded for the one before.
 *
 * `onSessionExpired` is called when any read is answered 401.
 */
export function createQueryClient(onSessionExpired: () => void): QueryClient {
  return new QueryClient({
    queryCache: new QueryCache({
      onError: (error) => {
        if (isApiError(error) && error.status === 401) {
          onSessionExpired()
        }
      },
    }),
    defaultOptions: {
      queries: {
        retry: shouldRetry,
        retryDelay,
        // Refreshing is on a timer and a button, both visible on the page. Nothing refetches merely because a
        // window regained focus.
        refetchOnWindowFocus: false,
      },
      // Every request this app makes through a query is a read. Nothing that changes data is ever retried.
      mutations: { retry: false },
    },
  })
}
