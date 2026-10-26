import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Link, useLocation, useParams } from 'react-router'

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
import { assignFirstOwner, assignOperationalIdentity } from '../provision/provisionApi.ts'
import { getLibrary, type LibraryDetail } from './librariesApi.ts'
import { AdminBreadcrumb } from '../AdminBreadcrumb.tsx'
import { AdminPageHeading } from '../AdminPageHeading.tsx'

const LOAD_FAILED = 'This library could not be loaded. Please try again.'

function Fact({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div>
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  )
}

export const NO_OWNER_YET = 'No one can sign in to this library yet. Assign its first owner on this page.'

/** Shown once, right after provisioning: what is left to do, on which page. */
function NextSteps({ orgSlug }: { orgSlug: string }) {
  const base = `/libraries/${encodeURIComponent(orgSlug)}`
  return (
    <section aria-labelledby="next-steps-heading" className="notice">
      <h3 id="next-steps-heading">Library provisioned: next steps</h3>
      <ol>
        <li>
          <Link to={`${base}/collector`}>Configure its Collector settings</Link>.
        </li>
        <li>
          <Link to={`${base}/installations`}>Add a Collector installation</Link>.
        </li>
        <li>Issue an enrollment code for that installation, on the same page.</li>
      </ol>
      <p>{NO_OWNER_YET}</p>
    </section>
  )
}

/** The identity, and -- while it is not complete -- the one action that completes it. */
function OperationalIdentity({ library }: { library: LibraryDetail }) {
  const queryClient = useQueryClient()
  const [working, setWorking] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)

  async function assign() {
    if (working) {
      return
    }
    setWorking(true)
    setFailure(null)
    try {
      await assignOperationalIdentity(library.slug)
    } catch (caught) {
      // Only a refusal (4xx) says nothing changed. A malformed answer, a server error or no answer at all may have
      // followed an assignment that did happen; the refetch below shows what the server holds.
      const refused = isApiError(caught) && caught.status !== null && caught.status >= 400 && caught.status < 500
      setFailure(
        refused
          ? `Nothing was changed. ${caught.message}`
          : 'The assignment result could not be confirmed. The library has been refreshed.',
      )
    } finally {
      setWorking(false)
      void queryClient.invalidateQueries({ queryKey: ['admin', 'library', library.slug] })
    }
  }

  return (
    <section aria-labelledby="identity-heading">
      <h3 id="identity-heading">Operational identity</h3>
      <p>{identityLabel(library.operational_identity)}</p>
      {failure !== null && (
        <p className="error-message" role="alert">
          {failure}
        </p>
      )}
      {/* A cancelled library is terminal: the API refuses this for it, so it is not offered. */}
      {library.operational_identity !== 'assigned' && library.status !== 'cancelled' && (
        <button type="button" aria-disabled={working} onClick={() => void assign()}>
          Assign operational identity
        </button>
      )}
    </section>
  )
}

interface OwnerProblems {
  email?: string
  confirmation?: string
}

function sameAddress(a: string, b: string): boolean {
  return a.trim().toLowerCase() === b.trim().toLowerCase()
}

/**
 * The library's first owner: shown as a fact once it has one, and -- while it has none and can be changed -- a form
 * that asks for the address twice, then a confirmation naming the library and the address before anything is sent.
 * The server decides: it refuses unless the library still has no owner.
 */
