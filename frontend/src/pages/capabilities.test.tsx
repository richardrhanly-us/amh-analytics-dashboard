import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { FeatureEntitlement } from '../api/organizations.ts'
import { defaultRange, earliestDate, presetsFor, rangeProblem } from '../reports/dateRange.ts'
import {
  ALICE,
  type ApiRoutes,
  type FetchMock,
  jsonResponse,
  liveRoutes,
  NORTHBRIDGE,
  NORTHBRIDGE_DETAIL,
  organizationReportRoutes,
  REPORT,
  reportRoutes,
  requestedUrls,
  RIVERSIDE,
  RIVERSIDE_DETAIL,
  serveApi,
} from '../test/http.ts'
import { renderApp } from '../test/render.tsx'
import { DEFAULT_HISTORY_DAYS, hasTransits, historyDays } from './capabilities.ts'

// 1:50 PM on Monday 5 October 2026 in Chicago: the fixtures' "today".
const NOW = '2026-10-05T18:50:00Z'
const TODAY = '2026-10-05'

type Entitlements = Record<string, FeatureEntitlement>
const on = (limit_value: number | null = null): FeatureEntitlement => ({ enabled: true, limit_value })
const off = (limit_value: number | null = null): FeatureEntitlement => ({ enabled: false, limit_value })

const EVERYTHING: Entitlements = { transits: on(), history_days: on() }
const NO_TRANSITS: Entitlements = { history_days: on() }

const LIVE_PAGE = '/organizations/northbridge/sorters/central'
const SORTER_REPORTS = `${LIVE_PAGE}/reports`
const ORG_REPORTS = '/organizations/northbridge/reports'
const ROUTING_SETTINGS = '/organizations/northbridge/settings/routing'
const GENERAL_SETTINGS = '/organizations/northbridge/settings/general'
const STORED_ROUTING = { home_branch_label: 'Central', destinations: [{ label: 'Westside', enabled: true }] }

/** The transit-only reads, as the app asks for them. */
const TRANSIT_ONLY = /\/checkins\/by-destination|\/reports\/routing\?|\/reports\/routing-network|\/settings\/routing/

/** Northbridge with `northbridge` as its plan's features, and Riverside with `riverside`. */
function serve({
  northbridge = EVERYTHING,
  riverside = NO_TRANSITS,
  role = 'viewer',
  overrides = {},
}: { northbridge?: Entitlements; riverside?: Entitlements; role?: string; overrides?: ApiRoutes } = {}): FetchMock {
  const sorters = NORTHBRIDGE_DETAIL.sorters.map((sorter) => ({ ...sorter, report: REPORT }))
  return serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    'GET /api/organizations': () => jsonResponse(200, [{ ...NORTHBRIDGE, role }, RIVERSIDE]),
    'GET /api/organizations/northbridge': () => jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role, entitlements: northbridge }),
    'GET /api/organizations/riverside': () => jsonResponse(200, { ...RIVERSIDE_DETAIL, entitlements: riverside }),
    ...liveRoutes('northbridge', 'central'),
    ...reportRoutes('northbridge', 'central'),
    ...organizationReportRoutes('northbridge', sorters),
    ...liveRoutes('riverside', 'main'),
    ...reportRoutes('riverside', 'main'),
    'GET /api/organizations/northbridge/settings/routing': () => jsonResponse(200, { routing: STORED_ROUTING }),
    ...overrides,
  })
}

const transitRequests = (fetchMock: FetchMock) => requestedUrls(fetchMock).filter((url) => TRANSIT_ONLY.test(url))
const user = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })
const settle = () => act(() => vi.advanceTimersByTimeAsync(1_000))
const region = (name: string) => screen.queryByRole('region', { name })
const metricLabels = (name: string) =>
  within(screen.getByRole('region', { name })).queryAllByRole('term').map((term) => term.textContent)

async function liveTodayLoaded() {
  renderApp(LIVE_PAGE)
  await screen.findByText('Check-ins today')
  await waitFor(() => expect(screen.getByRole('main')).not.toHaveTextContent('Loading'))
  await settle()
}

async function sorterReportsLoaded(path = SORTER_REPORTS) {
  renderApp(path)
  await screen.findByRole('img', { name: /^Bar chart of rejects/ })
  await waitFor(() => expect(screen.getByRole('main')).not.toHaveTextContent('Loading…'))
}

async function organizationReportsLoaded() {
  renderApp(ORG_REPORTS)
  await screen.findByRole('region', { name: 'System reliability' })
  await waitFor(() => expect(screen.getByRole('main')).not.toHaveTextContent('Loading…'))
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'], shouldAdvanceTime: true })
  vi.setSystemTime(new Date(NOW))
})

