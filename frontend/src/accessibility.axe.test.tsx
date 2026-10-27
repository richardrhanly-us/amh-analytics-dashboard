import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  ALICE,
  type ApiRoutes,
  type FetchMock,
  jsonResponse,
  liveRoutes,
  NORTHBRIDGE,
  NORTHBRIDGE_DETAIL,
  NOT_AUTHENTICATED,
  organizationReportRoutes,
  REPORT,
  reportRoutes,
  RIVERSIDE,
  RIVERSIDE_DETAIL,
  serveApi,
} from './test/http.ts'
import { axeViolations } from './test/axe.ts'
import { renderApp } from './test/render.tsx'

/**
 * The customer app's rendered pages checked by axe-core: no violation of any impact. This is the automated baseline
 * only -- colour contrast, reading order and screen-reader wording still need the manual audit.
 */

// 1:50 PM on Monday 5 October 2026 in Chicago: the report fixtures' "today".
const NOW = '2026-10-05T18:50:00Z'
const LIVE_PAGE = '/organizations/northbridge/sorters/central'
const ACCOUNT = {
  email: 'alice@example.test',
  full_name: 'Alice Example',
  last_login_at: '2026-10-05T18:50:00Z',
  last_password_changed_at: '2026-08-01T09:00:00Z',
}

/** Alice signed in, an owner of Northbridge, with every read the pages below make. */
function serve(overrides: ApiRoutes = {}): FetchMock {
  const sorters = NORTHBRIDGE_DETAIL.sorters.map((sorter) => ({ ...sorter, report: REPORT }))
  return serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    'GET /api/organizations': () => jsonResponse(200, [{ ...NORTHBRIDGE, role: 'owner' }, RIVERSIDE]),
    'GET /api/organizations/northbridge': () => jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role: 'owner' }),
    'GET /api/organizations/riverside': () => jsonResponse(200, RIVERSIDE_DETAIL),
    ...liveRoutes('northbridge', 'central'),
    ...reportRoutes('northbridge', 'central'),
    ...organizationReportRoutes('northbridge', sorters),
    'GET /api/organizations/northbridge/settings/routing': () =>
      jsonResponse(200, { routing: { home_branch_label: 'Central', destinations: [{ label: 'Westside', enabled: true }] } }),
    'GET /api/account': () => jsonResponse(200, ACCOUNT),
    ...overrides,
  })
}

const signedOut = () => serve({ 'GET /api/auth/session': () => jsonResponse(401, NOT_AUTHENTICATED) })
const main = () => screen.getByRole('main')
const settle = () => act(() => vi.advanceTimersByTimeAsync(1_000))

/** Waits for `path`'s page heading and for every section to finish loading. */
async function loaded(path: string, heading: string | RegExp) {
  renderApp(path)
  await screen.findByRole('heading', { level: 2, name: heading })
  await waitFor(() => expect(main()).not.toHaveTextContent(/Loading|Checking/))
  await settle()
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'], shouldAdvanceTime: true })
  vi.setSystemTime(new Date(NOW))
})

afterEach(() => {
  vi.useRealTimers()
})

describe('the check itself', () => {
  it('reports real violations, so a passing page means something', async () => {
    render(
      <div>
        {/* eslint-disable-next-line jsx-a11y-x/alt-text -- deliberately inaccessible: proves axe reports it */}
        <img src="/a.png" />
        <input type="text" />
        <button type="button" />
      </div>,
    )

    const found = (await axeViolations()).join('\n')
    expect(found).toMatch(/^image-alt /m)
    expect(found).toMatch(/^label /m)
    expect(found).toMatch(/^button-name /m)
  })
})

