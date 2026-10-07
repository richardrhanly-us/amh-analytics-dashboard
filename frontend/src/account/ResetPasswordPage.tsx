import { useEffect, useState, type FormEvent } from 'react'
import { Link, useLocation, useNavigate } from 'react-router'

import { completePasswordReset, fieldProblems, INVALID_PASSWORD_RESET, INVALID_RESET_TOKEN } from '../api/account.ts'
import { isApiError } from '../api/client.ts'
import { useAuth } from '../auth/useAuth.ts'
import { ArrivalHeading } from '../components/ArrivalHeading.tsx'
import { ErrorMessage } from '../components/ErrorMessage.tsx'
import { messageFor } from '../components/errorText.ts'
import { RESET_PASSWORD_PATH } from '../router/paths.ts'
import { PasswordField } from './PasswordField.tsx'
import { problemSentences } from './problemText.ts'

const TOKEN_PARAMETER = 'token'
// The API reads no more than this much token. Anything longer is not one it sent.
const TOKEN_MAX_LENGTH = 512
const INVALID_LINK = 'This password reset link is invalid or has expired.'

/**
 * The token in an emailed link's fragment, "#token=...", or null when the
 * fragment does not hold exactly one usable token. The fragment is
 * URL-encoded like a query string, and is read the same way.
 */
function tokenIn(hash: string): string | null {
  const tokens = new URLSearchParams(hash.startsWith('#') ? hash.slice(1) : hash).getAll(TOKEN_PARAMETER)
  if (tokens.length !== 1 || tokens[0] === '' || tokens[0].length > TOKEN_MAX_LENGTH) {
    return null
  }
  return tokens[0]
}

/**
 * Holds the reset token for as long as the form needs it, and nowhere a
 * person or a tool would come across it: in a closure, not in component
 * state, so it is not in anything React shows of this component. It is
 * never written to storage, a cookie, the address or a log.
 */
function holdToken(token: string | null) {
  let held = token
  return {
    held: () => held !== null,
    /** The token, for the one request that needs it. */
    take: () => held,
    /** Whether this is the token being held. */
    holds: (candidate: string) => held === candidate,
    forget: () => {
      held = null
    },
  }
}

type HeldToken = ReturnType<typeof holdToken>

const FIELDS = ['new_password', 'confirm_password'] as const
type FieldName = (typeof FIELDS)[number]
const REQUIRED: Record<FieldName, string> = {
  new_password: 'Enter a new password.',
  confirm_password: 'Enter your new password again.',
}

/**
 * /reset-password -- sets a new password with the token from an emailed
 * link, "/reset-password#token=...". Shown to anyone, signed in or not: the
 * person following the link has, by definition, no way to sign in.
 *
 * THE TOKEN is taken from the address's fragment as the page opens and the
 * fragment is removed from the address at once, so it is not left in the
 * address bar or in the list of pages the browser can go back to. From then on it exists only in this
 * page's memory, and goes to the API in the body of the one request that
 * completes the reset. It is never shown.
 *
 * Completing a reset signs nobody in, and signs out whoever was signed in
 * here: it ends with a way to the sign-in form, where the new password is
 * used.
 */
export function ResetPasswordPage() {
  const location = useLocation()
  const navigate = useNavigate()
  // What the page opened with: read as it opens, and not again once the fragment has been removed.
  const [arrival, setArrival] = useState(() => ({ number: 0, token: holdToken(tokenIn(location.hash)) }))

  // Another link followed while this page is already open only changes the fragment: the browser does not load
  // the page again. That link's token replaces whatever was here, and the form starts afresh for it.
  const arriving = tokenIn(location.hash)
  if (arriving !== null && !arrival.token.holds(arriving)) {
    setArrival({ number: arrival.number + 1, token: holdToken(arriving) })
  }

  // Whatever the fragment held -- a token, or something that was not one -- it does not stay in the address.
  const leftover = location.hash !== '' || location.search !== ''
  useEffect(() => {
    if (leftover) {
      void navigate(RESET_PASSWORD_PATH, { replace: true })
    }
  }, [leftover, navigate])

  return <ResetPassword key={arrival.number} token={arrival.token} />
}

