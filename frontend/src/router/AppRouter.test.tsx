import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import {
  ALICE,
  callOf,
  deferred,
  type FetchMock,
  jsonResponse,
  liveRoutes,
  networkFailure,
  noContent,
  NORTHBRIDGE,
  NORTHBRIDGE_DETAIL,
  NOT_AUTHENTICATED,
  ORGANIZATION_NOT_FOUND,
  requestedUrls,
  RIVERSIDE,
  RIVERSIDE_DETAIL,
  serveApi,
  textResponse,
} from '../test/http.ts'
import { appAt, renderApp } from '../test/render.tsx'

type Routes = Parameters<typeof serveApi>[0]

const SESSION = 'GET /api/auth/session'
const LIST = 'GET /api/organizations'
const NORTHBRIDGE_URL = 'GET /api/organizations/northbridge'
const RIVERSIDE_URL = 'GET /api/organizations/riverside'

const NOT_FOUND_TEXT = 'This page does not exist, or you do not have access to it.'

/** A signed-in user with two organizations whose branches all have live data, unless a test replaces a route. */
function serve(overrides: Routes = {}): FetchMock {
  return serveApi({
    [SESSION]: () => jsonResponse(200, ALICE),
    [LIST]: () => jsonResponse(200, [NORTHBRIDGE, RIVERSIDE]),
    [NORTHBRIDGE_URL]: () => jsonResponse(200, NORTHBRIDGE_DETAIL),
    [RIVERSIDE_URL]: () => jsonResponse(200, RIVERSIDE_DETAIL),
    ...liveRoutes('northbridge', 'central'),
    ...liveRoutes('northbridge', 'east-side'),
    ...liveRoutes('riverside', 'main'),
    ...overrides,
  })
}

const address = () => screen.getByTestId('address').textContent
const main = () => screen.getByRole('main')
const heading = (name: string) => screen.findByRole('heading', { level: 2, name })
const link = (name: string) => within(main()).getByRole('link', { name })
const linkNames = () =>
  within(main())
    .queryAllByRole('link')
    .map((element) => element.textContent)
/** Requests for the organization list or an organization -- not for a branch's live data. */
const organizationRequests = (fetchMock: FetchMock) =>
  requestedUrls(fetchMock).filter((url) => url.startsWith('/api/organizations') && !url.includes('/branches/'))
const liveRequests = (fetchMock: FetchMock) => requestedUrls(fetchMock).filter((url) => url.includes('/branches/'))

/** Answers a request first with each of `replies` in turn, then keeps giving the last one. */
function inTurn(...replies: Array<() => Response | Promise<Response>>) {
  let call = 0
  return () => replies[Math.min(call++, replies.length - 1)]()
}

describe('before sign-in', () => {
  it('shows the sign-in form and asks for no organization when nobody is signed in', async () => {
    const fetchMock = serve({ [SESSION]: () => jsonResponse(401, NOT_AUTHENTICATED) })

    renderApp('/organizations/northbridge/branches/central')

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session'])
  })

  it('asks for no organization while the session is being restored', async () => {
    const pending = deferred<Response>()
    const fetchMock = serve({ [SESSION]: () => pending.promise })

    renderApp('/organizations/northbridge')

    expect(screen.getByRole('status')).toHaveTextContent('Checking your session')
    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session'])

    pending.resolve(jsonResponse(200, ALICE))
    await heading('Northbridge Library')
  })

  it('asks for no organization when the session check fails', async () => {
    const fetchMock = serve({
      [SESSION]: inTurn(
        () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' }),
        () => jsonResponse(200, ALICE),
      ),
    })
    const user = userEvent.setup()

    renderApp('/organizations')

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session'])

    await user.click(screen.getByRole('button', { name: 'Try again' }))
    await heading('Organizations')
  })
})

