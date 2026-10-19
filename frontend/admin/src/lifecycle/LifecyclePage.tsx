import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useParams } from 'react-router'

import { isApiError } from '../../../src/api/client.ts'
import { libraryStatusLabel } from '../libraries/labels.ts'
import { getLibrary, type LibraryDetail } from '../libraries/librariesApi.ts'
import { offboardLibrary, reactivateLibrary, suspendLibrary, type LifecycleResult, type OffboardCounts } from './lifecycleApi.ts'
import { AdminPageHeading } from '../AdminPageHeading.tsx'

const CANCELLED_NOTICE = 'Cancelled — permanently offboarded. This cannot be undone in SortView administration.'

function failureText(caught: unknown): string {
  return `Nothing was changed. ${isApiError(caught) ? caught.message : 'Please try again.'}`
}

const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`

function offboardedText(counts: OffboardCounts): string {
  return (
    'Library offboarded: ' +
    [
      `${plural(counts.agent_tokens_deactivated, 'agent token', 'agent tokens')} deactivated`,
      `${plural(counts.installations_retired, 'installation', 'installations')} retired`,
      `${plural(counts.enrollment_codes_revoked, 'enrollment code', 'enrollment codes')} revoked`,
      `${plural(counts.ingest_keys_retired, 'ingest key', 'ingest keys')} retired`,
      `${plural(counts.sessions_revoked, 'session', 'sessions')} revoked`,
    ].join(', ') +
    '.'
  )
}

type Pending = 'suspend' | 'reactivate' | null

function Lifecycle({ library }: { library: LibraryDetail }) {
  const queryClient = useQueryClient()
  const orgSlug = library.slug
  const [pending, setPending] = useState<Pending>(null)
  const [typed, setTyped] = useState('')
  const [working, setWorking] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const status = library.status

  async function run<T extends LifecycleResult>(work: () => Promise<T>, message: (answer: T) => string) {
    if (working) {
      return
    }
    setWorking(true)
    setFailure(null)
    setDone(null)
    try {
      const answer = await work()
      // The new state at once; then everything that shows it is fetched again.
      queryClient.setQueryData<LibraryDetail>(['admin', 'library', orgSlug], (old) =>
        old === undefined ? old : { ...old, status: answer.library.status },
      )
      setDone(message(answer))
      setPending(null)
      setTyped('')
    } catch (caught) {
      setFailure(failureText(caught))
    } finally {
      setWorking(false)
      void queryClient.invalidateQueries({ queryKey: ['admin', 'library', orgSlug] })
      void queryClient.invalidateQueries({ queryKey: ['admin', 'libraries'] })
      void queryClient.invalidateQueries({ queryKey: ['admin', 'installations', orgSlug] })
    }
  }

  const usable = status === 'active' || status === 'trial'
  const offboardable = usable || status === 'suspended'

  return (
    <>
      <section aria-labelledby="lifecycle-status-heading">
        <h3 id="lifecycle-status-heading">Status</h3>
        <p>{libraryStatusLabel(status)}</p>
        {status === 'suspended' && (
          <p className="notice" role="note">
            Suspended. Collector uploads and enrollment are refused and customer changes are blocked. Members can still read
            their dashboard and history.
          </p>
        )}
        {status === 'cancelled' && (
          <p className="notice" role="note">
            {CANCELLED_NOTICE}
          </p>
        )}
      </section>

      {done !== null && (
        <p className="form-saved" role="status">
          {done}
        </p>
      )}
      {failure !== null && (
        <p className="error-message" role="alert">
          {failure}
        </p>
      )}

      {usable && (
        <section aria-labelledby="suspend-heading">
          <h3 id="suspend-heading">Suspend</h3>
          {pending === 'suspend' ? (
            <div role="group" aria-label="Suspend library">
              <p className="notice">
                Suspend this library? This is reversible. Collector uploads and enrollment stop, and customer changes are
                blocked. Agent tokens, installations, ingest keys and settings are kept. Members keep read-only access to
                their dashboard and history.
              </p>
              <div className="form-actions">
                <button
                  type="button"
                  aria-disabled={working}
                  onClick={() => void run(() => suspendLibrary(orgSlug), () => 'Library suspended.')}
                >
                  Confirm: Suspend library
                </button>
                <button type="button" className="button-secondary" onClick={() => setPending(null)}>
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <button type="button" className="button-secondary" onClick={() => setPending('suspend')}>
              Suspend library
            </button>
          )}
        </section>
      )}

      {status === 'suspended' && (
        <section aria-labelledby="reactivate-heading">
          <h3 id="reactivate-heading">Reactivate</h3>
          {pending === 'reactivate' ? (
            <div role="group" aria-label="Reactivate library">
              <p className="notice">
                Reactivate this library? Normal customer access and Collector authentication return, using the agent
                tokens, installations, keys and settings that were kept. Its status becomes Active; a previous Trial label
                is not restored.
              </p>
              <div className="form-actions">
                <button
                  type="button"
                  aria-disabled={working}
                  onClick={() => void run(() => reactivateLibrary(orgSlug), () => 'Library reactivated.')}
                >
                  Confirm: Reactivate library
                </button>
                <button type="button" className="button-secondary" onClick={() => setPending(null)}>
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <button type="button" onClick={() => setPending('reactivate')}>
              Reactivate library
            </button>
          )}
        </section>
      )}

      {offboardable && (
        <section aria-labelledby="offboard-heading">
          <h3 id="offboard-heading">Offboard permanently</h3>
          <div className="error-message" role="note">
            <p>
              <strong>Permanent in SortView administration.</strong> Offboarding cancels the library and:
            </p>
            <ul>
              <li>deactivates its agent tokens;</li>
              <li>retires its Collector installations;</li>
              <li>revokes its unused enrollment codes;</li>
              <li>retires its active ingest keys;</li>
              <li>signs out customers left with no other library.</li>
            </ul>
            <p>
              It does not delete customer data, memberships, subscriptions or settings, and it does not remove v2 cutovers.
              This is not a data purge. To pause a library instead, suspend it.
            </p>
          </div>
          <label htmlFor="offboard-confirm">{`Type the library's slug (${orgSlug}) to confirm`}</label>
          <input
            id="offboard-confirm"
            type="text"
            autoComplete="off"
            spellCheck={false}
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
          />
          <div className="form-actions">
            <button
              type="button"
              disabled={typed !== orgSlug}
              aria-disabled={working || typed !== orgSlug}
              onClick={() => void run(() => offboardLibrary(orgSlug, typed), (answer) => offboardedText(answer.counts))}
            >
              Offboard library permanently
            </button>
          </div>
        </section>
      )}
    </>
  )
}

