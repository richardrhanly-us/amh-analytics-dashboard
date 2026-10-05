import { Link } from 'react-router'

import { listOrganizations, type OrganizationSummary } from '../api/organizations.ts'
import { LoadFailure } from '../components/LoadFailure.tsx'
import { useResource } from '../hooks/useResource.ts'
import { organizationPath } from '../router/paths.ts'
import { roleLabel } from './labels.ts'

const loadOrganizations = (_key: string, signal: AbortSignal) => listOrganizations(signal)

function OrganizationRow({ organization }: { organization: OrganizationSummary }) {
  const role = roleLabel(organization.role)
  const details = [role, organization.access_mode === 'read_only' ? 'Suspended (read-only)' : null].filter(
    (detail) => detail !== null,
  )
  return (
    <li>
      <Link className="nav-list-link" to={organizationPath(organization.slug)}>
        {organization.name}
      </Link>
      {details.length > 0 && <span className="nav-list-meta">{details.join(' · ')}</span>}
    </li>
  )
}

export function OrganizationsPage() {
  const { resource, retry } = useResource('', loadOrganizations)

  return (
    <section aria-labelledby="organizations-heading">
      <h2 id="organizations-heading">Organizations</h2>

      {resource.status === 'loading' && <p role="status">Loading organizations…</p>}

      {resource.status === 'error' && <LoadFailure message={resource.message} onRetry={retry} />}

      {/* The list itself is never "not found": a 403 or 404 here is a fault, and gets the same retry. */}
      {resource.status === 'unavailable' && (
        <LoadFailure message="Something went wrong. Please try again." onRetry={retry} />
      )}

      {resource.status === 'ready' &&
        (resource.data.length === 0 ? (
          <p>Your account does not have access to any organizations.</p>
        ) : (
          <ul className="nav-list">
            {resource.data.map((organization) => (
              <OrganizationRow key={organization.slug} organization={organization} />
            ))}
          </ul>
        ))}
    </section>
  )
}
