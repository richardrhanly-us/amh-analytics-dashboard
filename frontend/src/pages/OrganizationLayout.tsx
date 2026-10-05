import { Outlet, useParams } from 'react-router'

import { getOrganization } from '../api/organizations.ts'
import { LoadFailure } from '../components/LoadFailure.tsx'
import { useResource } from '../hooks/useResource.ts'
import { NotFoundPage } from './NotFoundPage.tsx'

/**
 * Everything under /organizations/:orgSlug. Loads the organization once for
 * the slug in the address and hands it to the page below through the outlet
 * (read it with `useOutletContext<OrganizationDetail>()`), so moving between
 * an organization and its branches asks the API nothing new.
 *
 * The organization the API returns is the whole of what the user may see
 * there: a page below it shows a branch only if it is in that answer.
 */
export function OrganizationLayout() {
  const { orgSlug = '' } = useParams()
  const { resource, retry } = useResource(orgSlug, getOrganization)

  switch (resource.status) {
    case 'loading':
      return <p role="status">Loading organization…</p>
    case 'unavailable':
      return <NotFoundPage />
    case 'error':
      return <LoadFailure message={resource.message} onRetry={retry} />
    case 'ready':
      return (
        <>
          {resource.data.access_mode === 'read_only' && (
            <p className="notice" role="note">
              This organization&rsquo;s account is currently suspended. Historical dashboard data remains available.
            </p>
          )}
          <Outlet context={resource.data} />
        </>
      )
  }
}