afterEach(() => {
  vi.useRealTimers()
})

// =====================================================================================================================
// Reading the plan's features: the API's own reading, failing closed
// =====================================================================================================================

describe('what the plan allows', () => {
  it.each([
    [{ transits: on() }, true],
    [{ transits: on(4) }, true],
    [{ transits: off() }, false],
    [{}, false],
    [{ transits_tab: on() }, false],
  ])('transit routing for %j is %s', (entitlements, expected) => {
    expect(hasTransits({ entitlements })).toBe(expected)
  })

  it.each([
    [{ history_days: on(30) }, 30],
    [{ history_days: on(90) }, 90],
    [{ history_days: on(3650) }, 3650],
    [{ history_days: on(null) }, null],
    [{}, 30],
    [{ history_days: off(3650) }, 30],
    [{ history_days: off(null) }, 30],
    [{ history_days: on(0) }, 30],
    [{ history_days: on(-5) }, 30],
    [{ history_days: on(36.5) }, 30],
  ])('the history window for %j is %s days', (entitlements, expected) => {
    expect(historyDays({ entitlements })).toBe(expected)
    expect(DEFAULT_HISTORY_DAYS).toBe(30)
  })

  it('offers the presets that fit the window, and starts the range no earlier than it allows', () => {
    expect(presetsFor(30).map((preset) => preset.days)).toEqual([7, 30])
    expect(presetsFor(89).map((preset) => preset.days)).toEqual([7, 30])
    expect(presetsFor(90).map((preset) => preset.days)).toEqual([7, 30, 90])
    expect(presetsFor(null).map((preset) => preset.days)).toEqual([7, 30, 90])

    expect(earliestDate(TODAY, 30)).toBe('2026-09-06')
    expect(earliestDate(TODAY, 90)).toBe('2026-07-08')
    expect(earliestDate(TODAY, 1)).toBe(TODAY)
    expect(earliestDate(TODAY, null)).toBeNull()

    expect(defaultRange(TODAY, null)).toEqual({ from: '2026-09-06', to: TODAY })
    expect(defaultRange(TODAY, 7)).toEqual({ from: '2026-09-29', to: TODAY })
  })

  it('refuses a start before the window, after the rules the API applies first', () => {
    expect(rangeProblem({ from: '2026-09-06', to: TODAY }, TODAY, '2026-09-06')).toBeNull()
    expect(rangeProblem({ from: '2026-09-05', to: TODAY }, TODAY, '2026-09-06')).toBe('before_history')
    expect(rangeProblem({ from: '2026-09-05', to: '2026-09-01' }, TODAY, '2026-09-06')).toBe('order')
    expect(rangeProblem({ from: '2016-09-27', to: TODAY }, TODAY, '2026-09-06')).toBe('too_long')        // 3,661 days
    expect(rangeProblem({ from: '2026-01-01', to: TODAY }, TODAY, '2026-09-06')).toBe('before_history')
    expect(rangeProblem({ from: '2001-01-01', to: '2001-01-31' }, TODAY, null)).toBeNull()
  })
})

// =====================================================================================================================
// Transit routing
// =====================================================================================================================

describe('with transit routing', () => {
  it('Live Today shows where the sorter sent today’s check-ins, and asks for it', async () => {
    const fetchMock = serve()

    await liveTodayLoaded()

    expect(screen.getByRole('group', { name: 'Routing' })).toBeInTheDocument()
    expect(transitRequests(fetchMock).some((url) => url.includes('/checkins/by-destination'))).toBe(true)
  })

  it('the sorter’s reports include Routing, and the Overview its in-transit figure', async () => {
    const fetchMock = serve()

    await sorterReportsLoaded()

    expect(region('Routing')).toBeInTheDocument()
    expect(metricLabels('Overview')).toContain('In transit')
    expect(transitRequests(fetchMock).some((url) => url.includes('/reports/routing?'))).toBe(true)
  })

  it('the organization’s reports include the Routing network and the transit columns', async () => {
    const fetchMock = serve()

    await organizationReportsLoaded()

    expect(region('Routing network')).toBeInTheDocument()
    expect(metricLabels('Overview')).toContain('In transit')
    expect(within(region('Sorter comparison') as HTMLElement).getByRole('columnheader', { name: 'In transit' })).toBeInTheDocument()
    expect(transitRequests(fetchMock).some((url) => url.includes('/reports/routing-network'))).toBe(true)
  })

  it('Settings lists Routing, and the page reads and shows the stored routing', async () => {
    serve({ role: 'admin' })

    renderApp(ROUTING_SETTINGS)

    expect(await screen.findByLabelText('Home branch label')).toHaveValue('Central')
    expect(within(screen.getByRole('navigation', { name: 'Settings' })).getByRole('link', { name: 'Routing' })).toBeInTheDocument()
  })
})

