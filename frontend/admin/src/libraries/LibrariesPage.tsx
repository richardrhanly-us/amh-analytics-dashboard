import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router'

import { formatInstant } from '../../../src/time/productTime.ts'
import { libraryStatusLabel, primarySorterLabel, subscriptionStatusLabel } from './labels.ts'
import { getLibraries, type LibrarySummary } from './librariesApi.ts'

const LOAD_FAILED = 'The libraries could not be loaded. Please try again.'

function planText(library: LibrarySummary): string {
  const { subscription } = library
  if (subscription === null) {
    return 'No subscription'
  }
  // The plan's name, and its subscription's status only where that is not simply active.
  return subscription.status === 'active'
    ? subscription.plan_name
    : `${subscription.plan_name} · ${subscriptionStatusLabel(subscription.status)}`
}

/** /libraries -- every library, read-only, with its primary sorter's last report. */
export function LibrariesPage() {
  const query = useQuery({ queryKey: ['admin', 'libraries'], queryFn: ({ signal }) => getLibraries(signal) })

  return (
    <section aria-labelledby="libraries-heading">
      <h2 id="libraries-heading">Libraries</h2>
      {query.isPending && <p role="status">Loading libraries…</p>}
      {query.isError && (
        <>
          <p className="error-message" role="alert">
            {LOAD_FAILED}
          </p>
          <button type="button" onClick={() => void query.refetch()}>
            Try again
          </button>
        </>
      )}
      {query.data !== undefined &&
        (query.data.libraries.length === 0 ? (
          <p>No libraries have been provisioned yet.</p>
        ) : (
          <div className="table-scroll">
            <table className="data-table text-table" aria-labelledby="libraries-heading">
              <thead>
                <tr>
                  <th scope="col">Library</th>
                  <th scope="col">Status</th>
                  <th scope="col">Plan</th>
                  <th scope="col">Sorters</th>
                  <th scope="col">Primary sorter</th>
                  <th scope="col">Last reported</th>
                </tr>
              </thead>
              <tbody>
                {query.data.libraries.map((library) => {
                  const reported = library.primary_sorter?.pipeline.last_reported_at ?? null
                  return (
                    <tr key={library.slug}>
                      <th scope="row">
                        <Link to={`/libraries/${encodeURIComponent(library.slug)}`}>{library.name}</Link>
                      </th>
                      <td>{libraryStatusLabel(library.status)}</td>
                      <td>{planText(library)}</td>
                      <td>{library.sorter_count}</td>
                      <td>{primarySorterLabel(library.primary_sorter)}</td>
                      <td>
                        {reported === null ? (
                          <span className="quiet">Not reported</span>
                        ) : (
                          <time dateTime={reported}>{formatInstant(reported, query.data.timezone)}</time>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ))}
    </section>
  )
}
