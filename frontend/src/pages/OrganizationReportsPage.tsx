import { useOutletContext } from 'react-router'

import type { OrganizationDetail } from '../api/organizations.ts'
import { Breadcrumb } from '../components/Breadcrumb.tsx'
import { PageHeading } from '../components/PageHeading.tsx'
import { OrganizationReports } from '../reports/OrganizationReports.tsx'
import { ReportNav } from '../reports/ReportNav.tsx'
import { ORGANIZATIONS_PATH, organizationPath, organizationReportViewPath, type OrganizationReportView } from '../router/paths.ts'
import { canManageOrganization } from '../settings/settingsText.ts'

/**
 * /organizations/:orgSlug/reports/operations and .../reports/value -- the
 * organization's reports, a view at a time. `view` is the one in the address.
 *
 * Operations is its reports over a range of days: all of its sorting machines
 * together, and side by side. It belongs to the organization, not to any one
 * sorter; a sorter's own reports are under that sorter.
 *
 * Value is where SortView Value will be reported. NOT BUILT YET: it has an
 * address and says so, and nothing is read, worked out or asked of the API
 * for it. It is for the organization's owners and admins: nobody else is
 * offered it, and someone else who opens its address is told so.
 */
export function OrganizationReportsPage({ view }: { view: OrganizationReportView }) {
  const organization = useOutletContext<OrganizationDetail>()
  const value = canManageOrganization(organization.role)

  const header = (
    <>
      <Breadcrumb
        trail={[
          { to: ORGANIZATIONS_PATH, label: 'Organizations' },
          { to: organizationPath(organization.slug), label: organization.name },
        ]}
        current="Reports"
      />
      <PageHeading>{organization.name}</PageHeading>
      {/* `report-view`: what marks this page as a report for the print styles (index.css), and nothing else. */}
      <h3 id="organization-reports-heading" className="report-view">
        Organization Reports
      </h3>
      <p className="page-context">What all of this organization&rsquo;s sorting machines processed, together</p>
      <ReportNav
        views={[
          { to: organizationReportViewPath(organization.slug, 'operations'), label: 'Operations' },
          ...(value ? [{ to: organizationReportViewPath(organization.slug, 'value'), label: 'Value' }] : []),
        ]}
      />
    </>
  )

  if (view === 'value') {
    return (
      <>
        {header}
        <p className="notice" role="note">
          {value
            ? 'SortView Value reporting will be available in a later stage.'
            : 'SortView Value is available to this organization’s owners and administrators.'}
        </p>
      </>
    )
  }
  // Keyed by organization, so another organization starts as a new set of reports, with nothing carried over.
  return <OrganizationReports key={organization.slug} organization={organization} header={header} />
}
