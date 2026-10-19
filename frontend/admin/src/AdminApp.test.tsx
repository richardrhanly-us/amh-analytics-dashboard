import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import {
  ALICE,
  INVALID_CREDENTIALS,
  NOT_AUTHENTICATED,
  callOf,
  jsonResponse,
  noContent,
  requestedUrls,
  serveApi,
  type FetchMock,
} from '../../src/test/http.ts'
import { axeViolations } from '../../src/test/axe.ts'
import { AdminApp } from './AdminApp.tsx'

const FORBIDDEN = { code: 'forbidden', message: 'You do not have access to SortView administration.' }
const SESSION = 'GET /api/admin/session'
const LIBRARIES = 'GET /api/admin/libraries'
const NO_LIBRARIES = () => jsonResponse(200, { timezone: 'America/Chicago', libraries: [] })

/** Answers a request with each of `answers` in turn (the last one repeats). */
function inTurn(...answers: Array<() => Response>) {
  let call = 0
  return () => answers[Math.min(call++, answers.length - 1)]()
}

function renderAdmin(path = '/') {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AdminApp />
    </MemoryRouter>,
  )
}

async function signIn(fetchMock: FetchMock) {
  const person = userEvent.setup()
  await screen.findByRole('heading', { name: 'Sign in' })
  await person.type(screen.getByLabelText('Email'), ` ${ALICE.email} `)
  await person.type(screen.getByLabelText('Password'), 'synthetic-Password-123')
  await person.click(screen.getByRole('button', { name: 'Sign in' }))
  return { person, fetchMock }
}

