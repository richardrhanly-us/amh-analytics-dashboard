import { useQuery } from '@tanstack/react-query'
import { Outlet, useParams } from 'react-router'

import { isApiError } from '../api/client.ts'
import { getOrganization, organizationKey } from '../api/organizations.ts'
import { messageFor } from '../components/errorText.ts'
import { LoadFailure } from '../components/LoadFailure.tsx'
import { NotFoundPage } from './NotFoundPage.tsx'

/**
 * Everything under /organizations/:orgSlug. Loads the organization once for
 * the slug in the address and hands it to the page below through the outlet
 * (read it with `useOutletContext<OrganizationDetail>()`), so moving between
 * an organization and its branches asks the API nothing new.
 *
 * The organization the API returns is the whole of what the user may see
 * there: a page below it shows a branch only if it is in that answer.
 *
 * It is kept in the session's query cache under `organizationKey`, so that a
 * page which changes what the answer depends on -- the signed-in person's own
 * role -- can have it asked for again. While it is being asked for again the
 * page below stays as it is, and then shows what the API then says: the
 * organization shown is always one the API returned, never one worked out here.
 */
export function OrganizationLayout() {
  const { orgSlug = '' } = useParams()
  const query = useQuery({
    queryKey: organizationKey(orgSlug),
    queryFn: ({ signal }) => getOrganization(orgSlug, signal),
    // Asked once; a failure is shown at once, with the way to try again. Nothing is kept after leaving the
    // organization: coming back asks afresh.
    retry: false,
    gcTime: 0,
    // Asked when a page says so and at no other time: not because the connection came back. And asked even when
    // the browser thinks it is offline, so that a load which cannot succeed fails and says so, with the way to
    // try again, and does not wait in silence.
    refetchOnReconnect: false,
    networkMode: 'always',
  })

  // 404 or 403: it does not exist, or is not this user's to see -- now, whatever was answered before.
  if (isApiError(query.error) && (query.error.status === 404 || query.error.status === 403)) {
    return <NotFoundPage />
  }
  if (query.data !== undefined) {
    return (
      <>
        {query.data.access_mode === 'read_only' && (
          <p className="notice" role="note">
            This organization&rsquo;s account is currently suspended. Historical dashboard data remains available.
          </p>
        )}
        <Outlet context={query.data} />
      </>
    )
  }
  if (query.isError && !query.isFetching) {
    return <LoadFailure message={messageFor(query.error)} onRetry={() => void query.refetch()} />
  }
  return <p role="status">Loading organization…</p>
}
