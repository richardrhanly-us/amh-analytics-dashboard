import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StrictMode, useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import {
  ALICE,
  deferred,
  INVALID_CREDENTIALS,
  jsonResponse,
  networkFailure,
  noContent,
  NOT_AUTHENTICATED,
  ORIGIN_NOT_ALLOWED,
  requestedUrls,
  stubFetch,
} from '../test/http.ts'
import { AuthProvider } from './AuthProvider.tsx'
import { useAuth } from './useAuth.ts'

/** Shows the auth state as text and exposes each action as a button. */
function Probe() {
  const { state, login, logout, retryRestore, sessionExpired } = useAuth()
  const [outcome, setOutcome] = useState('')
  const [renders, setRenders] = useState(0)

  const run = (action: Promise<void>) => {
    action.then(
      () => setOutcome('resolved'),
      (error: unknown) => setOutcome(`rejected: ${error instanceof Error ? error.message : 'unknown'}`),
    )
  }

  return (
    <div>
      <p data-testid="status">{state.status}</p>
      <p data-testid="user">{state.status === 'authenticated' ? state.user.email : ''}</p>
      <p data-testid="message">{state.status === 'error' ? state.message : ''}</p>
      <p data-testid="outcome">{outcome}</p>
      <p data-testid="renders">{renders}</p>
      <button onClick={() => run(login('alice@example.test', 'pw'))}>login</button>
      <button onClick={() => run(logout())}>logout</button>
      <button onClick={retryRestore}>retry</button>
      <button onClick={sessionExpired}>expire</button>
      <button onClick={() => setRenders((count) => count + 1)}>rerender</button>
    </div>
  )
}

function renderProbe() {
  return render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  )
}

const status = () => screen.getByTestId('status').textContent

async function settled(expected: string) {
  await waitFor(() => expect(status()).toBe(expected))
}

describe('restoring the session', () => {
  it('starts in the restoring state while the session request is pending', async () => {
    const fetchMock = stubFetch()
    const pending = deferred<Response>()
    fetchMock.mockReturnValue(pending.promise)

    renderProbe()

    expect(status()).toBe('restoring')
    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session'])

    pending.resolve(jsonResponse(200, ALICE))
    await settled('authenticated')
  })

  it('becomes authenticated when the API returns a user', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, ALICE))

    renderProbe()

    await settled('authenticated')
    expect(screen.getByTestId('user')).toHaveTextContent('alice@example.test')
  })

  it('becomes unauthenticated on a 401', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(401, NOT_AUTHENTICATED))

    renderProbe()

    await settled('unauthenticated')
  })

  it.each([
    ['a server error', () => Promise.resolve(jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' }))],
    ['a network failure', () => Promise.reject(networkFailure())],
    ['an unreadable 200', () => Promise.resolve(new Response('<html></html>', { status: 200 }))],
    ['a 403', () => Promise.resolve(jsonResponse(403, ORIGIN_NOT_ALLOWED))],
  ])('reports %s as an error, not as signed out', async (_label, respond) => {
    const fetchMock = stubFetch()
    fetchMock.mockImplementation(respond)

    renderProbe()

    await settled('error')
    expect(screen.getByTestId('message').textContent).not.toBe('')
  })

  it('recovers from a failed restore when retried', async () => {
    const user = userEvent.setup()
    const fetchMock = stubFetch()
    fetchMock.mockRejectedValueOnce(networkFailure()).mockResolvedValueOnce(jsonResponse(200, ALICE))

    renderProbe()
    await settled('error')
    await user.click(screen.getByRole('button', { name: 'retry' }))

    await settled('authenticated')
    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session', '/api/auth/session'])
  })

  it('asks for the session once, and not again when components re-render', async () => {
    const user = userEvent.setup()
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, ALICE))

    const view = renderProbe()
    await settled('authenticated')
    await user.click(screen.getByRole('button', { name: 'rerender' }))
    await user.click(screen.getByRole('button', { name: 'rerender' }))
    view.rerender(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    )

    expect(screen.getByTestId('renders')).toHaveTextContent('2')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('asks for the session once under StrictMode as well', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, ALICE))

    render(
      <StrictMode>
        <AuthProvider>
          <Probe />
        </AuthProvider>
      </StrictMode>,
    )

    await settled('authenticated')
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})

