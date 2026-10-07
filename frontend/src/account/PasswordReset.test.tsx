import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation, useNavigate } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import App from '../App.tsx'
import { AuthProvider } from '../auth/AuthProvider.tsx'
import {
  ALICE,
  type ApiRoutes,
  callOf,
  deferred,
  type FetchMock,
  jsonResponse,
  networkFailure,
  noContent,
  NOT_AUTHENTICATED,
  serveApi,
} from '../test/http.ts'

// Made up, and unlike anything else on the page, so that finding it anywhere is finding a leak.
// No pause between keystrokes: these tests type two passwords each, and waiting on a timer for every key makes
// this file slow enough to crowd the others when the whole suite runs at once.
const INSTANT = { delay: null }
const TOKEN = 'synthetic-RESET-token-5f0c9a'
const OTHER_TOKEN = 'synthetic-SECOND-token-77b1e2'
const NEW = 'synthetic-New-2'
const REQUESTED = {
  code: 'password_reset_requested',
  message: 'If an active account exists for that email address, password reset instructions will be sent.',
}
const INVALID_TOKEN = () => jsonResponse(400, { code: 'invalid_reset_token', message: 'This password reset link is invalid or has expired.' })
const RATE_LIMITED = () => jsonResponse(429, { error: 'Rate limit exceeded: 5 per 1 minute' })
const refused = (field: string, why: string) => () =>
  jsonResponse(422, { code: 'invalid_password_reset', message: 'Not valid.', problems: [{ field, code: why }] })

/**
 * The whole address -- path, query and fragment -- and stand-ins for the browser's Back button and for
 * following an emailed link while the app is already open, which does not load the page again.
 */
function Browser() {
  const location = useLocation()
  const navigate = useNavigate()
  return (
    <div>
      <span data-testid="whole-address">{`${location.pathname}${location.search}${location.hash}`}</span>
      <button onClick={() => navigate(-1)}>browser-back</button>
      <button onClick={() => navigate({ pathname: '/reset-password', hash: `token=${TOKEN}` })}>follow-first-link</button>
      <button onClick={() => navigate({ pathname: '/reset-password', hash: `token=${OTHER_TOKEN}` })}>follow-second-link</button>
    </div>
  )
}

/** Nobody signed in, unless a test replaces the session route. */
function serve(overrides: ApiRoutes = {}): FetchMock {
  return serveApi({ 'GET /api/auth/session': () => jsonResponse(401, NOT_AUTHENTICATED), ...overrides })
}

function open(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <App />
      </AuthProvider>
      <Browser />
    </MemoryRouter>,
  )
}

const main = () => screen.getByRole('main')
const address = () => screen.getByTestId('whole-address').textContent
const requests = (fetchMock: FetchMock) => fetchMock.mock.calls.map(([url, init]) => `${init?.method ?? 'GET'} ${String(url)}`)
const COMPLETE = 'POST /api/auth/password-reset/complete'
const REQUEST = 'POST /api/auth/password-reset/request'
function bodies(fetchMock: FetchMock, request: string): unknown[] {
  return requests(fetchMock).flatMap((made, index) => (made === request ? [JSON.parse(String(callOf(fetchMock, index).init.body)) as unknown] : []))
}

async function fillAndReset(user: ReturnType<typeof userEvent.setup>, next = NEW, confirm = next) {
  await user.type(screen.getByLabelText('New password'), next)
  await user.type(screen.getByLabelText('Confirm new password'), confirm)
  await user.click(screen.getByRole('button', { name: 'Reset password' }))
}

// =====================================================================================================================
// Forgot password
// =====================================================================================================================

