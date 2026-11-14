import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Link, useParams } from 'react-router'

import { isApiError } from '../../../src/api/client.ts'
import { formatInstant, isValidTimeZone } from '../../../src/time/productTime.ts'
import {
  addInstallation,
  authorizeReplacement,
  cancelReplacement,
  changeInstallationStatus,
  getInstallations,
  getUnboundTokens,
  issueEnrollmentCode,
  issueRecoveryToken,
  renameInstallation,
  revokeUnboundToken,
  type Contract,
  type Installation,
  type Installations,
  type IssuedCode,
  type IssuedRecoveryToken,
  type Replacement,
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

/**
 * The one place a raw enrollment code is shown: from this page's own state, gone when the page is left. `replaces` is the
 * installation a pending replacement's code will replace, for the replacement's own instructions.
 */
function CodePanel({
  issued,
  zone,
  replaces,
  onDone,
}: {
  issued: IssuedCode
  zone: string
  replaces: number | null
  onDone: () => void
}) {
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
      {replaces !== null && (
        <p className="notice" role="note">
          {`For the replacement computer: enter this code in SortView Setup on the new computer. Installation #${replaces} keeps running until then. When Setup accepts the code, Installation #${replaces} stops being accepted: its agent token and ingest key are retired for good. Check that the new installation is Active and uploading before disabling the old computer's SortView Collector task and running tools\\uninstall.ps1.`}
        </p>
      )}
      {issued.revoked_previous_count > 0 && <p>The earlier unused code for this installation no longer works.</p>}
      <button type="button" className="button-secondary" onClick={onDone}>
        Done
      </button>
    </section>
  )
}

/** The one place a raw recovery token is shown: from this page's own state, gone on Done or when the page is left. */
function RecoveryTokenPanel({ issued, onDone }: { issued: IssuedRecoveryToken; onDone: () => void }) {
  const [copied, setCopied] = useState<string | null>(null)

  async function copy() {
    try {
      await navigator.clipboard.writeText(issued.agent_token)
      setCopied('Copied.')
    } catch {
      setCopied('Could not copy. Select the token and copy it by hand.')
    }
  }

  return (
    <section className="panel" aria-labelledby="recovery-token-heading">
      <h3 id="recovery-token-heading">
        Recovery token for Installation #{issued.installation.id} &mdash; {issued.installation.name}
      </h3>
      <p>
        <code className="enrollment-code">{issued.agent_token}</code>{' '}
        <button type="button" onClick={() => void copy()}>
          Copy
        </button>
      </p>
      {copied !== null && <p role="status">{copied}</p>}
      <p className="notice" role="note">
        Shown only now &mdash; copy it before leaving this page. It cannot be shown again. Store it on the Collector computer with
        tools\set-api-token.ps1, then run setup.ps1 again to resume.
      </p>
      {issued.revoked_previous_count > 0 && <p>This installation&rsquo;s earlier agent token no longer authenticates new requests.</p>}
      <button type="button" className="button-secondary" onClick={onDone}>
        Done
      </button>
    </section>
  )
}

type Pending =
  | { kind: 'rename' }
  | { kind: 'status'; to: StatusChange }
  | { kind: 'code' }
  | { kind: 'recovery' }
  | { kind: 'replace' }
  | { kind: 'cancel-replacement' }
  | null

/** How an installation stands in machine replacement (C6a), from the list alone. Display only: the server decides. */
interface ReplacementView {
  /** This installation is the library's pending replacement. */
  pending: boolean
  /** The installation this one was authorized to replace, if any. */
  replaces: number | null
  /** A replacement cancelled before its code was used. */
  cancelled: boolean
  /** The pending replacement that will replace this installation. */
  beingReplacedBy: number | null
  /** The redeemed replacement that replaced this installation. */
  replacedBy: number | null
  /** Whether "Replace this Collector computer" is offered: plausible here, and the server's flag is on. */
  canReplace: boolean
}

