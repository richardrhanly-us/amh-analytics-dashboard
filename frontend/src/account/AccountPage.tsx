import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router'

import {
  changePassword,
  fieldProblems,
  getAccount,
  INVALID_PASSWORD_CHANGE,
  INVALID_PROFILE,
  updateAccountProfile,
  type Account,
} from '../api/account.ts'
import { isApiError } from '../api/client.ts'
import { useAuth } from '../auth/useAuth.ts'
import { Breadcrumb } from '../components/Breadcrumb.tsx'
import { messageFor } from '../components/errorText.ts'
import { LoadFailure } from '../components/LoadFailure.tsx'
import { PageHeading } from '../components/PageHeading.tsx'
import { useResource } from '../hooks/useResource.ts'
import { ORGANIZATIONS_PATH } from '../router/paths.ts'
import { formatLocalInstant } from '../time/localTime.ts'
import { PasswordField } from './PasswordField.tsx'
import { problemSentences } from './problemText.ts'

const loadAccount = (_key: string, signal: AbortSignal) => getAccount(signal)
const isSignedOut = (error: unknown) => isApiError(error) && error.status === 401

/**
 * The person's name, which they can change, and the email they sign in with,
 * which they cannot. Nothing is shown as saved until the API has said so, and
 * what is then shown is the name the API stored.
 */
function ProfileForm({ account, onSaved }: { account: Account; onSaved: (account: Account) => void }) {
  const { sessionExpired, updateUserName } = useAuth()
  const [name, setName] = useState(account.full_name)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [problem, setProblem] = useState<string | undefined>(undefined)
  const [failure, setFailure] = useState<string | null>(null)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (saving) {
      return
    }
    setSaving(true)
    setSaved(false)
    setProblem(undefined)
    setFailure(null)
    try {
      const stored = await updateAccountProfile(name)
      setName(stored.full_name)
      setSaved(true)
      onSaved(stored)
      // The header shows the name too.
      updateUserName(stored.full_name)
    } catch (caught) {
      if (isSignedOut(caught)) {
        sessionExpired()
        return
      }
      const refused = fieldProblems(caught, INVALID_PROFILE)
      const sentence = refused === null ? undefined : problemSentences(refused, ['full_name']).full_name
      if (sentence !== undefined) {
        setProblem(sentence)
      } else {
        setFailure(`Your name was not saved. ${messageFor(caught)}`)
      }
    } finally {
      setSaving(false)
    }
  }

  return (
    <form className="account-form" onSubmit={handleSubmit} aria-labelledby="account-profile-heading" noValidate>
      {failure !== null && (
        <p className="error-message" role="alert">
          {failure}
        </p>
      )}

      <div className="field">
        <label htmlFor="account-full-name">Full name</label>
        <input
          id="account-full-name"
          name="full_name"
          type="text"
          autoComplete="name"
          value={name}
          aria-invalid={problem !== undefined}
          aria-describedby={problem === undefined ? undefined : 'account-full-name-problem'}
          onChange={(event) => {
            setName(event.target.value)
            setSaved(false)
          }}
        />
        {problem !== undefined && (
          <p className="field-problem" id="account-full-name-problem">
            {problem}
          </p>
        )}
      </div>

      {/* Not a box to type in: the email is how this person signs in and cannot be changed here. */}
      <dl className="account-facts">
        <div>
          <dt>Email</dt>
          <dd>{account.email}</dd>
          <dd className="field-help">You sign in with this email address. It cannot be changed here.</dd>
        </div>
      </dl>

      <div className="form-actions">
        {/* Unavailable while saving, but not `disabled`: a disabled button drops keyboard focus. */}
        <button type="submit" aria-disabled={saving}>
          {saving ? 'Saving…' : 'Save name'}
        </button>
        {saved && (
          <p className="form-saved" role="status">
            Saved.
          </p>
        )}
      </div>
    </form>
  )
}

/** When this person last signed in and last changed their password, in their own time zone. */
function Activity({ account }: { account: Account }) {
  return (
    <dl className="account-facts">
      <div>
        <dt>Last signed in</dt>
        <dd>{formatLocalInstant(account.last_login_at) ?? 'Not available'}</dd>
      </div>
      <div>
        <dt>Password last changed</dt>
        <dd>{formatLocalInstant(account.last_password_changed_at) ?? 'Not recorded'}</dd>
      </div>
    </dl>
  )
}

const PASSWORD_FIELDS = ['current_password', 'new_password', 'confirm_password'] as const
type PasswordFieldName = (typeof PASSWORD_FIELDS)[number]
const EMPTY = { current_password: '', new_password: '', confirm_password: '' }
const REQUIRED: Record<PasswordFieldName, string> = {
  current_password: 'Enter your current password.',
  new_password: 'Enter a new password.',
  confirm_password: 'Enter your new password again.',
}

/**
 * Changes the password. The API ends every session this person has when it
 * does -- this one too -- so a successful change signs them out here at once
 * and takes them to the sign-in form, which says why.
 */
