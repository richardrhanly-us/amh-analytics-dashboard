import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import {
  ALICE,
  type ApiRoutes,
  callOf,
  deferred,
  type FetchMock,
  jsonResponse,
  noContent,
  NOT_AUTHENTICATED,
  requestedUrls,
  serveApi,
} from '../test/http.ts'
import { renderApp } from '../test/render.tsx'
import { formatLocalInstant } from '../time/localTime.ts'
import { problemSentences } from './problemText.ts'

const ACCOUNT = {
  email: 'alice@example.test',
  full_name: 'Alice Example',
  last_login_at: '2026-10-05T18:50:00Z',
  last_password_changed_at: '2026-08-01T09:00:00Z',
}
// No pause between keystrokes: these tests type three passwords each, and waiting on a timer for every key
// makes this file slow enough to crowd the others when the whole suite runs at once.
const INSTANT = { delay: null }
const CURRENT = 'synthetic-Current-1'
const NEW = 'synthetic-New-2'
const SERVER_ERROR = () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' })
const refused = (code: string, field: string, why: string) => () =>
  jsonResponse(422, { code, message: 'Not valid.', problems: [{ field, code: why }] })

/** A signed-in person, their account, and an empty organization list, unless a test replaces a route. */
function serve(overrides: ApiRoutes = {}, account: typeof ACCOUNT | Record<string, unknown> = ACCOUNT): FetchMock {
  return serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    'GET /api/organizations': () => jsonResponse(200, []),
    'GET /api/account': () => jsonResponse(200, account),
    ...overrides,
  })
}

const main = () => screen.getByRole('main')
const region = (name: string) => screen.getByRole('region', { name })
const accountNav = () => within(screen.getByRole('navigation', { name: 'Account' }))
const requests = (fetchMock: FetchMock) => fetchMock.mock.calls.map(([url, init]) => `${init?.method ?? 'GET'} ${String(url)}`)
function bodyOf(fetchMock: FetchMock, request: string): unknown {
  const index = requests(fetchMock).lastIndexOf(request)
  return JSON.parse(String(callOf(fetchMock, index).init.body)) as unknown
}

/** Opens /account and waits for the account to have loaded. */
async function page() {
  renderApp('/account')
  await screen.findByRole('heading', { level: 3, name: 'Security' })
}

async function fillPasswords(user: ReturnType<typeof userEvent.setup>, current = CURRENT, next = NEW, confirm = NEW) {
  if (current !== '') await user.type(screen.getByLabelText('Current password'), current)
  if (next !== '') await user.type(screen.getByLabelText('New password'), next)
  if (confirm !== '') await user.type(screen.getByLabelText('Confirm new password'), confirm)
}

// =====================================================================================================================
// Getting there
// =====================================================================================================================