function replacementView(installation: Installation, data: Installations): ReplacementView {
  const id = installation.id
  const pending = data.pending_replacement_id === id
  const replaces = installation.replaces_installation_id
  const pendingOne = data.installations.find((other) => other.id === data.pending_replacement_id)
  const redeemedOne = data.installations.find(
    (other) => other.replaces_installation_id === id && other.last_code_redeemed_at !== null,
  )
  const canReplace =
    data.replacement_enabled &&
    data.pending_replacement_id === null &&
    installation.required_contract === 2 &&
    installation.branch.is_primary &&
    installation.branch.status === 'active' &&
    installation.status !== 'retired' &&
    installation.last_code_redeemed_at !== null
  return {
    pending,
    replaces,
    cancelled: replaces !== null && !pending && installation.status === 'retired' && installation.last_code_redeemed_at === null,
    beingReplacedBy: pendingOne !== undefined && pendingOne.replaces_installation_id === id ? pendingOne.id : null,
    replacedBy: redeemedOne?.id ?? null,
    canReplace,
  }
}

function replacementText(view: ReplacementView): string | null {
  const parts: string[] = []
  if (view.replaces !== null) {
    parts.push(
      view.pending
        ? `Pending replacement for Installation #${view.replaces}`
        : view.cancelled
          ? `Cancelled replacement for Installation #${view.replaces}`
          : `Replaces Installation #${view.replaces}`,
    )
  }
  if (view.beingReplacedBy !== null) {
    parts.push(`Being replaced by Installation #${view.beingReplacedBy}`)
  }
  if (view.replacedBy !== null) {
    parts.push(`Replaced by Installation #${view.replacedBy}`)
  }
  return parts.length === 0 ? null : parts.join('; ')
}

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
      `Retire Installation #${id}? Its Collector's agent token stops authenticating immediately. This cannot be undone in SortView administration. To replace a Contract 2 Collector computer, use Replace this Collector computer instead.`,
  },
}

