import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type FormEvent, type ReactNode } from 'react'
import { Link, useParams } from 'react-router'

import { isApiError } from '../../../src/api/client.ts'
import { formatInstant, isValidTimeZone } from '../../../src/time/productTime.ts'
import {
  addInstallation,
  changeInstallationStatus,
  getInstallations,
  issueEnrollmentCode,
  renameInstallation,
  type Contract,
  type Installation,
  type Installations,
  type IssuedCode,
  type StatusChange,
} from './installationsApi.ts'
import { AdminBreadcrumb } from '../AdminBreadcrumb.tsx'
import { AdminPageHeading } from '../AdminPageHeading.tsx'

const STATUS_LABEL: Record<Installation['status'], string> = {
  provisioning: 'Provisioning',
  active: 'Active',
  inactive: 'Inactive',
  retired: 'Retired',
}

// Times are shown in the viewer's own time zone, named in each one.
function viewerZone(): string {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone
  return isValidTimeZone(zone) ? zone : 'UTC'
}

function when(at: string | null, zone: string): string | null {
  return at === null ? null : formatInstant(at, zone)
}

function failureText(caught: unknown): string {
  return `Nothing was changed. ${isApiError(caught) ? caught.message : 'Please try again.'}`
}

function nameProblem(caught: unknown): string | null {
  if (!isApiError(caught) || caught.status !== 422) {
    return null
  }
  return caught.code === 'invalid_installation' ? 'Enter a name of at most 120 characters.' : null
}

function Fact({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div>
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  )
}

/** The one place a raw enrollment code is shown: from this page's own state, gone when the page is left. */
function CodePanel({ issued, zone, onDone }: { issued: IssuedCode; zone: string; onDone: () => void }) {
  const [copied, setCopied] = useState<string | null>(null)
  const expires = formatInstant(issued.expires_at, zone)

  async function copy() {
    try {
      await navigator.clipboard.writeText(issued.enrollment_code)
      setCopied('Copied.')
    } catch {
      setCopied('Could not copy. Select the code and copy it by hand.')
    }
  }

  return (
    <section className="panel" aria-labelledby="enrollment-code-heading">
      <h3 id="enrollment-code-heading">
        Enrollment code for Installation #{issued.installation.id} &mdash; {issued.installation.name}
      </h3>
      <p>
        <code className="enrollment-code">{issued.enrollment_code}</code>{' '}
        <button type="button" onClick={() => void copy()}>
          Copy
        </button>
      </p>
      {copied !== null && <p role="status">{copied}</p>}
      <p className="notice" role="note">
        Single use. Shown only now &mdash; copy it before leaving this page. It cannot be shown again. It expires at {expires} and stops working when
        used or when a new code is issued.
      </p>
      {issued.revoked_previous_count > 0 && <p>The earlier unused code for this installation no longer works.</p>}
      <button type="button" className="button-secondary" onClick={onDone}>
        Done
      </button>
    </section>
  )
}

type Pending = { kind: 'rename' } | { kind: 'status'; to: StatusChange } | { kind: 'code' } | null

const CONFIRM: Record<StatusChange, { action: string; text: (id: number) => string }> = {
  inactive: {
    action: 'Deactivate',
    text: (id) => `Deactivate Installation #${id}? Its Collector's agent token stops authenticating until the installation is reactivated.`,
  },
  provisioning: {
    action: 'Reactivate',
    text: (id) =>
      `Reactivate Installation #${id}? Its existing Collector's agent token can authenticate again immediately. The installation shows as Provisioning until its next successful heartbeat, then becomes Active.`,
  },
  retired: {
    action: 'Retire',
    text: (id) =>
      `Retire Installation #${id}? Its Collector's agent token stops authenticating immediately. This cannot be undone in SortView administration. Replacing a Contract 2 collector machine is not handled here.`,
  },
}