describe('the way to the account', () => {
  it('is in the header on every signed-in page, beside the way out', async () => {
    serve()
    const user = userEvent.setup(INSTANT)
    renderApp('/organizations')
    await screen.findByText('Your account does not have access to any organizations.')

    // A link and a button, both always in view: nothing has to be opened first.
    expect(accountNav().getByRole('link', { name: 'My account' })).toHaveAttribute('href', '/account')
    expect(accountNav().getByRole('button', { name: 'Sign out' })).toBeInTheDocument()
    expect(accountNav().getByRole('link', { name: 'My account' })).not.toHaveAttribute('aria-current')

    await user.click(accountNav().getByRole('link', { name: 'My account' }))

    expect(screen.getByTestId('address')).toHaveTextContent('/account')
    expect(await screen.findByRole('heading', { level: 2, name: 'My account' })).toHaveFocus()
    expect(accountNav().getByRole('link', { name: 'My account' })).toHaveAttribute('aria-current', 'page')
    expect(document.title).toBe('My account – SortView')
  })

  it('can be reached and left from the keyboard', async () => {
    serve()
    const user = userEvent.setup(INSTANT)
    renderApp('/organizations')
    await screen.findByText('Your account does not have access to any organizations.')

    accountNav().getByRole('link', { name: 'My account' }).focus()
    await user.keyboard('{Enter}')
    await screen.findByRole('heading', { level: 3, name: 'Profile' })
    await user.click(within(screen.getByRole('navigation', { name: 'Breadcrumb' })).getByRole('link', { name: 'Organizations' }))

    expect(screen.getByTestId('address')).toHaveTextContent('/organizations')
  })

  it('is under no organization and asks about none', async () => {
    const fetchMock = serve()

    await page()

    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session', '/api/account'])
    expect(main()).not.toHaveTextContent(/organization settings|role|admin|owner|users|invite|sorter|branch/i)
  })

  it('still signs out from the account page', async () => {
    const fetchMock = serve({ 'POST /api/auth/logout': () => noContent() })
    const user = userEvent.setup(INSTANT)
    await page()

    await user.click(accountNav().getByRole('button', { name: 'Sign out' }))

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(requests(fetchMock)).toContain('POST /api/auth/logout')
    expect(screen.getByTestId('address')).toHaveTextContent('/')
    // Signed out on purpose: no notice about a password.
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('needs a session: signed out, /account is the sign-in form and nothing is asked about an account', async () => {
    const fetchMock = serve({ 'GET /api/auth/session': () => jsonResponse(401, NOT_AUTHENTICATED) })

    renderApp('/account')

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session'])
    expect(screen.queryByText('My account')).not.toBeInTheDocument()
    // The address is kept, so signing in lands on the page that was asked for.
    expect(screen.getByTestId('address')).toHaveTextContent('/account')
  })
})

// =====================================================================================================================
// The page
// =====================================================================================================================

describe('the account page', () => {
  it('says it is loading, then shows three sections in order', async () => {
    const pending = deferred<Response>()
    serve({ 'GET /api/account': () => pending.promise })

    renderApp('/account')

    expect(await screen.findByText('Loading your account…')).toHaveRole('status')
    expect(screen.queryByRole('region')).not.toBeInTheDocument()

    pending.resolve(jsonResponse(200, ACCOUNT))
    await screen.findByRole('heading', { level: 3, name: 'Security' })
    expect(screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent)).toEqual(['Profile', 'Account activity', 'Security'])
    expect(screen.getAllByRole('region').map((section) => section.getAttribute('aria-labelledby'))).toEqual([
      'account-profile-heading',
      'account-activity-heading',
      'account-security-heading',
    ])
  })

  it('shows the email as something to read, not to edit', async () => {
    serve()

    await page()

    const profile = within(region('Profile'))
    expect(profile.getByText('alice@example.test')).toBeInTheDocument()
    expect(profile.getByText('You sign in with this email address. It cannot be changed here.')).toBeInTheDocument()
    // The only box in the profile is the name: there is nothing to type an email into.
    expect(profile.getAllByRole('textbox').map((box) => box.getAttribute('name'))).toEqual(['full_name'])
    expect(main().querySelector('input[type="email"]')).toBeNull()
  })

  it('shows when the person last signed in and changed their password, in their own time zone and in words', async () => {
    serve()

    await page()

    const activity = region('Account activity')
    const signedIn = formatLocalInstant(ACCOUNT.last_login_at) as string
    const changed = formatLocalInstant(ACCOUNT.last_password_changed_at) as string
    expect(within(activity).getByText('Last signed in').nextElementSibling).toHaveTextContent(signedIn)
    expect(within(activity).getByText('Password last changed').nextElementSibling).toHaveTextContent(changed)
    // A date, a time and the zone it is in -- never the raw timestamp.
    for (const written of [signedIn, changed]) {
      expect(written).toMatch(/^[A-Z][a-z]{2} \d{1,2}, \d{4}, \d{1,2}:\d{2} (AM|PM) \S+$/)
    }
    expect(activity).not.toHaveTextContent(/2026-10-05T|18:50:00Z|\dZ\b/)
  })

  it('says so, in words, when a time has never happened', async () => {
    serve({}, { ...ACCOUNT, last_login_at: null, last_password_changed_at: null })

    await page()

    const activity = within(region('Account activity'))
    expect(activity.getByText('Last signed in').nextElementSibling).toHaveTextContent(/^Not available$/)
    expect(activity.getByText('Password last changed').nextElementSibling).toHaveTextContent(/^Not recorded$/)
    expect(region('Account activity')).not.toHaveTextContent(/null|undefined|Invalid Date|1970|NaN/)
  })

  it('offers a retry when the account cannot be loaded, and loads it then', async () => {
    let calls = 0
    serve({ 'GET /api/account': () => (calls++ === 0 ? SERVER_ERROR() : jsonResponse(200, ACCOUNT)) })
    const user = userEvent.setup(INSTANT)

    renderApp('/account')

    expect(await screen.findByRole('alert')).toHaveTextContent('Internal server error.')
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByLabelText('Full name')).toHaveValue('Alice Example')
  })

  it('treats a malformed account as a failure and shows none of it', async () => {
    serve({}, { ...ACCOUNT, last_login_at: '2026-10-05T13:50:00-05:00', password_hash: 'CANARY' })

    renderApp('/account')

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/CANARY|alice@example\.test.*Profile/)
    expect(screen.queryByLabelText('Full name')).not.toBeInTheDocument()
  })

  it('returns to the sign-in form when the session has ended', async () => {
    serve({ 'GET /api/account': () => jsonResponse(401, NOT_AUTHENTICATED) })

    renderApp('/account')

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByText('alice@example.test')).not.toBeInTheDocument()
    expect(screen.getByTestId('address')).toHaveTextContent('/account')
  })
})

