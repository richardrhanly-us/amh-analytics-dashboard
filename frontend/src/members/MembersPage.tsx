import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode, type RefObject } from 'react'
import { useNavigate, useOutletContext } from 'react-router'

import { fieldProblems } from '../api/account.ts'
import { isApiError } from '../api/client.ts'
import { INVALID_MEMBER, type Member, type MemberActivity } from '../api/members.ts'
import type { OrganizationDetail } from '../api/organizations.ts'
import { messageFor } from '../components/errorText.ts'
import { LoadFailure } from '../components/LoadFailure.tsx'
import { NotFoundPage } from '../pages/NotFoundPage.tsx'
import { ORGANIZATIONS_PATH } from '../router/paths.ts'
import { SettingsFrame } from '../settings/SettingsLayout.tsx'
import { formatLocalInstant } from '../time/localTime.ts'
import { activitySentence, assignableRoles, canManageMembers, memberProblems, MEMBERS_PAGE_NAME, roleName } from './memberText.ts'
import { useMemberActivity, useMemberChanges, useMembers, type MemberChanges } from './useMembers.ts'

const MANAGED_BY_OWNERS_AND_ADMINS = "Members are managed by this organization's owners and admins."
const SELF_WARNING = 'You will lose access to this page.'

// The API's own answers to a change it will not make. Each comes with a sentence that says why, shown as it is.
const REFUSALS = ['forbidden', 'owner_required', 'last_owner', 'organization_read_only', 'member_not_found', 'already_member']

const isSignedOut = (error: unknown) => isApiError(error) && error.status === 401
const codeOf = (error: unknown) => (isApiError(error) ? error.code : null)

/**
 * What to say when a change was not made: the API's own sentence when it
 * refused, and otherwise that nothing was changed, with the safe reason.
 */
function failureText(error: unknown): string {
  return REFUSALS.includes(codeOf(error) ?? '') ? messageFor(error) : `The change was not made. ${messageFor(error)}`
}

function RoleChoices({ roles }: { roles: readonly string[] }) {
  return (
    <>
      {roles.map((role) => (
        <option key={role} value={role}>
          {roleName(role)}
        </option>
      ))}
    </>
  )
}

interface RowProps {
  member: Member
  organization: OrganizationDetail
  /** The roles the signed-in person may give; empty when the organization cannot be changed. */
  choices: readonly string[]
  changes: MemberChanges
  /** The member is no longer in the organization: the row is about to go. */
  onRemoved: () => void
}

type RowMode = 'idle' | 'role' | 'remove'

/**
 * What can be done to one member, in their row: change their role, or take
 * them out of this organization. Each opens in place -- nothing covers the
 * page -- and removal asks once more before it happens. Nothing here works out
 * whether a change is allowed: it is asked for, and the API's answer is shown.
 */