describe('AdminApp', () => {
  it('asks the admin session on load and shows a platform admin the libraries', async () => {
    const fetchMock = serveApi({ [SESSION]: () => jsonResponse(200, ALICE), [LIBRARIES]: NO_LIBRARIES })

    renderAdmin()

    expect(screen.getByRole('status')).toHaveTextContent('Checking your administration access')
    expect(await screen.findByRole('heading', { level: 2, name: 'Libraries' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: 'SortView Administration' })).toBeInTheDocument()
    expect(screen.getByText(`Signed in as ${ALICE.email}`, { exact: false })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Log out' })).toBeInTheDocument()
    expect(await screen.findByText('No libraries have been provisioned yet.')).toBeInTheDocument()
    expect(requestedUrls(fetchMock)).toEqual(['/api/admin/session', '/api/admin/libraries'])
  })

  it('offers sign-in on 401, then asks the admin session again rather than trusting the sign-in', async () => {
    const fetchMock = serveApi({
      [SESSION]: inTurn(() => jsonResponse(401, NOT_AUTHENTICATED), () => jsonResponse(200, ALICE)),
      'POST /api/auth/login': () => jsonResponse(200, ALICE),
      [LIBRARIES]: NO_LIBRARIES,
    })

    renderAdmin()
    await signIn(fetchMock)

    expect(await screen.findByRole('heading', { level: 2, name: 'Libraries' })).toBeInTheDocument()
    expect(requestedUrls(fetchMock).slice(0, 3)).toEqual(['/api/admin/session', '/api/auth/login', '/api/admin/session'])
    const loginCall = callOf(fetchMock, 1)
    expect(loginCall.init.method).toBe('POST')
    expect(JSON.parse(String(loginCall.init.body))).toEqual({ email: ALICE.email, password: 'synthetic-Password-123' })
  })

  it('shows no access when a successful sign-in is not a platform admin, and asks for no library', async () => {
    const fetchMock = serveApi({
      [SESSION]: inTurn(() => jsonResponse(401, NOT_AUTHENTICATED), () => jsonResponse(403, FORBIDDEN)),
      'POST /api/auth/login': () => jsonResponse(200, ALICE),
    })

    renderAdmin()
    await signIn(fetchMock)

    expect(await screen.findByText('This account does not have SortView administration access.')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Libraries' })).not.toBeInTheDocument()
    expect(requestedUrls(fetchMock)).not.toContain('/api/admin/libraries')
  })

  it('shows the sign-in error and stays on the form when sign-in fails', async () => {
    const fetchMock = serveApi({
      [SESSION]: () => jsonResponse(401, NOT_AUTHENTICATED),
      'POST /api/auth/login': () => jsonResponse(401, INVALID_CREDENTIALS),
    })

    renderAdmin()
    await signIn(fetchMock)

    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid email or password.')
    expect(screen.getByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(requestedUrls(fetchMock)).toEqual(['/api/admin/session', '/api/auth/login'])
  })

  it('shows no access on 403, with a working log out', async () => {
    const fetchMock = serveApi({
      [SESSION]: () => jsonResponse(403, FORBIDDEN),
      'POST /api/auth/logout': () => noContent(),
    })
    const person = userEvent.setup()

    renderAdmin()

    expect(await screen.findByText('This account does not have SortView administration access.')).toBeInTheDocument()
    await person.click(screen.getByRole('button', { name: 'Log out' }))

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(requestedUrls(fetchMock)).toEqual(['/api/admin/session', '/api/auth/logout'])
    expect(callOf(fetchMock, 1).init.method).toBe('POST')
  })

  it('logs a platform admin out through the existing auth endpoint', async () => {
    const fetchMock = serveApi({
      [SESSION]: () => jsonResponse(200, ALICE),
      [LIBRARIES]: NO_LIBRARIES,
      'POST /api/auth/logout': () => noContent(),
    })
    const person = userEvent.setup()

    renderAdmin()
    await person.click(await screen.findByRole('button', { name: 'Log out' }))

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Libraries' })).not.toBeInTheDocument()
    expect(requestedUrls(fetchMock)).toContain('/api/auth/logout')
  })

  it('stays signed in and says so when log out fails', async () => {
    serveApi({
      [SESSION]: () => jsonResponse(200, ALICE),
      [LIBRARIES]: NO_LIBRARIES,
      'POST /api/auth/logout': () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' }),
    })
    const person = userEvent.setup()

    renderAdmin()
    await person.click(await screen.findByRole('button', { name: 'Log out' }))

    expect(await screen.findByText('Sign-out failed. Please try again.')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Libraries' })).toBeInTheDocument()
  })

  it('returns to sign-in when the session ends while a page is loading', async () => {
    serveApi({
      [SESSION]: () => jsonResponse(200, ALICE),
      [LIBRARIES]: () => jsonResponse(401, NOT_AUTHENTICATED),
    })

    renderAdmin()

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('shows a fixed error with a retry for any other failure, and retries', async () => {
    const fetchMock = serveApi({
      [SESSION]: inTurn(
        () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' }),
        () => jsonResponse(200, ALICE),
      ),
      [LIBRARIES]: NO_LIBRARIES,
    })
    const person = userEvent.setup()

    renderAdmin()

    expect(await screen.findByRole('alert')).toHaveTextContent('We could not check your administration access. Please try again.')
    await person.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByRole('heading', { name: 'Libraries' })).toBeInTheDocument()
    expect(requestedUrls(fetchMock).slice(0, 2)).toEqual(['/api/admin/session', '/api/admin/session'])
  })

  it('treats a 200 that is not a user as a failure, never as access', async () => {
    serveApi({ [SESSION]: () => jsonResponse(200, { is_platform_admin: true }) })

    renderAdmin()

    expect(await screen.findByRole('alert')).toHaveTextContent('We could not check your administration access.')
    expect(screen.queryByRole('heading', { name: 'Libraries' })).not.toBeInTheDocument()
  })
})

describe('accessibility', () => {
  it('axe finds no violation on the sign-in form', async () => {
    serveApi({ [SESSION]: () => jsonResponse(401, NOT_AUTHENTICATED) })
    renderAdmin()
    await screen.findByRole('heading', { name: 'Sign in' })
    expect(await axeViolations()).toEqual([])
  })

  it('has a skip link, first in the tab order, that moves focus to the main content', async () => {
    serveApi({ [SESSION]: () => jsonResponse(200, ALICE), [LIBRARIES]: NO_LIBRARIES })
    renderAdmin()
    await screen.findByRole('heading', { level: 2, name: 'Libraries' })
    const person = userEvent.setup()

    await person.tab()
    const skip = screen.getByRole('link', { name: 'Skip to main content' })
    expect(skip).toHaveFocus()
    expect(skip).toHaveAttribute('href', '#main-content')

    await person.click(skip)
    const main = screen.getByRole('main')
    expect(main).toHaveFocus()
    expect(main).toHaveAttribute('id', 'main-content')
    expect(window.location.hash).toBe('')
  })
  it('names the sign-in form in the browser tab, and leaves focus alone when the app opens on it', async () => {
    serveApi({ [SESSION]: () => jsonResponse(401, NOT_AUTHENTICATED) })
    renderAdmin()
    await screen.findByRole('heading', { name: 'Sign in' })

    expect(document.title).toBe('Sign in – SortView Administration')
    expect(screen.getByRole('main')).not.toHaveFocus()
  })

  it('moves focus to the main content on signing in, and names the view arrived at', async () => {
    const fetchMock = serveApi({
      [SESSION]: inTurn(() => jsonResponse(401, NOT_AUTHENTICATED), () => jsonResponse(200, ALICE)),
      'POST /api/auth/login': () => jsonResponse(200, ALICE),
      [LIBRARIES]: NO_LIBRARIES,
    })
    renderAdmin()
    await signIn(fetchMock)

    await screen.findByRole('heading', { level: 2, name: 'Libraries' })
    await waitFor(() => expect(screen.getByRole('main')).toHaveFocus())
    expect(document.title).toBe('Libraries – SortView Administration')
  })

  it('moves focus to the main content when a sign-in has no access', async () => {
    const fetchMock = serveApi({
      [SESSION]: inTurn(() => jsonResponse(401, NOT_AUTHENTICATED), () => jsonResponse(403, FORBIDDEN)),
      'POST /api/auth/login': () => jsonResponse(200, ALICE),
    })
    renderAdmin()
    await signIn(fetchMock)

    await screen.findByText('This account does not have SortView administration access.')
    await waitFor(() => expect(screen.getByRole('main')).toHaveFocus())
  })

  it('moves focus to the main content on signing out, where the sign-in form is', async () => {
    serveApi({
      [SESSION]: () => jsonResponse(200, ALICE),
      [LIBRARIES]: NO_LIBRARIES,
      'POST /api/auth/logout': () => noContent(),
    })
    const person = userEvent.setup()
    renderAdmin()
    await person.click(await screen.findByRole('button', { name: 'Log out' }))

    await screen.findByRole('heading', { name: 'Sign in' })
    await waitFor(() => expect(screen.getByRole('main')).toHaveFocus())
    expect(document.title).toBe('Sign in – SortView Administration')
  })

  it('axe finds no violation once signed in and focus has moved', async () => {
    const fetchMock = serveApi({
      [SESSION]: inTurn(() => jsonResponse(401, NOT_AUTHENTICATED), () => jsonResponse(200, ALICE)),
      'POST /api/auth/login': () => jsonResponse(200, ALICE),
      [LIBRARIES]: NO_LIBRARIES,
    })
    renderAdmin()
    await signIn(fetchMock)
    await screen.findByText('No libraries have been provisioned yet.')

    expect(await axeViolations()).toEqual([])
  })
})
