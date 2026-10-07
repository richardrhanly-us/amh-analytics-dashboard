import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import {
  ALICE,
  callOf,
  deferred,
  type FetchMock,
  INVALID_CREDENTIALS,
  jsonResponse,
  networkFailure,
  noContent,
  NOT_AUTHENTICATED,
  ORIGIN_NOT_ALLOWED,
  requestedUrls,
  stubFetch,
} from './test/http.ts'
import { renderApp } from './test/render.tsx'

const PASSWORD = '  Pä$$ w0rd"\\ é  '

/**
 * A stubbed fetch for the whole app. Responses queued with mockResolvedValueOnce and friends are used first;
 * any other request -- once signed in, the app asks for the organization list -- gets an empty list.
 */
function stubApp(): FetchMock {
  const fetchMock = stubFetch()
  fetchMock.mockImplementation(() => Promise.resolve(jsonResponse(200, [])))
  return fetchMock
}

/** Renders the app for a visitor with no session and waits for the sign-in form. */
async function renderSignedOut(): Promise<FetchMock> {
  const fetchMock = stubApp()
  fetchMock.mockResolvedValueOnce(jsonResponse(401, NOT_AUTHENTICATED))
  renderApp()
  await screen.findByRole('button', { name: 'Sign in' })
  return fetchMock
}

/** Renders the app for a visitor whose session restores as ALICE and waits for the shell. */
async function renderSignedIn(user: typeof ALICE = ALICE): Promise<FetchMock> {
  const fetchMock = stubApp()
  fetchMock.mockResolvedValueOnce(jsonResponse(200, user))
  renderApp()
  await screen.findByRole('button', { name: 'Sign out' })
  await landed()
  return fetchMock
}

/** Waits until the signed-in app has asked for, and shown, its (empty) organization list. */
async function landed() {
  await screen.findByText('Your account does not have access to any organizations.')
}

const emailInput = () => screen.getByLabelText('Email')
const passwordInput = () => screen.getByLabelText('Password')

function sentLogin(fetchMock: FetchMock): unknown {
  return JSON.parse(String(callOf(fetchMock, 1).init.body))
}

