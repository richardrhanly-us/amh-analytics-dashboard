import { Navigate, useOutletContext, useParams } from 'react-router'

import type { OrganizationDetail } from '../api/organizations.ts'
import { sorterPath } from '../router/paths.ts'
import { NotFoundPage } from './NotFoundPage.tsx'

/**
 * /organizations/:orgSlug/branches/:branchSlug -- the address a sorter's
 * dashboard had before sorters had addresses of their own.
 *
 * If a sorter is hosted at that branch, this replaces the address with the
 * sorter's. If none is -- the branch has no sorter, is only somewhere items
 * are routed to, or does not exist -- it is the same "not found" as any other
 * address, and nothing is asked of the API. There is no dashboard here.
 */
export function LegacyBranchRedirect() {
  const organization = useOutletContext<OrganizationDetail>()
  const { branchSlug = '' } = useParams()
  const sorter = organization.sorters.find((candidate) => candidate.host_branch.slug === branchSlug)

  if (sorter === undefined) {
    return <NotFoundPage />
  }
  return <Navigate to={sorterPath(organization.slug, sorter.slug)} replace />
}