function MemberActions({ member, organization, choices, changes, onRemoved }: RowProps) {
  const navigate = useNavigate()
  const roleId = useId()
  const [mode, setMode] = useState<RowMode>('idle')
  const [role, setRole] = useState(member.role)
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const openRole = useRef<HTMLButtonElement>(null)
  const openRemove = useRef<HTMLButtonElement>(null)
  const roleSelect = useRef<HTMLSelectElement>(null)
  const confirmRemove = useRef<HTMLButtonElement>(null)
  // Which control takes focus once the row has been drawn again: the first one of what just opened, or the
  // button that opened what just closed.
  const focusNext = useRef<RefObject<HTMLElement | null> | null>(null)

  useEffect(() => {
    focusNext.current?.current?.focus()
    focusNext.current = null
  }, [mode])

  function open(next: 'role' | 'remove') {
    setRole(member.role)
    setFailure(null)
    setSaved(false)
    focusNext.current = next === 'role' ? roleSelect : confirmRemove
    setMode(next)
  }

  function close() {
    setFailure(null)
    focusNext.current = mode === 'role' ? openRole : openRemove
    setMode('idle')
  }

  function failed(error: unknown) {
    setBusy(false)
    if (isSignedOut(error)) {
      return
    }
    setFailure(failureText(error))
    if (codeOf(error) === 'member_not_found') {
      // Someone else removed them first: the list is out of date.
      changes.refresh()
    }
  }

  async function saveRole(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy) {
      return
    }
    setBusy(true)
    setFailure(null)
    try {
      // For one's own role this also waits for the organization to be read again: what this page offers from
      // here on goes by the role the API then says, not by the one just chosen.
      await changes.changeRole(member.email, role, member.is_self)
    } catch (caught) {
      failed(caught)
      return
    }
    if (member.is_self && !canManageMembers(role)) {
      // No longer one of the people this page is for. Leave it.
      void navigate(ORGANIZATIONS_PATH)
      return
    }
    setBusy(false)
    setSaved(true)
    focusNext.current = openRole
    setMode('idle')
  }

  async function remove() {
    if (busy) {
      return
    }
    setBusy(true)
    setFailure(null)
    try {
      await changes.remove(member.email)
    } catch (caught) {
      failed(caught)
      return
    }
    if (member.is_self) {
      void navigate(ORGANIZATIONS_PATH)
      return
    }
    // The list has been asked for again and this row is normally gone already. If it is still here, it is as it was.
    setBusy(false)
    setMode('idle')
    onRemoved()
  }

  if (choices.length === 0) {
    return null
  }
  // Only what is offered: an owner's row has no controls for someone who is not one. The API refuses it regardless.
  if (member.role === 'owner' && !choices.includes('owner')) {
    return <span className="quiet">Only an owner can change this member.</span>
  }

  if (mode === 'role') {
    return (
      <form className="member-editor" onSubmit={saveRole} noValidate>
        <div className="field">
          <label htmlFor={roleId}>Role for {member.email}</label>
          <select id={roleId} ref={roleSelect} value={role} onChange={(event) => setRole(event.target.value)}>
            <RoleChoices roles={choices} />
          </select>
        </div>
        {member.is_self && <p className="quiet">{SELF_WARNING}</p>}
        {failure !== null && (
          <p className="error-message" role="alert">
            {failure}
          </p>
        )}
        <div className="form-actions">
          {/* Unavailable while saving, but not `disabled`: a disabled button drops keyboard focus. */}
          <button type="submit" aria-disabled={busy}>
            {busy ? 'Saving…' : 'Save role'}
          </button>
          <button type="button" className="button-secondary" onClick={close}>
            Cancel
          </button>
        </div>
      </form>
    )
  }

  if (mode === 'remove') {
    return (
      <div className="member-editor">
        <p>
          Remove {member.email} from {organization.name}? Their SortView account and any other organizations are not
          affected.
        </p>
        {member.is_self && <p className="quiet">{SELF_WARNING}</p>}
        {failure !== null && (
          <p className="error-message" role="alert">
            {failure}
          </p>
        )}
        <div className="form-actions">
          <button type="button" ref={confirmRemove} onClick={() => void remove()} aria-disabled={busy}>
            {busy ? 'Removing…' : 'Remove'}
          </button>
          <button type="button" className="button-secondary" onClick={close}>
            Cancel
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="form-actions">
      <button type="button" className="button-secondary" ref={openRole} onClick={() => open('role')}>
        Change role <span className="visually-hidden">for {member.email}</span>
      </button>
      <button type="button" className="button-secondary" ref={openRemove} onClick={() => open('remove')}>
        Remove <span className="visually-hidden">{member.email}</span>
      </button>
      {saved && (
        <p className="form-saved" role="status">
          Role updated.
        </p>
      )}
    </div>
  )
}