describe('login', () => {
  it('becomes authenticated with the returned user, without asking for the session again', async () => {
    const user = userEvent.setup()
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValueOnce(jsonResponse(401, NOT_AUTHENTICATED)).mockResolvedValueOnce(jsonResponse(200, ALICE))

    renderProbe()
    await settled('unauthenticated')
    await user.click(screen.getByRole('button', { name: 'login' }))

    await settled('authenticated')
    expect(screen.getByTestId('user')).toHaveTextContent('alice@example.test')
    expect(screen.getByTestId('outcome')).toHaveTextContent('resolved')
    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session', '/api/auth/login'])
  })

  it('stays unauthenticated and rejects when the login fails', async () => {
    const user = userEvent.setup()
    const fetchMock = stubFetch()
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, NOT_AUTHENTICATED))
      .mockResolvedValueOnce(jsonResponse(401, INVALID_CREDENTIALS))

    renderProbe()
    await settled('unauthenticated')
    await user.click(screen.getByRole('button', { name: 'login' }))

    await waitFor(() => expect(screen.getByTestId('outcome')).toHaveTextContent('rejected: Invalid email or password.'))
    expect(status()).toBe('unauthenticated')
  })
})

describe('logout', () => {
  it('becomes unauthenticated once the server answers 204', async () => {
    const user = userEvent.setup()
    const fetchMock = stubFetch()
    const pending = deferred<Response>()
    fetchMock.mockResolvedValueOnce(jsonResponse(200, ALICE)).mockReturnValueOnce(pending.promise)

    renderProbe()
    await settled('authenticated')
    await user.click(screen.getByRole('button', { name: 'logout' }))

    // Still signed in while the server has not answered.
    expect(status()).toBe('authenticated')

    pending.resolve(noContent())
    await settled('unauthenticated')
    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session', '/api/auth/logout'])
  })

  it.each([
    ['a refused origin', () => Promise.resolve(jsonResponse(403, ORIGIN_NOT_ALLOWED))],
    ['a network failure', () => Promise.reject(networkFailure())],
  ])('stays authenticated and rejects on %s', async (_label, respond) => {
    const user = userEvent.setup()
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValueOnce(jsonResponse(200, ALICE)).mockImplementationOnce(respond)

    renderProbe()
    await settled('authenticated')
    await user.click(screen.getByRole('button', { name: 'logout' }))

    await waitFor(() => expect(screen.getByTestId('outcome').textContent).toMatch(/^rejected: /))
    expect(status()).toBe('authenticated')
    expect(screen.getByTestId('user')).toHaveTextContent('alice@example.test')
  })
})

describe('a session that ended on the server', () => {
  it('drops the user and becomes unauthenticated, with no request', async () => {
    const user = userEvent.setup()
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValueOnce(jsonResponse(200, ALICE))

    renderProbe()
    await settled('authenticated')
    await user.click(screen.getByRole('button', { name: 'expire' }))

    expect(status()).toBe('unauthenticated')
    expect(screen.getByTestId('user')).toHaveTextContent('')
    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session'])
  })

  it('can be followed by a new login', async () => {
    const user = userEvent.setup()
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValueOnce(jsonResponse(200, ALICE)).mockResolvedValueOnce(jsonResponse(200, ALICE))

    renderProbe()
    await settled('authenticated')
    await user.click(screen.getByRole('button', { name: 'expire' }))
    await user.click(screen.getByRole('button', { name: 'login' }))

    await settled('authenticated')
    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session', '/api/auth/login'])
  })
})

describe('what the provider leaves alone', () => {
  it('touches no cookie and no browser storage across restore, login and logout', async () => {
    const cookieRead = vi.spyOn(Document.prototype, 'cookie', 'get')
    const cookieWrite = vi.spyOn(Document.prototype, 'cookie', 'set')
    const storageRead = vi.spyOn(Storage.prototype, 'getItem')
    const storageWrite = vi.spyOn(Storage.prototype, 'setItem')
    const user = userEvent.setup()
    const fetchMock = stubFetch()
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, NOT_AUTHENTICATED))
      .mockResolvedValueOnce(jsonResponse(200, ALICE))
      .mockResolvedValueOnce(noContent())

    renderProbe()
    await settled('unauthenticated')
    await user.click(screen.getByRole('button', { name: 'login' }))
    await settled('authenticated')
    await user.click(screen.getByRole('button', { name: 'logout' }))
    await settled('unauthenticated')

    expect(cookieRead).not.toHaveBeenCalled()
    expect(cookieWrite).not.toHaveBeenCalled()
    expect(storageRead).not.toHaveBeenCalled()
    expect(storageWrite).not.toHaveBeenCalled()
    expect(window.localStorage).toHaveLength(0)
    expect(window.sessionStorage).toHaveLength(0)
  })
})

describe('useAuth', () => {
  it('throws a clear error outside an AuthProvider', () => {
    // React reports the render error on the console; keep the test output quiet.
    vi.spyOn(console, 'error').mockImplementation(() => {})

    expect(() => render(<Probe />)).toThrow('useAuth must be used inside an AuthProvider.')
  })
})