// =====================================================================================================================
// The name
// =====================================================================================================================

describe('changing the name', () => {
  it('saves the name alone, then shows what the API stored -- on the page and in the header', async () => {
    const fetchMock = serve({ 'PUT /api/account/profile': () => jsonResponse(200, { ...ACCOUNT, full_name: 'Alicia Example-Ruiz' }) })
    const user = userEvent.setup(INSTANT)
    await page()

    const name = screen.getByLabelText('Full name')
    await user.clear(name)
    await user.type(name, '  Alicia Example-Ruiz  ')
    await user.click(screen.getByRole('button', { name: 'Save name' }))

    expect(await within(region('Profile')).findByRole('status')).toHaveTextContent('Saved.')
    // Sent as typed; the trimmed name the API answered with is what is then shown.
    expect(bodyOf(fetchMock, 'PUT /api/account/profile')).toStrictEqual({ full_name: '  Alicia Example-Ruiz  ' })
    expect(name).toHaveValue('Alicia Example-Ruiz')
    expect(within(screen.getByRole('banner')).getByText('Alicia Example-Ruiz')).toBeInTheDocument()
    expect(within(screen.getByRole('banner')).queryByText('Alice Example')).not.toBeInTheDocument()
    // Still signed in, on the same page; the email is as it was.
    expect(screen.getByTestId('address')).toHaveTextContent('/account')
    expect(within(region('Profile')).getByText('alice@example.test')).toBeInTheDocument()
    expect(name).toHaveAttribute('aria-invalid', 'false')
  })

  it('shows nothing as saved until the API has answered, and sends one request however often the button is pressed', async () => {
    const pending = deferred<Response>()
    const fetchMock = serve({ 'PUT /api/account/profile': () => pending.promise })
    const user = userEvent.setup(INSTANT)
    await page()
    const save = screen.getByRole('button', { name: 'Save name' })

    await user.click(save)
    const busy = screen.getByRole('button', { name: 'Saving…' })
    await user.click(busy)
    await user.keyboard('{Enter}')

    expect(busy).toHaveAttribute('aria-disabled', 'true')
    expect(busy).not.toBeDisabled()
    expect(busy).toHaveFocus()
    expect(screen.queryByText('Saved.')).not.toBeInTheDocument()
    expect(within(screen.getByRole('banner')).getByText('Alice Example')).toBeInTheDocument()
    expect(requests(fetchMock).filter((request) => request === 'PUT /api/account/profile')).toHaveLength(1)

    pending.resolve(jsonResponse(200, ACCOUNT))
    expect(await screen.findByText('Saved.')).toBeInTheDocument()
  })

  it.each([
    ['required', 'Enter your name.'],
    ['too_long', 'Your name must be 120 characters or fewer.'],
    ['invalid_characters', 'Your name contains characters that cannot be used.'],
    ['a_word_this_app_does_not_know', 'This is not valid. Check it and try again.'],
  ])('says what is wrong beside the box when the API refuses the name as %s', async (why, sentence) => {
    serve({ 'PUT /api/account/profile': refused('invalid_profile', 'full_name', why) })
    const user = userEvent.setup(INSTANT)
    await page()

    await user.click(screen.getByRole('button', { name: 'Save name' }))

    const name = await screen.findByLabelText('Full name')
    await waitFor(() => expect(name).toHaveAttribute('aria-invalid', 'true'))
    expect(name).toHaveAccessibleDescription(sentence)
    expect(screen.queryByText('Saved.')).not.toBeInTheDocument()
    // What was typed is kept, to be corrected; the header has not changed.
    expect(name).toHaveValue('Alice Example')
    expect(within(screen.getByRole('banner')).getByText('Alice Example')).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/invalid_profile|problems|\[object/)
  })

  it('clears the problem and the "Saved" note when the name is edited again', async () => {
    let calls = 0
    serve({
      'PUT /api/account/profile': () =>
        calls++ === 0 ? refused('invalid_profile', 'full_name', 'required')() : jsonResponse(200, { ...ACCOUNT, full_name: 'Al' }),
    })
    const user = userEvent.setup(INSTANT)
    await page()
    await user.click(screen.getByRole('button', { name: 'Save name' }))
    await screen.findByText('Enter your name.')

    await user.click(screen.getByRole('button', { name: 'Save name' }))
    expect(await screen.findByText('Saved.')).toBeInTheDocument()
    expect(screen.queryByText('Enter your name.')).not.toBeInTheDocument()

    await user.type(screen.getByLabelText('Full name'), 'x')
    expect(screen.queryByText('Saved.')).not.toBeInTheDocument()
  })

  it('says the name was not saved, safely, for any other failure', async () => {
    serve({ 'PUT /api/account/profile': SERVER_ERROR })
    const user = userEvent.setup(INSTANT)
    await page()

    await user.click(screen.getByRole('button', { name: 'Save name' }))

    expect(await within(region('Profile')).findByRole('alert')).toHaveTextContent('Your name was not saved. Internal server error.')
    expect(screen.queryByText('Saved.')).not.toBeInTheDocument()
  })

  it('returns to the sign-in form when saving finds the session has ended', async () => {
    serve({ 'PUT /api/account/profile': () => jsonResponse(401, NOT_AUTHENTICATED) })
    const user = userEvent.setup(INSTANT)
    await page()

    await user.click(screen.getByRole('button', { name: 'Save name' }))

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})

// =====================================================================================================================
// The password
// =====================================================================================================================

describe('changing the password', () => {
  it('has three labelled password boxes that a password manager understands, and no way to reveal them', async () => {
    serve()

    await page()

    const security = within(region('Security'))
    const boxes = ['Current password', 'New password', 'Confirm new password'].map((label) => security.getByLabelText(label))
    expect(boxes.map((box) => [box.getAttribute('type'), box.getAttribute('autocomplete'), box.getAttribute('name')])).toEqual([
      ['password', 'current-password', 'current_password'],
      ['password', 'new-password', 'new_password'],
      ['password', 'new-password', 'confirm_password'],
    ])
    expect(security.getByLabelText('New password')).toHaveAccessibleDescription('At least 8 characters.')
    expect(security.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(security.queryByRole('button', { name: /show|reveal|hide/i })).not.toBeInTheDocument()
    expect(security.getByText(/signs you out everywhere, including here/)).toBeInTheDocument()
    // For a password manager: which account these belong to. Not shown.
    const username = region('Security').querySelector('input[autocomplete="username"]') as HTMLInputElement
    expect([username.value, username.hidden, username.readOnly]).toEqual(['alice@example.test', true, true])
  })

  it('asks for all three before anything is sent', async () => {
    const fetchMock = serve()
    const user = userEvent.setup(INSTANT)
    await page()

    await user.click(screen.getByRole('button', { name: 'Change password' }))

    expect(screen.getByLabelText('Current password')).toHaveAccessibleDescription('Enter your current password.')
    expect(screen.getByLabelText('New password')).toHaveAccessibleDescription('At least 8 characters. Enter a new password.')
    expect(screen.getByLabelText('Confirm new password')).toHaveAccessibleDescription('Enter your new password again.')
    for (const label of ['Current password', 'New password', 'Confirm new password']) {
      expect(screen.getByLabelText(label)).toHaveAttribute('aria-invalid', 'true')
    }
    expect(requests(fetchMock)).not.toContain('POST /api/account/change-password')

    // Only the ones still empty are asked for.
    await user.type(screen.getByLabelText('Current password'), CURRENT)
    await user.click(screen.getByRole('button', { name: 'Change password' }))
    expect(screen.getByLabelText('Current password')).toHaveAttribute('aria-invalid', 'false')
    expect(screen.getByLabelText('New password')).toHaveAttribute('aria-invalid', 'true')
    expect(requests(fetchMock)).not.toContain('POST /api/account/change-password')
  })

  it('on success signs the person out here at once, goes to the start and says to sign in again', async () => {
    const fetchMock = serve({ 'POST /api/account/change-password': () => noContent() })
    const user = userEvent.setup(INSTANT)
    await page()

    await fillPasswords(user)
    await user.click(screen.getByRole('button', { name: 'Change password' }))

    // The API has ended every session: this app does not wait to be told so by a 401.
    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Password changed. Sign in again with your new password.')
    // The router changes the address a moment after the sign-in form appears.
    await waitFor(() => expect(screen.getByTestId('address')).toHaveTextContent(/^\/$/))
    expect(screen.queryByRole('navigation', { name: 'Account' })).not.toBeInTheDocument()
    expect(bodyOf(fetchMock, 'POST /api/account/change-password')).toStrictEqual({
      current_password: CURRENT,
      new_password: NEW,
      confirm_password: NEW,
    })
    // No request was made to end the session or to check it: there is none left.
    expect(requests(fetchMock).slice(requests(fetchMock).indexOf('POST /api/account/change-password') + 1)).toEqual([])
    // No password is carried over, into the sign-in form or anywhere on the page.
    expect(screen.getByLabelText('Password')).toHaveValue('')
    expect(document.body.innerHTML).not.toMatch(/synthetic-/)
    expect(screen.getByRole('main')).toHaveFocus()
  })

  it('the notice is gone once the person signs in again, and is not shown for an ordinary sign-out', async () => {
    serve({
      'POST /api/account/change-password': () => noContent(),
      'POST /api/auth/login': () => jsonResponse(200, ALICE),
      'POST /api/auth/logout': () => noContent(),
    })
    const user = userEvent.setup(INSTANT)
    await page()
    await fillPasswords(user)
    await user.click(screen.getByRole('button', { name: 'Change password' }))
    await screen.findByText('Password changed. Sign in again with your new password.')

    await user.type(screen.getByLabelText('Email'), 'alice@example.test')
    await user.type(screen.getByLabelText('Password'), `${NEW}{Enter}`)
    await screen.findByRole('navigation', { name: 'Account' })
    await user.click(screen.getByRole('button', { name: 'Sign out' }))

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByText(/Password changed/)).not.toBeInTheDocument()
  })

  it.each([
    ['current_password', 'incorrect', 'Current password', 'Your current password is incorrect.'],
    ['new_password', 'too_short', 'New password', 'At least 8 characters. Your new password must be at least 8 characters long.'],
    ['confirm_password', 'mismatch', 'Confirm new password', 'New password and confirmation do not match.'],
    ['new_password', 'same_as_current', 'New password', 'At least 8 characters. Your new password must be different from your current password.'],
  ])('puts the API’s refusal of %s (%s) beside that box, and stays signed in', async (field, why, label, description) => {
    serve({ 'POST /api/account/change-password': refused('invalid_password_change', field, why) })
    const user = userEvent.setup(INSTANT)
    await page()

    await fillPasswords(user)
    await user.click(screen.getByRole('button', { name: 'Change password' }))

    const box = screen.getByLabelText(label)
    await waitFor(() => expect(box).toHaveAttribute('aria-invalid', 'true'))
    expect(box).toHaveAccessibleDescription(description)
    for (const other of ['Current password', 'New password', 'Confirm new password'].filter((name) => name !== label)) {
      expect(screen.getByLabelText(other)).toHaveAttribute('aria-invalid', 'false')
    }
    // Still signed in, on the account page, and able to try again.
    expect(screen.getByRole('navigation', { name: 'Account' })).toBeInTheDocument()
    expect(screen.getByTestId('address')).toHaveTextContent('/account')
    expect(screen.getByRole('button', { name: 'Change password' })).toHaveAttribute('aria-disabled', 'false')
    expect(main()).not.toHaveTextContent(/invalid_password_change|problems|synthetic-/)
  })

  it('sends one request however often the button is pressed, and keeps the button in place meanwhile', async () => {
    const pending = deferred<Response>()
    const fetchMock = serve({ 'POST /api/account/change-password': () => pending.promise })
    const user = userEvent.setup(INSTANT)
    await page()
    await fillPasswords(user)

    await user.click(screen.getByRole('button', { name: 'Change password' }))
    const busy = screen.getByRole('button', { name: 'Changing password…' })
    await user.click(busy)
    await user.keyboard('{Enter}')

    expect(busy).toHaveAttribute('aria-disabled', 'true')
    expect(busy).not.toBeDisabled()
    expect(requests(fetchMock).filter((request) => request === 'POST /api/account/change-password')).toHaveLength(1)
    pending.resolve(noContent())
    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  it.each([
    [429, { error: 'Rate limit exceeded: 10 per 1 minute' }, 'Your password was not changed. Too many attempts. Please wait a moment and try again.'],
    [500, { code: 'internal_error', message: 'Internal server error.' }, 'Your password was not changed. Internal server error.'],
    [403, { code: 'origin_not_allowed', message: 'Request origin is not allowed.' }, 'Your password was not changed. Request origin is not allowed.'],
  ])('says the password was not changed, safely, for a %i', async (status, body, message) => {
    serve({ 'POST /api/account/change-password': () => jsonResponse(status, body) })
    const user = userEvent.setup(INSTANT)
    await page()

    await fillPasswords(user)
    await user.click(screen.getByRole('button', { name: 'Change password' }))

    expect(await within(region('Security')).findByRole('alert')).toHaveTextContent(message)
    expect(screen.getByRole('navigation', { name: 'Account' })).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/Rate limit exceeded|synthetic-/)
  })

  it('returns to the sign-in form, with no password notice, when the session had already ended', async () => {
    serve({ 'POST /api/account/change-password': () => jsonResponse(401, NOT_AUTHENTICATED) })
    const user = userEvent.setup(INSTANT)
    await page()

    await fillPasswords(user)
    await user.click(screen.getByRole('button', { name: 'Change password' }))

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByText(/Password changed/)).not.toBeInTheDocument()
  })

  it('never writes a password to the console', async () => {
    const spies = (['log', 'info', 'warn', 'error', 'debug'] as const).map((level) => vi.spyOn(console, level).mockImplementation(() => {}))
    serve({ 'POST /api/account/change-password': refused('invalid_password_change', 'current_password', 'incorrect') })
    const user = userEvent.setup(INSTANT)
    await page()

    await fillPasswords(user)
    await user.click(screen.getByRole('button', { name: 'Change password' }))
    await screen.findByText('Your current password is incorrect.')

    for (const spy of spies) {
      expect(JSON.stringify(spy.mock.calls)).not.toMatch(/synthetic-/)
      spy.mockRestore()
    }
  })
})

// =====================================================================================================================
// What is said about a refused field
// =====================================================================================================================

describe('problemSentences', () => {
  it('gives each refused field of the form its sentence, and leaves out fields the form does not have', () => {
    const sentences = problemSentences(
      [
        { field: 'new_password', code: 'too_short' },
        { field: 'token', code: 'invalid' },
        { field: 'confirm_password', code: 'mismatch' },
      ],
      ['new_password', 'confirm_password'],
    )

    expect(sentences).toEqual({
      new_password: 'Your new password must be at least 8 characters long.',
      confirm_password: 'New password and confirmation do not match.',
    })
  })

  it('keeps the first problem of a field, and has one plain sentence for a word it does not know', () => {
    expect(problemSentences([{ field: 'full_name', code: 'required' }, { field: 'full_name', code: 'too_long' }], ['full_name'])).toEqual({
      full_name: 'Enter your name.',
    })
    expect(problemSentences([{ field: 'full_name', code: 'constructor' }], ['full_name'])).toEqual({
      full_name: 'This is not valid. Check it and try again.',
    })
    expect(problemSentences([], ['full_name'])).toEqual({})
  })
})