function InstallationCard({
  orgSlug,
  installation,
  view,
  replacementEnabled,
  zone,
  readOnly,
  focusRequested,
  onFocused,
  onChanged,
  onIssued,
  onRecovered,
  onReplaced,
  onReplacementCancelled,
}: {
  orgSlug: string
  installation: Installation
  view: ReplacementView
  replacementEnabled: boolean
  zone: string
  readOnly: boolean
  focusRequested: boolean
  onFocused: () => void
  onChanged: () => void
  onIssued: (issued: IssuedCode) => void
  onRecovered: (issued: IssuedRecoveryToken) => void
  onReplaced: (replacement: Replacement) => void
  onReplacementCancelled: (replacement: Replacement) => void
}) {
  const [pending, setPending] = useState<Pending>(null)
  const [name, setName] = useState(installation.name)
  const [problem, setProblem] = useState<string | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [working, setWorking] = useState(false)
  const [replacementName, setReplacementName] = useState('')
  const [replacementProblem, setReplacementProblem] = useState<string | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [confirmationProblem, setConfirmationProblem] = useState<string | null>(null)
  const [confirmationRequired, setConfirmationRequired] = useState(false)
  const heading = useRef<HTMLHeadingElement>(null)
  const id = installation.id
  const statusActions: StatusChange[] = (
    installation.status === 'inactive' ? ['provisioning', 'retired'] : installation.status === 'retired' ? [] : ['inactive', 'retired']
  ).filter((to) => !(view.pending && to === 'retired')) as StatusChange[]
  // A pending replacement gets a code only while the server allows replacement; it never owns a key to recover.
  const canIssue = (installation.status === 'provisioning' || installation.status === 'active') && !(view.pending && !replacementEnabled)
  const canRecover = canIssue && installation.required_contract === 2 && !view.pending
  const pendingCode = when(installation.pending_code_expires_at, zone)
  // A Provisioning source needs the lost-response confirmation (design 14.3a), as does any the server says needs it.
  const askConfirmation = installation.status === 'provisioning' || confirmationRequired
  const relation = replacementText(view)

  useEffect(() => {
    if (focusRequested) {
      heading.current?.focus()
      onFocused()
    }
  }, [focusRequested, onFocused])

  /** `refused` may handle a refusal itself (returning true); otherwise it is shown as a failure. */
  async function run(work: () => Promise<void>, refused?: (caught: unknown) => boolean) {
    if (working) {
      return
    }
    setWorking(true)
    setFailure(null)
    try {
      await work()
      setPending(null)
    } catch (caught) {
      if (refused?.(caught)) {
        return
      }
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

  /** A refusal of a replacement action: another administrator may have changed things, so the list is read again. */
  function replacementRefused(caught: unknown): boolean {
    if (isApiError(caught) && caught.status === 409) {
      if (caught.code === 'lost_response_confirmation_required') {
        setConfirmationRequired(true)
      }
      onChanged()
    }
    return false
  }

  function openReplace() {
    setReplacementName('')
    setReplacementProblem(null)
    setConfirmed(false)
    setConfirmationProblem(null)
    setConfirmationRequired(false)
    setFailure(null)
    setPending({ kind: 'replace' })
  }

  function replace(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const nameMissing = replacementName.trim() === ''
    const confirmationMissing = askConfirmation && !confirmed
    setReplacementProblem(nameMissing ? 'Enter a name.' : null)
    setConfirmationProblem(confirmationMissing ? 'Confirm that this installation’s enrollment response was lost.' : null)
    if (nameMissing || confirmationMissing) {
      return
    }
    void run(
      async () => {
        onReplaced(await authorizeReplacement(orgSlug, id, replacementName, askConfirmation && confirmed))
      },
      (caught) => {
        const forName = nameProblem(caught)
        if (forName !== null) {
          setReplacementProblem(forName)
          return true
        }
        return replacementRefused(caught)
      },
    )
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
      <h3 id={`installation-${id}-heading`} ref={heading} tabIndex={-1}>
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
        {relation !== null && <Fact term="Replacement">{relation}</Fact>}
      </dl>
      {view.pending && pendingCode === null && !readOnly && (
        <p className="quiet">
          {replacementEnabled
            ? 'No unused code. Issue one when the replacement computer is ready.'
            : 'Machine replacement is turned off on this server, so this replacement cannot be given a code or used. It can be cancelled.'}
        </p>
      )}

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
          {canRecover && (
            <button type="button" className="button-secondary" onClick={() => setPending({ kind: 'recovery' })}>
              Issue recovery token
            </button>
          )}
          {view.canReplace && (
            <button type="button" className="button-secondary" onClick={openReplace}>
              Replace this Collector computer
            </button>
          )}
          {view.pending && (
            <button type="button" className="button-secondary" onClick={() => setPending({ kind: 'cancel-replacement' })}>
              Cancel replacement
            </button>
          )}
        </div>
      )}

      {pending?.kind === 'replace' && (
        <form onSubmit={replace} noValidate aria-label={`Replace Installation #${id}`} aria-describedby={`replace-${id}-consequences`}>
          <p className="notice" id={`replace-${id}-consequences`}>
            {`Authorizing adds a new Provisioning installation for the replacement computer. It does not disable this computer: Installation #${id} keeps running and uploading until the replacement's enrollment code is used in SortView Setup. Only then are this computer's agent token and ingest key retired, for good. Collected data may have a gap between this computer's last upload and the new computer's first, and items near the switch may be counted twice. Verify that the new computer is Active and uploading before cleaning up this one.`}
          </p>
          <div className="field">
            <label htmlFor={`replace-${id}-name`}>Name for the replacement computer</label>
            <input
              id={`replace-${id}-name`}
              type="text"
              autoComplete="off"
              value={replacementName}
              aria-invalid={replacementProblem !== null}
              aria-describedby={replacementProblem === null ? undefined : `replace-${id}-name-problem`}
              onChange={(event) => {
                setReplacementName(event.target.value)
                setReplacementProblem(null)
              }}
            />
            {replacementProblem !== null && (
              <p className="field-problem" id={`replace-${id}-name-problem`}>
                {replacementProblem}
              </p>
            )}
          </div>
          {askConfirmation && (
            <div className="field">
              <label>
                <input
                  type="checkbox"
                  checked={confirmed}
                  aria-invalid={confirmationProblem !== null}
                  aria-describedby={confirmationProblem === null ? undefined : `replace-${id}-confirm-problem`}
                  onChange={(event) => {
                    setConfirmed(event.target.checked)
                    setConfirmationProblem(null)
                  }}
                />{' '}
                {`Installation #${id} is still Provisioning. I confirm SortView Setup reported an unknown result and offered no Resume setup, and that Resume setup and a recovery token were tried first.`}
              </label>
              {confirmationProblem !== null && (
                <p className="field-problem" id={`replace-${id}-confirm-problem`}>
                  {confirmationProblem}
                </p>
              )}
            </div>
          )}
          <div className="form-actions">
            <button type="submit" aria-disabled={working}>
              Confirm: Authorize replacement
            </button>
            <button type="button" className="button-secondary" onClick={() => setPending(null)}>
              Cancel
            </button>
          </div>
        </form>
      )}

      {pending?.kind === 'cancel-replacement' && (
        <div role="group" aria-label={`Cancel the replacement Installation #${id}`}>
          <p className="notice">
            {`Cancel this replacement? Installation #${id} is retired and its unused enrollment codes stop working. Installation #${view.replaces ?? ''} keeps running unchanged. This cannot be undone; a new replacement can be authorized afterwards.`}
          </p>
          <div className="form-actions">
            <button
              type="button"
              aria-disabled={working}
              onClick={() =>
                void run(async () => {
                  onReplacementCancelled(await cancelReplacement(orgSlug, id))
                }, replacementRefused)
              }
            >
              Confirm: Cancel replacement
            </button>
            <button type="button" className="button-secondary" onClick={() => setPending(null)}>
              Keep replacement
            </button>
          </div>
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

      {pending?.kind === 'recovery' && (
        <div role="group" aria-label={`Issue a recovery token for Installation #${id}`}>
          <p className="notice">
            {`Replace Installation #${id}'s agent token? Its current token stops authenticating new requests, so a running Collector stops until the new token is stored. A request already in progress may still finish. Use this when a Collector could not store its token.`}
          </p>
          <div className="form-actions">
            <button
              type="button"
              aria-disabled={working}
              onClick={() =>
                void run(async () => {
                  onRecovered(await issueRecoveryToken(orgSlug, id))
                  onChanged()
                })
              }
            >
              Confirm: Issue recovery token
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

/** A library's legacy unbound tokens, on request. Deactivating an installation does not stop these. */
function UnboundTokens({ orgSlug, zone }: { orgSlug: string; zone: string }) {
  const queryClient = useQueryClient()
  const [shown, setShown] = useState(false)
  const [confirming, setConfirming] = useState<number | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [working, setWorking] = useState(false)
  const queryKey = ['admin', 'installations', orgSlug, 'unbound-tokens']
  const query = useQuery({
    queryKey,
    queryFn: ({ signal }) => getUnboundTokens(orgSlug, signal),
    enabled: shown,
  })

  async function revoke(id: number) {
    if (working) {
      return
    }
    setWorking(true)
    setFailure(null)
    try {
      await revokeUnboundToken(orgSlug, id)
      setConfirming(null)
      await queryClient.invalidateQueries({ queryKey })
    } catch (caught) {
      setFailure(failureText(caught))
    } finally {
      setWorking(false)
    }
  }

  return (
    <section aria-labelledby="unbound-tokens-heading">
      <h3 id="unbound-tokens-heading">Legacy unbound tokens</h3>
      {!shown && (
        <button type="button" className="button-secondary" onClick={() => setShown(true)}>
          Show legacy unbound tokens
        </button>
      )}
      {shown && query.isPending && <p role="status">Loading unbound tokens…</p>}
      {shown && query.isError && (
        <p className="error-message" role="alert">
          The unbound tokens could not be loaded. Please try again.
        </p>
      )}
      {failure !== null && (
        <p className="error-message" role="alert">
          {failure}
        </p>
      )}
      {query.data?.length === 0 && <p>This library has no active unbound tokens.</p>}
      {query.data?.map((token) => (
        <article className="panel" key={token.id} aria-labelledby={`unbound-token-${token.id}-heading`}>
          <h4 id={`unbound-token-${token.id}-heading`}>
            Token #{token.id}
            {token.description !== null ? ` — ${token.description}` : ''}
          </h4>
          <dl className="account-facts">
            <Fact term="Branch">{token.branch_name ?? <span className="quiet">Not mapped</span>}</Fact>
            <Fact term="Created">{formatInstant(token.created_at, zone)}</Fact>
            <Fact term="Last used">{when(token.last_used_at, zone) ?? <span className="quiet">Never</span>}</Fact>
          </dl>
          {confirming === token.id ? (
            <div role="group" aria-label={`Revoke token #${token.id}`}>
              <p className="notice">
                {`Revoke token #${token.id}? Anything still using it stops authenticating new requests, and it cannot be restored. A request already in progress may still finish, so this is not an upload fence.`}
              </p>
              <div className="form-actions">
                <button type="button" aria-disabled={working} onClick={() => void revoke(token.id)}>
                  Confirm: Revoke
                </button>
                <button type="button" className="button-secondary" onClick={() => setConfirming(null)}>
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <button type="button" className="button-secondary" onClick={() => setConfirming(token.id)}>
              Revoke
            </button>
          )}
        </article>
      ))}
    </section>
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
  // The one raw recovery token this page may hold, under the same rule.
  const [recovered, setRecovered] = useState<IssuedRecoveryToken | null>(null)
  // What a replacement action did, announced; and the card to take focus once it is on the page.
  const [announcement, setAnnouncement] = useState<string | null>(null)
  const [focusId, setFocusId] = useState<number | null>(null)
  const query = useQuery({
    queryKey: ['admin', 'installations', orgSlug],
    queryFn: ({ signal }) => getInstallations(orgSlug, signal),
  })
  const notFound = isApiError(query.error) && query.error.status === 404
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ['admin', 'installations', orgSlug] })
  const data = query.data
  const cancelled = data?.add_problems.includes('library_cancelled') ?? false
  const noPrimary = data?.add_problems.includes('no_active_primary_branch') ?? false
  const issuedReplaces =
    issued === null ? null : (data?.installations.find((one) => one.id === issued.installation.id)?.replaces_installation_id ?? null)
  const focused = useCallback(() => setFocusId(null), [])

  function replaced(replacement: Replacement) {
    const source = replacement.replaces_installation_id
    setAnnouncement(
      `Replacement authorized: Installation #${replacement.installation.id} — ${replacement.installation.name} will replace Installation #${source}. Nothing has changed on Installation #${source} yet. Issue the replacement's enrollment code when the new computer is ready.`,
    )
    setFocusId(replacement.installation.id)
    refresh()
  }

  function replacementCancelled(replacement: Replacement) {
    setAnnouncement(
      `Replacement cancelled: Installation #${replacement.installation.id} is retired and its unused enrollment codes no longer work. Installation #${replacement.replaces_installation_id} is unchanged.`,
    )
    setFocusId(replacement.installation.id)
    refresh()
  }

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
            {`Times are shown in ${zone}.`}
          </p>
          {!data.replacement_enabled && !cancelled && (
            <p className="notice" role="note">
              Machine replacement is turned off on this server. A pending replacement can still be cancelled.
            </p>
          )}
          {announcement !== null && (
            <p className="form-saved" role="status">
              {announcement}
            </p>
          )}
          {cancelled && (
            <p className="notice" role="note">
              This library is cancelled. Its installations can be seen and not changed.
            </p>
          )}
          {issued !== null && <CodePanel issued={issued} zone={zone} replaces={issuedReplaces} onDone={() => setIssued(null)} />}
          {recovered !== null && <RecoveryTokenPanel issued={recovered} onDone={() => setRecovered(null)} />}

          {data.installations.length === 0 ? (
            <p>No installations are registered for this library.</p>
          ) : (
            data.installations.map((installation) => (
              <InstallationCard
                key={installation.id}
                orgSlug={orgSlug}
                installation={installation}
                view={replacementView(installation, data)}
                replacementEnabled={data.replacement_enabled}
                zone={zone}
                readOnly={cancelled}
                focusRequested={focusId === installation.id}
                onFocused={focused}
                onChanged={refresh}
                onIssued={setIssued}
                onRecovered={setRecovered}
                onReplaced={replaced}
                onReplacementCancelled={replacementCancelled}
              />
            ))
          )}

          {!cancelled && <UnboundTokens orgSlug={orgSlug} zone={zone} />}

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