function InstallationCard({
  orgSlug,
  installation,
  zone,
  readOnly,
  onChanged,
  onIssued,
}: {
  orgSlug: string
  installation: Installation
  zone: string
  readOnly: boolean
  onChanged: () => void
  onIssued: (issued: IssuedCode) => void
}) {
  const [pending, setPending] = useState<Pending>(null)
  const [name, setName] = useState(installation.name)
  const [problem, setProblem] = useState<string | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [working, setWorking] = useState(false)
  const id = installation.id
  const statusActions: StatusChange[] =
    installation.status === 'inactive' ? ['provisioning', 'retired'] : installation.status === 'retired' ? [] : ['inactive', 'retired']
  const canIssue = installation.status === 'provisioning' || installation.status === 'active'
  const pendingCode = when(installation.pending_code_expires_at, zone)

  async function run(work: () => Promise<void>) {
    if (working) {
      return
    }
    setWorking(true)
    setFailure(null)
    try {
      await work()
      setPending(null)
    } catch (caught) {
      const forName = pending?.kind === 'rename' ? nameProblem(caught) : null
      if (forName !== null) {
        setProblem(forName)
      } else {
        setFailure(failureText(caught))
      }
    } finally {
      setWorking(false)
    }
  }

  function rename(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (name.trim() === '') {
      setProblem('Enter a name.')
      return
    }
    setProblem(null)
    void run(async () => {
      await renameInstallation(orgSlug, id, name)
      onChanged()
    })
  }

  function issue() {
    void run(async () => {
      onIssued(await issueEnrollmentCode(orgSlug, id))
      onChanged()
    })
  }

  return (
    <article className="panel" aria-labelledby={`installation-${id}-heading`}>
      <h3 id={`installation-${id}-heading`}>
        Installation #{id} &mdash; {installation.name}
      </h3>
      <dl className="account-facts">
        <Fact term="Branch">
          {installation.branch.name}
          {installation.branch.is_primary ? ' (primary)' : ''}
          {installation.branch.status !== 'active' ? ' — inactive branch' : ''}
        </Fact>
        <Fact term="Status">{STATUS_LABEL[installation.status]}</Fact>
        <Fact term="Contract">{`Contract ${installation.required_contract}`}</Fact>
        <Fact term="Installed">{when(installation.installed_at, zone) ?? <span className="quiet">Not yet</span>}</Fact>
        <Fact term="Last seen">{when(installation.last_seen_at, zone) ?? <span className="quiet">Never</span>}</Fact>
        <Fact term="Enrollment code">
          {pendingCode !== null
            ? `Unused code until ${pendingCode}`
            : installation.last_code_redeemed_at !== null
              ? `Last used ${when(installation.last_code_redeemed_at, zone)}`
              : <span className="quiet">None</span>}
        </Fact>
      </dl>

      {failure !== null && (
        <p className="error-message" role="alert">
          {failure}
        </p>
      )}

      {!readOnly && pending === null && (
        <div className="form-actions">
          <button type="button" className="button-secondary" onClick={() => setPending({ kind: 'rename' })}>
            Rename
          </button>
          {statusActions.map((to) => (
            <button key={to} type="button" className="button-secondary" onClick={() => setPending({ kind: 'status', to })}>
              {CONFIRM[to].action}
            </button>
          ))}
          {canIssue && (
            <button type="button" onClick={() => (pendingCode === null ? issue() : setPending({ kind: 'code' }))}>
              Issue enrollment code
            </button>
          )}
        </div>
      )}

      {pending?.kind === 'rename' && (
        <form onSubmit={rename} noValidate aria-label={`Rename Installation #${id}`}>
          <div className="field">
            <label htmlFor={`installation-${id}-name`}>Installation name</label>
            <input
              id={`installation-${id}-name`}
              type="text"
              autoComplete="off"
              value={name}
              aria-invalid={problem !== null}
              aria-describedby={problem === null ? undefined : `installation-${id}-name-problem`}
              onChange={(event) => {
                setName(event.target.value)
                setProblem(null)
              }}
            />
            {problem !== null && (
              <p className="field-problem" id={`installation-${id}-name-problem`}>
                {problem}
              </p>
            )}
          </div>
          <div className="form-actions">
            <button type="submit" aria-disabled={working}>
              Save name
            </button>
            <button
              type="button"
              className="button-secondary"
              onClick={() => {
                setPending(null)
                setName(installation.name)
                setProblem(null)
                setFailure(null)
              }}
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {pending?.kind === 'status' && (
        <div role="group" aria-label={`${CONFIRM[pending.to].action} Installation #${id}`}>
          <p className="notice">{CONFIRM[pending.to].text(id)}</p>
          <div className="form-actions">
            <button
              type="button"
              aria-disabled={working}
              onClick={() =>
                void run(async () => {
                  await changeInstallationStatus(orgSlug, id, pending.to)
                  onChanged()
                })
              }
            >
              {`Confirm: ${CONFIRM[pending.to].action}`}
            </button>
            <button type="button" className="button-secondary" onClick={() => setPending(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {pending?.kind === 'code' && (
        <div role="group" aria-label={`Issue an enrollment code for Installation #${id}`}>
          <p className="notice">
            {`This installation has an unused enrollment code (until ${pendingCode}). Issuing a new code makes that code stop working.`}
          </p>
          <div className="form-actions">
            <button type="button" aria-disabled={working} onClick={issue}>
              Confirm: Issue a new code
            </button>
            <button type="button" className="button-secondary" onClick={() => setPending(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}
    </article>
  )
}

function AddForm({ orgSlug, data, onAdded }: { orgSlug: string; data: Installations; onAdded: () => void }) {
  const [name, setName] = useState('')
  const [contract, setContract] = useState<Contract>(2)
  const [problem, setProblem] = useState<string | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [added, setAdded] = useState<string | null>(null)
  const [working, setWorking] = useState(false)
  const v2NotReady = data.add_problems.includes('collector_settings_not_ready')

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (working) {
      return
    }
    setAdded(null)
    setFailure(null)
    if (name.trim() === '') {
      setProblem('Enter a name.')
      return
    }
    if (contract === 2 && v2NotReady) {
      setFailure('Nothing was added. A Contract 2 installation needs this library’s Collector configuration first.')
      return
    }
    setProblem(null)
    setWorking(true)
    try {
      const installation = await addInstallation(orgSlug, name, contract)
      setName('')
      setAdded(`Added Installation #${installation.id} — ${installation.name}.`)
      onAdded()
    } catch (caught) {
      const forName = nameProblem(caught)
      if (forName !== null) {
        setProblem(forName)
      } else {
        setFailure(`Nothing was added. ${isApiError(caught) ? caught.message : 'Please try again.'}`)
      }
    } finally {
      setWorking(false)
    }
  }

  return (
    <form onSubmit={submit} noValidate aria-labelledby="add-installation-heading">
      {failure !== null && (
        <p className="error-message" role="alert">
          {failure}
        </p>
      )}
      <p>
        It will be added at the primary branch, <strong>{data.primary_branch?.name}</strong>, as provisioning.
      </p>
      <div className="field">
        <label htmlFor="new-installation-name">Installation name</label>
        <input
          id="new-installation-name"
          type="text"
          autoComplete="off"
          value={name}
          aria-invalid={problem !== null}
          aria-describedby={problem === null ? undefined : 'new-installation-name-problem'}
          onChange={(event) => {
            setName(event.target.value)
            setProblem(null)
          }}
        />
        {problem !== null && (
          <p className="field-problem" id="new-installation-name-problem">
            {problem}
          </p>
        )}
      </div>
      <fieldset>
        <legend>Contract</legend>
        <label>
          <input type="radio" name="contract" checked={contract === 2} onChange={() => setContract(2)} /> Contract 2 (current)
        </label>
        <label>
          <input type="radio" name="contract" checked={contract === 1} onChange={() => setContract(1)} /> Contract 1 (legacy)
        </label>
      </fieldset>
      {contract === 2 && v2NotReady && (
        <p className="notice" role="note">
          A Contract 2 installation needs this library&rsquo;s Collector time zone and routing review first:{' '}
          <Link to={`/libraries/${encodeURIComponent(orgSlug)}/collector`}>Collector configuration</Link>.
        </p>
      )}
      <div className="form-actions">
        <button type="submit" aria-disabled={working}>
          Add installation
        </button>
        {added !== null && (
          <p className="form-saved" role="status">
            {added}
          </p>
        )}
      </div>
    </form>
  )
}

/** /libraries/:orgSlug/installations -- a library's Collector installations and their enrollment codes. */
export function InstallationsPage() {
  const { orgSlug = '' } = useParams()
  const queryClient = useQueryClient()
  const zone = viewerZone()
  // The one raw enrollment code this page may hold: here, in component state, and nowhere else.
  const [issued, setIssued] = useState<IssuedCode | null>(null)
  const query = useQuery({
    queryKey: ['admin', 'installations', orgSlug],
    queryFn: ({ signal }) => getInstallations(orgSlug, signal),
  })
  const notFound = isApiError(query.error) && query.error.status === 404
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ['admin', 'installations', orgSlug] })
  const data = query.data
  const cancelled = data?.add_problems.includes('library_cancelled') ?? false
  const noPrimary = data?.add_problems.includes('no_active_primary_branch') ?? false

  return (
    <section aria-labelledby="installations-heading">
      <AdminBreadcrumb
        trail={[
          { to: '/libraries', label: 'Libraries' },
          ...(notFound ? [] : [{ to: `/libraries/${encodeURIComponent(orgSlug)}`, label: data?.library.name ?? 'Library' }]),
        ]}
        current={notFound ? 'Library not found' : 'Installations'}
      />
      <AdminPageHeading id="installations-heading">{notFound ? 'Library not found' : 'Installations'}</AdminPageHeading>
      {notFound && <p>There is no library with this address.</p>}
      {query.isPending && <p role="status">Loading installations…</p>}
      {query.isError && !notFound && (
        <>
          <p className="error-message" role="alert">
            The installations could not be loaded. Please try again.
          </p>
          <button type="button" onClick={() => void query.refetch()}>
            Try again
          </button>
        </>
      )}
      {data !== undefined && (
        <>
          <p className="notice" role="note">
            {`Times are shown in ${zone}. Replacing a Contract 2 collector machine is not handled here.`}
          </p>
          {cancelled && (
            <p className="notice" role="note">
              This library is cancelled. Its installations can be seen and not changed.
            </p>
          )}
          {issued !== null && <CodePanel issued={issued} zone={zone} onDone={() => setIssued(null)} />}

          {data.installations.length === 0 ? (
            <p>No installations are registered for this library.</p>
          ) : (
            data.installations.map((installation) => (
              <InstallationCard
                key={installation.id}
                orgSlug={orgSlug}
                installation={installation}
                zone={zone}
                readOnly={cancelled}
                onChanged={refresh}
                onIssued={setIssued}
              />
            ))
          )}

          {!cancelled && (
            <section aria-labelledby="add-installation-heading">
              <h3 id="add-installation-heading">Add an installation</h3>
              {noPrimary ? (
                <p className="notice" role="note">
                  This library has no active primary branch, so no installation can be added.
                </p>
              ) : (
                <AddForm orgSlug={orgSlug} data={data} onAdded={refresh} />
              )}
            </section>
          )}
        </>
      )}
    </section>
  )
}
