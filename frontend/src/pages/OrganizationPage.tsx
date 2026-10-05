import { Link, useOutletContext } from 'react-router'

import type { OrganizationDetail } from '../api/organizations.ts'
import { branchPath, ORGANIZATIONS_PATH } from '../router/paths.ts'

/** /organizations/:orgSlug -- the organization and its branches to choose from. */
export function OrganizationPage() {
  const organization = useOutletContext<OrganizationDetail>()

  return (
    <section aria-labelledby="organization-heading">
      <nav aria-label="Breadcrumb" className="breadcrumb">
        <Link to={ORGANIZATIONS_PATH}>Organizations</Link>
      </nav>

      <h2 id="organization-heading">{organization.name}</h2>

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
    </section>
  )
}
