import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
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
import { AdminApp } from './AdminApp.tsx'

const FORBIDDEN = { code: 'forbidden', message: 'You do not have access to SortView administration.' }
const SESSION = 'GET /api/admin/session'

/** Answers GET /api/admin/session with each of `answers` in turn (the last one repeats). */
function sessionAnswers(...answers: Array<() => Response>) {
  let call = 0
  return () => answers[Math.min(call++, answers.length - 1)]()
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
  it('asks the admin session on load and shows the empty administration home to a platform admin', async () => {
    const fetchMock = serveApi({ [SESSION]: () => jsonResponse(200, ALICE) })

    render(<AdminApp />)

    expect(screen.getByRole('status')).toHaveTextContent('Checking your administration access')
    expect(await screen.findByText('No administration features yet.')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: 'SortView Administration' })).toBeInTheDocument()
    expect(screen.getByText(`Signed in as ${ALICE.email}`, { exact: false })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Log out' })).toBeInTheDocument()
    expect(requestedUrls(fetchMock)).toEqual(['/api/admin/session'])
    // Nothing of later administration features: no navigation, no links.
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
    expect(screen.queryAllByRole('link')).toEqual([])
  })

  it('offers sign-in on 401, then asks the admin session again rather than trusting the sign-in', async () => {
    const fetchMock = serveApi({
      [SESSION]: sessionAnswers(() => jsonResponse(401, NOT_AUTHENTICATED), () => jsonResponse(200, ALICE)),
      'POST /api/auth/login': () => jsonResponse(200, ALICE),
    })

    render(<AdminApp />)
    await signIn(fetchMock)

    expect(await screen.findByText('No administration features yet.')).toBeInTheDocument()
    expect(requestedUrls(fetchMock)).toEqual(['/api/admin/session', '/api/auth/login', '/api/admin/session'])
    const loginCall = callOf(fetchMock, 1)
    expect(loginCall.init.method).toBe('POST')
    expect(JSON.parse(String(loginCall.init.body))).toEqual({ email: ALICE.email, password: 'synthetic-Password-123' })
  })

  it('shows no access when a successful sign-in is not a platform admin', async () => {
    const fetchMock = serveApi({
      [SESSION]: sessionAnswers(() => jsonResponse(401, NOT_AUTHENTICATED), () => jsonResponse(403, FORBIDDEN)),
      'POST /api/auth/login': () => jsonResponse(200, ALICE),
    })

    render(<AdminApp />)
    await signIn(fetchMock)

    expect(await screen.findByText('This account does not have SortView administration access.')).toBeInTheDocument()
    expect(screen.queryByText('No administration features yet.')).not.toBeInTheDocument()
  })

  it('shows the sign-in error and stays on the form when sign-in fails', async () => {
    const fetchMock = serveApi({
      [SESSION]: () => jsonResponse(401, NOT_AUTHENTICATED),
      'POST /api/auth/login': () => jsonResponse(401, INVALID_CREDENTIALS),
    })

    render(<AdminApp />)
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

    render(<AdminApp />)

    expect(await screen.findByText('This account does not have SortView administration access.')).toBeInTheDocument()
    await person.click(screen.getByRole('button', { name: 'Log out' }))

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(requestedUrls(fetchMock)).toEqual(['/api/admin/session', '/api/auth/logout'])
    expect(callOf(fetchMock, 1).init.method).toBe('POST')
  })

  it('logs a platform admin out through the existing auth endpoint', async () => {
    const fetchMock = serveApi({
      [SESSION]: () => jsonResponse(200, ALICE),
      'POST /api/auth/logout': () => noContent(),
    })
    const person = userEvent.setup()

    render(<AdminApp />)
    await person.click(await screen.findByRole('button', { name: 'Log out' }))

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByText('No administration features yet.')).not.toBeInTheDocument()
    expect(requestedUrls(fetchMock)).toEqual(['/api/admin/session', '/api/auth/logout'])
  })

  it('stays signed in and says so when log out fails', async () => {
    serveApi({
      [SESSION]: () => jsonResponse(200, ALICE),
      'POST /api/auth/logout': () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' }),
    })
    const person = userEvent.setup()

    render(<AdminApp />)
    await person.click(await screen.findByRole('button', { name: 'Log out' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Sign-out failed. Please try again.')
    expect(screen.getByText('No administration features yet.')).toBeInTheDocument()
  })

  it('shows a fixed error with a retry for any other failure, and retries', async () => {
    const fetchMock = serveApi({
      [SESSION]: sessionAnswers(
        () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' }),
        () => jsonResponse(200, ALICE),
      ),
    })
    const person = userEvent.setup()

    render(<AdminApp />)

    expect(await screen.findByRole('alert')).toHaveTextContent('We could not check your administration access. Please try again.')
    await person.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByText('No administration features yet.')).toBeInTheDocument()
    expect(requestedUrls(fetchMock)).toEqual(['/api/admin/session', '/api/admin/session'])
  })

  it('treats a 200 that is not a user as a failure, never as access', async () => {
    serveApi({ [SESSION]: () => jsonResponse(200, { is_platform_admin: true }) })

    render(<AdminApp />)

    expect(await screen.findByRole('alert')).toHaveTextContent('We could not check your administration access.')
    expect(screen.queryByText('No administration features yet.')).not.toBeInTheDocument()
  })
})
