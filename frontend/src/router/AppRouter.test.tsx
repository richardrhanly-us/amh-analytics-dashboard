import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import {
  ALICE,
  callOf,
  deferred,
  type FetchMock,
  jsonResponse,
  LIVE,
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

    renderApp('/organizations/northbridge/sorters/central')

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

    renderApp('/organizations/northbridge/sorters/central')

    await heading('Central Library AMH')
    const banner = screen.getByRole('banner')
    expect(within(banner).getByRole('heading', { level: 1, name: 'SortView' })).toBeInTheDocument()
    expect(within(banner).getByText('Alice Example')).toBeInTheDocument()
    expect(within(banner).getByText('alice@example.test')).toBeInTheDocument()
  })

  it.each(['/nowhere', '/organizations/northbridge/settings/nowhere', '/organizations/northbridge/branches', '/branches/central'])(
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

  it.each(['/organizations', '/organizations/northbridge', '/organizations/northbridge/sorters/central', '/nowhere'])(
    'leaves %s for / on a successful sign-out',
    async (path) => {
      serve({ 'POST /api/auth/logout': () => noContent() })
      const user = userEvent.setup()

      renderApp(path)
      await screen.findByRole('heading', { level: 2 })
      expect(address()).toBe(path)
      await user.click(screen.getByRole('button', { name: 'Sign out' }))

      await screen.findByRole('button', { name: 'Sign in' })
      await waitFor(() => expect(address()).toBe('/'))
    },
  )

  it('starts the next person to sign in at the organization list, not the previous page', async () => {
    const fetchMock = serve({
      'POST /api/auth/logout': () => noContent(),
      'POST /api/auth/login': () => jsonResponse(200, { id: 8, email: 'bob@example.test', full_name: 'Bob Example' }),
    })
    const user = userEvent.setup()

    renderApp('/organizations/northbridge/sorters/central')
    await heading('Central Library AMH')
    await user.click(screen.getByRole('button', { name: 'Sign out' }))
    await screen.findByRole('button', { name: 'Sign in' })
    await user.type(screen.getByLabelText('Email'), 'bob@example.test')
    await user.type(screen.getByLabelText('Password'), 'pw{Enter}')

    await heading('Organizations')
    expect(address()).toBe('/organizations')
    expect(screen.getByRole('banner')).toHaveTextContent('bob@example.test')
    // The organization was loaded once, for the first user, and not again for the second.
    await waitFor(() =>
      expect(organizationRequests(fetchMock)).toEqual(['/api/organizations/northbridge', '/api/organizations']),
    )
  })

  it.each([
    ['a refused origin', () => jsonResponse(403, { code: 'origin_not_allowed', message: 'Request origin is not allowed.' })],
    ['a server error', () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' })],
    ['a network failure', () => Promise.reject(networkFailure())],
  ])('stays signed in on the same page when sign-out fails with %s', async (_label, fail) => {
    const fetchMock = serve({ 'POST /api/auth/logout': fail })
    const user = userEvent.setup()

    renderApp('/organizations/northbridge/sorters/central')
    await heading('Central Library AMH')
    await user.click(screen.getByRole('button', { name: 'Sign out' }))

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(address()).toBe('/organizations/northbridge/sorters/central')
    expect(screen.getByRole('heading', { level: 2, name: 'Central Library AMH' })).toBeInTheDocument()
    expect(screen.getByRole('banner')).toHaveTextContent('alice@example.test')
    expect(screen.getByRole('button', { name: 'Sign out' })).not.toHaveAttribute('aria-disabled', 'true')
    expect(organizationRequests(fetchMock)).toEqual(['/api/organizations/northbridge'])
  })

  it.each([
    ['/organizations', 'GET /api/organizations'],
    ['/organizations/northbridge', 'GET /api/organizations/northbridge'],
    ['/organizations/northbridge/sorters/central', 'GET /api/organizations/northbridge'],
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
    await user.click(link('East Side AMH'))
    await heading('East Side AMH')
    expect(address()).toBe('/organizations/northbridge/sorters/east-side')

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

    renderApp('/organizations/northbridge/sorters/central')
    await screen.findByRole('button', { name: 'Sign in' })
    await user.type(screen.getByLabelText('Email'), 'alice@example.test')
    await user.type(screen.getByLabelText('Password'), 'pw{Enter}')

    await heading('Central Library AMH')
    expect(address()).toBe('/organizations/northbridge/sorters/central')
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

  it('shows the organization, a way back, and exactly its returned sorters as links', async () => {
    serve()

    renderApp('/organizations/northbridge')
    await heading('Northbridge Library')

    expect(within(main()).getByRole('heading', { level: 3, name: 'Sorting machines' })).toBeInTheDocument()
    const breadcrumb = within(main()).getByRole('navigation', { name: 'Breadcrumb' })
    expect(within(breadcrumb).getByRole('link', { name: 'Organizations' })).toHaveAttribute('href', '/organizations')

    const sorters = within(main()).getByRole('list', { name: 'Sorting machines' })
    expect(within(sorters).getAllByRole('link').map((element) => element.textContent)).toEqual([
      'Central Library AMH',
      'East Side AMH',
    ])
    expect(link('Central Library AMH')).toHaveAttribute('href', '/organizations/northbridge/sorters/central')
    expect(link('East Side AMH')).toHaveAttribute('href', '/organizations/northbridge/sorters/east-side')
  })

  it('says where each machine is, in words beside its name', async () => {
    serve()

    renderApp('/organizations/northbridge')
    await heading('Northbridge Library')

    const [central, eastSide] = within(within(main()).getByRole('list', { name: 'Sorting machines' })).getAllByRole('listitem')
    expect(central).toHaveTextContent(/^Central Library AMHCentral Branch$/)
    expect(eastSide).toHaveTextContent(/^East Side AMHEast Side Branch$/)
    expect(main()).not.toHaveTextContent(/Primary|Branches/)
  })

  it('says so when the organization has no sorting machines, however many branches it has', async () => {
    serve({ [NORTHBRIDGE_URL]: () => jsonResponse(200, { ...NORTHBRIDGE_DETAIL, sorters: [] }) })

    renderApp('/organizations/northbridge')
    await heading('Northbridge Library')

    expect(screen.getByText('No sorting machines are registered for this organization yet.')).toBeInTheDocument()
    expect(within(main()).getByRole('heading', { level: 3, name: 'Sorting machines' })).toBeInTheDocument()
    // Northbridge's fixture makes Alice an admin, so the organization's own configuration is offered beside the reports.
    expect(linkNames()).toEqual(['Organizations', 'Organization Reports', 'Settings'])
    expect(main()).not.toHaveTextContent(/Central Branch|East Side Branch|Westside/)
  })

  it('explains a suspended organization and keeps its branches reachable', async () => {
    serve()
    const user = userEvent.setup()

    renderApp('/organizations/riverside')
    await heading('Riverside Library')

    expect(screen.getByRole('note')).toHaveTextContent(
      'This organization’s account is currently suspended. Historical dashboard data remains available.',
    )
    await user.click(link('Riverside Main AMH'))
    await heading('Riverside Main AMH')
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
    expect(main()).not.toHaveTextContent(/Northbridge|Central Library AMH/)
    expect(linkNames()).toEqual(['Organizations', 'Organization Reports', 'Riverside Main AMH'])
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
    expect(main()).not.toHaveTextContent(/Northbridge|Central Library AMH/)

    slow.resolve(jsonResponse(200, RIVERSIDE_DETAIL))
    await heading('Riverside Library')
  })
})

describe('a branch page', () => {
  it('shows the organization and the branch from a fresh load of its address', async () => {
    const session = deferred<Response>()
    const fetchMock = serve({ [SESSION]: () => session.promise })

    renderApp('/organizations/northbridge/sorters/east-side')
    expect(screen.getByRole('status')).toHaveTextContent('Checking your session')
    session.resolve(jsonResponse(200, ALICE))

    await heading('East Side AMH')
    expect(link('Northbridge Library')).toBeInTheDocument()
    expect(await screen.findByRole('heading', { level: 3, name: 'Today' })).toBeInTheDocument()
    expect(address()).toBe('/organizations/northbridge/sorters/east-side')
    expect(requestedUrls(fetchMock).slice(0, 3)).toEqual([
      '/api/auth/session',
      '/api/organizations/northbridge',
      '/api/organizations/northbridge/branches/east-side/pipeline-status',
    ])
  })

  it('asks for live data only for the branch in the address', async () => {
    const fetchMock = serve()

    renderApp('/organizations/northbridge/sorters/central')
    await heading('Central Library AMH')
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

    renderApp('/organizations/northbridge/sorters/west')
    await heading('Page not found')

    expect(liveRequests(fetchMock)).toEqual([])
  })

  it('links back to the organization and to the organization list', async () => {
    const fetchMock = serve()
    const user = userEvent.setup()

    renderApp('/organizations/northbridge/sorters/central')
    await heading('Central Library AMH')

    const breadcrumb = within(main()).getByRole('navigation', { name: 'Breadcrumb' })
    expect(within(breadcrumb).getByRole('link', { name: 'Organizations' })).toHaveAttribute('href', '/organizations')
    expect(within(breadcrumb).getByRole('link', { name: 'Northbridge Library' })).toHaveAttribute(
      'href',
      '/organizations/northbridge',
    )

    await user.click(link('Northbridge Library'))
    await heading('Northbridge Library')
    await user.click(link('East Side AMH'))
    await heading('East Side AMH')

    // Moving between an organization and its branches reuses the organization already loaded.
    expect(organizationRequests(fetchMock)).toEqual(['/api/organizations/northbridge'])
  })

  it('is reached by a slug-only address when a branch is chosen', async () => {
    serve()
    const user = userEvent.setup()

    renderApp('/organizations/northbridge')
    await heading('Northbridge Library')
    await user.click(link('East Side AMH'))

    await heading('East Side AMH')
    expect(address()).toBe('/organizations/northbridge/sorters/east-side')
    expect(address()).not.toMatch(/\d/)
  })

  it.each(['west', 'Central', 'central%20', 'main'])(
    'shows the not-found page for the branch slug %j, which the organization did not return',
    async (branchSlug) => {
      const fetchMock = serve()

      renderApp(`/organizations/northbridge/sorters/${branchSlug}`)

      await heading('Page not found')
      expect(main()).toHaveTextContent(NOT_FOUND_TEXT)
      expect(main()).not.toHaveTextContent(/Northbridge|Central Library AMH|west/)
      expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session', '/api/organizations/northbridge'])
    },
  )

  it('shows the not-found page when the organization itself is not available', async () => {
    serve({ [NORTHBRIDGE_URL]: () => jsonResponse(404, ORGANIZATION_NOT_FOUND) })

    renderApp('/organizations/northbridge/sorters/central')

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
    const unknownBranch = await notFoundMarkup('/organizations/northbridge/sorters/no-such-branch')

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
        jsonResponse(200, {
          ...NORTHBRIDGE_DETAIL,
          slug: odd.slug,
          branches: [{ slug: 'a b', name: 'Odd Branch', is_primary: false }],
          sorters: [{ slug: 'a b', name: 'Odd Sorter', host_branch: { slug: 'a b', name: 'Odd Branch' }, status: 'active', collector_count: 1 }],
        }),
      ...liveRoutes('north bridge?x', 'a b'),
    })
    const user = userEvent.setup()

    renderApp('/organizations')
    await waitFor(() => expect(linkNames()).toHaveLength(1))
    expect(link('Northbridge Library')).toHaveAttribute('href', '/organizations/north%20bridge%3Fx')

    await user.click(link('Northbridge Library'))
    await heading('Northbridge Library')
    expect(link('Odd Sorter')).toHaveAttribute('href', '/organizations/north%20bridge%3Fx/sorters/a%20b')

    await user.click(link('Odd Sorter'))
    await heading('Odd Sorter')
    expect(organizationRequests(fetchMock)).toEqual(['/api/organizations', '/api/organizations/north%20bridge%3Fx'])
  })
})

