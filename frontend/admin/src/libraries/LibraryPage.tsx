import { useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { Link, useParams } from 'react-router'

import { isApiError } from '../../../src/api/client.ts'
import { formatInstant } from '../../../src/time/productTime.ts'
import {
  branchStatusLabel,
  identityLabel,
  installationStatusLabel,
  libraryStatusLabel,
  onboardingLabel,
  pipelineStateLabel,
  subscriptionStatusLabel,
} from './labels.ts'
import { getLibrary, type LibraryDetail } from './librariesApi.ts'

const LOAD_FAILED = 'This library could not be loaded. Please try again.'

function Fact({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div>
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  )
}

function Overview({ library }: { library: LibraryDetail }) {
  const { primary_branch: branch, subscription } = library
  return (
    <>
      <section aria-labelledby="library-section-heading">
        <h3 id="library-section-heading">Library</h3>
        <dl className="account-facts">
          <Fact term="Slug">{library.slug}</Fact>
          <Fact term="Status">{libraryStatusLabel(library.status)}</Fact>
        </dl>
      </section>

      <section aria-labelledby="subscription-heading">
        <h3 id="subscription-heading">Subscription</h3>
        {subscription === null ? (
          <p>No subscription.</p>
        ) : (
          <dl className="account-facts">
            <Fact term="Plan">{subscription.plan_name}</Fact>
            <Fact term="Status">{subscriptionStatusLabel(subscription.status)}</Fact>
          </dl>
        )}
      </section>

      <section aria-labelledby="primary-branch-heading">
        <h3 id="primary-branch-heading">Primary branch</h3>
        {branch === null ? (
          <p>No primary branch.</p>
        ) : (
          <>
            <dl className="account-facts">
              <Fact term="Name">{branch.name}</Fact>
              <Fact term="Slug">{branch.slug}</Fact>
              <Fact term="Status">{branchStatusLabel(branch.status)}</Fact>
            </dl>
            {branch.status !== 'active' && (
              <p className="notice" role="note">
                The primary branch is not active.
              </p>
            )}
          </>
        )}
      </section>

      <section aria-labelledby="identity-heading">
        <h3 id="identity-heading">Operational identity</h3>
        <p>{identityLabel(library.operational_identity)}</p>
      </section>

      <section aria-labelledby="sorters-heading">
        <h3 id="sorters-heading">Sorters</h3>
        {library.sorters.length === 0 ? (
          <p>No sorters are registered for this library.</p>
        ) : (
          <div className="table-scroll">
            <table className="data-table text-table" aria-labelledby="sorters-heading">
              <thead>
                <tr>
                  <th scope="col">Sorter</th>
                  <th scope="col">Host branch</th>
                  <th scope="col">Installation</th>
                  <th scope="col">Collectors</th>
                  <th scope="col">Onboarding</th>
                  <th scope="col">Pipeline</th>
                  <th scope="col">Last reported</th>
                </tr>
              </thead>
              <tbody>
                {library.sorters.map((sorter) => (
                  <tr key={sorter.slug}>
                    <th scope="row">
                      <Link to={`/libraries/${encodeURIComponent(library.slug)}/sorters/${encodeURIComponent(sorter.slug)}`}>
                        {sorter.name}
                      </Link>
                    </th>
                    <td>{sorter.host_branch.name}</td>
                    <td>{installationStatusLabel(sorter.status)}</td>
                    <td>{sorter.collector_count}</td>
                    <td>{onboardingLabel(sorter.onboarding_status)}</td>
                    <td>{pipelineStateLabel(sorter.pipeline)}</td>
                    <td>
                      {sorter.pipeline.last_reported_at === null ? (
                        <span className="quiet">Not reported</span>
                      ) : (
                        <time dateTime={sorter.pipeline.last_reported_at}>
                          {formatInstant(sorter.pipeline.last_reported_at, library.timezone)}
                        </time>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  )
}

/** /libraries/:orgSlug -- one library, read-only. */
export function LibraryPage() {
  const { orgSlug = '' } = useParams()
  const query = useQuery({
    queryKey: ['admin', 'library', orgSlug],
    queryFn: ({ signal }) => getLibrary(orgSlug, signal),
  })
  const notFound = isApiError(query.error) && query.error.status === 404

  return (
    <section aria-labelledby="library-heading">
      <p>
        <Link to="/libraries">Libraries</Link>
      </p>
      <h2 id="library-heading">{query.data?.name ?? (notFound ? 'Library not found' : 'Library')}</h2>
      {query.isPending && <p role="status">Loading library…</p>}
      {notFound && <p>There is no library with this address.</p>}
      {query.isError && !notFound && (
        <>
          <p className="error-message" role="alert">
            {LOAD_FAILED}
          </p>
          <button type="button" onClick={() => void query.refetch()}>
            Try again
          </button>
        </>
      )}
      {query.data !== undefined && <Overview library={query.data} />}
    </section>
  )
}