function ResetPassword({ token }: { token: HeldToken }) {
  const { state, sessionExpired } = useAuth()
  const [phase, setPhase] = useState<'form' | 'invalid' | 'done'>(() => (token.held() ? 'form' : 'invalid'))
  const [moved, setMoved] = useState(false)
  const [values, setValues] = useState<Record<FieldName, string>>({ new_password: '', confirm_password: '' })
  const [problems, setProblems] = useState<Partial<Record<FieldName, string>>>({})
  const [failure, setFailure] = useState<string | null>(null)
  const [resetting, setResetting] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const held = token.take()
    if (resetting || held === null) {
      return
    }
    const missing = FIELDS.filter((field) => values[field] === '')
    if (missing.length > 0) {
      setProblems(Object.fromEntries(missing.map((field) => [field, REQUIRED[field]])))
      setFailure(null)
      return
    }

    setResetting(true)
    setProblems({})
    setFailure(null)
    try {
      await completePasswordReset(held, values.new_password, values.confirm_password)
      token.forget()
      // The API has ended every session of the account, so whatever session this browser held is not to be
      // trusted either. It is dropped here and now, with no request: there is none left to end or to ask about.
      sessionExpired()
      setValues({ new_password: '', confirm_password: '' })
      setMoved(true)
      setPhase('done')
    } catch (caught) {
      if (isApiError(caught) && caught.code === INVALID_RESET_TOKEN) {
        // Never existed, expired, or already used: one answer, and the link is of no further use.
        token.forget()
        setValues({ new_password: '', confirm_password: '' })
        setMoved(true)
        setPhase('invalid')
        return
      }
      const refused = fieldProblems(caught, INVALID_PASSWORD_RESET)
      const sentences = refused === null ? {} : problemSentences(refused, FIELDS)
      if (Object.keys(sentences).length > 0) {
        setProblems(sentences)
      } else {
        setFailure(`Your password was not reset. ${messageFor(caught)}`)
      }
    } finally {
      setResetting(false)
    }
  }

  if (phase === 'done') {
    return (
      <section className="auth-form" aria-labelledby="reset-heading">
        <ArrivalHeading id="reset-heading" arrive={moved}>
          Password reset
        </ArrivalHeading>
        <p role="status">Password reset. Sign in with your new password.</p>
        <p>
          <Link to="/" replace>
            Go to sign in
          </Link>
        </p>
      </section>
    )
  }

  if (phase === 'invalid') {
    // Nothing was reset, so a session this browser holds is untouched: signed in, there is no sign-in form to go to.
    const onward = state.status === 'authenticated' ? 'Continue to SortView' : 'Go to sign in'
    return (
      <section className="auth-form" aria-labelledby="reset-heading">
        <ArrivalHeading id="reset-heading" arrive={moved}>
          Reset link not valid
        </ArrivalHeading>
        <p className="error-message" role="alert">
          {INVALID_LINK}
        </p>
        <p>
          A reset link works once and expires after 30 minutes. To get a new one, choose “Forgot password?” on the
          sign-in form.
        </p>
        <p>
          <Link to="/" replace>
            {onward}
          </Link>
        </p>
      </section>
    )
  }

  return (
    <form className="auth-form" onSubmit={handleSubmit} aria-labelledby="reset-heading" noValidate>
      <ArrivalHeading id="reset-heading" arrive={false}>
        Choose a new password
      </ArrivalHeading>

      <ErrorMessage message={failure} />

      <PasswordField
        id="reset-new-password"
        name="new_password"
        label="New password"
        autoComplete="new-password"
        value={values.new_password}
        onChange={(value) => setValues({ ...values, new_password: value })}
        help="At least 8 characters."
        problem={problems.new_password}
      />
      <PasswordField
        id="reset-confirm-password"
        name="confirm_password"
        label="Confirm new password"
        autoComplete="new-password"
        value={values.confirm_password}
        onChange={(value) => setValues({ ...values, confirm_password: value })}
        problem={problems.confirm_password}
      />

      {/* Unavailable while resetting, but not `disabled`: a disabled button drops keyboard focus. */}
      <button type="submit" aria-disabled={resetting}>
        {resetting ? 'Resetting password…' : 'Reset password'}
      </button>
    </form>
  )
}
