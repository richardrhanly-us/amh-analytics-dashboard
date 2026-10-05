import { Link, useOutletContext, useParams } from 'react-router'

import type { OrganizationDetail } from '../api/organizations.ts'
import { LiveToday } from '../liveToday/LiveToday.tsx'
import { ORGANIZATIONS_PATH, organizationPath } from '../router/paths.ts'
import { NotFoundPage } from './NotFoundPage.tsx'

/**
 * /organizations/:orgSlug/branches/:branchSlug -- the selected branch and its
 * Live Today dashboard. The branch is whichever one of the organization's
 * returned branches has this slug; if none does the page is the same "not
 * found" as for anything else the user cannot see, and no live data is asked
 * for.
 */
export function BranchPage() {
  const organization = useOutletContext<OrganizationDetail>()
  const { branchSlug = '' } = useParams()
  const branch = organization.branches.find((candidate) => candidate.slug === branchSlug)

  if (branch === undefined) {
    return <NotFoundPage />
  }

  return (
    <section aria-labelledby="branch-heading">
      <nav aria-label="Breadcrumb" className="breadcrumb">
        <Link to={ORGANIZATIONS_PATH}>Organizations</Link>
        <span aria-hidden="true"> / </span>
        <Link to={organizationPath(organization.slug)}>{organization.name}</Link>
      </nav>

      <h2 id="branch-heading">{branch.name}</h2>
      {/* Keyed by branch, so another branch starts as a new dashboard: running, and with nothing carried over. */}
      <LiveToday key={`${organization.slug}/${branch.slug}`} orgSlug={organization.slug} branchSlug={branch.slug} />
    </section>
  )
}