function FirstOwner({ library }: { library: LibraryDetail }) {
  const queryClient = useQueryClient()
  const [email, setEmail] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [fullName, setFullName] = useState('')
  const [problems, setProblems] = useState<OwnerProblems>({})
  const [confirming, setConfirming] = useState(false)
  const [sending, setSending] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  const [assigned, setAssigned] = useState<string | null>(null)
  const confirmHeading = useRef<HTMLHeadingElement>(null)

  useEffect(() => {
    if (confirming) {
      confirmHeading.current?.focus()
    }
  }, [confirming])

  function review(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const found: OwnerProblems = {}
    if (!email.includes('@')) {
      found.email = "Enter the owner's email address."
    } else if (!sameAddress(email, confirmation)) {
      found.confirmation = 'The two email addresses are not the same.'
    }
    setProblems(found)
    setFailure(null)
    setConfirming(Object.keys(found).length === 0)
  }

  async function assign() {
    if (sending) {
      return
    }
    setSending(true)
    setFailure(null)
    try {
      await assignFirstOwner(library.slug, { email: email.trim(), email_confirmation: confirmation.trim(), full_name: fullName.trim() })
      setAssigned(email.trim().toLowerCase())
    } catch (caught) {
      // Only a refusal (4xx) says nothing changed. Anything else may have followed an assignment that did happen; the
      // refetch below shows whether the library now has an owner.
      const refused = isApiError(caught) && caught.status !== null && caught.status >= 400 && caught.status < 500
      setFailure(refused ? `Nothing was changed. ${caught.message}` : 'The result could not be confirmed. The library has been refreshed.')
      setConfirming(false)
    } finally {
      setSending(false)
      void queryClient.invalidateQueries({ queryKey: ['admin', 'library', library.slug] })
    }
  }

  const changeable = library.status === 'active' || library.status === 'trial'

  return (
    <section aria-labelledby="owner-heading">
      <h3 id="owner-heading">First owner</h3>
      {assigned !== null && (
        <p role="status">
          First owner assigned. Ask {assigned} to open the SortView app, choose &lsquo;Forgot password?&rsquo; on the sign-in
          page, and set a password with this address.
        </p>
      )}
      {library.has_active_owner ? (
        <p>This library has an owner. Its owners add everyone else from the Members page in the SortView app.</p>
      ) : !changeable ? (
        <p>No one can sign in to this library. An owner can be assigned only while the library is active.</p>
      ) : (
        <>
          <p>{NO_OWNER_YET}</p>
          {failure !== null && (
            <p className="error-message" role="alert">
              {failure}
            </p>
          )}
          {confirming ? (
            <div className="notice">
              <h4 id="owner-confirm-heading" ref={confirmHeading} tabIndex={-1}>
                Confirm the first owner
              </h4>
              <p>
                Make <strong>{email.trim().toLowerCase()}</strong> the first owner of <strong>{library.name}</strong>? They will be
                able to sign in, see all of this library&apos;s SortView data, and add other people. After this, only the
                library&apos;s owners can change who its owners are.
              </p>
              <div className="form-actions">
                <button type="button" disabled={sending} aria-disabled={sending} onClick={() => void assign()}>
                  {sending ? 'Assigning…' : 'Assign first owner'}
                </button>
                <button type="button" disabled={sending} onClick={() => setConfirming(false)}>
                  Change details
                </button>
              </div>
            </div>
          ) : (
            <form onSubmit={review} noValidate aria-labelledby="owner-heading">
              <div className="field">
                <label htmlFor="owner-email">Owner&apos;s email address</label>
                <input
                  id="owner-email"
                  type="email"
                  autoComplete="off"
                  spellCheck={false}
                  required
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  aria-invalid={problems.email !== undefined}
                  aria-describedby={problems.email === undefined ? undefined : 'owner-email-problem'}
                />
                {problems.email !== undefined && (
                  <p className="field-problem" id="owner-email-problem">
                    {problems.email}
                  </p>
                )}
              </div>
              <div className="field">
                <label htmlFor="owner-email-confirmation">Type the email address again</label>
                <input
                  id="owner-email-confirmation"
                  type="email"
                  autoComplete="off"
                  spellCheck={false}
                  required
                  value={confirmation}
                  onChange={(event) => setConfirmation(event.target.value)}
                  aria-invalid={problems.confirmation !== undefined}
                  aria-describedby={problems.confirmation === undefined ? undefined : 'owner-email-confirmation-problem'}
                />
                {problems.confirmation !== undefined && (
                  <p className="field-problem" id="owner-email-confirmation-problem">
                    {problems.confirmation}
                  </p>
                )}
              </div>
              <div className="field">
                <label htmlFor="owner-name">Owner&apos;s name (optional)</label>
                <input id="owner-name" type="text" autoComplete="off" value={fullName} onChange={(event) => setFullName(event.target.value)} />
              </div>
              <p className="field-help">
                No password is set here. The owner sets their own with &lsquo;Forgot password?&rsquo; in the SortView app. If the
                address already has a SortView account, its password stays as it is.
              </p>
              <div className="form-actions">
                <button type="submit">Review</button>
              </div>
            </form>
          )}
        </>
      )}
    </section>
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

      <OperationalIdentity library={library} />

      <FirstOwner library={library} />

      <section aria-labelledby="collector-heading">
        <h3 id="collector-heading">Collector</h3>
        <p>
          <Link to={`/libraries/${encodeURIComponent(library.slug)}/collector`}>Collector configuration</Link>
        </p>
        <p>
          <Link to={`/libraries/${encodeURIComponent(library.slug)}/installations`}>Installations</Link>
        </p>
        <p>
          <Link to={`/libraries/${encodeURIComponent(library.slug)}/lifecycle`}>Library lifecycle</Link>
        </p>
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

/** /libraries/:orgSlug -- one library. Its actions here complete a missing operational identity and assign a first owner. */
export function LibraryPage() {
  const { orgSlug = '' } = useParams()
  // Set by the provisioning page's navigation only: a reload or a link here shows no next steps.
  const provisioned = (useLocation().state as { provisioned?: unknown } | null)?.provisioned === true
  const query = useQuery({
    queryKey: ['admin', 'library', orgSlug],
    queryFn: ({ signal }) => getLibrary(orgSlug, signal),
  })
  const notFound = isApiError(query.error) && query.error.status === 404
  const title = query.data?.name ?? (notFound ? 'Library not found' : 'Library')

  return (
    <section aria-labelledby="library-heading">
      <AdminBreadcrumb trail={[{ to: '/libraries', label: 'Libraries' }]} current={title} />
      <AdminPageHeading id="library-heading">{title}</AdminPageHeading>
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
      {query.data !== undefined && provisioned && <NextSteps orgSlug={query.data.slug} />}
      {query.data !== undefined && <Overview library={query.data} />}
    </section>
  )
}
