import { useOutletContext } from 'react-router'

import type { OrganizationDetail } from '../api/organizations.ts'
import { Breadcrumb } from '../components/Breadcrumb.tsx'
import { PageHeading } from '../components/PageHeading.tsx'
import { OrganizationReports } from '../reports/OrganizationReports.tsx'
import { ORGANIZATIONS_PATH, organizationPath } from '../router/paths.ts'

/**
 * /organizations/:orgSlug/reports -- the organization's reports over a range
 * of days: all of its sorting machines together, and side by side.
 *
 * It belongs to the organization, not to any one sorter; a sorter's own
 * reports are under that sorter.
 */
export function OrganizationReportsPage() {
  const organization = useOutletContext<OrganizationDetail>()

  return (
    // Keyed by organization, so another organization starts as a new set of reports, with nothing carried over.
    <OrganizationReports
      key={organization.slug}
      organization={organization}
      header={
        <>
          <Breadcrumb
            trail={[
              { to: ORGANIZATIONS_PATH, label: 'Organizations' },
              { to: organizationPath(organization.slug), label: organization.name },
            ]}
            current="Reports"
          />
          <PageHeading>{organization.name}</PageHeading>
          <h3 id="organization-reports-heading">Organization Reports</h3>
          <p className="page-context">What all of this organization&rsquo;s sorting machines processed, together</p>
        </>
      }
    />
  )
}
