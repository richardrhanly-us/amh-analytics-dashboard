import { useOutletContext } from 'react-router'

import type { OrganizationDetail } from '../api/organizations.ts'
import { Breadcrumb } from '../components/Breadcrumb.tsx'
import { PageHeading } from '../components/PageHeading.tsx'
import { OrganizationReports } from '../reports/OrganizationReports.tsx'
import { ReportNav } from '../reports/ReportNav.tsx'
import { SortViewValue } from '../reports/SortViewValue.tsx'
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
 * Value is the organization's SortView Value report (reports/SortViewValue):
 * an estimate from assumptions the organization entered, with no range of
 * days. It is for the organization's owners and admins: nobody else is
 * offered it, and someone else who opens its address is told so and nothing
 * is asked of the API on their behalf.
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
        {value ? (
          // Keyed by organization, so another organization starts as a new report, with nothing carried over.
          <SortViewValue key={organization.slug} organization={organization} />
        ) : (
          <p className="notice" role="note">
            SortView Value is available to this organization&rsquo;s owners and administrators.
          </p>
        )}
      </>
    )
  }
  // Keyed by organization, so another organization starts as a new set of reports, with nothing carried over.
  return <OrganizationReports key={organization.slug} organization={organization} header={header} />
}