describe('asking for a reset link', () => {
  async function forgot(user: ReturnType<typeof userEvent.setup>) {
    open('/')
    await user.click(await screen.findByRole('button', { name: 'Forgot password?' }))
  }

  it('is reached from the sign-in form, takes the address already typed, and never the password', async () => {
    serve()
    const user = userEvent.setup(INSTANT)
    open('/')
    await user.type(await screen.findByLabelText('Email'), '  alice@example.test ')
    await user.type(screen.getByLabelText('Password'), 'synthetic-typed-password')

    await user.click(screen.getByRole('button', { name: 'Forgot password?' }))

    expect(screen.getByRole('heading', { level: 2, name: 'Reset your password' })).toHaveFocus()
    expect(screen.getByRole('form', { name: 'Reset your password' })).toBeInTheDocument()
    expect(screen.getByLabelText('Email')).toHaveValue('alice@example.test')
    expect(screen.getByLabelText('Email')).toHaveAttribute('type', 'email')
    expect(screen.getByLabelText('Email')).toHaveAttribute('autocomplete', 'username')
    expect(main().querySelector('input[type="password"]')).toBeNull()
    // It is a view of the sign-in page, not another address.
    expect(address()).toBe('/')

    // Coming back, the password box is empty: it was dropped on the way out.
    await user.click(screen.getByRole('button', { name: 'Back to sign in' }))
    expect(screen.getByRole('heading', { level: 2, name: 'Sign in' })).toHaveFocus()
    expect(screen.getByLabelText('Password')).toHaveValue('')
    expect(screen.getByLabelText('Email')).toHaveValue('alice@example.test')
  })

  it('sends the trimmed address, then shows the one fixed sentence without repeating the address', async () => {
    const fetchMock = serve({ [REQUEST]: () => jsonResponse(202, REQUESTED) })
    const user = userEvent.setup(INSTANT)
    await forgot(user)

    await user.type(screen.getByLabelText('Email'), '  alice@example.test  {Enter}')

    expect(await screen.findByRole('heading', { level: 2, name: 'Check your email' })).toHaveFocus()
    expect(screen.getByRole('status')).toHaveTextContent(/^If an active account exists for that email address, password reset instructions will be sent\.$/)
    expect(bodies(fetchMock, REQUEST)).toStrictEqual([{ email: 'alice@example.test' }])
    expect(main()).not.toHaveTextContent(/alice|@/)
    expect(screen.queryByLabelText('Email')).not.toBeInTheDocument()
    // Nobody was signed in by asking.
    expect(screen.queryByRole('navigation', { name: 'Account' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Back to sign in' }))
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('looks exactly the same for an address with an account and one without', async () => {
    const shown: string[] = []
    for (const email of ['alice@example.test', 'nobody-at-all@elsewhere.test']) {
      // The API's answer is the same for both; so is everything this app does with it.
      serve({ [REQUEST]: () => jsonResponse(202, { ...REQUESTED, account_exists: email.startsWith('alice') }) })
      const user = userEvent.setup(INSTANT)
      const view = open('/')
      await user.click(await screen.findByRole('button', { name: 'Forgot password?' }))
      await user.type(screen.getByLabelText('Email'), `${email}{Enter}`)
      await screen.findByRole('heading', { name: 'Check your email' })
      shown.push(main().innerHTML)
      view.unmount()
    }

    expect(shown[0]).toBe(shown[1])
    expect(shown[0]).not.toMatch(/account_exists|true|false/)
  })

  it('says reset is unavailable when the API cannot send email, and says nothing about the address', async () => {
    serve({ [REQUEST]: () => jsonResponse(503, { code: 'password_reset_unavailable', message: 'Password reset is not available right now.' }) })
    const user = userEvent.setup(INSTANT)
    await forgot(user)

    await user.type(screen.getByLabelText('Email'), 'alice@example.test{Enter}')

    expect(await screen.findByRole('alert')).toHaveTextContent(/^Password reset is not available right now\.$/)
    expect(screen.queryByText(/If an active account exists/)).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/password_reset_unavailable|503|no account|not found|unknown/i)
    // The form is still there to try again.
    expect(screen.getByLabelText('Email')).toHaveValue('alice@example.test')
    expect(screen.getByRole('button', { name: 'Send reset link' })).toHaveAttribute('aria-disabled', 'false')
  })

  it('says to wait when there have been too many requests', async () => {
    serve({ [REQUEST]: RATE_LIMITED })
    const user = userEvent.setup(INSTANT)
    await forgot(user)

    await user.type(screen.getByLabelText('Email'), 'alice@example.test{Enter}')

    expect(await screen.findByRole('alert')).toHaveTextContent('Too many attempts. Please wait a moment and try again.')
    expect(main()).not.toHaveTextContent(/Rate limit exceeded|per 1 minute/)
  })

  it('says something safe when the request fails outright or the answer is not the expected one', async () => {
    let calls = 0
    serve({ [REQUEST]: () => (calls++ === 0 ? Promise.reject(networkFailure()) : jsonResponse(202, { code: 'something_else' })) })
    const user = userEvent.setup(INSTANT)
    await forgot(user)

    await user.type(screen.getByLabelText('Email'), 'alice@example.test{Enter}')
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.queryByText(/If an active account exists/)).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Send reset link' }))
    await waitFor(() => expect(calls).toBe(2))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    // An answer that is not the API's "requested" is not taken for one.
    expect(screen.queryByRole('heading', { name: 'Check your email' })).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/TypeError|something_else|\[object/)
  })

  it('sends one request however often the button is pressed, and nothing for an empty address', async () => {
    const pending = deferred<Response>()
    const fetchMock = serve({ [REQUEST]: () => pending.promise })
    const user = userEvent.setup(INSTANT)
    await forgot(user)

    await user.type(screen.getByLabelText('Email'), '   {Enter}')
    await user.click(screen.getByRole('button', { name: 'Send reset link' }))
    expect(requests(fetchMock)).not.toContain(REQUEST)

    await user.type(screen.getByLabelText('Email'), 'alice@example.test')
    await user.click(screen.getByRole('button', { name: 'Send reset link' }))
    const busy = screen.getByRole('button', { name: 'Sending…' })
    await user.click(busy)
    await user.type(screen.getByLabelText('Email'), '{Enter}')

    expect(busy).toHaveAttribute('aria-disabled', 'true')
    expect(busy).not.toBeDisabled()
    expect(requests(fetchMock).filter((request) => request === REQUEST)).toHaveLength(1)
    pending.resolve(jsonResponse(202, REQUESTED))
    expect(await screen.findByRole('heading', { name: 'Check your email' })).toBeInTheDocument()
  })

  it('is not offered to someone who is signed in', async () => {
    serve({ 'GET /api/auth/session': () => jsonResponse(200, ALICE), 'GET /api/organizations': () => jsonResponse(200, []) })

    open('/')

    await screen.findByRole('navigation', { name: 'Account' })
    expect(screen.queryByRole('button', { name: 'Forgot password?' })).not.toBeInTheDocument()
  })
})

// =====================================================================================================================
// The reset page: the link and its token
// =====================================================================================================================

describe('opening a reset link', () => {
  it('is public: the form is shown to someone who is not signed in', async () => {
    serve()

    open(`/reset-password#token=${TOKEN}`)

    expect(await screen.findByRole('heading', { level: 2, name: 'Choose a new password' })).toBeInTheDocument()
    expect(screen.getByRole('form', { name: 'Choose a new password' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Email')).not.toBeInTheDocument()
  })

  it('takes the token out of the address at once, and Back does not bring it back', async () => {
    serve()
    const user = userEvent.setup(INSTANT)

    open(`/reset-password#token=${TOKEN}`)

    await waitFor(() => expect(address()).toBe('/reset-password'))
    // The entry with the token was replaced, not added to: there is nothing to go back to.
    await user.click(screen.getByRole('button', { name: 'browser-back' }))
    expect(address()).toBe('/reset-password')
    expect(screen.getByRole('heading', { name: 'Choose a new password' })).toBeInTheDocument()
  })

  it('shows the token nowhere, stores it nowhere, logs it nowhere and sends it nowhere until the form is submitted', async () => {
    const consoles = (['log', 'info', 'warn', 'error', 'debug'] as const).map((level) => vi.spyOn(console, level).mockImplementation(() => {}))
    const stored = vi.spyOn(Storage.prototype, 'setItem')
    const fetchMock = serve({ [COMPLETE]: () => noContent() })
    const user = userEvent.setup(INSTANT)

    open(`/reset-password#token=${TOKEN}`)
    await waitFor(() => expect(address()).toBe('/reset-password'))

    const nowhere = () => {
      // Not in the page -- text, attribute, hidden input or link -- and not in anything the browser keeps.
      expect(document.documentElement.outerHTML).not.toContain(TOKEN)
      expect(document.querySelectorAll('input[type="hidden"]')).toHaveLength(0)
      expect(document.cookie).not.toContain(TOKEN)
      expect(stored).not.toHaveBeenCalled()
      expect(localStorage).toHaveLength(0)
      expect(sessionStorage).toHaveLength(0)
      expect(document.title).not.toContain(TOKEN)
    }
    nowhere()
    // Opening the page asked the API only whether there is a session, and that did not carry the token.
    expect(requests(fetchMock)).toEqual(['GET /api/auth/session'])
    expect(JSON.stringify(fetchMock.mock.calls)).not.toContain(TOKEN)

    await user.type(screen.getByLabelText('New password'), NEW)
    nowhere()
    await user.type(screen.getByLabelText('Confirm new password'), NEW)
    await user.click(screen.getByRole('button', { name: 'Reset password' }))
    await screen.findByRole('heading', { name: 'Password reset' })
    nowhere()

    // The one place it went: the body of the one request, to the one address. Not a URL, not a header.
    const call = callOf(fetchMock, requests(fetchMock).indexOf(COMPLETE))
    expect(call.url).toBe('/api/auth/password-reset/complete')
    expect(JSON.stringify(call.headers)).not.toContain(TOKEN)
    expect(bodies(fetchMock, COMPLETE)).toStrictEqual([{ token: TOKEN, new_password: NEW, confirm_password: NEW }])
    for (const spy of consoles) {
      expect(JSON.stringify(spy.mock.calls)).not.toMatch(/synthetic-/)
      spy.mockRestore()
    }
    stored.mockRestore()
  })

  it('reads the token as the link encoded it', async () => {
    const fetchMock = serve({ [COMPLETE]: () => noContent() })
    const user = userEvent.setup(INSTANT)

    open('/reset-password#token=a%2Bb%2Fc%3D%3D_-d')
    await screen.findByRole('heading', { name: 'Choose a new password' })
    await fillAndReset(user)

    await screen.findByRole('heading', { name: 'Password reset' })
    expect(bodies(fetchMock, COMPLETE)).toMatchObject([{ token: 'a+b/c==_-d' }])
  })

  it('works with a trailing slash, and tidies a query string away with the fragment', async () => {
    serve()

    open(`/reset-password/?utm_source=email#token=${TOKEN}`)

    expect(await screen.findByRole('heading', { name: 'Choose a new password' })).toBeInTheDocument()
    await waitFor(() => expect(address()).toBe('/reset-password'))
  })

  it.each([
    ['no fragment', '/reset-password'],
    ['an empty fragment', '/reset-password#'],
    ['an empty token', '/reset-password#token='],
    ['a fragment that is something else', '/reset-password#section-2'],
    ['another parameter only', '/reset-password#code=abc123'],
    ['two tokens', `/reset-password#token=${TOKEN}&token=${OTHER_TOKEN}`],
    ['a token longer than any the API sends', `/reset-password#token=${'x'.repeat(513)}`],
    // The API puts the token in the fragment. One in the query string is not a link this app made, and a
    // query string is sent to servers and kept in their logs: it is not read.
    ['the token in the query string', `/reset-password?token=${TOKEN}`],
  ])('with %s, says the link is not valid, shows no form and asks the API nothing', async (_label, path) => {
    const fetchMock = serve({ [COMPLETE]: () => noContent() })

    open(path)

    expect(await screen.findByRole('heading', { level: 2, name: 'Reset link not valid' })).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(/^This password reset link is invalid or has expired\.$/)
    expect(screen.getByText(/To get a new one, choose “Forgot password\?” on the sign-in form\./)).toBeInTheDocument()
    expect(main().querySelector('input')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reset password' })).not.toBeInTheDocument()
    // Whatever was in the address is tidied out of it all the same.
    await waitFor(() => expect(address()).toBe('/reset-password'))
    expect(requests(fetchMock)).toEqual(['GET /api/auth/session'])
    expect(main().innerHTML).not.toContain(TOKEN)
    expect(screen.getByRole('link', { name: 'Go to sign in' })).toHaveAttribute('href', '/')
  })

  it('uses a link followed while the page is already open, in place of whatever was there', async () => {
    const fetchMock = serve({ [COMPLETE]: () => noContent() })
    const user = userEvent.setup(INSTANT)
    open('/reset-password')
    await screen.findByRole('heading', { name: 'Reset link not valid' })

    // Only the fragment changes: the browser does not load the page again.
    await user.click(screen.getByRole('button', { name: 'follow-first-link' }))

    expect(await screen.findByRole('heading', { name: 'Choose a new password' })).toBeInTheDocument()
    await waitFor(() => expect(address()).toBe('/reset-password'))
    expect(main().innerHTML).not.toContain(TOKEN)

    // A second link replaces the first, and what had been typed for the first is not carried over.
    await user.type(screen.getByLabelText('New password'), 'synthetic-half-typed')
    await user.click(screen.getByRole('button', { name: 'follow-second-link' }))
    await waitFor(() => expect(screen.getByLabelText('New password')).toHaveValue(''))
    await waitFor(() => expect(address()).toBe('/reset-password'))
    await fillAndReset(user)

    await screen.findByRole('heading', { name: 'Password reset' })
    expect(bodies(fetchMock, COMPLETE)).toMatchObject([{ token: OTHER_TOKEN }])
  })

  it('keeps the form when the address is tidied: removing the fragment is not a link with no token', async () => {
    serve()
    const user = userEvent.setup(INSTANT)

    open(`/reset-password#token=${TOKEN}`)
    await waitFor(() => expect(address()).toBe('/reset-password'))
    await user.type(screen.getByLabelText('New password'), NEW)

    // Still the form, with what was typed, after the address changed underneath it.
    expect(screen.getByRole('heading', { name: 'Choose a new password' })).toBeInTheDocument()
    expect(screen.getByLabelText('New password')).toHaveValue(NEW)
  })

  it('is shown in place of the app to someone who happens to be signed in, and asks for none of their data', async () => {
    const fetchMock = serve({ 'GET /api/auth/session': () => jsonResponse(200, ALICE) })

    open(`/reset-password#token=${TOKEN}`)

    expect(await screen.findByRole('heading', { name: 'Choose a new password' })).toBeInTheDocument()
    // Signed in -- the header says who -- and still the reset form, not the app's pages.
    expect(await screen.findByRole('navigation', { name: 'Account' })).toBeInTheDocument()
    expect(screen.getByLabelText('New password')).toBeInTheDocument()
    expect(requests(fetchMock)).toEqual(['GET /api/auth/session'])
  })

  it('leaves a signed-in person signed in when the link turns out not to be valid: nothing was reset', async () => {
    const fetchMock = serve({ 'GET /api/auth/session': () => jsonResponse(200, ALICE), [COMPLETE]: INVALID_TOKEN })
    const user = userEvent.setup(INSTANT)
    open(`/reset-password#token=${TOKEN}`)
    await screen.findByRole('navigation', { name: 'Account' })

    await fillAndReset(user)

    expect(await screen.findByRole('heading', { name: 'Reset link not valid' })).toHaveFocus()
    expect(screen.getByRole('navigation', { name: 'Account' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Continue to SortView' })).toHaveAttribute('href', '/')
    expect(requests(fetchMock)).toEqual(['GET /api/auth/session', COMPLETE])
  })
})

// =====================================================================================================================
// The reset page: the form
// =====================================================================================================================

describe('choosing a new password', () => {
  async function form() {
    open(`/reset-password#token=${TOKEN}`)
    await screen.findByRole('heading', { name: 'Choose a new password' })
    await waitFor(() => expect(address()).toBe('/reset-password'))
  }

  it('has two labelled new-password boxes and no way to reveal them', async () => {
    serve()

    await form()

    const boxes = [screen.getByLabelText('New password'), screen.getByLabelText('Confirm new password')]
    expect(boxes.map((box) => [box.getAttribute('type'), box.getAttribute('autocomplete'), box.getAttribute('name')])).toEqual([
      ['password', 'new-password', 'new_password'],
      ['password', 'new-password', 'confirm_password'],
    ])
    expect(boxes[0]).toHaveAccessibleDescription('At least 8 characters.')
    expect(main().querySelectorAll('input')).toHaveLength(2)
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /show|reveal|hide/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reset password' })).toHaveAttribute('type', 'submit')
  })

  it('asks for both passwords before anything is sent', async () => {
    const fetchMock = serve({ [COMPLETE]: () => noContent() })
    const user = userEvent.setup(INSTANT)
    await form()

    await user.click(screen.getByRole('button', { name: 'Reset password' }))

    expect(screen.getByLabelText('New password')).toHaveAccessibleDescription('At least 8 characters. Enter a new password.')
    expect(screen.getByLabelText('Confirm new password')).toHaveAccessibleDescription('Enter your new password again.')
    expect(screen.getByLabelText('New password')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText('Confirm new password')).toHaveAttribute('aria-invalid', 'true')
    expect(requests(fetchMock)).not.toContain(COMPLETE)
  })

  it('on success says so, signs nobody in, and leads to the sign-in form', async () => {
    const fetchMock = serve({ [COMPLETE]: () => noContent() })
    const user = userEvent.setup(INSTANT)
    await form()

    await fillAndReset(user)

    expect(await screen.findByRole('heading', { level: 2, name: 'Password reset' })).toHaveFocus()
    expect(screen.getByRole('status')).toHaveTextContent(/^Password reset\. Sign in with your new password\.$/)
    expect(main().querySelector('input')).toBeNull()
    expect(document.documentElement.outerHTML).not.toMatch(/synthetic-/)
    // No sign-in was attempted with the new password, and no session was looked for again.
    expect(requests(fetchMock)).toEqual(['GET /api/auth/session', COMPLETE])
    expect(screen.queryByRole('navigation', { name: 'Account' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('link', { name: 'Go to sign in' }))

    expect(address()).toBe('/')
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.getByLabelText('Password')).toHaveValue('')
    expect(requests(fetchMock)).not.toContain('POST /api/auth/login')
    // The reset page was replaced: Back does not return to a page with a used link.
    await user.click(screen.getByRole('button', { name: 'browser-back' }))
    expect(address()).toBe('/')
  })

  it('signs out a person who was signed in here, at once and with no request, and offers only the way to sign in', async () => {
    // Nothing is served for logout or for an account, so a request for either would fail the test; and a
    // second session check would be counted.
    let sessionChecks = 0
    const fetchMock = serve({
      'GET /api/auth/session': () => (sessionChecks++ === 0 ? jsonResponse(200, ALICE) : jsonResponse(401, NOT_AUTHENTICATED)),
      [COMPLETE]: () => noContent(),
    })
    const user = userEvent.setup(INSTANT)
    open(`/reset-password#token=${TOKEN}`)
    await screen.findByRole('navigation', { name: 'Account' })
    expect(within(screen.getByRole('banner')).getByText('Alice Example')).toBeInTheDocument()

    await fillAndReset(user)

    expect(await screen.findByRole('heading', { level: 2, name: 'Password reset' })).toHaveFocus()
    expect(screen.getByRole('status')).toHaveTextContent(/^Password reset\. Sign in with your new password\.$/)
    // Signed out locally already: the header no longer names anyone or offers their account.
    expect(screen.queryByRole('navigation', { name: 'Account' })).not.toBeInTheDocument()
    expect(screen.queryByText('Alice Example')).not.toBeInTheDocument()
    expect(screen.queryByText('alice@example.test')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign out' })).not.toBeInTheDocument()
    expect(screen.queryByText('Continue to SortView')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Go to sign in' })).toHaveAttribute('href', '/')
    // The reset was the last request: no logout, and no session or account request to find out.
    expect(requests(fetchMock)).toEqual(['GET /api/auth/session', COMPLETE])

    await user.click(screen.getByRole('link', { name: 'Go to sign in' }))

    // The sign-in form, not the app -- and still without asking the API anything.
    expect(address()).toBe('/')
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: 'Account' })).not.toBeInTheDocument()
    // A reset is not a password change made from the account page: that notice is not shown.
    expect(screen.queryByText(/Password changed/)).not.toBeInTheDocument()
    expect(requests(fetchMock)).toEqual(['GET /api/auth/session', COMPLETE])
    expect(sessionChecks).toBe(1)
  })

  it('says one generic thing for a link that is invalid, expired or already used, and the link is done with', async () => {
    const fetchMock = serve({ [COMPLETE]: INVALID_TOKEN })
    const user = userEvent.setup(INSTANT)
    await form()

    await fillAndReset(user)

    expect(await screen.findByRole('heading', { level: 2, name: 'Reset link not valid' })).toHaveFocus()
    expect(screen.getByRole('alert')).toHaveTextContent(/^This password reset link is invalid or has expired\.$/)
    expect(main()).not.toHaveTextContent(/invalid_reset_token|used|revoked|400/)
    expect(main().querySelector('input')).toBeNull()
    expect(document.documentElement.outerHTML).not.toMatch(/synthetic-/)
    expect(requests(fetchMock).filter((request) => request === COMPLETE)).toHaveLength(1)
  })

  it.each([
    ['new_password', 'too_short', 'New password', 'At least 8 characters. Your new password must be at least 8 characters long.'],
    ['confirm_password', 'mismatch', 'Confirm new password', 'New password and confirmation do not match.'],
    ['new_password', 'same_as_current', 'New password', 'At least 8 characters. Your new password must be different from your current password.'],
  ])('puts the API’s refusal of %s (%s) beside that box, and the link can still be used', async (field, why, label, description) => {
    let calls = 0
    const fetchMock = serve({ [COMPLETE]: () => (calls++ === 0 ? refused(field, why)() : noContent()) })
    const user = userEvent.setup(INSTANT)
    await form()

    await fillAndReset(user, 'short')

    const box = screen.getByLabelText(label)
    await waitFor(() => expect(box).toHaveAttribute('aria-invalid', 'true'))
    expect(box).toHaveAccessibleDescription(description)
    expect(screen.getByRole('heading', { name: 'Choose a new password' })).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/invalid_password_reset|problems/)
    expect(main().innerHTML).not.toContain(TOKEN)

    // Corrected and sent again, with the same token: a refused password did not use the link up.
    await user.clear(screen.getByLabelText('New password'))
    await user.clear(screen.getByLabelText('Confirm new password'))
    await fillAndReset(user)
    await screen.findByRole('heading', { name: 'Password reset' })
    expect(bodies(fetchMock, COMPLETE)).toStrictEqual([
      { token: TOKEN, new_password: 'short', confirm_password: 'short' },
      { token: TOKEN, new_password: NEW, confirm_password: NEW },
    ])
  })

  it.each([
    ['too many attempts', RATE_LIMITED, 'Your password was not reset. Too many attempts. Please wait a moment and try again.'],
    ['a server failure', () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' }), 'Your password was not reset. Internal server error.'],
    ['a refusal that names no field of this form', refused('token', 'required'), 'Your password was not reset. Not valid.'],
  ])('says the password was not reset, safely, for %s, and keeps the form', async (_label, answer, message) => {
    serve({ [COMPLETE]: answer })
    const user = userEvent.setup(INSTANT)
    await form()

    await fillAndReset(user)

    expect(await screen.findByRole('alert')).toHaveTextContent(message)
    expect(screen.getByRole('heading', { name: 'Choose a new password' })).toBeInTheDocument()
    expect(screen.getByLabelText('New password')).toHaveValue(NEW)
    expect(main()).not.toHaveTextContent(/Rate limit exceeded/)
    expect(main().innerHTML).not.toContain(TOKEN)
    expect(within(main()).queryByText(/Password reset\./)).not.toBeInTheDocument()
  })

  it('sends one request however often the button is pressed', async () => {
    const pending = deferred<Response>()
    const fetchMock = serve({ [COMPLETE]: () => pending.promise })
    const user = userEvent.setup(INSTANT)
    await form()

    await fillAndReset(user)
    const busy = screen.getByRole('button', { name: 'Resetting password…' })
    await user.click(busy)
    await user.type(screen.getByLabelText('Confirm new password'), '{Enter}')

    expect(busy).toHaveAttribute('aria-disabled', 'true')
    expect(busy).not.toBeDisabled()
    expect(requests(fetchMock).filter((request) => request === COMPLETE)).toHaveLength(1)
    pending.resolve(noContent())
    expect(await screen.findByRole('heading', { name: 'Password reset' })).toBeInTheDocument()
  })
})