describe('the signed-in app', () => {
  it('takes / to the organization list', async () => {
    serve()

    renderApp('/')

    await heading('Organizations')
    expect(address()).toBe('/organizations')
  })

  it('shows who is signed in on every page', async () => {
    serve()

    renderApp('/organizations/northbridge/branches/central')

    await heading('Central Branch')
    const banner = screen.getByRole('banner')
    expect(within(banner).getByRole('heading', { level: 1, name: 'SortView' })).toBeInTheDocument()
    expect(within(banner).getByText('Alice Example')).toBeInTheDocument()
    expect(within(banner).getByText('alice@example.test')).toBeInTheDocument()
  })

  it.each(['/nowhere', '/organizations/northbridge/settings', '/organizations/northbridge/branches', '/branches/central'])(
    'shows the not-found page at %s and asks the API for nothing more',
    async (path) => {
      const fetchMock = serve()
      const user = userEvent.setup()

      renderApp(path)

      await heading('Page not found')
      expect(main()).toHaveTextContent(NOT_FOUND_TEXT)
      expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session'])

      await user.click(link('Go to organizations'))
      await heading('Organizations')
      expect(address()).toBe('/organizations')
    },
  )

  it('signs out from a routed page', async () => {
    const fetchMock = serve({ 'POST /api/auth/logout': () => noContent() })
    const user = userEvent.setup()

    renderApp('/organizations/northbridge')
    await heading('Northbridge Library')
    await user.click(screen.getByRole('button', { name: 'Sign out' }))

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByText('Northbridge Library')).not.toBeInTheDocument()
    expect(screen.queryByText('alice@example.test')).not.toBeInTheDocument()
    expect(requestedUrls(fetchMock).at(-1)).toBe('/api/auth/logout')
  })

  it.each(['/organizations', '/organizations/northbridge', '/organizations/northbridge/branches/central', '/nowhere'])(
    'leaves %s for / on a successful sign-out',
    async (path) => {
      serve({ 'POST /api/auth/logout': () => noContent() })
      const user = userEvent.setup()

      renderApp(path)
      await screen.findByRole('heading', { level: 2 })
      expect(address()).toBe(path)
      await user.click(screen.getByRole('button', { name: 'Sign out' }))

      await screen.findByRole('button', { name: 'Sign in' })
      expect(address()).toBe('/')
    },
  )

  it('starts the next person to sign in at the organization list, not the previous page', async () => {
    const fetchMock = serve({
      'POST /api/auth/logout': () => noContent(),
      'POST /api/auth/login': () => jsonResponse(200, { id: 8, email: 'bob@example.test', full_name: 'Bob Example' }),
    })
    const user = userEvent.setup()

    renderApp('/organizations/northbridge/branches/central')
    await heading('Central Branch')
    await user.click(screen.getByRole('button', { name: 'Sign out' }))
    await screen.findByRole('button', { name: 'Sign in' })
    await user.type(screen.getByLabelText('Email'), 'bob@example.test')
    await user.type(screen.getByLabelText('Password'), 'pw{Enter}')

    await heading('Organizations')
    expect(address()).toBe('/organizations')
    expect(screen.getByRole('banner')).toHaveTextContent('bob@example.test')
    // The organization was loaded once, for the first user, and not again for the second.
    expect(organizationRequests(fetchMock)).toEqual(['/api/organizations/northbridge', '/api/organizations'])
  })

  it.each([
    ['a refused origin', () => jsonResponse(403, { code: 'origin_not_allowed', message: 'Request origin is not allowed.' })],
    ['a server error', () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' })],
    ['a network failure', () => Promise.reject(networkFailure())],
  ])('stays signed in on the same page when sign-out fails with %s', async (_label, fail) => {
    const fetchMock = serve({ 'POST /api/auth/logout': fail })
    const user = userEvent.setup()

    renderApp('/organizations/northbridge/branches/central')
    await heading('Central Branch')
    await user.click(screen.getByRole('button', { name: 'Sign out' }))

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(address()).toBe('/organizations/northbridge/branches/central')
    expect(screen.getByRole('heading', { level: 2, name: 'Central Branch' })).toBeInTheDocument()
    expect(screen.getByRole('banner')).toHaveTextContent('alice@example.test')
    expect(screen.getByRole('button', { name: 'Sign out' })).not.toHaveAttribute('aria-disabled', 'true')
    expect(organizationRequests(fetchMock)).toEqual(['/api/organizations/northbridge'])
  })

  it.each([
    ['/organizations', 'GET /api/organizations'],
    ['/organizations/northbridge', 'GET /api/organizations/northbridge'],
    ['/organizations/northbridge/branches/central', 'GET /api/organizations/northbridge'],
  ])('keeps the address %s when the session expires', async (path, expiredRequest) => {
    serve({ [expiredRequest]: () => jsonResponse(401, NOT_AUTHENTICATED) })

    renderApp(path)

    await screen.findByRole('button', { name: 'Sign in' })
    expect(address()).toBe(path)
  })

  it('moves back and forward through the pages like any site', async () => {
    serve()
    const user = userEvent.setup()

    renderApp('/')
    await user.click(await within(main()).findByRole('link', { name: 'Northbridge Library' }))
    await heading('Northbridge Library')
    await user.click(link('East Side Branch'))
    await heading('East Side Branch')
    expect(address()).toBe('/organizations/northbridge/branches/east-side')

    await user.click(screen.getByRole('button', { name: 'browser-back' }))
    await heading('Northbridge Library')
    expect(address()).toBe('/organizations/northbridge')

    await user.click(screen.getByRole('button', { name: 'browser-back' }))
    await heading('Organizations')
    expect(address()).toBe('/organizations')

    await user.click(screen.getByRole('button', { name: 'browser-forward' }))
    await heading('Northbridge Library')
    expect(address()).toBe('/organizations/northbridge')
  })
})