function MembersTable({ members, ...row }: { members: Member[] } & Omit<RowProps, 'member'>) {
  const writable = row.choices.length > 0

  return (
    <>
      <div className="table-scroll">
        <table className="data-table members-table" aria-labelledby="members-heading">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Email</th>
              <th scope="col">Role</th>
              <th scope="col">Account</th>
              {writable && <th scope="col">Actions</th>}
            </tr>
          </thead>
          <tbody>
            {members.map((member) => (
              <tr key={member.email}>
                <th scope="row">
                  {member.full_name.trim() === '' ? <span className="quiet">Not provided</span> : member.full_name}
                  {member.is_self && ' (you)'}
                </th>
                <td>{member.email}</td>
                <td>{roleName(member.role)}</td>
                {/* The person's whole SortView account, in words. There is nothing here to change it with. */}
                <td>{member.account_active ? 'Active' : 'Deactivated'}</td>
                {writable && (
                  <td>
                    <MemberActions member={member} {...row} />
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {members.some((member) => !member.account_active) && (
        <p className="quiet">
          A deactivated SortView account cannot sign in to any organization. This is managed by SortView support and
          cannot be changed here.
        </p>
      )}
    </>
  )
}

const ADD_FIELDS = ['email', 'role'] as const

/**
 * Adds someone to the organization: a name, an email and a role, and no
 * password. The API answers the same way whether or not the email already had
 * a SortView account, so what is said afterwards is the same too.
 */
function AddMemberForm({ choices, changes }: { choices: readonly string[]; changes: MemberChanges }) {
  const [fullName, setFullName] = useState('')
  const [email, setEmail] = useState('')
  const [role, setRole] = useState(choices.includes('viewer') ? 'viewer' : choices[0])
  const [problems, setProblems] = useState<Partial<Record<(typeof ADD_FIELDS)[number], string>>>({})
  const [failure, setFailure] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [added, setAdded] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (adding) {
      return
    }
    setAdded(false)
    setFailure(null)
    // Nothing is asked of the API without an email. Every other rule is the API's.
    if (email.trim() === '') {
      setProblems({ email: 'Enter an email address.' })
      return
    }

    setAdding(true)
    setProblems({})
    try {
      await changes.add({ email, full_name: fullName, role })
      setFullName('')
      setEmail('')
      setAdded(true)
    } catch (caught) {
      if (isSignedOut(caught)) {
        return
      }
      const refused = fieldProblems(caught, INVALID_MEMBER)
      const sentences = refused === null ? {} : memberProblems(refused, ADD_FIELDS)
      if (Object.keys(sentences).length > 0) {
        setProblems(sentences)
      } else if (codeOf(caught) === 'already_member') {
        setProblems({ email: messageFor(caught) })
      } else {
        // What was typed stays where it is.
        setFailure(failureText(caught))
      }
    } finally {
      setAdding(false)
    }
  }

  return (
    <form className="member-form" onSubmit={handleSubmit} aria-labelledby="add-member-heading" noValidate>
      {failure !== null && (
        <p className="error-message" role="alert">
          {failure}
        </p>
      )}

      <div className="field">
        <label htmlFor="member-full-name">Full name</label>
        <input
          id="member-full-name"
          name="full_name"
          type="text"
          autoComplete="off"
          value={fullName}
          onChange={(event) => setFullName(event.target.value)}
        />
      </div>

      <div className="field">
        <label htmlFor="member-email">Email</label>
        <input
          id="member-email"
          name="email"
          type="email"
          autoComplete="off"
          value={email}
          aria-invalid={problems.email !== undefined}
          aria-describedby={problems.email === undefined ? 'member-add-help' : 'member-email-problem member-add-help'}
          onChange={(event) => {
            setEmail(event.target.value)
            setAdded(false)
          }}
        />
        {problems.email !== undefined && (
          <p className="field-problem" id="member-email-problem">
            {problems.email}
          </p>
        )}
      </div>

      <div className="field">
        <label htmlFor="member-role">Role</label>
        <select
          id="member-role"
          name="role"
          value={role}
          aria-invalid={problems.role !== undefined}
          aria-describedby={problems.role === undefined ? undefined : 'member-role-problem'}
          onChange={(event) => setRole(event.target.value)}
        >
          <RoleChoices roles={choices} />
        </select>
        {problems.role !== undefined && (
          <p className="field-problem" id="member-role-problem">
            {problems.role}
          </p>
        )}
      </div>

      <p className="field-help" id="member-add-help">
        If this person already has a SortView account, they keep their existing password and sign in as usual. If
        they are new, they set a password by choosing &lsquo;Forgot password?&rsquo; on the sign-in page. No email is
        sent from here.
      </p>

      <div className="form-actions">
        {/* Unavailable while adding, but not `disabled`: a disabled button drops keyboard focus. */}
        <button type="submit" aria-disabled={adding}>
          {adding ? 'Adding…' : 'Add member'}
        </button>
        {added && (
          <p className="form-saved" role="status">
            Added. If they are new to SortView, they will need to use &lsquo;Forgot password?&rsquo; to set a
            password.
          </p>
        )}
      </div>
    </form>
  )
}

function RecentChanges({ changes }: { changes: MemberActivity[] }) {
  if (changes.length === 0) {
    return <p>No member changes have been recorded yet.</p>
  }
  return (
    <div className="table-scroll">
      <table className="data-table members-table" aria-labelledby="member-activity-heading">
        <thead>
          <tr>
            <th scope="col">When</th>
            <th scope="col">Change</th>
            <th scope="col">By</th>
          </tr>
        </thead>
        <tbody>
          {changes.map((change, index) => (
            // A list that is only ever replaced whole: its order is its identity.
            <tr key={index}>
              <td>{formatLocalInstant(change.occurred_at) ?? 'Not recorded'}</td>
              <th scope="row">{activitySentence(change)}</th>
              <td>{change.actor_email ?? 'Not recorded'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * The way back up, the page's name, and -- for the people it is for -- the strip of sections it is one of. It
 * keeps its own address; it is drawn as part of the organization's configuration.
 */
function Frame({ organization, offered = true, children }: { organization: OrganizationDetail; offered?: boolean; children: ReactNode }) {
  return (
    <SettingsFrame organization={organization} section={MEMBERS_PAGE_NAME} offered={offered}>
      {children}
    </SettingsFrame>
  )
}

/** The page once it is known that it is for this person: the members, a way to add one, and what changed lately. */
function MembersSections({ organization }: { organization: OrganizationDetail }) {
  const { members, retry } = useMembers(organization.slug, true)
  const { activity, retry: retryActivity } = useMemberActivity(organization.slug, true)
  const changes = useMemberChanges(organization.slug)
  const heading = useRef<HTMLHeadingElement>(null)
  const [removed, setRemoved] = useState(false)

  // A suspended organization's members can be read and not changed: nothing to change them with is offered.
  const writable = organization.access_mode === 'full'
  const choices = writable ? assignableRoles(organization.role) : []

  switch (members.status) {
    case 'loading':
      return (
        <Frame organization={organization}>
          <p role="status">Loading members…</p>
        </Frame>
      )
    case 'not_found':
      // The organization is no longer this person's to see: the one page for that, and nothing of this one.
      return <NotFoundPage />
    case 'forbidden':
      return (
        <Frame organization={organization}>
          <p className="notice" role="note">
            {MANAGED_BY_OWNERS_AND_ADMINS}
          </p>
        </Frame>
      )
    case 'error':
      return (
        <Frame organization={organization}>
          <LoadFailure message={members.message} onRetry={retry} />
        </Frame>
      )
  }

  return (
    <Frame organization={organization}>
    <div className="members">
      {!writable && (
        <p className="notice" role="note">
          This organization is suspended, so its members cannot be changed.
        </p>
      )}

      <section className="panel" aria-labelledby="members-heading">
        {/* Takes focus when a member has been removed: the row that held it is gone. Not a tab stop. */}
        <h3 id="members-heading" ref={heading} tabIndex={-1}>
          Members
        </h3>
        {removed && (
          <p className="form-saved" role="status">
            Removed from this organization.
          </p>
        )}
        <MembersTable
          members={members.data}
          organization={organization}
          choices={choices}
          changes={changes}
          onRemoved={() => {
            setRemoved(true)
            heading.current?.focus()
          }}
        />
      </section>

      {writable && (
        <section className="panel" aria-labelledby="add-member-heading">
          <h3 id="add-member-heading">Add a member</h3>
          <AddMemberForm choices={choices} changes={changes} />
        </section>
      )}

      <section className="panel" aria-labelledby="member-activity-heading">
        <h3 id="member-activity-heading">Recent changes</h3>
        {activity.status === 'loading' && <p role="status">Loading recent changes…</p>}
        {activity.status === 'error' && <LoadFailure message={activity.message} onRetry={retryActivity} />}
        {activity.status === 'ready' && <RecentChanges changes={activity.data} />}
      </section>
    </div>
    </Frame>
  )
}

/**
 * /organizations/:orgSlug/members -- who belongs to the organization, with
 * what role, and the means to add, change and remove them.
 *
 * For the organization's owners and admins. Anyone else is told so and is
 * shown nothing else: no list is asked for on their behalf. That is only this
 * app not asking -- the API refuses them whatever this page does.
 */
export function MembersPage() {
  const organization = useOutletContext<OrganizationDetail>()

  if (!canManageMembers(organization.role)) {
    return (
      <Frame organization={organization} offered={false}>
        <p className="notice" role="note">
          {MANAGED_BY_OWNERS_AND_ADMINS}
        </p>
      </Frame>
    )
  }
  return <MembersSections organization={organization} />
}