// =====================================================================================================================
// F5.6: an organization's sorting machines, and the address each one has
// =====================================================================================================================

const sorter = (slug: string, name: string, hostName: string, changes: Record<string, unknown> = {}) => ({
  slug,
  name,
  host_branch: { slug, name: hostName },
  status: 'active',
  collector_count: 1,
  ...changes,
})

/** Metro: a sorter at Central and one at Westside. Each routes to the other's location. */
const METRO = { slug: 'metro', name: 'Metro Library System', role: 'viewer', access_mode: 'full' }
const METRO_DETAIL = {
  ...METRO,
  branches: [
    { slug: 'central', name: 'Central Library', is_primary: true },
    { slug: 'westside', name: 'Westside', is_primary: false },
    { slug: 'north', name: 'North', is_primary: false },
  ],
  sorters: [sorter('central', 'Central Library AMH', 'Central Library'), sorter('westside', 'Westside AMH', 'Westside')],
  subscription: null,
  entitlements: {},
}
const hours = (counts: Record<number, number>) => Array.from({ length: 24 }, (_, hour) => counts[hour] ?? 0)
const METRO_CENTRAL = { ...LIVE, hours: hours({ 9: 100 }), routing: { home: 'Central', transit: [['westside', 'Westside', 30], ['north', 'North', 5]] as [string, string, number][] } }
const METRO_WESTSIDE = { ...LIVE, hours: hours({ 10: 40 }), routing: { home: 'Westside', transit: [['central', 'Central', 12]] as [string, string, number][] } }

