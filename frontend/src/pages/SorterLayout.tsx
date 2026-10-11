import { NavLink, Outlet, useLocation, useMatch, useOutletContext, useParams } from 'react-router'

import type { OrganizationDetail, SorterSummary } from '../api/organizations.ts'
import { Breadcrumb } from '../components/Breadcrumb.tsx'
import { PageHeading } from '../components/PageHeading.tsx'
import { ORGANIZATIONS_PATH, organizationPath, sorterPath, sorterReportsPath } from '../router/paths.ts'
import { NotFoundPage } from './NotFoundPage.tsx'

/** What a page under a sorter is given: the organization and the sorter it is about. */
export interface SorterContext {
  organization: OrganizationDetail
  sorter: SorterSummary
}

/**
 * Everything under /organizations/:orgSlug/sorters/:sorterSlug -- one of the
 * organization's sorting machines, and the two ways of looking at it: Live
 * Today and Reports.
 *
 * The sorter is whichever one of the organization's returned sorters has this
 * slug; if none does the page is the same "not found" as for anything else
 * the user cannot see, and nothing is asked of the API. A place the sorter
 * merely routes items to is not a sorter and has no page here.
 *
 * The page below reads the sorter with `useOutletContext<SorterContext>()`.
 */
export function SorterLayout() {
  const organization = useOutletContext<OrganizationDetail>()
  const { sorterSlug = '' } = useParams()
  // Within this sorter's Reports the address's query is the range being reported on, and the Reports link keeps it.
  // From Live Today there is no range to keep, and none is made up.
  const { search } = useLocation()
  const inReports = useMatch('/organizations/:orgSlug/sorters/:sorterSlug/reports/*') !== null
  const sorter = organization.sorters.find((candidate) => candidate.slug === sorterSlug)

  if (sorter === undefined) {
    return <NotFoundPage />
  }

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

      {/* The two views of this sorter. Links, because each has an address; the current one says so. */}
      <nav aria-label="Sorter views" className="view-nav">
        <ul>
          <li>
            <NavLink to={sorterPath(organization.slug, sorter.slug)} end>
              Live Today
            </NavLink>
          </li>
          <li>
            <NavLink to={{ pathname: sorterReportsPath(organization.slug, sorter.slug), search: inReports ? search : '' }}>Reports</NavLink>
          </li>
        </ul>
      </nav>

      <Outlet context={{ organization, sorter } satisfies SorterContext} />
    </>
  )
}
