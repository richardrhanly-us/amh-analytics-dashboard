import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import {
  ALICE,
  deferred,
  type FetchMock,
  jsonResponse,
  liveRoutes,
  networkFailure,
  noContent,
  NORTHBRIDGE,
  NORTHBRIDGE_DETAIL,
  NOT_AUTHENTICATED,
  RIVERSIDE,
  RIVERSIDE_DETAIL,
  serveApi,
} from './test/http.ts'
import { renderApp } from './test/render.tsx'

/**
 * How the app behaves for someone using a keyboard or a screen reader: what
 * the page is made of, where focus is, and what can be reached.
 */

type Routes = Parameters<typeof serveApi>[0]

const CENTRAL = '/organizations/northbridge/sorters/central'
const PIPELINE = 'GET /api/organizations/northbridge/branches/central/pipeline-status'

function serve(overrides: Routes = {}): FetchMock {
  return serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    'GET /api/organizations': () => jsonResponse(200, [NORTHBRIDGE, RIVERSIDE]),
    'GET /api/organizations/northbridge': () => jsonResponse(200, NORTHBRIDGE_DETAIL),
    'GET /api/organizations/riverside': () => jsonResponse(200, RIVERSIDE_DETAIL),
    ...liveRoutes('northbridge', 'central'),
    ...liveRoutes('northbridge', 'east-side'),
    ...liveRoutes('riverside', 'main'),
    ...overrides,
  })
}

const main = () => screen.getByRole('main')
const address = () => screen.getByTestId('address').textContent
const pageHeading = (name: string) => screen.findByRole('heading', { level: 2, name })
const link = (name: string) => within(main()).getByRole('link', { name })
/** Waits until the branch dashboard has loaded every section. */
async function dashboard() {
  await screen.findByRole('img', { name: /^Bar chart of check-ins/ })
  await screen.findByRole('table', { name: 'Top reject reasons' })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).not.toHaveAttribute('aria-disabled', 'true'))
}
/** Everything in the app that Tab stops at, in order, by accessible name. The test's own browser buttons are left out. */
async function tabStops(user: ReturnType<typeof userEvent.setup>): Promise<string[]> {
  const app = main().parentElement as HTMLElement
  const stops: string[] = []
  for (let presses = 0; presses < 40; presses++) {
    await user.tab()
    const focused = document.activeElement as HTMLElement
    if (!app.contains(focused)) {
      if (stops.length > 0) {
        break
      }
      continue
    }
    stops.push(`${focused.tagName.toLowerCase()}: ${focused.textContent}`)
  }
  return stops
}

describe('the structure of a page', () => {
  it('has one h1, the page as h2 and its sections as h3, in that order', async () => {
    serve()

    renderApp(CENTRAL)
    await dashboard()

    expect(screen.getAllByRole('heading').map((heading) => `${heading.tagName} ${heading.textContent}`)).toEqual([
      'H1 SortView',
      'H2 Central Library AMH',
      'H3 Pipeline',
      'H3 Today',
      'H4 Operations',
      'H4 Routing',
      'H4 Rejects',
      'H3 Hourly check-ins',
      'H3 Top reject reasons',
    ])
  })

  it.each([
    ['/organizations', ['H1 SortView', 'H2 Organizations']],
    ['/organizations/northbridge', ['H1 SortView', 'H2 Northbridge Library', 'H3 Sorting machines']],
    ['/nowhere', ['H1 SortView', 'H2 Page not found']],
  ])('keeps the same heading order at %s', async (path, headings) => {
    serve()

    renderApp(path)
    await screen.findByRole('heading', { level: 2 })

    expect(screen.getAllByRole('heading').map((heading) => `${heading.tagName} ${heading.textContent}`)).toEqual(headings)
  })

  it('has one banner and one main, and no region that only repeats the page', async () => {
    serve()

    renderApp(CENTRAL)
    await dashboard()

    expect(screen.getAllByRole('banner')).toHaveLength(1)
    expect(screen.getAllByRole('main')).toHaveLength(1)
    expect(screen.getAllByRole('navigation')).toHaveLength(1)
    // The dashboard's four parts are the only named regions; the page itself is <main>.
    expect(screen.getAllByRole('region').map((region) => region.getAttribute('aria-labelledby'))).toEqual([
      'pipeline-heading',
      'today-heading',
      'hourly-heading',
      'reasons-heading',
    ])
    expect(within(main()).getAllByRole('region')).toHaveLength(4)
  })

  it('gives the breadcrumb a name, links for the pages above and the current page marked as such', async () => {
    serve()

    renderApp(CENTRAL)
    await dashboard()

    const breadcrumb = screen.getByRole('navigation', { name: 'Breadcrumb' })
    expect(main()).toContainElement(breadcrumb)
    const steps = within(breadcrumb).getAllByRole('listitem')
    expect(steps.map((step) => step.textContent)).toEqual(['Organizations', 'Northbridge Library', 'Central Library AMH'])
    expect(within(breadcrumb).getAllByRole('link').map((step) => step.getAttribute('href'))).toEqual([
      '/organizations',
      '/organizations/northbridge',
    ])
    const current = within(steps[2]).getByText('Central Library AMH')
    expect(current).toHaveAttribute('aria-current', 'page')
    expect(current.tagName).toBe('SPAN')
  })

  it('names the page in the browser tab, and gives a missing page nothing from the address', async () => {
    serve()
    const user = userEvent.setup()

    renderApp(CENTRAL)
    await pageHeading('Central Library AMH')
    expect(document.title).toBe('Central Library AMH – SortView')

    await user.click(link('Northbridge Library'))
    await pageHeading('Northbridge Library')
    expect(document.title).toBe('Northbridge Library – SortView')
  })

  it('titles a missing page without naming what was asked for', async () => {
    serve()

    renderApp('/organizations/northbridge/sorters/secret-annex')
    await pageHeading('Page not found')

    expect(document.title).toBe('Page not found – SortView')
  })
})