function serveMetro(overrides: Routes = {}): FetchMock {
  return serve({
    [LIST]: () => jsonResponse(200, [METRO, NORTHBRIDGE]),
    'GET /api/organizations/metro': () => jsonResponse(200, METRO_DETAIL),
    ...liveRoutes('metro', 'central', METRO_CENTRAL),
    ...liveRoutes('metro', 'westside', METRO_WESTSIDE),
    ...overrides,
  })
}

const figure = (label: string) => screen.getByText(label, { selector: 'dt' }).nextElementSibling as HTMLElement
const sorterList = () => within(main()).getByRole('list', { name: 'Sorting machines' })

describe('the sorting machines of an organization', () => {
  it('is headed Sorting machines, and lists only the sorters the API returned', async () => {
    serve()

    renderApp('/organizations/northbridge')
    await heading('Northbridge Library')

    expect(within(main()).getByRole('heading', { level: 3, name: 'Sorting machines' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Branches' })).not.toBeInTheDocument()
    expect(within(sorterList()).getAllByRole('listitem')).toHaveLength(2)
    // Westside is one of the organization's branches, and somewhere items are routed to. It has no machine.
    expect(NORTHBRIDGE_DETAIL.branches.map((branch) => branch.name)).toContain('Westside')
    expect(main()).not.toHaveTextContent('Westside')
    expect(linkNames()).toEqual(['Organizations', 'Organization Reports', 'Settings', 'Central Library AMH', 'East Side AMH'])
  })

  it('shows an organization with one sorter as a list of one, not straight to its dashboard', async () => {
    const fetchMock = serve()

    renderApp('/organizations/riverside')
    await heading('Riverside Library')

    expect(address()).toBe('/organizations/riverside')
    expect(within(sorterList()).getAllByRole('link').map((element) => element.textContent)).toEqual(['Riverside Main AMH'])
    expect(link('Riverside Main AMH')).toHaveAttribute('href', '/organizations/riverside/sorters/main')
    expect(liveRequests(fetchMock)).toEqual([])
  })

  it('names the machine and says where it is, and links by the sorter, not the name', async () => {
    serveMetro()

    renderApp('/organizations/metro')
    await heading('Metro Library System')

    const [central, westside] = within(sorterList()).getAllByRole('listitem')
    expect(central).toHaveTextContent(/^Central Library AMHCentral Library$/)
    expect(westside).toHaveTextContent(/^Westside AMHWestside$/)
    expect(link('Central Library AMH')).toHaveAttribute('href', '/organizations/metro/sorters/central')
    expect(link('Westside AMH')).toHaveAttribute('href', '/organizations/metro/sorters/westside')
    // North is a branch and a routing destination only.
    expect(main()).not.toHaveTextContent('North')
  })

  it('says so, in words, when a machine is being set up or is inactive, and still lists it', async () => {
    serveMetro({
      'GET /api/organizations/metro': () =>
        jsonResponse(200, {
          ...METRO_DETAIL,
          sorters: [
            sorter('central', 'Central Library AMH', 'Central Library'),
            sorter('westside', 'Westside AMH', 'Westside', { status: 'provisioning' }),
            sorter('north', 'North Sorter', 'North', { status: 'inactive', collector_count: 0 }),
          ],
        }),
    })

    renderApp('/organizations/metro')
    await heading('Metro Library System')

    const [central, westside, north] = within(sorterList()).getAllByRole('listitem')
    expect(central).toHaveTextContent(/^Central Library AMHCentral Library$/)
    expect(westside).toHaveTextContent(/^Westside AMHWestside · Being set up$/)
    expect(north).toHaveTextContent(/^North SorterNorth · Inactive$/)
    expect(linkNames()).toEqual(['Organizations', 'Organization Reports', 'Central Library AMH', 'Westside AMH', 'North Sorter'])
    expect(main()).not.toHaveTextContent(/provisioning|collector_count/)
  })

  it('shows a long machine name whole', async () => {
    const long = 'Bartholomew-Featherstonehaugh Memorial Library — Tech Logic UltraSort, Returns Room B (replacement unit)'
    serveMetro({
      'GET /api/organizations/metro': () => jsonResponse(200, { ...METRO_DETAIL, sorters: [sorter('central', long, 'Central Library')] }),
    })
    const user = userEvent.setup()

    renderApp('/organizations/metro')
    await heading('Metro Library System')
    expect(link(long)).toBeVisible()

    await user.click(link(long))
    expect(await heading(long)).toBeVisible()
    expect(within(main()).getByRole('navigation', { name: 'Breadcrumb' })).toHaveTextContent(long)
  })

  it.each([
    ['two sorters with one slug', [sorter('central', 'A', 'Central'), { ...sorter('central', 'B', 'East'), host_branch: { slug: 'east', name: 'East' } }]],
    ['two sorters at one host branch', [sorter('central', 'A', 'Central'), { ...sorter('other', 'B', 'Central'), host_branch: { slug: 'central', name: 'Central' } }]],
    ['a sorter with no slug', [sorter('', 'A', 'Central')]],
    ['a sorter with no name', [sorter('central', '   ', 'Central')]],
    ['a sorter with no host branch', [{ slug: 'central', name: 'A', status: 'active', collector_count: 1 }]],
    ['a status this app does not know', [sorter('central', 'A', 'Central', { status: 'retired' })]],
    ['a collector count that is not a count', [sorter('central', 'A', 'Central', { collector_count: -1 })]],
    ['sorters that are not a list', { central: sorter('central', 'A', 'Central') }],
    ['no sorters field at all', undefined],
  ])('refuses an organization answered with %s, rather than guessing', async (_label, sorters) => {
    serveMetro({ 'GET /api/organizations/metro': () => jsonResponse(200, { ...METRO_DETAIL, sorters }) })

    renderApp('/organizations/metro')

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Sorting machines' })).not.toBeInTheDocument()
    expect(linkNames()).toEqual([])
  })
})

describe('a sorter page', () => {
  it('shows the machine, where it is, and its dashboard, read by its host branch', async () => {
    const fetchMock = serveMetro()

    renderApp('/organizations/metro/sorters/central')
    await heading('Central Library AMH')
    await waitFor(() => expect(figure('Check-ins today')).toHaveTextContent(/^100$/))

    expect(main()).toHaveTextContent('Live activity for this sorter, at Central Library')
    expect(main()).not.toHaveTextContent(/branch/i)
    expect(liveRequests(fetchMock).every((url) => url.startsWith('/api/organizations/metro/branches/central/'))).toBe(true)
    expect(liveRequests(fetchMock)).toHaveLength(6)
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
  })

  it('names the sorter, not its host branch, in the breadcrumb and the browser tab', async () => {
    serveMetro()

    renderApp('/organizations/metro/sorters/westside')
    await heading('Westside AMH')

    const steps = within(within(main()).getByRole('navigation', { name: 'Breadcrumb' })).getAllByRole('listitem')
    expect(steps.map((step) => step.textContent)).toEqual(['Organizations', 'Metro Library System', 'Westside AMH'])
    expect(within(steps[2]).getByText('Westside AMH')).toHaveAttribute('aria-current', 'page')
    await waitFor(() => expect(document.title).toBe('Westside AMH – SortView'))
  })

  it('keeps two sorters of one organization apart: each has its own figures and its own routing', async () => {
    const fetchMock = serveMetro()
    const user = userEvent.setup()

    renderApp('/organizations/metro')
    await heading('Metro Library System')
    await user.click(link('Central Library AMH'))
    await heading('Central Library AMH')
    await waitFor(() => expect(figure('Westside')).toHaveTextContent(/^30$/))
    expect(figure('Check-ins today')).toHaveTextContent(/^100$/)
    expect(figure('North')).toHaveTextContent(/^5$/)
    expect(main()).toHaveTextContent('Kept at Central: 65.')

    await user.click(link('Metro Library System'))
    await user.click(link('Westside AMH'))
    await heading('Westside AMH')
    await waitFor(() => expect(figure('Central')).toHaveTextContent(/^12$/))
    expect(figure('Check-ins today')).toHaveTextContent(/^40$/)
    expect(main()).toHaveTextContent('Kept at Westside: 28.')
    expect(screen.queryByText('North', { selector: 'dt' })).not.toBeInTheDocument()

    expect(liveRequests(fetchMock).filter((url) => url.includes('/branches/central/'))).toHaveLength(6)
    expect(liveRequests(fetchMock).filter((url) => url.includes('/branches/westside/'))).toHaveLength(6)
    // The organization was read once: moving between its sorters asks nothing new about it.
    expect(organizationRequests(fetchMock)).toEqual(['/api/organizations/metro'])
  })

  it('lets a place be a destination of one sorter and the host of another, without confusing the two', async () => {
    serveMetro()

    renderApp('/organizations/metro/sorters/central')
    await heading('Central Library AMH')
    await waitFor(() => expect(figure('Westside')).toHaveTextContent(/^30$/))

    // Westside hosts a sorter of its own. On Central's dashboard it is still just where 30 items went:
    // a figure, not a link, and not left out.
    const routing = screen.getByRole('group', { name: 'Routing' })
    expect(within(routing).getByText('Westside', { selector: 'dt' })).toBeInTheDocument()
    expect(within(routing).queryByRole('link')).not.toBeInTheDocument()
    expect(linkNames()).toEqual(['Organizations', 'Metro Library System', 'Live Today', 'Reports'])
  })

  it('never shows the first sorter under the second while the second loads', async () => {
    const westsidePipeline = deferred<Response>()
    serveMetro({ 'GET /api/organizations/metro/branches/westside/pipeline-status': () => westsidePipeline.promise })
    const user = userEvent.setup()

    renderApp('/organizations/metro/sorters/central')
    await heading('Central Library AMH')
    await waitFor(() => expect(figure('Check-ins today')).toHaveTextContent(/^100$/))
    await user.click(link('Metro Library System'))
    await user.click(link('Westside AMH'))

    await heading('Westside AMH')
    expect(await screen.findByText('Loading live data…')).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/100|Central Library AMH|Kept at/)

    westsidePipeline.resolve(jsonResponse(200, { timezone: 'America/Chicago', state: 'ok', last_reported_at: null }))
    await waitFor(() => expect(figure('Check-ins today')).toHaveTextContent(/^40$/))
  })

  it('says when several collectors report for the site, and that their figures are combined', async () => {
    serveMetro({
      'GET /api/organizations/metro': () =>
        jsonResponse(200, { ...METRO_DETAIL, sorters: [sorter('central', 'Central Library AMH', 'Central Library', { collector_count: 2 })] }),
    })

    renderApp('/organizations/metro/sorters/central')
    await heading('Central Library AMH')

    expect(screen.getByRole('note')).toHaveTextContent(
      '2 collectors report for this site. Their figures are combined here and cannot be shown separately.',
    )
    // One sorter, one dashboard: the second collector is not a second place to go.
    expect(linkNames()).toEqual(['Organizations', 'Metro Library System', 'Live Today', 'Reports'])
  })

  it('says a sorter is being set up, and that its data is not available yet, without calling it missing', async () => {
    serveMetro({
      'GET /api/organizations/metro': () =>
        jsonResponse(200, { ...METRO_DETAIL, sorters: [sorter('north', 'North Sorter', 'North', { status: 'provisioning' })] }),
      'GET /api/organizations/metro/branches/north/pipeline-status': () =>
        jsonResponse(404, { code: 'tenant_not_found', message: 'Organization or branch not found.' }),
    })

    renderApp('/organizations/metro/sorters/north')
    await heading('North Sorter')

    expect(main()).toHaveTextContent('Live activity for this sorter, at North · Being set up')
    expect(await screen.findByText('Live dashboard data is not available for this sorter yet.')).toHaveRole('note')
    expect(screen.queryByRole('heading', { name: 'Page not found' })).not.toBeInTheDocument()
  })

  it.each(['north', 'Central', 'central%20', 'library-express', 'no-such-sorter'])(
    'shows the not-found page for %j, which is not one of the organization’s sorters, and asks for no live data',
    async (sorterSlug) => {
      const fetchMock = serveMetro()

      renderApp(`/organizations/metro/sorters/${sorterSlug}`)

      await heading('Page not found')
      expect(main()).toHaveTextContent(NOT_FOUND_TEXT)
      expect(main()).not.toHaveTextContent(/Metro|Central|North/)
      expect(liveRequests(fetchMock)).toEqual([])
    },
  )

  it('does not find one organization’s sorter under another organization', async () => {
    const fetchMock = serveMetro()

    // Northbridge has a sorter called "central" too. Metro's "westside" is not Northbridge's.
    renderApp('/organizations/northbridge/sorters/westside')

    await heading('Page not found')
    expect(liveRequests(fetchMock)).toEqual([])
    expect(requestedUrls(fetchMock)).toEqual(['/api/auth/session', '/api/organizations/northbridge'])
  })

  it('reads a suspended organization’s sorter as usual, with the notice', async () => {
    serve()

    renderApp('/organizations/riverside/sorters/main')
    await heading('Riverside Main AMH')

    expect(screen.getByRole('note')).toHaveTextContent('currently suspended')
    await waitFor(() => expect(figure('Check-ins today')).toHaveTextContent(/^120$/))
  })

  it('shows the not-found page for a sorter of an organization the user cannot see', async () => {
    const fetchMock = serveMetro({ 'GET /api/organizations/metro': () => jsonResponse(404, ORGANIZATION_NOT_FOUND) })

    renderApp('/organizations/metro/sorters/central')

    await heading('Page not found')
    expect(main()).not.toHaveTextContent(/Metro|Central/)
    expect(liveRequests(fetchMock)).toEqual([])
  })
})

describe('the address a sorter used to have', () => {
  it('redirects a branch address to the sorter hosted at that branch, replacing it in history', async () => {
    const fetchMock = serveMetro()
    const user = userEvent.setup()

    renderApp('/organizations')
    await user.click(await within(main()).findByRole('link', { name: 'Metro Library System' }))
    await heading('Metro Library System')
    // An old bookmark, followed from the organization page.
    const view = renderApp('/organizations/metro/branches/westside')
    await within(view.container).findByRole('heading', { level: 2, name: 'Westside AMH' })

    expect(within(view.container).getByTestId('address')).toHaveTextContent('/organizations/metro/sorters/westside')
    expect(liveRequests(fetchMock).every((url) => url.includes('/branches/westside/'))).toBe(true)
  })

  it('lands on the sorter page itself: one dashboard, at the sorter address', async () => {
    const fetchMock = serveMetro()

    renderApp('/organizations/metro/branches/central')

    await heading('Central Library AMH')
    expect(address()).toBe('/organizations/metro/sorters/central')
    await waitFor(() => expect(figure('Check-ins today')).toHaveTextContent(/^100$/))
    expect(liveRequests(fetchMock)).toHaveLength(6)
    // The redirect took no focus: nobody asked to be moved.
    expect(document.body).toHaveFocus()
  })

  it('goes back past the redirect, not into it', async () => {
    serveMetro()
    const user = userEvent.setup()

    renderApp('/organizations/metro')
    await heading('Metro Library System')
    await user.click(link('Central Library AMH'))
    await heading('Central Library AMH')
    await user.click(screen.getByRole('button', { name: 'browser-back' }))
    await heading('Metro Library System')
    expect(address()).toBe('/organizations/metro')

    await user.click(screen.getByRole('button', { name: 'browser-forward' }))
    await heading('Central Library AMH')
    expect(address()).toBe('/organizations/metro/sorters/central')
  })

  it.each([
    ['a branch with no sorter', 'north'],
    ['a branch that does not exist', 'no-such-branch'],
    ['a routing destination that is not a branch', 'library-express'],
  ])('shows the not-found page for %s, and asks for no live data', async (_label, branchSlug) => {
    const fetchMock = serveMetro()

    renderApp(`/organizations/metro/branches/${branchSlug}`)

    await heading('Page not found')
    expect(address()).toBe(`/organizations/metro/branches/${branchSlug}`)
    expect(main()).toHaveTextContent(NOT_FOUND_TEXT)
    expect(main()).not.toHaveTextContent(/Metro|North/)
    expect(liveRequests(fetchMock)).toEqual([])
  })

  it('redirects by host branch even when the sorter’s slug is something else', async () => {
    serveMetro({
      'GET /api/organizations/metro': () =>
        jsonResponse(200, {
          ...METRO_DETAIL,
          sorters: [{ slug: 'amh-1', name: 'Central Library AMH', host_branch: { slug: 'central', name: 'Central Library' }, status: 'active', collector_count: 1 }],
        }),
    })

    renderApp('/organizations/metro/branches/central')

    await heading('Central Library AMH')
    expect(address()).toBe('/organizations/metro/sorters/amh-1')
    await waitFor(() => expect(figure('Check-ins today')).toHaveTextContent(/^100$/))
  })
})

describe('signing in and out around a sorter address', () => {
  it('restores a sorter address opened directly, once the session is restored', async () => {
    const session = deferred<Response>()
    serveMetro({ [SESSION]: () => session.promise })

    renderApp('/organizations/metro/sorters/westside')
    expect(screen.getByRole('status')).toHaveTextContent('Checking your session')
    session.resolve(jsonResponse(200, ALICE))

    await heading('Westside AMH')
    expect(address()).toBe('/organizations/metro/sorters/westside')
  })

  it('returns to the sorter address after signing in', async () => {
    serveMetro({
      [SESSION]: () => jsonResponse(401, NOT_AUTHENTICATED),
      'POST /api/auth/login': () => jsonResponse(200, ALICE),
    })
    const user = userEvent.setup()

    renderApp('/organizations/metro/sorters/central')
    await screen.findByRole('button', { name: 'Sign in' })
    await user.type(screen.getByLabelText('Email'), 'alice@example.test')
    await user.type(screen.getByLabelText('Password'), 'pw{Enter}')

    await heading('Central Library AMH')
    expect(address()).toBe('/organizations/metro/sorters/central')
  })

  it('keeps the sorter address when the session expires', async () => {
    serveMetro({ 'GET /api/organizations/metro/branches/central/pipeline-status': () => jsonResponse(401, NOT_AUTHENTICATED) })

    renderApp('/organizations/metro/sorters/central')

    await screen.findByRole('button', { name: 'Sign in' })
    expect(address()).toBe('/organizations/metro/sorters/central')
    expect(screen.queryByText('Central Library AMH')).not.toBeInTheDocument()
  })

  it('leaves a sorter address for / on a successful sign-out', async () => {
    serveMetro({ 'POST /api/auth/logout': () => noContent() })
    const user = userEvent.setup()

    renderApp('/organizations/metro/sorters/central')
    await heading('Central Library AMH')
    await user.click(screen.getByRole('button', { name: 'Sign out' }))

    await screen.findByRole('button', { name: 'Sign in' })
    await waitFor(() => expect(address()).toBe('/'))
  })
})

describe('how an organization is loaded', () => {
  // What a browser tells a page when its connection goes and comes back.
  const connection = (state: 'offline' | 'online') => window.dispatchEvent(new Event(state))

  it('is not asked for again merely because the connection came back', async () => {
    const fetchMock = serve()
    renderApp('/organizations/northbridge')
    await heading('Northbridge Library')
    expect(organizationRequests(fetchMock)).toEqual(['/api/organizations/northbridge'])

    try {
      connection('offline')
      connection('online')
      // Long enough for a request to have been made, had one been asked for.
      await new Promise((resolve) => setTimeout(resolve, 50))
    } finally {
      connection('online')
    }

    expect(organizationRequests(fetchMock)).toEqual(['/api/organizations/northbridge'])
    expect(screen.getByRole('heading', { level: 2, name: 'Northbridge Library' })).toBeInTheDocument()
  })

  it('is asked for, and says it failed, when the browser thinks it is offline -- it does not wait', async () => {
    const fetchMock = serve({ [NORTHBRIDGE_URL]: () => Promise.reject(networkFailure()) })
    const user = userEvent.setup()
    renderApp('/organizations')
    await heading('Organizations')

    try {
      connection('offline')
      await user.click(await screen.findByRole('link', { name: 'Northbridge Library' }))

      expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach the server. Check your connection and try again.')
      expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
      expect(screen.queryByText('Loading organization…')).not.toBeInTheDocument()
      expect(organizationRequests(fetchMock)).toContain('/api/organizations/northbridge')
    } finally {
      connection('online')
    }
  })
})