describe('axe finds no violation on', () => {
  it('the sign-in form', async () => {
    signedOut()
    renderApp('/')
    await screen.findByRole('button', { name: 'Sign in' })

    expect(await axeViolations()).toEqual([])
  })

  it('the forgot-password form', async () => {
    signedOut()
    renderApp('/')
    await userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) }).click(
      await screen.findByRole('button', { name: 'Forgot password?' }),
    )
    await screen.findByRole('heading', { level: 2, name: /password/i })

    expect(await axeViolations()).toEqual([])
  })

  it('the password-reset page', async () => {
    signedOut()
    renderApp('/reset-password')
    await screen.findByRole('heading', { level: 2 })

    expect(await axeViolations()).toEqual([])
  })

  it('the organizations list', async () => {
    serve()
    await loaded('/organizations', 'Organizations')

    expect(await axeViolations()).toEqual([])
  })

  it('an organization and its sorters', async () => {
    serve()
    await loaded('/organizations/northbridge', NORTHBRIDGE.name)

    expect(await axeViolations()).toEqual([])
  })

  it("a sorter's Live Today", async () => {
    serve()
    renderApp(LIVE_PAGE)
    await screen.findByText('Check-ins today')
    await waitFor(() => expect(main()).not.toHaveTextContent('Loading'))
    await settle()

    expect(await axeViolations()).toEqual([])
  })

  it("a sorter's Live Today with today's holds, and the bin table open", async () => {
    serve({
      'GET /api/organizations/northbridge': () =>
        jsonResponse(200, {
          ...NORTHBRIDGE_DETAIL,
          role: 'owner',
          entitlements: { ...NORTHBRIDGE_DETAIL.entitlements, internal_workflow: { enabled: true, limit_value: null } },
        }),
    })
    renderApp(LIVE_PAGE)
    await screen.findByText('Public holds today')
    await waitFor(() => expect(main()).not.toHaveTextContent('Loading'))
    await userEvent
      .setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })
      .click(screen.getByRole('button', { name: 'Show table: Check-ins by bin today' }))
    await screen.findByRole('table', { name: 'Check-ins by bin today' })
    await settle()

    expect(await axeViolations()).toEqual([])
  })

  it("a sorter's reports", async () => {
    serve()
    renderApp(`${LIVE_PAGE}/reports`)
    await screen.findByRole('img', { name: /^Bar chart of rejects/ })
    await waitFor(() => expect(main()).not.toHaveTextContent('Loading…'))
    await settle()

    expect(await axeViolations()).toEqual([])
  })

  it("the organization's reports", async () => {
    serve()
    renderApp('/organizations/northbridge/reports')
    await screen.findByRole('region', { name: 'System reliability' })
    await waitFor(() => expect(main()).not.toHaveTextContent('Loading…'))
    await settle()

    expect(await axeViolations()).toEqual([])
  })

  it('the routing settings form', async () => {
    serve()
    renderApp('/organizations/northbridge/settings/routing')
    await screen.findByRole('heading', { level: 3, name: 'Transit destinations' })
    await settle()

    expect(await axeViolations()).toEqual([])
  })

  it('My account', async () => {
    serve()
    await loaded('/account', 'My account')

    expect(await axeViolations()).toEqual([])
  })
})

describe('the skip link', () => {
  it('is the first thing Tab reaches, and moves focus to the main content', async () => {
    serve()
    await loaded('/organizations', 'Organizations')
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })
    ;(document.activeElement as HTMLElement | null)?.blur()

    await user.tab()
    const skip = screen.getByRole('link', { name: 'Skip to main content' })
    expect(skip).toHaveFocus()
    expect(skip).toHaveAttribute('href', '#main-content')

    await user.click(skip)
    expect(main()).toHaveFocus()
    expect(main()).toHaveAttribute('id', 'main-content')
    // The address is untouched: following the link is not a navigation.
    expect(screen.getByTestId('address')).toHaveTextContent('/organizations')
    expect(window.location.hash).toBe('')
  })

  it('still works after a page change', async () => {
    serve()
    await loaded('/organizations', 'Organizations')
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })

    await user.click(screen.getByRole('link', { name: NORTHBRIDGE.name }))
    await screen.findByRole('heading', { level: 2, name: NORTHBRIDGE.name })
    await user.click(screen.getByRole('link', { name: 'Skip to main content' }))

    expect(main()).toHaveFocus()
    expect(document.querySelectorAll('#main-content')).toHaveLength(1)
  })

  it('is there before sign-in too', async () => {
    signedOut()
    renderApp('/')
    await screen.findByRole('button', { name: 'Sign in' })

    expect(screen.getByRole('link', { name: 'Skip to main content' })).toBeInTheDocument()
  })
})