describe('the organization list', () => {
  it('announces that it is loading', async () => {
    const pending = deferred<Response>()
    serve({ [LIST]: () => pending.promise })

    renderApp('/organizations')

    expect(await screen.findByText('Loading organizations…')).toHaveRole('status')
    expect(linkNames()).toEqual([])

    pending.resolve(jsonResponse(200, [NORTHBRIDGE]))
    await waitFor(() => expect(linkNames()).toEqual(['Northbridge Library']))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('lists exactly the organizations the API returned, in its order, each a link to its page', async () => {
    serve()

    renderApp('/organizations')
    await heading('Organizations')

    await waitFor(() => expect(linkNames()).toEqual(['Northbridge Library', 'Riverside Library']))
    expect(link('Northbridge Library')).toHaveAttribute('href', '/organizations/northbridge')
    expect(link('Riverside Library')).toHaveAttribute('href', '/organizations/riverside')
  })

  it('shows the role and a suspended organization in words, not just colour', async () => {
    serve()

    renderApp('/organizations')
    await waitFor(() => expect(linkNames()).toHaveLength(2))

    const [northbridge, riverside] = within(main()).getAllByRole('listitem')
    expect(northbridge).toHaveTextContent('Admin')
    expect(northbridge).not.toHaveTextContent(/suspended|read-only/i)
    expect(riverside).toHaveTextContent('Viewer')
    expect(riverside).toHaveTextContent('Suspended (read-only)')
    expect(main()).not.toHaveTextContent(/read_only|access_mode/)
  })

  it('still links a suspended organization and one with a role it does not know', async () => {
    serve({ [LIST]: () => jsonResponse(200, [{ ...RIVERSIDE, role: 'curator' }]) })

    renderApp('/organizations')
    await waitFor(() => expect(linkNames()).toEqual(['Riverside Library']))

    expect(link('Riverside Library')).toHaveAttribute('href', '/organizations/riverside')
    expect(main()).not.toHaveTextContent(/curator/i)
  })

  it('says so when the account has no organizations', async () => {
    serve({ [LIST]: () => jsonResponse(200, []) })

    renderApp('/organizations')

    expect(await screen.findByText('Your account does not have access to any organizations.')).toBeInTheDocument()
    expect(linkNames()).toEqual([])
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it.each([
    ['a server error', () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' })],
    ['a crash page', () => textResponse(502, 'Traceback (most recent call last): organization_id=41')],
    ['a network failure', () => Promise.reject(networkFailure())],
    ['a malformed list', () => jsonResponse(200, [{ ...NORTHBRIDGE, access_mode: 'blocked' }])],
    ['an unexpected 404', () => jsonResponse(404, ORGANIZATION_NOT_FOUND)],
  ])('shows a safe message for %s, and the list after a retry', async (_label, fail) => {
    const fetchMock = serve({ [LIST]: inTurn(fail, () => jsonResponse(200, [NORTHBRIDGE])) })
    const user = userEvent.setup()

    renderApp('/organizations')

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).not.toMatch(/Traceback|organization_id|41|Failed to fetch|blocked|\[object/)
    expect(alert.textContent?.length).toBeLessThan(120)
    expect(linkNames()).toEqual([])
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Try again' }))

    await waitFor(() => expect(linkNames()).toEqual(['Northbridge Library']))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(organizationRequests(fetchMock)).toEqual(['/api/organizations', '/api/organizations'])
  })

  it('returns to the sign-in form, with no user left, when the API answers 401', async () => {
    const fetchMock = serve({ [LIST]: () => jsonResponse(401, NOT_AUTHENTICATED) })

    renderApp('/organizations')

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByText('alice@example.test')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign out' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Organizations' })).not.toBeInTheDocument()
    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session', '/api/organizations'])
  })

  it('comes back to the same page after signing in again', async () => {
    const fetchMock = serve({
      [NORTHBRIDGE_URL]: inTurn(
        () => jsonResponse(401, NOT_AUTHENTICATED),
        () => jsonResponse(200, NORTHBRIDGE_DETAIL),
      ),
      'POST /api/auth/login': () => jsonResponse(200, ALICE),
    })
    const user = userEvent.setup()

    renderApp('/organizations/northbridge/branches/central')
    await screen.findByRole('button', { name: 'Sign in' })
    await user.type(screen.getByLabelText('Email'), 'alice@example.test')
    await user.type(screen.getByLabelText('Password'), 'pw{Enter}')

    await heading('Central Branch')
    expect(address()).toBe('/organizations/northbridge/branches/central')
    expect(requestedUrls(fetchMock).filter((url) => url === '/api/auth/session')).toHaveLength(1)
  })

  it('asks for the list once, however often the app re-renders', async () => {
    const fetchMock = serve({ 'POST /api/auth/logout': () => jsonResponse(500, {}) })
    const user = userEvent.setup()

    const view = renderApp('/organizations')
    await waitFor(() => expect(linkNames()).toHaveLength(2))
    view.rerender(appAt('/organizations'))
    view.rerender(appAt('/organizations'))
    // A failed sign-out re-renders the header, and nothing else.
    await user.click(screen.getByRole('button', { name: 'Sign out' }))
    await screen.findByRole('alert')

    expect(linkNames()).toHaveLength(2)
    expect(organizationRequests(fetchMock)).toEqual(['/api/organizations'])
  })
})

describe('an organization page', () => {
  it('asks for exactly the organization in the address, once', async () => {
    const fetchMock = serve()

    const view = renderApp('/organizations/riverside')
    await heading('Riverside Library')
    view.rerender(appAt('/organizations/riverside'))

    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session', '/api/organizations/riverside'])
    expect(callOf(fetchMock, 1).init.method).toBe('GET')
  })

  it('announces that it is loading', async () => {
    const pending = deferred<Response>()
    serve({ [NORTHBRIDGE_URL]: () => pending.promise })

    renderApp('/organizations/northbridge')

    expect(await screen.findByText('Loading organization…')).toHaveRole('status')

    pending.resolve(jsonResponse(200, NORTHBRIDGE_DETAIL))
    await heading('Northbridge Library')
  })

  it('shows the organization, a way back, and exactly its returned branches as links', async () => {
    serve()

    renderApp('/organizations/northbridge')
    await heading('Northbridge Library')

    expect(within(main()).getByRole('heading', { level: 3, name: 'Branches' })).toBeInTheDocument()
    const breadcrumb = within(main()).getByRole('navigation', { name: 'Breadcrumb' })
    expect(within(breadcrumb).getByRole('link', { name: 'Organizations' })).toHaveAttribute('href', '/organizations')

    const branches = within(main()).getByRole('list', { name: 'Branches' })
    expect(within(branches).getAllByRole('link').map((element) => element.textContent)).toEqual([
      'Central Branch',
      'East Side Branch',
    ])
    expect(link('Central Branch')).toHaveAttribute('href', '/organizations/northbridge/branches/central')
    expect(link('East Side Branch')).toHaveAttribute('href', '/organizations/northbridge/branches/east-side')
  })

  it('marks the primary branch in words', async () => {
    serve()

    renderApp('/organizations/northbridge')
    await heading('Northbridge Library')

    const [central, eastSide] = within(within(main()).getByRole('list', { name: 'Branches' })).getAllByRole('listitem')
    expect(central).toHaveTextContent('Primary branch')
    expect(eastSide).not.toHaveTextContent('Primary')
  })

  it('says so when the organization has no branches', async () => {
    serve({ [NORTHBRIDGE_URL]: () => jsonResponse(200, { ...NORTHBRIDGE_DETAIL, branches: [] }) })

    renderApp('/organizations/northbridge')
    await heading('Northbridge Library')

    expect(screen.getByText('This organization has no active branches.')).toBeInTheDocument()
    expect(linkNames()).toEqual(['Organizations'])
  })

  it('explains a suspended organization and keeps its branches reachable', async () => {
    serve()
    const user = userEvent.setup()

    renderApp('/organizations/riverside')
    await heading('Riverside Library')

    expect(screen.getByRole('note')).toHaveTextContent(
      'This organization’s account is currently suspended. Historical dashboard data remains available.',
    )
    await user.click(link('Riverside Main'))
    await heading('Riverside Main')
    expect(screen.getByRole('note')).toHaveTextContent('currently suspended')
  })

  it('shows no suspension notice for an organization with full access', async () => {
    serve()

    renderApp('/organizations/northbridge')
    await heading('Northbridge Library')

    expect(screen.queryByRole('note')).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/suspended/i)
  })

  it('shows the not-found page for an organization the API will not return', async () => {
    const fetchMock = serve({ 'GET /api/organizations/someone-elses': () => jsonResponse(404, ORGANIZATION_NOT_FOUND) })

    renderApp('/organizations/someone-elses')

    await heading('Page not found')
    expect(main()).toHaveTextContent(NOT_FOUND_TEXT)
    expect(main()).not.toHaveTextContent(/someone-elses|Organization not found/)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
    expect(link('Go to organizations')).toHaveAttribute('href', '/organizations')
    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session', '/api/organizations/someone-elses'])
  })

  it('treats a 403 the same way', async () => {
    serve({ [NORTHBRIDGE_URL]: () => jsonResponse(403, { code: 'forbidden', message: 'Tenant 41 is not yours.' }) })

    renderApp('/organizations/northbridge')

    await heading('Page not found')
    expect(main()).not.toHaveTextContent(/41|Tenant|northbridge/i)
  })

  it('returns to the sign-in form when the API answers 401', async () => {
    serve({ [NORTHBRIDGE_URL]: () => jsonResponse(401, NOT_AUTHENTICATED) })

    renderApp('/organizations/northbridge')

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByText('alice@example.test')).not.toBeInTheDocument()
    expect(screen.queryByText('Northbridge Library')).not.toBeInTheDocument()
  })

  it.each([
    ['a server error', () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' })],
    ['a crash page', () => textResponse(500, 'Traceback: customer_id=41')],
    ['a network failure', () => Promise.reject(networkFailure())],
    ['a malformed organization', () => jsonResponse(200, { ...NORTHBRIDGE_DETAIL, branches: 'central' })],
    ['an answer about another organization', () => jsonResponse(200, RIVERSIDE_DETAIL)],
  ])('shows a safe message for %s, and the organization after a retry', async (_label, fail) => {
    const fetchMock = serve({ [NORTHBRIDGE_URL]: inTurn(fail, () => jsonResponse(200, NORTHBRIDGE_DETAIL)) })
    const user = userEvent.setup()

    renderApp('/organizations/northbridge')

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).not.toMatch(/Traceback|customer_id|41|Failed to fetch|Riverside|\[object/)
    expect(screen.queryByRole('heading', { name: 'Page not found' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Try again' }))

    await heading('Northbridge Library')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(organizationRequests(fetchMock)).toEqual(['/api/organizations/northbridge', '/api/organizations/northbridge'])
  })

  it('never shows a slow answer for one organization under another', async () => {
    const slow = deferred<Response>()
    const fetchMock = serve({ [NORTHBRIDGE_URL]: () => slow.promise })
    const user = userEvent.setup()

    renderApp('/organizations')
    await waitFor(() => expect(linkNames()).toHaveLength(2))
    await user.click(link('Northbridge Library'))
    await screen.findByText('Loading organization…')
    await user.click(screen.getByRole('button', { name: 'browser-back' }))
    await waitFor(() => expect(linkNames()).toHaveLength(2))
    await user.click(link('Riverside Library'))
    await heading('Riverside Library')

    // The abandoned request was cancelled, and its answer, arriving now, changes nothing.
    const abandoned = fetchMock.mock.calls.find(([url]) => url === '/api/organizations/northbridge')
    expect(abandoned?.[1]?.signal?.aborted).toBe(true)
    slow.resolve(jsonResponse(200, NORTHBRIDGE_DETAIL))
    await Promise.resolve()
    await waitFor(() => expect(address()).toBe('/organizations/riverside'))

    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('Riverside Library')
    expect(main()).not.toHaveTextContent(/Northbridge|Central Branch/)
    expect(linkNames()).toEqual(['Organizations', 'Riverside Main'])
  })

  it('shows the loading state, not the previous organization, the moment the address changes', async () => {
    const slow = deferred<Response>()
    serve({ [RIVERSIDE_URL]: () => slow.promise })
    const user = userEvent.setup()

    renderApp('/organizations')
    await waitFor(() => expect(linkNames()).toHaveLength(2))
    await user.click(link('Northbridge Library'))
    await heading('Northbridge Library')
    await user.click(screen.getByRole('button', { name: 'browser-back' }))
    await waitFor(() => expect(linkNames()).toHaveLength(2))
    await user.click(link('Riverside Library'))

    expect(await screen.findByText('Loading organization…')).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/Northbridge|Central Branch/)

    slow.resolve(jsonResponse(200, RIVERSIDE_DETAIL))
    await heading('Riverside Library')
  })
})

describe('a branch page', () => {
  it('shows the organization and the branch from a fresh load of its address', async () => {
    const session = deferred<Response>()
    const fetchMock = serve({ [SESSION]: () => session.promise })

    renderApp('/organizations/northbridge/branches/east-side')
    expect(screen.getByRole('status')).toHaveTextContent('Checking your session')
    session.resolve(jsonResponse(200, ALICE))

    await heading('East Side Branch')
    expect(link('Northbridge Library')).toBeInTheDocument()
    expect(await screen.findByRole('heading', { level: 3, name: 'Today' })).toBeInTheDocument()
    expect(address()).toBe('/organizations/northbridge/branches/east-side')
    expect(requestedUrls(fetchMock).slice(0, 3)).toEqual([
      '/api/auth/session',
      '/api/organizations/northbridge',
      '/api/organizations/northbridge/branches/east-side/pipeline-status',
    ])
  })

  it('asks for live data only for the branch in the address', async () => {
    const fetchMock = serve()

    renderApp('/organizations/northbridge/branches/central')
    await heading('Central Branch')
    await screen.findByRole('heading', { level: 3, name: 'Top reject reasons' })

    expect(liveRequests(fetchMock)).toHaveLength(6)
    expect(liveRequests(fetchMock).every((url) => url.startsWith('/api/organizations/northbridge/branches/central/'))).toBe(true)
    // The hourly chart is the one image; there is still no picker, tab strip or gauge.
    for (const role of ['combobox', 'tab', 'tablist', 'progressbar', 'meter']) {
      expect(screen.queryByRole(role)).not.toBeInTheDocument()
    }
  })

  it('asks for no live data for a branch the organization did not return', async () => {
    const fetchMock = serve()

    renderApp('/organizations/northbridge/branches/west')
    await heading('Page not found')

    expect(liveRequests(fetchMock)).toEqual([])
  })

  it('links back to the organization and to the organization list', async () => {
    const fetchMock = serve()
    const user = userEvent.setup()

    renderApp('/organizations/northbridge/branches/central')
    await heading('Central Branch')

    const breadcrumb = within(main()).getByRole('navigation', { name: 'Breadcrumb' })
    expect(within(breadcrumb).getByRole('link', { name: 'Organizations' })).toHaveAttribute('href', '/organizations')
    expect(within(breadcrumb).getByRole('link', { name: 'Northbridge Library' })).toHaveAttribute(
      'href',
      '/organizations/northbridge',
    )

    await user.click(link('Northbridge Library'))
    await heading('Northbridge Library')
    await user.click(link('East Side Branch'))
    await heading('East Side Branch')

    // Moving between an organization and its branches reuses the organization already loaded.
    expect(organizationRequests(fetchMock)).toEqual(['/api/organizations/northbridge'])
  })

  it('is reached by a slug-only address when a branch is chosen', async () => {
    serve()
    const user = userEvent.setup()

    renderApp('/organizations/northbridge')
    await heading('Northbridge Library')
    await user.click(link('East Side Branch'))

    await heading('East Side Branch')
    expect(address()).toBe('/organizations/northbridge/branches/east-side')
    expect(address()).not.toMatch(/\d/)
  })

  it.each(['west', 'Central', 'central%20', 'main'])(
    'shows the not-found page for the branch slug %j, which the organization did not return',
    async (branchSlug) => {
      const fetchMock = serve()

      renderApp(`/organizations/northbridge/branches/${branchSlug}`)

      await heading('Page not found')
      expect(main()).toHaveTextContent(NOT_FOUND_TEXT)
      expect(main()).not.toHaveTextContent(/Northbridge|Central Branch|west/)
      expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session', '/api/organizations/northbridge'])
    },
  )

  it('shows the not-found page when the organization itself is not available', async () => {
    serve({ [NORTHBRIDGE_URL]: () => jsonResponse(404, ORGANIZATION_NOT_FOUND) })

    renderApp('/organizations/northbridge/branches/central')

    await heading('Page not found')
    expect(main()).not.toHaveTextContent(/Central|northbridge/i)
  })
})

describe('what a missing page gives away', () => {
  async function notFoundMarkup(path: string, overrides: Routes = {}): Promise<string> {
    serve(overrides)
    const view = renderApp(path)
    await heading('Page not found')
    const markup = main().innerHTML
    view.unmount()
    return markup
  }

  it('is identical for an unknown address, a hidden organization, a missing one and an unknown branch', async () => {
    const unknownAddress = await notFoundMarkup('/nowhere')
    const hidden = await notFoundMarkup('/organizations/riverside', {
      [RIVERSIDE_URL]: () => jsonResponse(404, ORGANIZATION_NOT_FOUND),
    })
    const missing = await notFoundMarkup('/organizations/no-such-org', {
      'GET /api/organizations/no-such-org': () => jsonResponse(404, ORGANIZATION_NOT_FOUND),
    })
    const unknownBranch = await notFoundMarkup('/organizations/northbridge/branches/no-such-branch')

    expect(hidden).toBe(unknownAddress)
    expect(missing).toBe(unknownAddress)
    expect(unknownBranch).toBe(unknownAddress)
    expect(unknownAddress).not.toMatch(/riverside|no-such|northbridge|central/i)
  })
})

describe('slugs in addresses', () => {
  it('encodes a slug with unusual characters in the link and in the API request', async () => {
    const odd = { ...NORTHBRIDGE, slug: 'north bridge?x' }
    const fetchMock = serve({
      [LIST]: () => jsonResponse(200, [odd]),
      'GET /api/organizations/north%20bridge%3Fx': () =>
        jsonResponse(200, { ...NORTHBRIDGE_DETAIL, slug: odd.slug, branches: [{ slug: 'a b', name: 'Odd Branch', is_primary: false }] }),
    })
    const user = userEvent.setup()

    renderApp('/organizations')
    await waitFor(() => expect(linkNames()).toHaveLength(1))
    expect(link('Northbridge Library')).toHaveAttribute('href', '/organizations/north%20bridge%3Fx')

    await user.click(link('Northbridge Library'))
    await heading('Northbridge Library')
    expect(link('Odd Branch')).toHaveAttribute('href', '/organizations/north%20bridge%3Fx/branches/a%20b')

    await user.click(link('Odd Branch'))
    await heading('Odd Branch')
    expect(organizationRequests(fetchMock)).toEqual(['/api/organizations', '/api/organizations/north%20bridge%3Fx'])
  })
})
