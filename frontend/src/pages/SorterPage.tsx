import { useOutletContext, useParams } from 'react-router'

import type { OrganizationDetail } from '../api/organizations.ts'
import { Breadcrumb } from '../components/Breadcrumb.tsx'
import { PageHeading } from '../components/PageHeading.tsx'
import { LiveToday } from '../liveToday/LiveToday.tsx'
import { ORGANIZATIONS_PATH, organizationPath } from '../router/paths.ts'
import { sorterStatusLabel } from './labels.ts'
import { NotFoundPage } from './NotFoundPage.tsx'

/**
 * /organizations/:orgSlug/sorters/:sorterSlug -- one of the organization's
 * sorting machines and its Live Today dashboard.
 *
 * The sorter is whichever one of the organization's returned sorters has this
 * slug; if none does the page is the same "not found" as for anything else
 * the user cannot see, and no live data is asked for. A place the sorter
 * merely routes items to is not a sorter and has no page here.
 *
 * The dashboard's reads are addressed by the sorter's HOST BRANCH: that is
 * the scope its collector uploads under, and the only one the operational API
 * takes. Nothing the person sees depends on that.
 */
export function SorterPage() {
  const organization = useOutletContext<OrganizationDetail>()
  const { sorterSlug = '' } = useParams()
  const sorter = organization.sorters.find((candidate) => candidate.slug === sorterSlug)

  if (sorter === undefined) {
    return <NotFoundPage />
  }

  const status = sorterStatusLabel(sorter.status)

  return (
    <>
      <Breadcrumb
        trail={[
          { to: ORGANIZATIONS_PATH, label: 'Organizations' },
          { to: organizationPath(organization.slug), label: organization.name },
        ]}
        current={sorter.name}
      />

      <PageHeading>{sorter.name}</PageHeading>
      {/* Everything this sorter processed, wherever it sent it -- not the returns that belong to one branch. */}
      <p className="page-context">
        Live activity for this sorter, at {sorter.host_branch.name}
        {status !== null && ` · ${status}`}
      </p>
      {sorter.collector_count > 1 && (
        <p className="notice" role="note">
          {sorter.collector_count} collectors report for this site. Their figures are combined here and cannot be
          shown separately.
        </p>
      )}
      {/* Keyed by sorter, so another sorter starts as a new dashboard: running, and with nothing carried over. */}
      <LiveToday
        key={`${organization.slug}/${sorter.slug}`}
        orgSlug={organization.slug}
        branchSlug={sorter.host_branch.slug}
      />
    </>
  )
}
