import { Link, useOutletContext } from 'react-router'

import type { OrganizationDetail } from '../api/organizations.ts'
import { Breadcrumb } from '../components/Breadcrumb.tsx'
import { PageHeading } from '../components/PageHeading.tsx'
import { branchPath, ORGANIZATIONS_PATH } from '../router/paths.ts'

/** /organizations/:orgSlug -- the organization and its branches to choose from. */
export function OrganizationPage() {
  const organization = useOutletContext<OrganizationDetail>()

  return (
    <>
      <Breadcrumb trail={[{ to: ORGANIZATIONS_PATH, label: 'Organizations' }]} current={organization.name} />

      <PageHeading>{organization.name}</PageHeading>

      <h3 id="branches-heading">Branches</h3>
      {organization.branches.length === 0 ? (
        <p>This organization has no active branches.</p>
      ) : (
        <ul className="nav-list" aria-labelledby="branches-heading">
          {organization.branches.map((branch) => (
            <li key={branch.slug}>
              <Link className="nav-list-link" to={branchPath(organization.slug, branch.slug)}>
                {branch.name}
              </Link>
              {branch.is_primary && <span className="nav-list-meta">Primary branch</span>}
            </li>
          ))}
        </ul>
      )}
    </>
  )
}