describe.each([
  ['switched off', { transits: off(), history_days: on() }],
  ['missing', NO_TRANSITS],
])('without transit routing (%s)', (_label, northbridge) => {
  it('Live Today has no Routing group and never asks where check-ins went', async () => {
    const fetchMock = serve({ northbridge })

    await liveTodayLoaded()
    // A refresh asks for everything again: still nothing about routing.
    await user().click(screen.getByRole('button', { name: 'Refresh' }))
    await settle()

    expect(screen.queryByRole('group', { name: 'Routing' })).not.toBeInTheDocument()
    expect(screen.getByText('Check-ins today')).toBeInTheDocument()
    expect(screen.getByText('Rejects today')).toBeInTheDocument()
    expect(transitRequests(fetchMock)).toEqual([])
  })

  it('the sorter’s reports have no Routing and no in-transit figure, and keep everything else', async () => {
    const fetchMock = serve({ northbridge })

    await sorterReportsLoaded()

    expect(region('Routing')).not.toBeInTheDocument()
    expect(metricLabels('Overview')).not.toContain('In transit')
    expect(metricLabels('Overview')).toEqual(expect.arrayContaining(['Check-ins', 'Rejects', 'Active days']))
    for (const name of ['Volume & capacity', 'Bin volume', 'Reliability']) {
      expect(region(name)).toBeInTheDocument()
    }
    expect(transitRequests(fetchMock)).toEqual([])
  })

  it('the organization’s reports have no Routing network and no transit columns', async () => {
    const fetchMock = serve({ northbridge })

    await organizationReportsLoaded()

    expect(region('Routing network')).not.toBeInTheDocument()
    expect(metricLabels('Overview')).not.toContain('In transit')
    const comparison = within(region('Sorter comparison') as HTMLElement)
    expect(comparison.queryByRole('columnheader', { name: 'In transit' })).not.toBeInTheDocument()
    expect(comparison.queryByRole('columnheader', { name: 'Transit rate' })).not.toBeInTheDocument()
    expect(comparison.getByRole('columnheader', { name: 'Rejects' })).toBeInTheDocument()
    expect(transitRequests(fetchMock)).toEqual([])
  })

  it('Settings does not list Routing, and its address says it is not available without asking for it', async () => {
    const fetchMock = serve({ northbridge, role: 'admin' })

    renderApp(GENERAL_SETTINGS)
    const nav = await screen.findByRole('navigation', { name: 'Settings' })
    expect(within(nav).queryByRole('link', { name: 'Routing' })).not.toBeInTheDocument()
    expect(within(nav).getByRole('link', { name: 'Efficiency' })).toBeInTheDocument()

    renderApp(ROUTING_SETTINGS)
    expect(await screen.findByText('Routing is not available for this organization.')).toBeInTheDocument()
    await settle()
    expect(screen.queryByLabelText('Home branch label')).not.toBeInTheDocument()
    expect(transitRequests(fetchMock)).toEqual([])
  })
})

// =====================================================================================================================
// How far back a report may start
// =====================================================================================================================

const presetNames = () =>
  within(screen.getAllByRole('group', { name: 'Date range presets' })[0]).getAllByRole('button').map((button) => button.textContent)
const fromInput = () => screen.getAllByLabelText('From')[0] as HTMLInputElement

async function applyCustom(from: string, to: string) {
  fireEvent.change(fromInput(), { target: { value: from } })
  fireEvent.change(screen.getAllByLabelText('To')[0], { target: { value: to } })
  fireEvent.click(screen.getAllByRole('button', { name: 'Apply dates' })[0])
  await settle()
}

