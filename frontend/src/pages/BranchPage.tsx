import { useOutletContext, useParams } from 'react-router'

import type { OrganizationDetail } from '../api/organizations.ts'
import { Breadcrumb } from '../components/Breadcrumb.tsx'
import { PageHeading } from '../components/PageHeading.tsx'
import { LiveToday } from '../liveToday/LiveToday.tsx'
import { ORGANIZATIONS_PATH, organizationPath } from '../router/paths.ts'
import { NotFoundPage } from './NotFoundPage.tsx'

/**
 * /organizations/:orgSlug/branches/:branchSlug -- the selected branch and its
 * Live Today dashboard. Operationally a branch here is a sorter site: the
 * dashboard covers everything that site's sorter processed, including items
 * it routed on to other destinations. The branch is whichever one of the organization's
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
    <>
      <Breadcrumb
        trail={[
          { to: ORGANIZATIONS_PATH, label: 'Organizations' },
          { to: organizationPath(organization.slug), label: organization.name },
        ]}
        current={branch.name}
      />

      <PageHeading>{branch.name}</PageHeading>
      {/* A branch page is the dashboard of the sorter at that site: everything it processed, wherever it went. */}
      <p className="page-context">Live activity for this sorter site</p>
      {/* Keyed by branch, so another branch starts as a new dashboard: running, and with nothing carried over. */}
      <LiveToday key={`${organization.slug}/${branch.slug}`} orgSlug={organization.slug} branchSlug={branch.slug} />
    </>
  )
}