describe('controls', () => {
  it('makes every action a button and every destination a link, each with a name', async () => {
    serve()

    renderApp(CENTRAL)
    await dashboard()

    const app = main().parentElement as HTMLElement
    expect(within(app).getAllByRole('button').map((button) => button.textContent)).toEqual([
      'Sign out',
      'Refresh',
      'Pause automatic refresh',
      'Show hourly table',
    ])
    for (const button of within(app).getAllByRole('button')) {
      expect(button.tagName).toBe('BUTTON')
      expect(button).toHaveAttribute('type', 'button')
      expect(button).toHaveAccessibleName()
    }
    for (const anchor of within(app).getAllByRole('link')) {
      expect(anchor.tagName).toBe('A')
      expect(anchor).toHaveAttribute('href')
      expect(anchor).toHaveAccessibleName()
    }
    // Nothing else pretends to be a control.
    expect(app.querySelectorAll('[role="button"], [role="link"], [onclick], div[tabindex], span[tabindex]')).toHaveLength(0)
  })

  it('is reached by Tab in reading order, with the chart and the headings not among the stops', async () => {
    serve()
    const user = userEvent.setup()

    renderApp(CENTRAL)
    await dashboard()

    expect(await tabStops(user)).toEqual([
      'button: Sign out',
      'a: Organizations',
      'a: Northbridge Library',
      'button: Refresh',
      'button: Pause automatic refresh',
      'button: Show hourly table',
    ])
  })

  it('uses no tab order of its own: the only tabindex is -1, on what code may focus', async () => {
    serve()

    renderApp(CENTRAL)
    await dashboard()

    const indexed = Array.from(document.querySelectorAll('[tabindex]'))
    expect(indexed.map((element) => `${element.tagName} ${element.getAttribute('tabindex')}`)).toEqual(['MAIN -1', 'H2 -1'])
  })

  it('can be worked from the keyboard alone', async () => {
    const fetchMock = serve()
    const user = userEvent.setup()

    renderApp(CENTRAL)
    await dashboard()
    screen.getByRole('button', { name: 'Pause automatic refresh' }).focus()
    await user.keyboard('{Enter}')
    expect(screen.getByRole('button', { name: 'Resume automatic refresh' })).toHaveFocus()

    screen.getByRole('button', { name: 'Show hourly table' }).focus()
    await user.keyboard(' ')
    expect(screen.getByRole('table', { name: 'Hourly check-ins' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Hide hourly table' })).toHaveFocus()

    screen.getByRole('button', { name: 'Refresh' }).focus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/pipeline-status'))).toHaveLength(2))
  })
})

describe('focus when the page changes', () => {
  it('leaves focus alone on the page the app opens on', async () => {
    serve()

    renderApp(CENTRAL)
    await dashboard()

    expect(document.body).toHaveFocus()
  })

  it('leaves focus alone when / redirects to the organization list', async () => {
    serve()

    renderApp('/')
    await pageHeading('Organizations')
    await within(main()).findByRole('link', { name: 'Northbridge Library' })

    expect(address()).toBe('/organizations')
    expect(document.body).toHaveFocus()
  })

  it('moves focus to the new page heading when a link is followed', async () => {
    serve()
    const user = userEvent.setup()

    renderApp('/organizations')
    await user.click(await within(main()).findByRole('link', { name: 'Northbridge Library' }))

    expect(await pageHeading('Northbridge Library')).toHaveFocus()

    await user.click(link('Central Library AMH'))
    expect(await pageHeading('Central Library AMH')).toHaveFocus()

    await user.click(link('Organizations'))
    expect(await pageHeading('Organizations')).toHaveFocus()
  })

  it('moves focus to the heading once it exists, when the page has to load first', async () => {
    const slow = deferred<Response>()
    serve({ 'GET /api/organizations/northbridge': () => slow.promise })
    const user = userEvent.setup()

    renderApp('/organizations')
    await user.click(await within(main()).findByRole('link', { name: 'Northbridge Library' }))
    await screen.findByText('Loading organization…')
    // The link that was followed is gone; nothing has focus until there is a page to give it to.
    expect(document.body).toHaveFocus()

    slow.resolve(jsonResponse(200, NORTHBRIDGE_DETAIL))

    expect(await pageHeading('Northbridge Library')).toHaveFocus()
  })

  it('moves focus to the not-found heading when a link leads nowhere the user can see', async () => {
    serve({ 'GET /api/organizations/riverside': () => jsonResponse(404, { code: 'organization_not_found', message: 'x' }) })
    const user = userEvent.setup()

    renderApp('/organizations')
    await user.click(await within(main()).findByRole('link', { name: 'Riverside Library' }))

    expect(await pageHeading('Page not found')).toHaveFocus()
  })

  it('does the same for Back and Forward, which still go where they should', async () => {
    serve()
    const user = userEvent.setup()

    renderApp('/organizations')
    await user.click(await within(main()).findByRole('link', { name: 'Northbridge Library' }))
    await pageHeading('Northbridge Library')
    await user.click(link('East Side AMH'))
    await pageHeading('East Side AMH')

    await user.click(screen.getByRole('button', { name: 'browser-back' }))
    expect(await pageHeading('Northbridge Library')).toHaveFocus()
    expect(address()).toBe('/organizations/northbridge')

    await user.click(screen.getByRole('button', { name: 'browser-back' }))
    expect(await pageHeading('Organizations')).toHaveFocus()
    expect(address()).toBe('/organizations')

    await user.click(screen.getByRole('button', { name: 'browser-forward' }))
    expect(await pageHeading('Northbridge Library')).toHaveFocus()
    expect(address()).toBe('/organizations/northbridge')
  })

  it('keeps the heading out of the tab order: the next Tab goes on into the page', async () => {
    serve()
    const user = userEvent.setup()

    renderApp('/organizations/northbridge')
    await pageHeading('Northbridge Library')
    await user.click(link('Central Library AMH'))
    expect(await pageHeading('Central Library AMH')).toHaveFocus()
    await dashboard()

    await user.tab()
    expect(screen.getByRole('button', { name: 'Refresh' })).toHaveFocus()
    await user.tab({ shift: true })
    expect(link('Northbridge Library')).toHaveFocus()
  })

  it('does not move focus when the dashboard refreshes or a section fails', async () => {
    serve({ 'GET /api/organizations/northbridge/branches/central/rejects/by-reason?date=*': () => jsonResponse(500, {}) })
    const user = userEvent.setup()

    renderApp(CENTRAL)
    await screen.findByRole('alert')
    const refresh = screen.getByRole('button', { name: 'Refresh' })
    await waitFor(() => expect(refresh).not.toHaveAttribute('aria-disabled', 'true'))
    await user.click(refresh)

    await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh' })).not.toHaveAttribute('aria-disabled', 'true'))
    expect(screen.getByRole('button', { name: 'Refresh' })).toHaveFocus()
  })
})

describe('focus when signing in and out', () => {
  it('takes no focus while the session is restored, signed in or not', async () => {
    const session = deferred<Response>()
    serve({ 'GET /api/auth/session': () => session.promise })

    const view = renderApp('/organizations')
    expect(document.body).toHaveFocus()
    session.resolve(jsonResponse(200, ALICE))
    await pageHeading('Organizations')
    expect(document.body).toHaveFocus()
    view.unmount()

    serve({ 'GET /api/auth/session': () => jsonResponse(401, NOT_AUTHENTICATED) })
    renderApp('/organizations')
    await screen.findByRole('button', { name: 'Sign in' })
    expect(document.body).toHaveFocus()
  })

  it('moves focus to the page after signing in, so the form that is gone does not keep it', async () => {
    serve({
      'GET /api/auth/session': () => jsonResponse(401, NOT_AUTHENTICATED),
      'POST /api/auth/login': () => jsonResponse(200, ALICE),
    })
    const user = userEvent.setup()

    renderApp('/organizations')
    await screen.findByRole('button', { name: 'Sign in' })
    await user.type(screen.getByLabelText('Email'), 'alice@example.test')
    await user.type(screen.getByLabelText('Password'), 'pw{Enter}')

    await pageHeading('Organizations')
    expect(main()).toHaveFocus()
    await user.tab()
    expect(await within(main()).findByRole('link', { name: 'Northbridge Library' })).toHaveFocus()
  })

  it('moves focus to the sign-in form after signing out', async () => {
    serve({ 'POST /api/auth/logout': () => noContent() })
    const user = userEvent.setup()

    renderApp(CENTRAL)
    await dashboard()
    await user.click(screen.getByRole('button', { name: 'Sign out' }))

    await screen.findByRole('button', { name: 'Sign in' })
    expect(main()).toHaveFocus()
    await user.tab()
    expect(screen.getByLabelText('Email')).toHaveFocus()
  })

  it('keeps focus on Sign out, and the page as it was, when signing out fails', async () => {
    serve({ 'POST /api/auth/logout': () => Promise.reject(networkFailure()) })
    const user = userEvent.setup()

    renderApp(CENTRAL)
    await dashboard()
    await user.click(screen.getByRole('button', { name: 'Sign out' }))

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign out' })).toHaveFocus()
    expect(screen.getByRole('heading', { level: 2, name: 'Central Library AMH' })).toBeInTheDocument()
    expect(address()).toBe(CENTRAL)
  })

  it('moves focus to the sign-in form, at the same address, when the session expires', async () => {
    let expired = false
    serve({
      [PIPELINE]: () =>
        expired
          ? jsonResponse(401, NOT_AUTHENTICATED)
          : jsonResponse(200, { timezone: 'America/Chicago', state: 'ok', last_reported_at: null }),
    })
    const user = userEvent.setup()

    renderApp(CENTRAL)
    await dashboard()
    expired = true
    await user.click(screen.getByRole('button', { name: 'Refresh' }))

    expect(await screen.findByRole('form', { name: 'Sign in' })).toBeInTheDocument()
    expect(address()).toBe(CENTRAL)
    expect(main()).toHaveFocus()
    expect(main()).toContainElement(screen.getByRole('form', { name: 'Sign in' }))
    await user.tab()
    expect(screen.getByLabelText('Email')).toHaveFocus()
  })
})

describe('the sign-in form', () => {
  it('announces a failed sign-in and leaves the fields filled in and reachable', async () => {
    serve({
      'GET /api/auth/session': () => jsonResponse(401, NOT_AUTHENTICATED),
      'POST /api/auth/login': () => jsonResponse(401, { code: 'invalid_credentials', message: 'Invalid email or password.' }),
    })
    const user = userEvent.setup()

    renderApp('/organizations')
    await screen.findByRole('button', { name: 'Sign in' })
    await user.type(screen.getByLabelText('Email'), 'alice@example.test')
    await user.type(screen.getByLabelText('Password'), 'wrong{Enter}')

    const alert = await screen.findByRole('alert')
    expect(screen.getByRole('form', { name: 'Sign in' })).toContainElement(alert)
    // Focus is where the person left it, in the field they will correct.
    expect(screen.getByLabelText('Password')).toHaveFocus()
    expect(screen.getByLabelText('Email')).toHaveValue('alice@example.test')
    expect(screen.getByRole('button', { name: 'Sign in' })).not.toHaveAttribute('aria-disabled', 'true')
  })
})