describe('while the session is being restored', () => {
  it('shows a loading state and neither the form nor the shell', async () => {
    const fetchMock = stubFetch()
    const pending = deferred<Response>()
    fetchMock.mockReturnValue(pending.promise)

    renderApp()

    expect(screen.getByRole('heading', { level: 1, name: 'SortView' })).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Checking your session')
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign out' })).not.toBeInTheDocument()

    pending.resolve(jsonResponse(401, NOT_AUTHENTICATED))
    await screen.findByRole('button', { name: 'Sign in' })
  })

  it('shows a recoverable error, not the sign-in form, when the check fails', async () => {
    const user = userEvent.setup()
    const fetchMock = stubApp()
    fetchMock.mockRejectedValueOnce(networkFailure()).mockResolvedValueOnce(jsonResponse(200, ALICE))

    renderApp()

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach the server')
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByRole('button', { name: 'Sign out' })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('the sign-in form', () => {
  it('has a SortView heading and labelled email and password controls', async () => {
    await renderSignedOut()

    expect(screen.getByRole('heading', { level: 1, name: 'SortView' })).toBeInTheDocument()
    expect(screen.getByRole('form', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Email' })).toBe(emailInput())
    expect(emailInput()).toHaveAttribute('type', 'email')
    expect(emailInput()).toBeRequired()
    expect(passwordInput()).toBeRequired()
    expect(screen.getByRole('button', { name: 'Sign in' })).toHaveAttribute('type', 'submit')
  })

  it('uses a password input', async () => {
    await renderSignedOut()

    expect(passwordInput()).toHaveAttribute('type', 'password')
  })

  it('sets autocomplete hints a password manager understands', async () => {
    await renderSignedOut()

    expect(emailInput()).toHaveAttribute('autocomplete', 'username')
    expect(passwordInput()).toHaveAttribute('autocomplete', 'current-password')
  })

  it('offers a way to reset a forgotten password, and no remember-me box or sample credentials', async () => {
    await renderSignedOut()

    // A button that swaps this form for the request form -- not a link, and not the form's submit button.
    expect(screen.getByRole('button', { name: 'Forgot password?' })).toHaveAttribute('type', 'button')
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(emailInput()).toHaveValue('')
    expect(emailInput()).not.toHaveAttribute('placeholder')
    expect(passwordInput()).toHaveValue('')
  })

  it('disables the button while a login is in flight and sends only one request', async () => {
    const user = userEvent.setup()
    const fetchMock = await renderSignedOut()
    const pending = deferred<Response>()
    fetchMock.mockReturnValueOnce(pending.promise)

    await user.type(emailInput(), 'alice@example.test')
    await user.type(passwordInput(), 'pw')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    const busy = screen.getByRole('button', { name: 'Signing in…' })
    expect(busy).toHaveAttribute('aria-disabled', 'true')
    await user.type(passwordInput(), '{Enter}')
    expect(fetchMock).toHaveBeenCalledTimes(2)

    pending.resolve(jsonResponse(401, INVALID_CREDENTIALS))
    expect(await screen.findByRole('button', { name: 'Sign in' })).not.toHaveAttribute('aria-disabled', 'true')
  })

  it('shows the signed-in shell after a successful login, with no second session request', async () => {
    const user = userEvent.setup()
    const fetchMock = await renderSignedOut()
    fetchMock.mockResolvedValueOnce(jsonResponse(200, ALICE))

    await user.type(emailInput(), 'alice@example.test')
    await user.type(passwordInput(), 'pw')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByText('Alice Example')).toBeInTheDocument()
    expect(screen.getByText('alice@example.test')).toBeInTheDocument()
    expect(screen.queryByLabelText('Password')).not.toBeInTheDocument()
    await landed()
    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session', '/api/auth/login', '/api/organizations'])
  })

  it('does not bring the password back when the form is shown again after signing out', async () => {
    const user = userEvent.setup()
    const fetchMock = await renderSignedOut()
    fetchMock.mockResolvedValueOnce(jsonResponse(200, ALICE))

    await user.type(emailInput(), 'alice@example.test')
    await user.type(passwordInput(), 'pw')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    const signOut = await screen.findByRole('button', { name: 'Sign out' })
    await landed()
    fetchMock.mockResolvedValueOnce(noContent())
    await user.click(signOut)

    await screen.findByRole('button', { name: 'Sign in' })
    expect(passwordInput()).toHaveValue('')
  })

  it('shows the same generic message for any rejected credentials', async () => {
    const user = userEvent.setup()
    const fetchMock = await renderSignedOut()
    fetchMock.mockResolvedValueOnce(jsonResponse(401, INVALID_CREDENTIALS))

    await user.type(emailInput(), 'nobody@example.test')
    await user.type(passwordInput(), 'wrong')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/^Invalid email or password\.$/)
    expect(screen.getByRole('button', { name: 'Sign in' })).not.toHaveAttribute('aria-disabled', 'true')
    expect(screen.queryByRole('button', { name: 'Sign out' })).not.toBeInTheDocument()
  })

  it.each([
    ['a rate limit', () => Promise.resolve(jsonResponse(429, { error: 'Rate limit exceeded: 10 per 1 minute' }))],
    ['a refused origin', () => Promise.resolve(jsonResponse(403, ORIGIN_NOT_ALLOWED))],
    ['a server crash page', () => Promise.resolve(new Response('Traceback (most recent call last)', { status: 500 }))],
    ['a network failure', () => Promise.reject(networkFailure())],
  ])('shows a short safe message for %s and lets the person try again', async (_label, respond) => {
    const user = userEvent.setup()
    const fetchMock = await renderSignedOut()
    fetchMock.mockImplementationOnce(respond).mockResolvedValueOnce(jsonResponse(200, ALICE))

    await user.type(emailInput(), 'alice@example.test')
    await user.type(passwordInput(), 'pw')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).not.toMatch(/Traceback|Rate limit exceeded|Failed to fetch|\[object/)
    expect(alert.textContent?.length).toBeLessThan(120)

    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('button', { name: 'Sign out' })).toBeInTheDocument()
  })

  it('never renders or logs the password, in an error or anywhere else', async () => {
    const logs = (['log', 'info', 'warn', 'error', 'debug'] as const).map((level) =>
      vi.spyOn(console, level).mockImplementation(() => {}),
    )
    const user = userEvent.setup()
    const fetchMock = await renderSignedOut()
    fetchMock.mockResolvedValueOnce(jsonResponse(401, INVALID_CREDENTIALS))

    await user.type(emailInput(), 'alice@example.test')
    await user.type(passwordInput(), 'hunter2-secret')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    await screen.findByRole('alert')

    expect(document.body.textContent).not.toContain('hunter2-secret')
    for (const log of logs) {
      expect(JSON.stringify(log.mock.calls)).not.toContain('hunter2-secret')
      expect(log).not.toHaveBeenCalled()
    }
    expect(window.localStorage).toHaveLength(0)
    expect(window.sessionStorage).toHaveLength(0)
  })

  it('submits when Enter is pressed in the password field', async () => {
    const user = userEvent.setup()
    const fetchMock = await renderSignedOut()
    fetchMock.mockResolvedValueOnce(jsonResponse(200, ALICE))

    await user.type(emailInput(), 'alice@example.test')
    await user.type(passwordInput(), 'pw{Enter}')

    expect(await screen.findByRole('button', { name: 'Sign out' })).toBeInTheDocument()
    expect(callOf(fetchMock, 1).url).toBe('/api/auth/login')
  })

  it('sends the email without surrounding spaces', async () => {
    const user = userEvent.setup()
    const fetchMock = await renderSignedOut()
    fetchMock.mockResolvedValueOnce(jsonResponse(200, ALICE))

    await user.type(emailInput(), '  alice@example.test  ')
    await user.type(passwordInput(), 'pw{Enter}')
    await screen.findByRole('button', { name: 'Sign out' })

    expect(sentLogin(fetchMock)).toStrictEqual({ email: 'alice@example.test', password: 'pw' })
  })

  it('sends the password exactly as typed', async () => {
    const user = userEvent.setup()
    const fetchMock = await renderSignedOut()
    fetchMock.mockResolvedValueOnce(jsonResponse(200, ALICE))

    await user.type(emailInput(), 'alice@example.test')
    await user.type(passwordInput(), PASSWORD)
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    await screen.findByRole('button', { name: 'Sign out' })

    expect(sentLogin(fetchMock)).toStrictEqual({ email: 'alice@example.test', password: PASSWORD })
  })
})

describe('the signed-in header', () => {
  it('shows the SortView heading and who is signed in', async () => {
    await renderSignedIn()

    expect(screen.getByRole('heading', { level: 1, name: 'SortView' })).toBeInTheDocument()
    expect(screen.getByText('Alice Example')).toBeInTheDocument()
    expect(screen.getByText('alice@example.test')).toBeInTheDocument()
    expect(screen.getByRole('banner')).toContainElement(screen.getByRole('button', { name: 'Sign out' }))
  })

  it('shows just the email for a user with no name', async () => {
    await renderSignedIn({ ...ALICE, full_name: '' })

    const identity = screen.getByText('alice@example.test').parentElement
    expect(identity).not.toBeNull()
    expect(within(identity as HTMLElement).getAllByText(/./)).toHaveLength(1)
  })

  it('returns to the sign-in form after a successful logout', async () => {
    const user = userEvent.setup()
    const fetchMock = await renderSignedIn()
    fetchMock.mockResolvedValueOnce(noContent())

    await user.click(screen.getByRole('button', { name: 'Sign out' }))

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByText('alice@example.test')).not.toBeInTheDocument()
    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session', '/api/organizations', '/api/auth/logout'])
    expect(callOf(fetchMock, 2).init.method).toBe('POST')
  })

  it('disables the button while a logout is in flight', async () => {
    const user = userEvent.setup()
    const fetchMock = await renderSignedIn()
    const pending = deferred<Response>()
    fetchMock.mockReturnValueOnce(pending.promise)

    await user.click(screen.getByRole('button', { name: 'Sign out' }))

    expect(screen.getByRole('button', { name: 'Signing out…' })).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByText('alice@example.test')).toBeInTheDocument()

    pending.resolve(noContent())
    await screen.findByRole('button', { name: 'Sign in' })
  })

  it.each([
    ['a refused origin', () => Promise.resolve(jsonResponse(403, ORIGIN_NOT_ALLOWED)), 'Request origin is not allowed.'],
    ['a network failure', () => Promise.reject(networkFailure()), 'Could not reach the server'],
  ])('stays signed in, explains and allows a retry after %s', async (_label, respond, message) => {
    const user = userEvent.setup()
    const fetchMock = await renderSignedIn()
    fetchMock.mockImplementationOnce(respond).mockResolvedValueOnce(noContent())

    await user.click(screen.getByRole('button', { name: 'Sign out' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(message)
    expect(screen.getByText('alice@example.test')).toBeInTheDocument()
    const retry = screen.getByRole('button', { name: 'Sign out' })
    expect(retry).not.toHaveAttribute('aria-disabled', 'true')

    await user.click(retry)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument())
  })

  it('contains nothing of the dashboard yet', async () => {
    const fetchMock = await renderSignedIn()
    await screen.findByText('Your account does not have access to any organizations.')

    for (const role of ['combobox', 'listbox', 'table', 'img', 'tab', 'textbox']) {
      expect(screen.queryByRole(role)).not.toBeInTheDocument()
    }
    expect(within(screen.getByRole('main')).queryByRole('button')).not.toBeInTheDocument()
    expect(screen.getByRole('main').textContent).not.toMatch(/reject|check-?in|pipeline|items|today|\d{2,}/i)
    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session', '/api/organizations'])
  })
})