describe('the history window', () => {
  it.each([
    ['thirty days', { ...EVERYTHING, history_days: on(30) }],
    ['no history feature', { transits: on() }],
    ['a switched-off one', { ...EVERYTHING, history_days: off(3650) }],
    ['an unusable one', { ...EVERYTHING, history_days: on(0) }],
  ])('%s: the last 7 and 30 days, nothing before 6 September, and an earlier start is refused here', async (_label, northbridge) => {
    const fetchMock = serve({ northbridge })
    await sorterReportsLoaded()

    expect(presetNames()).toEqual(['Last 7 days', 'Last 30 days'])
    expect(fromInput()).toHaveAttribute('min', '2026-09-06')
    expect(screen.getByText(/The earliest date that can be chosen is/)).toBeInTheDocument()

    await applyCustom('2026-09-05', TODAY)

    expect(screen.getByRole('alert')).toHaveTextContent("The start date is before this organization's available reporting window.")
    expect(requestedUrls(fetchMock).filter((url) => url.includes('from=2026-09-05'))).toEqual([])
  })

  it('ninety days: the last 90 days too, and nothing before 8 July', async () => {
    const fetchMock = serve({ northbridge: { ...EVERYTHING, history_days: on(90) } })
    await sorterReportsLoaded()

    expect(presetNames()).toEqual(['Last 7 days', 'Last 30 days', 'Last 90 days'])
    expect(fromInput()).toHaveAttribute('min', '2026-07-08')

    await applyCustom('2026-07-08', '2026-08-31')
    expect(requestedUrls(fetchMock).some((url) => url.includes('/reports/overview?from=2026-07-08&to=2026-08-31'))).toBe(true)
  })

  it('no limit: no earliest date, an old range is asked for, and 3,660 days is the longest report', async () => {
    const fetchMock = serve({ northbridge: EVERYTHING })
    await sorterReportsLoaded()

    expect(presetNames()).toEqual(['Last 7 days', 'Last 30 days', 'Last 90 days', 'Year to date'])
    expect(fromInput()).not.toHaveAttribute('min')
    expect(screen.queryByText(/The earliest date that can be chosen is/)).not.toBeInTheDocument()

    await applyCustom('2021-01-01', '2021-01-31')
    expect(requestedUrls(fetchMock).some((url) => url.includes('/reports/overview?from=2021-01-01&to=2021-01-31'))).toBe(true)

    await applyCustom('2016-09-27', TODAY)
    expect(screen.getByRole('alert')).toHaveTextContent('A single report can cover up to 3,660 days.')
  })

  it('the organization’s reports hold to the same window', async () => {
    serve({ northbridge: { ...EVERYTHING, history_days: on(30) } })
    await organizationReportsLoaded()

    expect(presetNames()).toEqual(['Last 7 days', 'Last 30 days'])
    expect(fromInput()).toHaveAttribute('min', '2026-09-06')
  })

  it('says so, and shows no figures, when the API refuses a range as starting before the window', async () => {
    const refused = () =>
      jsonResponse(422, { code: 'range_before_history', message: "The selected range starts before this organization's available reporting window." })
    const api = '/api/organizations/northbridge/branches/central/reports'
    serve({
      overrides: Object.fromEntries(
        ['overview', 'volume', 'routing', 'bins', 'reliability'].map((kind) => [`GET ${api}/${kind}?from=*&to=*`, refused]),
      ),
    })

    renderApp(SORTER_REPORTS)

    expect(await screen.findByText(/starts before this organization's available reporting window\. Choose a later start date\./)).toBeInTheDocument()
    expect(region('Overview')).not.toBeInTheDocument()
  })
})

// =====================================================================================================================
// Another organization, another plan
// =====================================================================================================================

describe('moving to another organization', () => {
  it('takes that organization’s plan: transit routing, the history window and Holds, with nothing carried over', async () => {
    const fetchMock = serve({
      northbridge: { ...EVERYTHING, internal_workflow: on() },
      riverside: { history_days: on(30) },
    })
    const person = user()
    await sorterReportsLoaded()
    expect(region('Routing')).toBeInTheDocument()
    expect(region('Holds')).toBeInTheDocument()
    expect(presetNames()).toEqual(['Last 7 days', 'Last 30 days', 'Last 90 days', 'Year to date'])
    const before = transitRequests(fetchMock).length

    await person.click(screen.getByRole('link', { name: 'Organizations' }))
    await person.click(await screen.findByRole('link', { name: /Riverside Library/ }))
    await person.click(await screen.findByRole('link', { name: /Riverside Main AMH/ }))
    await screen.findByText('Check-ins today')
    expect(screen.queryByRole('group', { name: 'Routing' })).not.toBeInTheDocument()
    await person.click(screen.getByRole('link', { name: 'Reports' }))
    await screen.findByRole('img', { name: /^Bar chart of rejects/ })
    await settle()

    expect(region('Routing')).not.toBeInTheDocument()
    expect(region('Holds')).not.toBeInTheDocument()
    expect(presetNames()).toEqual(['Last 7 days', 'Last 30 days'])
    expect(fromInput()).toHaveAttribute('min', '2026-09-06')
    expect(transitRequests(fetchMock).slice(before).filter((url) => url.includes('/riverside/'))).toEqual([])
  })
})
