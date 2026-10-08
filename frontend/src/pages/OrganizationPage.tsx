import { Link, useOutletContext } from 'react-router'

import type { OrganizationDetail, SorterSummary } from '../api/organizations.ts'
import { Breadcrumb } from '../components/Breadcrumb.tsx'
import { PageHeading } from '../components/PageHeading.tsx'
import { canManageMembers, MEMBERS_PAGE_NAME } from '../members/memberText.ts'
import { ORGANIZATIONS_PATH, organizationMembersPath, organizationReportsPath, sorterPath } from '../router/paths.ts'
import { sorterStatusLabel } from './labels.ts'

function SorterRow({ orgSlug, sorter }: { orgSlug: string; sorter: SorterSummary }) {
  // Where the machine is, then anything about its state worth saying. Words, not colour.
  const details = [sorter.host_branch.name, sorterStatusLabel(sorter.status)].filter((detail) => detail !== null)
  return (
    <li>
      <Link className="nav-list-link" to={sorterPath(orgSlug, sorter.slug)}>
        {sorter.name}
      </Link>
      <span className="nav-list-meta">{details.join(' · ')}</span>
    </li>
  )
}

/**
 * /organizations/:orgSlug -- the organization and the sorting machines it
 * runs SortView on. Only the sorters the API returns are listed: a branch
 * with no sorter, and a place a sorter routes items to, are not machines and
 * are not here.
 */
export function OrganizationPage() {
  const organization = useOutletContext<OrganizationDetail>()

  return (
    <>
      <Breadcrumb trail={[{ to: ORGANIZATIONS_PATH, label: 'Organizations' }]} current={organization.name} />

      <PageHeading>{organization.name}</PageHeading>

      {/* The organization's own reports -- every machine together. Each machine's reports are under that machine. */}
      <p className="page-links">
        <Link to={organizationReportsPath(organization.slug)}>Organization Reports</Link>
        {/* Offered to the organization's owners and admins only. The API decides who may actually use it. */}
        {canManageMembers(organization.role) && <Link to={organizationMembersPath(organization.slug)}>{MEMBERS_PAGE_NAME}</Link>}
      </p>

      <h3 id="sorters-heading">Sorting machines</h3>
      {organization.sorters.length === 0 ? (
        <p>No sorting machines are registered for this organization yet.</p>
      ) : (
        <ul className="nav-list" aria-labelledby="sorters-heading">
          {organization.sorters.map((sorter) => (
            <SorterRow key={sorter.slug} orgSlug={organization.slug} sorter={sorter} />
          ))}
        </ul>
      )}
    </>
  )
}
