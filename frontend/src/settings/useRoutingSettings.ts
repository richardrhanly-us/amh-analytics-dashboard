import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { isApiError } from '../api/client.ts'
import { getRoutingSettings, putRoutingSettings, type RoutingSettings } from '../api/routingSettings.ts'
import { useAuth } from '../auth/useAuth.ts'
import { messageFor } from '../components/errorText.ts'

/**
 * What a read of the organization's routing has to show:
 *
 *   loading     the answer has not arrived
 *   ready       the API's answer -- or, after a save, what the API then stored
 *   forbidden   403: this person may not manage it (any longer). Nothing of it is shown.
 *   not_found   404: the organization is not this person's to see (any longer)
 *   error       anything else; `message` is safe to show and the read can be tried again
 */
export type StoredRouting =
  | { status: 'loading' }
  | { status: 'ready'; data: RoutingSettings }
  | { status: 'forbidden' }
  | { status: 'not_found' }
  | { status: 'error'; message: string }

const routingKey = (orgSlug: string) => ['routing-settings', orgSlug] as const

/**
 * The organization's routing, and the one way to change it.
 *
 * It is read once when the page opens and is not read again by itself: not
 * when the window is returned to, not when the connection comes back and not
 * on a timer. Someone may be part-way through editing it, and what they are
 * editing from must not change underneath them. Nothing is kept once the
 * page is left, so the next visit reads it afresh.
 *
 * `save` replaces the whole of it and resolves with what the API then
 * stored, which becomes what `routing` holds: the page shows the API's
 * answer, never what was typed. It rejects with the failure, for the form to
 * show; a 401 means the session is gone, exactly as for a read. A save is
 * never tried again by itself.
 */
export function useRoutingSettings(orgSlug: string) {
  const queryClient = useQueryClient()
  const { sessionExpired } = useAuth()

  const query = useQuery({
    queryKey: routingKey(orgSlug),
    queryFn: ({ signal }) => getRoutingSettings(orgSlug, signal),
    gcTime: 0,
    refetchOnReconnect: false,
  })

  const mutation = useMutation({
    mutationFn: (routing: RoutingSettings) => putRoutingSettings(orgSlug, routing),
    onSuccess: (stored) => {
      queryClient.setQueryData(routingKey(orgSlug), stored)
    },
    onError: (error: unknown) => {
      if (isApiError(error) && error.status === 401) {
        sessionExpired()
      }
    },
  })

  let routing: StoredRouting
  if (isApiError(query.error) && query.error.status === 403) {
    routing = { status: 'forbidden' }
  } else if (isApiError(query.error) && query.error.status === 404) {
    routing = { status: 'not_found' }
  } else if (query.data !== undefined) {
    routing = { status: 'ready', data: query.data }
  } else if (query.isError && !query.isFetching) {
    routing = { status: 'error', message: messageFor(query.error) }
  } else {
    routing = { status: 'loading' }
  }

  return {
    routing,
    retry: () => void query.refetch({ cancelRefetch: false }),
    save: (wanted: RoutingSettings) => mutation.mutateAsync(wanted),
    saving: mutation.isPending,
  }
}