function ChangePasswordForm({ email }: { email: string }) {
  const { passwordChanged, sessionExpired } = useAuth()
  const navigate = useNavigate()
  const [values, setValues] = useState<Record<PasswordFieldName, string>>(EMPTY)
  const [problems, setProblems] = useState<Partial<Record<PasswordFieldName, string>>>({})
  const [failure, setFailure] = useState<string | null>(null)
  const [changing, setChanging] = useState(false)

  const set = (field: PasswordFieldName) => (value: string) => setValues({ ...values, [field]: value })

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (changing) {
      return
    }
    // Nothing is asked of the API until all three are filled in. Every other rule is the API's.
    const missing = PASSWORD_FIELDS.filter((field) => values[field] === '')
    if (missing.length > 0) {
      setProblems(Object.fromEntries(missing.map((field) => [field, REQUIRED[field]])))
      setFailure(null)
      return
    }

    setChanging(true)
    setProblems({})
    setFailure(null)
    try {
      await changePassword(values.current_password, values.new_password, values.confirm_password)
      // The passwords do not outlive the change.
      setValues(EMPTY)
      // Signed out, by the change itself: leave this page for the start, where the sign-in form says why.
      void navigate('/', { replace: true })
      passwordChanged()
    } catch (caught) {
      setChanging(false)
      if (isSignedOut(caught)) {
        sessionExpired()
        return
      }
      const refused = fieldProblems(caught, INVALID_PASSWORD_CHANGE)
      const sentences = refused === null ? {} : problemSentences(refused, PASSWORD_FIELDS)
      if (Object.keys(sentences).length > 0) {
        setProblems(sentences)
      } else {
        setFailure(`Your password was not changed. ${messageFor(caught)}`)
      }
    }
  }

  return (
    <form className="account-form" onSubmit={handleSubmit} aria-labelledby="account-security-heading" noValidate>
      <p className="quiet">
        Changing your password signs you out everywhere, including here. You will sign in again with the new
        password.
      </p>

      {failure !== null && (
        <p className="error-message" role="alert">
          {failure}
        </p>
      )}

      {/* For password managers only: which account these passwords belong to. Not shown, and not sent. */}
      <input type="text" name="username" autoComplete="username" value={email} readOnly hidden />

      <PasswordField
        id="account-current-password"
        name="current_password"
        label="Current password"
        autoComplete="current-password"
        value={values.current_password}
        onChange={set('current_password')}
        problem={problems.current_password}
      />
      <PasswordField
        id="account-new-password"
        name="new_password"
        label="New password"
        autoComplete="new-password"
        value={values.new_password}
        onChange={set('new_password')}
        help="At least 8 characters."
        problem={problems.new_password}
      />
      <PasswordField
        id="account-confirm-password"
        name="confirm_password"
        label="Confirm new password"
        autoComplete="new-password"
        value={values.confirm_password}
        onChange={set('confirm_password')}
        problem={problems.confirm_password}
      />

      <div className="form-actions">
        {/* Unavailable while changing, but not `disabled`: a disabled button drops keyboard focus. */}
        <button type="submit" aria-disabled={changing}>
          {changing ? 'Changing password…' : 'Change password'}
        </button>
      </div>
    </form>
  )
}

function AccountSections({ loaded }: { loaded: Account }) {
  // What the API last said the account is: what was loaded, then whatever a save answered with.
  const [account, setAccount] = useState(loaded)

  return (
    <div className="account">
      <section className="panel" aria-labelledby="account-profile-heading">
        <h3 id="account-profile-heading">Profile</h3>
        <ProfileForm account={account} onSaved={setAccount} />
      </section>

      <section className="panel" aria-labelledby="account-activity-heading">
        <h3 id="account-activity-heading">Account activity</h3>
        <Activity account={account} />
      </section>

      <section className="panel" aria-labelledby="account-security-heading">
        <h3 id="account-security-heading">Security</h3>
        <ChangePasswordForm email={account.email} />
      </section>
    </div>
  )
}

/**
 * /account -- the signed-in person's own account: their name, the email they
 * sign in with, when they last signed in and changed their password, and a
 * way to change the password.
 *
 * It is about the person and not about any organization: it is at no
 * organization's address and asks for none.
 */
export function AccountPage() {
  const { resource, retry } = useResource('', loadAccount)

  return (
    <>
      <Breadcrumb trail={[{ to: ORGANIZATIONS_PATH, label: 'Organizations' }]} current="My account" />
      <PageHeading>My account</PageHeading>

      {resource.status === 'loading' && <p role="status">Loading your account…</p>}
      {resource.status === 'error' && <LoadFailure message={resource.message} onRetry={retry} />}
      {/* One's own account is never "not found": a 403 or 404 here is a fault, and gets the same retry. */}
      {resource.status === 'unavailable' && <LoadFailure message="Something went wrong. Please try again." onRetry={retry} />}
      {resource.status === 'ready' && <AccountSections loaded={resource.data} />}
    </>
  )
}