/** /libraries/:orgSlug/lifecycle -- suspend, reactivate or permanently offboard one library. */
export function LifecyclePage() {
  const { orgSlug = '' } = useParams()
  const query = useQuery({
    queryKey: ['admin', 'library', orgSlug],
    queryFn: ({ signal }) => getLibrary(orgSlug, signal),
  })
  const notFound = isApiError(query.error) && query.error.status === 404

  return (
    <section aria-labelledby="lifecycle-heading">
      <p>
        {notFound ? (
          <Link to="/libraries">Libraries</Link>
        ) : (
          <Link to={`/libraries/${encodeURIComponent(orgSlug)}`}>{query.data?.name ?? 'Library'}</Link>
        )}
      </p>
      <AdminPageHeading id="lifecycle-heading">{notFound ? 'Library not found' : 'Library lifecycle'}</AdminPageHeading>
      {notFound && <p>There is no library with this address.</p>}
      {query.isPending && <p role="status">Loading library…</p>}
      {query.isError && !notFound && (
        <>
          <p className="error-message" role="alert">
            The library could not be loaded. Please try again.
          </p>
          <button type="button" onClick={() => void query.refetch()}>
            Try again
          </button>
        </>
      )}
      {query.data !== undefined && <Lifecycle library={query.data} />}
    </section>
  )
}
