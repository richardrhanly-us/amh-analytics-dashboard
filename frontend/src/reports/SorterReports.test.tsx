import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  ALICE,
  type ApiRoutes,
  deferred,
  type FetchMock,
  jsonResponse,
  livePath,
  liveRoutes,
  networkFailure,
  NORTHBRIDGE_DETAIL,
  NOT_AUTHENTICATED,
  REPORT,
  reportBody,
  type ReportFixture,
  reportRoutes,
  requestedUrls,
  RIVERSIDE_DETAIL,
  serveApi,
  TENANT_NOT_FOUND,
} from '../test/http.ts'
import { renderApp } from '../test/render.tsx'

const CENTRAL = '/organizations/northbridge/sorters/central'
const CENTRAL_REPORTS = `${CENTRAL}/reports`
const API = livePath('northbridge', 'central')
const PIPELINE = `GET ${API}/pipeline-status`
const OVERVIEW = `GET ${API}/reports/overview?from=*&to=*`
const VOLUME = `GET ${API}/reports/volume?from=*&to=*`
const ROUTING = `GET ${API}/reports/routing?from=*&to=*`
const RELIABILITY = `GET ${API}/reports/reliability?from=*&to=*`
const KINDS = [
  ['overview', OVERVIEW, 'Overview'],
  ['volume', VOLUME, 'Volume & capacity'],
  ['routing', ROUTING, 'Routing'],
  ['reliability', RELIABILITY, 'Reliability'],
] as const

// 1:50 PM on Monday 5 October 2026 in Chicago (CDT, UTC-5): the fixture's "today".
const NOW = '2026-10-05T18:50:00Z'
// The default range: the last 30 days, ending today.
const FROM = '2026-09-06'
const TO = '2026-10-05'
const DEFAULT_RANGE = `from=${FROM}&to=${TO}`

const SERVER_ERROR = () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' })
const UNAVAILABLE = 'Reports are not available for this sorter yet.'
const NOT_A_FIGURE = /NaN|Infinity|undefined|null|\[object/

const report = (changes: Partial<ReportFixture>): ReportFixture => ({ ...REPORT, ...changes })
/** The East Side sorter: seven check-ins every day, sent nowhere, none rejected. */
const EAST = report({ checkins: () => 7, rejects: () => 0, transit: [], home: 'East Side' })

/** A signed-in user at a sorter whose reports answer `fixture`, unless a test replaces a route. */
function serve(overrides: ApiRoutes = {}, fixture: ReportFixture | (() => ReportFixture) = REPORT): FetchMock {
  return serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    // A member who is not an owner or admin: these are the four reports every member sees. The fifth, Efficiency,
    // is for owners and admins only and is tested in EfficiencySection.test.tsx.
    'GET /api/organizations/northbridge': () => jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role: 'manager' }),
    'GET /api/organizations/riverside': () => jsonResponse(200, RIVERSIDE_DETAIL),
    ...reportRoutes('northbridge', 'central', fixture),
    ...reportRoutes('northbridge', 'east-side', EAST),
    ...reportRoutes('riverside', 'main', fixture),
    // Live Today's own reads, for the tests that arrive from it. Its pipeline status is the fuller one.
    ...liveRoutes('northbridge', 'central'),
    ...liveRoutes('northbridge', 'east-side'),
    ...overrides,
  })
}

/** Answers first with each of `replies` in turn, then keeps giving the last one. */
function inTurn(...replies: Array<(url: string) => Response | Promise<Response>>) {
  let call = 0
  return (url: string) => replies[Math.min(call++, replies.length - 1)](url)
}

const rangeIn = (url: string): [string, string] => {
  const params = new URL(url, 'http://test.invalid').searchParams
  return [params.get('from') ?? '', params.get('to') ?? '']
}
/** The real answer to whatever range `url` asks for. */
const answer = (kind: keyof typeof reportBody, fixture: ReportFixture = REPORT) => (url: string) =>
  jsonResponse(200, reportBody[kind](fixture, ...rangeIn(url)))

const main = () => screen.getByRole('main')
/** Each report request made, as "kind?from=..&to=..". */
const reportRequests = (fetchMock: FetchMock) =>
  requestedUrls(fetchMock)
    .filter((url) => url.includes('/reports/'))
    .map((url) => url.split('/reports/')[1])
const requestsFor = (fetchMock: FetchMock, kind: string) => reportRequests(fetchMock).filter((request) => request.startsWith(`${kind}?`))

const section = (name: string) => screen.getByRole('region', { name })
/** The value shown beside a label in one report section. */
function metric(sectionName: string, label: string): HTMLElement {
  return within(section(sectionName)).getByText(label, { selector: 'dt' }).nextElementSibling as HTMLElement
}
/** The line under that figure, or null. */
function note(sectionName: string, label: string): string | null {
  return metric(sectionName, label).nextElementSibling?.textContent ?? null
}
/** A table's body rows, cell by cell. */
function rows(name: string): string[][] {
  return within(screen.getByRole('table', { name }))
    .getAllByRole('row')
    .slice(1)
    .map((row) => Array.from(row.children).map((cell) => cell.textContent ?? ''))
}
const columns = (name: string) =>
  within(screen.getByRole('table', { name }))
    .getAllByRole('columnheader')
    .map((cell) => cell.textContent)
/** Opens a chart's table and returns its rows. */
function chartRows(heading: string): string[][] {
  const show = screen.queryByRole('button', { name: `Show table: ${heading}` })
  if (show !== null) {
    fireEvent.click(show)
  }
  return rows(heading)
}
const bars = (chartName: RegExp) => Array.from(screen.getByRole('img', { name: chartName }).querySelectorAll('rect[data-bar]'))

const user = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })
const pass = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms))
const preset = (name: string) => screen.getByRole('button', { name })
const shown = () => (document.querySelector('.range-shown') as HTMLElement).textContent
/** Types a custom range and applies it. */
function applyDates(from: string, to: string) {
  fireEvent.change(screen.getByLabelText('From'), { target: { value: from } })
  fireEvent.change(screen.getByLabelText('To'), { target: { value: to } })
  fireEvent.click(screen.getByRole('button', { name: 'Apply dates' }))
}
/** Waits until all four sections show their content. */
async function loaded() {
  await screen.findByRole('img', { name: /^Bar chart of rejects/ })
  await waitFor(() => expect(main()).not.toHaveTextContent('Loading…'))
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'], shouldAdvanceTime: true })
  vi.setSystemTime(new Date(NOW))
})

afterEach(() => {
  vi.useRealTimers()
})

describe('arriving at a sorter’s reports', () => {
  it('is reached from the sorter by its Reports link, and says which view is current', async () => {
    serve()
    const person = user()

    renderApp(CENTRAL)
    const views = within(await screen.findByRole('navigation', { name: 'Sorter views' }))
    expect(views.getByRole('link', { name: 'Live Today' })).toHaveAttribute('aria-current', 'page')
    expect(views.getByRole('link', { name: 'Reports' })).not.toHaveAttribute('aria-current')
    await person.click(views.getByRole('link', { name: 'Reports' }))

    expect(screen.getByTestId('address')).toHaveTextContent(CENTRAL_REPORTS)
    await loaded()
    expect(views.getByRole('link', { name: 'Reports' })).toHaveAttribute('aria-current', 'page')
    expect(views.getByRole('link', { name: 'Live Today' })).not.toHaveAttribute('aria-current')
    // The link that had focus is still there, but the page under it is new: focus goes to the page's heading.
    expect(screen.getByRole('heading', { level: 2, name: 'Central Library AMH' })).toHaveFocus()
  })

  it('asks for pipeline status alone, and shows nothing, until the product’s day is known', async () => {
    const pipeline = deferred<Response>()
    const fetchMock = serve({ [PIPELINE]: () => pipeline.promise })

    renderApp(CENTRAL_REPORTS)

    expect(await screen.findByText('Loading reports…')).toHaveRole('status')
    await pass(1000)
    expect(reportRequests(fetchMock)).toEqual([])
    expect(screen.queryByRole('button', { name: 'Last 30 days' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region')).not.toBeInTheDocument()

    pipeline.resolve(jsonResponse(200, { timezone: 'America/Chicago', state: 'ok', last_reported_at: null }))
    await loaded()
    expect(reportRequests(fetchMock).sort()).toEqual([
      `overview?${DEFAULT_RANGE}`,
      `reliability?${DEFAULT_RANGE}`,
      `routing?${DEFAULT_RANGE}`,
      `volume?${DEFAULT_RANGE}`,
    ])
  })

  it('opens on the last 30 days, ending today, and says today is not over', async () => {
    serve()

    renderApp(CENTRAL_REPORTS)
    await loaded()

    expect(preset('Last 30 days')).toHaveAttribute('aria-pressed', 'true')
    expect(preset('Last 7 days')).toHaveAttribute('aria-pressed', 'false')
    expect(preset('Last 90 days')).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByLabelText('From')).toHaveValue(FROM)
    expect(screen.getByLabelText('To')).toHaveValue(TO)
    expect(shown()).toBe(
      'Showing Sep 6, 2026 to Oct 5, 2026: 30 days, in America/Chicago time. This range includes today, which is not over yet: its figures will still rise.',
    )
  })

  it('takes today from the product’s zone, not this machine’s or UTC', async () => {
    // 10:30 PM on 5 October in Chicago. In UTC -- and anywhere east of it -- it is already the 6th.
    vi.setSystemTime(new Date('2026-10-06T03:30:00Z'))
    const fetchMock = serve()

    renderApp(CENTRAL_REPORTS)
    await loaded()

    expect(new Set(reportRequests(fetchMock).map((request) => request.split('?')[1]))).toEqual(new Set([DEFAULT_RANGE]))
    expect(screen.getByLabelText('To')).toHaveAttribute('max', '2026-10-05')
    expect(screen.getByText(/The latest date that can be chosen is today, Oct 5, 2026\./)).toBeInTheDocument()
  })

  it('follows a product in another zone', async () => {
    // 18:50 UTC on the 5th is 7:50 AM on the 6th in Auckland.
    const auckland = report({ timezone: 'Pacific/Auckland', today: '2026-10-06' })
    const fetchMock = serve({ ...reportRoutes('northbridge', 'central', auckland) })

    renderApp(CENTRAL_REPORTS)
    await loaded()

    expect(reportRequests(fetchMock)[0]).toMatch(/from=2026-09-07&to=2026-10-06$/)
    expect(shown()).toContain('in Pacific/Auckland time.')
  })

  it('has four sections, in order, under the sorter and nothing else', async () => {
    serve()

    renderApp(CENTRAL_REPORTS)
    await loaded()

    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('Central Library AMH')
    expect(screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent)).toEqual(['Reports'])
    expect(screen.getAllByRole('heading', { level: 4 }).map((heading) => heading.textContent)).toEqual([
      'Overview',
      'Volume & capacity',
      'Routing',
      'Reliability',
    ])
    expect(screen.getAllByRole('heading', { level: 5 }).map((heading) => heading.textContent)).toEqual([
      'Daily check-ins',
      'Typical week',
      'Typical day',
      'Where check-ins went',
      'Daily transit',
      'Daily rejects',
      'Reject reasons',
    ])
    expect(screen.getByText(/^What this sorter processed, at Central Branch/)).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/efficien|ROI|savings|return on|staff hours/i)
    expect(main()).not.toHaveTextContent(NOT_A_FIGURE)
  })

  it('says so when more than one collector reports for the site', async () => {
    const detail = { ...NORTHBRIDGE_DETAIL, role: 'manager', sorters: [{ ...NORTHBRIDGE_DETAIL.sorters[0], collector_count: 2 }] }
    serve({ 'GET /api/organizations/northbridge': () => jsonResponse(200, detail) })

    renderApp(CENTRAL_REPORTS)
    await loaded()

    expect(screen.getByText(/2 collectors report for this site\. Their figures are combined here/)).toHaveRole('note')
  })

  it('is not a page for a place the sorter only routes to', async () => {
    const fetchMock = serve()

    renderApp('/organizations/northbridge/sorters/westside/reports')

    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument()
    expect(requestedUrls(fetchMock).filter((url) => url.includes('/branches/'))).toEqual([])
  })
})

describe('the figures of a range', () => {
  // 6 September to 5 October 2026: five Sundays (0) and Mondays (100), four of each other day.
  beforeEach(async () => {
    serve()
    renderApp(CENTRAL_REPORTS)
    await loaded()
  })

  it('overview: totals, and rates made from the totals', () => {
    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^3,140$/)
    expect(note('Overview', 'Check-ins')).toBe('Over 30 days')
    expect(metric('Overview', 'Average per day')).toHaveTextContent(/^105$/)
    expect(metric('Overview', 'Active days')).toHaveTextContent(/^25$/)
    expect(note('Overview', 'Active days')).toBe('Of 30 days')
    expect(metric('Overview', 'In transit')).toHaveTextContent(/^471$/)
    expect(note('Overview', 'In transit')).toBe('15.0% of check-ins')
    expect(metric('Overview', 'Rejects')).toHaveTextContent(/^157$/)
    expect(note('Overview', 'Rejects')).toBe('5.0% reject rate')
    expect(metric('Overview', 'Busiest day')).toHaveTextContent(/^180$/)
    // Four Fridays had 180: the earliest is named.
    expect(note('Overview', 'Busiest day')).toBe('Sep 11, 2026')
  })

  it('overview: a bar for every day, zero days included, with the same figures as a table', () => {
    expect(bars(/^Bar chart of check-ins on each day/)).toHaveLength(30)
    expect(screen.getByText('3,140 check-ins over 30 days. Busiest day: Sep 11, 2026, with 180.')).toBeInTheDocument()

    const days = chartRows('Daily check-ins')

    expect(columns('Daily check-ins')).toEqual(['Date', 'Check-ins', 'Rejects'])
    expect(days).toHaveLength(30)
    expect(days[0]).toEqual(['Sun, Sep 6', '0', '0'])
    expect(days[5]).toEqual(['Fri, Sep 11', '180', '9'])
    expect(days[29]).toEqual(['Mon, Oct 5', '100', '5'])
  })

  it('volume: per-day and per-active-day averages, and the busiest day, weekday and hour', () => {
    expect(metric('Volume & capacity', 'Check-ins')).toHaveTextContent(/^3,140$/)
    expect(metric('Volume & capacity', 'Average per day')).toHaveTextContent(/^105$/)
    expect(note('Volume & capacity', 'Average per day')).toBe('All 30 days')
    expect(metric('Volume & capacity', 'Average per active day')).toHaveTextContent(/^126$/)
    expect(note('Volume & capacity', 'Average per active day')).toBe('25 days with check-ins')
    expect(metric('Volume & capacity', 'Busiest day')).toHaveTextContent(/^180$/)
    expect(metric('Volume & capacity', 'Busiest weekday')).toHaveTextContent(/^Friday$/)
    expect(note('Volume & capacity', 'Busiest weekday')).toBe('180 a day on average')
    // 10 AM and 2 PM tie: the earlier hour is named.
    expect(metric('Volume & capacity', 'Busiest hour')).toHaveTextContent(/^10–11 AM$/)
    expect(note('Volume & capacity', 'Busiest hour')).toBe('1,570 check-ins in the range')
  })

  it('volume: a typical week of seven days and a typical day of 24 hours', () => {
    expect(bars(/each day of the week$/)).toHaveLength(7)
    expect(bars(/each hour of the day$/)).toHaveLength(24)

    const week = chartRows('Typical week')
    expect(columns('Typical week')).toEqual(['Weekday', 'Days in range', 'Check-ins', 'Average per day'])
    expect(week).toEqual([
      ['Monday', '5', '500', '100'],
      ['Tuesday', '4', '480', '120'],
      ['Wednesday', '4', '560', '140'],
      ['Thursday', '4', '640', '160'],
      ['Friday', '4', '720', '180'],
      ['Saturday', '4', '240', '60.0'],
      ['Sunday', '5', '0', '0.0'],
    ])
    const hours = chartRows('Typical day')
    expect(hours).toHaveLength(24)
    expect(hours[0]).toEqual(['12 AM', '0', '0.0'])
    expect(hours[10]).toEqual(['10 AM', '1,570', '52.3'])
    expect(hours[14]).toEqual(['2 PM', '1,570', '52.3'])
    expect(screen.getByText(/Busiest hour: 10–11 AM\. Hours are in America\/Chicago time\./)).toBeInTheDocument()
  })

  it('routing: home, then each destination in the order the API gave, each a share of all check-ins', () => {
    expect(metric('Routing', 'Total transit')).toHaveTextContent(/^471$/)
    expect(metric('Routing', 'Transit rate')).toHaveTextContent(/^15\.0%$/)
    expect(metric('Routing', 'Kept at Main')).toHaveTextContent(/^2,669$/)
    expect(note('Routing', 'Kept at Main')).toBe('85.0% of check-ins')
    expect(within(section('Routing')).queryByText('Other routing')).not.toBeInTheDocument()

    expect(rows('Where check-ins went')).toEqual([
      ['Main (home)', '2,669', '85.0%'],
      ['Westside', '314', '10.0%'],
      ['Library Express', '157', '5.0%'],
    ])
    // A destination is where items went, not somewhere to go: nothing in the section is a link.
    expect(within(section('Routing')).queryByRole('link')).not.toBeInTheDocument()
  })

  it('routing: daily transit, with a column for each destination', () => {
    expect(bars(/sent to transit destinations/)).toHaveLength(30)

    const days = chartRows('Daily transit')

    expect(columns('Daily transit')).toEqual(['Date', 'Total transit', 'Westside', 'Library Express'])
    expect(days[0]).toEqual(['Sun, Sep 6', '0', '0', '0'])
    expect(days[1]).toEqual(['Mon, Sep 7', '15', '10', '5'])
    expect(screen.getByText(/^471 check-ins were sent to a transit destination over 30 days\. Most on one day: 27, on Sep 11, 2026\.$/)).toBeInTheDocument()
  })

  it('reliability: rejects against check-ins, by day and by reason', () => {
    expect(metric('Reliability', 'Rejects')).toHaveTextContent(/^157$/)
    expect(metric('Reliability', 'Reject rate')).toHaveTextContent(/^5\.0%$/)
    expect(note('Reliability', 'Reject rate')).toBe('Rejects against check-ins')
    expect(bars(/^Bar chart of rejects/)).toHaveLength(30)
    expect(chartRows('Daily rejects')[5]).toEqual(['Fri, Sep 11', '9', '180'])

    const sent = reportBody.reliability(REPORT, FROM, TO).reasons.filter((reason) => reason.reject_count > 0)
    const reasons = rows('Reject reasons')
    // Only the reasons that occurred, the most frequent first, under the API's own classification.
    expect(sent.map((reason) => reason.reason).sort()).toEqual(['item_not_found', 'rfid_collision'])
    expect(reasons.map((row) => row[0]).sort()).toEqual(['Item not found', 'RFID collision'])
    expect(reasons.map((row) => Number(row[1]))).toEqual(sent.map((reason) => reason.reject_count).sort((a, b) => b - a))
    expect(reasons.reduce((sum, row) => sum + Number(row[1]), 0)).toBe(157)
    expect(reasons.reduce((sum, row) => sum + parseFloat(row[2]), 0)).toBeCloseTo(100, 0)
  })
})

describe('choosing a range', () => {
  it('a preset applies at once: new requests, new figures, and the pressed preset moves', async () => {
    const fetchMock = serve()
    const person = user()
    renderApp(CENTRAL_REPORTS)
    await loaded()
    fetchMock.mockClear()

    await person.click(preset('Last 7 days'))
    await loaded()

    expect(reportRequests(fetchMock).sort()).toEqual(
      ['overview', 'reliability', 'routing', 'volume'].map((kind) => `${kind}?from=2026-09-29&to=2026-10-05`),
    )
    expect(preset('Last 7 days')).toHaveAttribute('aria-pressed', 'true')
    expect(preset('Last 30 days')).toHaveAttribute('aria-pressed', 'false')
    expect(preset('Last 7 days')).toHaveFocus()
    expect(screen.getByLabelText('From')).toHaveValue('2026-09-29')
    expect(shown()).toMatch(/^Showing Sep 29, 2026 to Oct 5, 2026: 7 days, .* includes today/)
    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^760$/)
    expect(bars(/^Bar chart of check-ins on each day/)).toHaveLength(7)
  })

  it('offers the last 90 days', async () => {
    const fetchMock = serve()
    const person = user()
    renderApp(CENTRAL_REPORTS)
    await loaded()

    await person.click(preset('Last 90 days'))
    await loaded()

    expect(reportRequests(fetchMock).at(-1)).toMatch(/from=2026-07-08&to=2026-10-05$/)
    expect(shown()).toContain('90 days')
    expect(bars(/^Bar chart of check-ins on each day/)).toHaveLength(90)
  })

  it('a custom range applies when submitted; one that ends before today is not called partial', async () => {
    const fetchMock = serve()
    renderApp(CENTRAL_REPORTS)
    await loaded()
    fetchMock.mockClear()

    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-09-01' } })
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2026-09-14' } })
    // Typing asks for nothing and changes nothing shown.
    expect(reportRequests(fetchMock)).toEqual([])
    expect(shown()).toContain('Sep 6, 2026 to Oct 5, 2026')
    fireEvent.click(screen.getByRole('button', { name: 'Apply dates' }))
    await loaded()

    expect(new Set(reportRequests(fetchMock).map((request) => request.split('?')[1]))).toEqual(new Set(['from=2026-09-01&to=2026-09-14']))
    expect(shown()).toBe('Showing Sep 1, 2026 to Sep 14, 2026: 14 days, in America/Chicago time.')
    expect(screen.queryAllByRole('button', { pressed: true })).toHaveLength(0)
    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^1,520$/)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('a custom range that is one of the presets shows that preset as chosen', async () => {
    serve()
    renderApp(CENTRAL_REPORTS)
    await loaded()

    applyDates('2026-09-29', '2026-10-05')
    await loaded()

    expect(preset('Last 7 days')).toHaveAttribute('aria-pressed', 'true')
  })

  it('a single day is a range', async () => {
    serve()
    renderApp(CENTRAL_REPORTS)
    await loaded()

    applyDates('2026-10-02', '2026-10-02')
    await loaded()

    expect(shown()).toBe('Showing Oct 2, 2026 to Oct 2, 2026: 1 day, in America/Chicago time.')
    expect(note('Overview', 'Check-ins')).toBe('Over 1 day')
    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^180$/)
  })

  it.each([
    ['reversed', '2026-10-01', '2026-09-01', 'The start date must be on or before the end date.'],
    ['ending after today', '2026-10-01', '2026-10-06', 'The end date cannot be after today.'],
    ['wholly in the future', '2026-11-01', '2026-11-05', 'The end date cannot be after today.'],
    ['longer than 92 days', '2026-07-05', '2026-10-05', 'Choose a range of 92 days or fewer. That is the longest range available at present.'],
    ['with no start date', '', '2026-10-01', 'Enter both a start date and an end date.'],
    ['with no end date', '2026-09-01', '', 'Enter both a start date and an end date.'],
  ])('refuses a range %s, asks for nothing and keeps what is shown', async (_label, from, to, problem) => {
    const fetchMock = serve()
    renderApp(CENTRAL_REPORTS)
    await loaded()
    fetchMock.mockClear()

    applyDates(from, to)

    expect(await screen.findByRole('alert')).toHaveTextContent(problem)
    await pass(500)
    expect(reportRequests(fetchMock)).toEqual([])
    expect(shown()).toContain('Sep 6, 2026 to Oct 5, 2026: 30 days')
    expect(preset('Last 30 days')).toHaveAttribute('aria-pressed', 'true')
    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^3,140$/)
    expect(screen.getByLabelText('From')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText('To')).toHaveAttribute('aria-invalid', 'true')
  })

  it('accepts exactly 92 days, and a refused range once it is corrected', async () => {
    const fetchMock = serve()
    renderApp(CENTRAL_REPORTS)
    await loaded()

    applyDates('2026-07-05', '2026-10-05')
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    applyDates('2026-07-06', '2026-10-05')
    await loaded()

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByLabelText('From')).toHaveAttribute('aria-invalid', 'false')
    expect(shown()).toContain('92 days')
    expect(reportRequests(fetchMock).at(-1)).toMatch(/from=2026-07-06&to=2026-10-05$/)
  })

  it('a preset clears a refused custom range', async () => {
    serve()
    const person = user()
    renderApp(CENTRAL_REPORTS)
    await loaded()
    applyDates('2026-10-01', '2026-09-01')
    await screen.findByRole('alert')

    await person.click(preset('Last 7 days'))
    await loaded()

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByLabelText('From')).toHaveValue('2026-09-29')
    expect(screen.getByLabelText('To')).toHaveValue('2026-10-05')
  })

  it('says how long a range can be without calling that permanent, and keeps the browser from offering later dates', async () => {
    serve()
    renderApp(CENTRAL_REPORTS)
    await loaded()

    expect(screen.getByText(/^Up to 92 days can be shown at a time at present\./)).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/maximum|limit|never|always|only 92/i)
    expect(screen.getByLabelText('From')).toHaveAttribute('max', TO)
    expect(screen.getByLabelText('To')).toHaveAttribute('max', TO)
    expect(screen.getByLabelText('To')).toHaveAttribute('min', FROM)
    expect(screen.getByLabelText('From')).toHaveAccessibleDescription(/Up to 92 days/)
  })
})

describe('a range with little or nothing in it', () => {
  it('shows a range with no check-ins as a report, with words where a rate has no denominator', async () => {
    serve({}, report({ checkins: () => 0, rejects: () => 0 }))

    renderApp(CENTRAL_REPORTS)
    await loaded()

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByText(UNAVAILABLE)).not.toBeInTheDocument()
    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^0$/)
    expect(metric('Overview', 'Average per day')).toHaveTextContent(/^0\.0$/)
    expect(metric('Overview', 'Active days')).toHaveTextContent(/^0$/)
    expect(note('Overview', 'In transit')).toBe('Transit rate not available')
    expect(note('Overview', 'Rejects')).toBe('Reject rate not available')
    expect(metric('Overview', 'Busiest day')).toHaveTextContent('No check-ins in this range')
    expect(metric('Volume & capacity', 'Average per active day')).toHaveTextContent('Not available')
    expect(note('Volume & capacity', 'Average per active day')).toBe('No day had check-ins')
    expect(metric('Volume & capacity', 'Busiest weekday')).toHaveTextContent('No check-ins in this range')
    expect(metric('Volume & capacity', 'Busiest hour')).toHaveTextContent('No check-ins in this range')
    expect(metric('Routing', 'Transit rate')).toHaveTextContent('Not available')
    expect(note('Routing', 'Kept at Main')).toBe('No check-ins in this range')
    expect(metric('Reliability', 'Reject rate')).toHaveTextContent('Not available')
    expect(rows('Where check-ins went')).toEqual([
      ['Main (home)', '0', 'Not available'],
      ['Westside', '0', 'Not available'],
      ['Library Express', '0', 'Not available'],
    ])
    expect(screen.queryByRole('table', { name: 'Reject reasons' })).not.toBeInTheDocument()
    // No rate is written as a number, and nothing that is not a figure is written as one.
    expect(main()).not.toHaveTextContent(/\d%/)
    expect(main()).not.toHaveTextContent(NOT_A_FIGURE)
  })

  it('still draws every day and every hour, and still gives the tables', async () => {
    serve({}, report({ checkins: () => 0, rejects: () => 0 }))

    renderApp(CENTRAL_REPORTS)
    await loaded()

    expect(bars(/^Bar chart of check-ins on each day/)).toHaveLength(30)
    expect(bars(/each hour of the day$/)).toHaveLength(24)
    expect(bars(/^Bar chart of check-ins on each day/).every((bar) => bar.getAttribute('height') === '0')).toBe(true)
    expect(chartRows('Daily check-ins')).toHaveLength(30)
    expect(chartRows('Typical week')[0]).toEqual(['Monday', '5', '0', '0.0'])
    expect(chartRows('Typical day')[10]).toEqual(['10 AM', '0', '0.0'])
  })

  it('shows check-ins with no rejects as a 0.0% reject rate, and says there were none', async () => {
    serve({}, report({ rejects: () => 0 }))

    renderApp(CENTRAL_REPORTS)
    await loaded()

    expect(metric('Reliability', 'Rejects')).toHaveTextContent(/^0$/)
    expect(metric('Reliability', 'Reject rate')).toHaveTextContent(/^0\.0%$/)
    expect(note('Overview', 'Rejects')).toBe('0.0% reject rate')
    expect(within(section('Reliability')).getAllByText('No rejects in this range.')).toHaveLength(2)
    expect(screen.queryByRole('table', { name: 'Reject reasons' })).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(NOT_A_FIGURE)
  })

  it('shows rejects with no check-ins as a count with no rate', async () => {
    serve({}, report({ checkins: () => 0, rejects: () => 3 }))

    renderApp(CENTRAL_REPORTS)
    await loaded()

    expect(metric('Reliability', 'Rejects')).toHaveTextContent(/^90$/)
    expect(metric('Reliability', 'Reject rate')).toHaveTextContent('Not available')
    expect(note('Reliability', 'Reject rate')).toBe('No check-ins in this range')
    expect(rows('Reject reasons').reduce((sum, row) => sum + Number(row[1]), 0)).toBe(90)
  })
})

describe('destinations', () => {
  it('shows whatever destinations the sorter has, however many, in the order given', async () => {
    const transit: ReportFixture['transit'] = [
      ['harbor', 'Harbor Annex', 0.02],
      ['bookmobile', 'Bookmobile', 0.2],
      ['north_point', 'North Point', 0.1],
      ['archive', 'County Archive', 0],
    ]
    serve({}, report({ transit }))

    renderApp(CENTRAL_REPORTS)
    await loaded()

    // In the API's order: not by size, and not by name.
    expect(rows('Where check-ins went').map((row) => row[0])).toEqual(['Main (home)', 'Harbor Annex', 'Bookmobile', 'North Point', 'County Archive'])
    expect(rows('Where check-ins went')[4]).toEqual(['County Archive', '0', '0.0%'])
    chartRows('Daily transit')
    expect(columns('Daily transit')).toEqual(['Date', 'Total transit', 'Harbor Annex', 'Bookmobile', 'North Point', 'County Archive'])
    expect(main()).not.toHaveTextContent(/Westside|Library Express/)
  })

  it('says so when the sorter has none, and leaves out the transit chart', async () => {
    serve({}, report({ transit: [] }))

    renderApp(CENTRAL_REPORTS)
    await loaded()

    expect(screen.getByText('No transit destinations are configured for this sorter.')).toBeInTheDocument()
    expect(rows('Where check-ins went')).toEqual([['Main (home)', '3,140', '100.0%']])
    expect(metric('Routing', 'Total transit')).toHaveTextContent(/^0$/)
    expect(metric('Routing', 'Transit rate')).toHaveTextContent(/^0\.0%$/)
    expect(screen.queryByRole('img', { name: /transit destinations/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Daily transit' })).not.toBeInTheDocument()
  })

  it('shows "Other" only when something was routed neither home nor to a destination', async () => {
    serve({}, report({ other: 0.25 }))

    renderApp(CENTRAL_REPORTS)
    await loaded()

    // 25% other, 10% and 5% in transit: 60% stayed home.
    expect(rows('Where check-ins went')).toEqual([
      ['Main (home)', '1,884', '60.0%'],
      ['Westside', '314', '10.0%'],
      ['Library Express', '157', '5.0%'],
      ['Other', '785', '25.0%'],
    ])
    expect(metric('Routing', 'Other routing')).toHaveTextContent(/^785$/)
    expect(note('Routing', 'Other routing')).toBe('25.0% of check-ins')
    // "Other" is not transit: the transit figures are the two destinations alone.
    expect(metric('Routing', 'Total transit')).toHaveTextContent(/^471$/)
    expect(metric('Routing', 'Transit rate')).toHaveTextContent(/^15\.0%$/)
  })
})

describe('when a report cannot be loaded', () => {
  it.each(KINDS)('a failed %s report says so in its own section and leaves the other three alone', async (kind, route, heading) => {
    serve({ [route]: SERVER_ERROR })

    renderApp(CENTRAL_REPORTS)
    const failed = await screen.findByRole('region', { name: heading })
    await waitFor(() => expect(main()).not.toHaveTextContent('Loading…'))

    expect(within(failed).getByText('Could not load.')).toBeInTheDocument()
    expect(within(failed).getByRole('button', { name: `Try again: ${heading}` })).toBeInTheDocument()
    expect(within(failed).queryByRole('img')).not.toBeInTheDocument()
    expect(within(failed).queryByText('Internal server error.')).not.toBeInTheDocument()
    expect(screen.getAllByText('Could not load.')).toHaveLength(1)
    for (const [other, , otherHeading] of KINDS) {
      if (other !== kind) {
        expect(within(section(otherHeading)).getAllByRole('img').length).toBeGreaterThan(0)
      }
    }
    // The range, and its controls, are still there.
    expect(preset('Last 30 days')).toHaveAttribute('aria-pressed', 'true')
    expect(shown()).toContain('30 days')
  })

  it.each([
    ['a dead network', () => Promise.reject(networkFailure())],
    ['a body that is not the report', () => jsonResponse(200, { checkin_count: 12 })],
    ['a report for another range', () => jsonResponse(200, reportBody.volume(REPORT, '2026-01-01', '2026-01-30'))],
  ])('treats %s the same way, and shows none of what was sent', async (_label, reply) => {
    serve({ [VOLUME]: reply as () => Response })

    renderApp(CENTRAL_REPORTS)
    await screen.findByRole('button', { name: 'Try again: Volume & capacity' })

    expect(within(section('Volume & capacity')).queryByRole('img')).not.toBeInTheDocument()
    expect(within(section('Volume & capacity')).queryByText('Check-ins')).not.toBeInTheDocument()
    await waitFor(() => expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^3,140$/))
  })

  it('tries only that report again, keeps the button in place meanwhile, then moves focus to the section', async () => {
    const again = deferred<Response>()
    const fetchMock = serve({ [VOLUME]: inTurn(SERVER_ERROR, () => again.promise) })
    const person = user()
    renderApp(CENTRAL_REPORTS)
    const retry = await screen.findByRole('button', { name: 'Try again: Volume & capacity' })
    await waitFor(() => expect(main()).not.toHaveTextContent('Loading…'))
    fetchMock.mockClear()

    await person.click(retry)

    // Still there, still focused, and saying it is busy rather than refusing focus.
    expect(retry).toBeInTheDocument()
    expect(retry).toHaveFocus()
    expect(retry).toHaveAttribute('aria-disabled', 'true')
    expect(retry).not.toBeDisabled()
    expect(within(section('Volume & capacity')).getByText('Trying again…')).toBeInTheDocument()
    expect(screen.queryByText('Could not load.')).not.toBeInTheDocument()
    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^3,140$/)

    // Pressing it again while it is trying asks for nothing more.
    await person.click(retry)
    await person.keyboard('{Enter}')
    await pass(200)
    expect(reportRequests(fetchMock)).toEqual([`volume?${DEFAULT_RANGE}`])

    again.resolve(jsonResponse(200, reportBody.volume(REPORT, FROM, TO)))
    await waitFor(() => expect(metric('Volume & capacity', 'Busiest weekday')).toHaveTextContent('Friday'))
    expect(screen.queryByRole('button', { name: /^Try again/ })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 4, name: 'Volume & capacity' })).toHaveFocus()
    expect(reportRequests(fetchMock)).toEqual([`volume?${DEFAULT_RANGE}`])
  })

  it('says so again when trying again fails, and can be tried once more', async () => {
    const fetchMock = serve({ [ROUTING]: inTurn(SERVER_ERROR, SERVER_ERROR, answer('routing')) })
    const person = user()
    renderApp(CENTRAL_REPORTS)

    await person.click(await screen.findByRole('button', { name: 'Try again: Routing' }))
    await waitFor(() => expect(requestsFor(fetchMock, 'routing')).toHaveLength(2))
    const retry = await screen.findByRole('button', { name: 'Try again: Routing' })
    await waitFor(() => expect(retry).toHaveAttribute('aria-disabled', 'false'))
    expect(within(section('Routing')).getByText('Could not load.')).toBeInTheDocument()
    expect(retry).toHaveFocus()

    await person.click(retry)

    await waitFor(() => expect(metric('Routing', 'Total transit')).toHaveTextContent(/^471$/))
    expect(requestsFor(fetchMock, 'routing')).toHaveLength(3)
  })

  it('shows all four as failed, each with its own way to try again, when all four fail', async () => {
    serve({ [OVERVIEW]: SERVER_ERROR, [VOLUME]: SERVER_ERROR, [ROUTING]: SERVER_ERROR, [RELIABILITY]: SERVER_ERROR })

    renderApp(CENTRAL_REPORTS)

    await waitFor(() => expect(screen.getAllByText('Could not load.')).toHaveLength(4))
    expect(screen.getAllByRole('button', { name: /^Try again: / }).map((button) => button.textContent)).toEqual(
      KINDS.map(([, , heading]) => `Try again: ${heading}`),
    )
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(preset('Last 7 days')).toBeInTheDocument()
  })

  it('a failed section does not outlast its range', async () => {
    serve({ [VOLUME]: (url) => (url.includes(DEFAULT_RANGE) ? SERVER_ERROR() : answer('volume')(url)) })
    const person = user()
    renderApp(CENTRAL_REPORTS)
    await screen.findByRole('button', { name: 'Try again: Volume & capacity' })

    await person.click(preset('Last 7 days'))
    await loaded()

    expect(screen.queryByText('Could not load.')).not.toBeInTheDocument()
    expect(metric('Volume & capacity', 'Check-ins')).toHaveTextContent(/^760$/)
  })

  it('offers the whole page again when the product’s day could not be read', async () => {
    const fetchMock = serve({ [PIPELINE]: inTurn(SERVER_ERROR, () => jsonResponse(200, { timezone: 'America/Chicago', state: 'ok', last_reported_at: null })) })
    const person = user()

    renderApp(CENTRAL_REPORTS)

    expect(await screen.findByRole('alert')).toHaveTextContent('Internal server error.')
    expect(reportRequests(fetchMock)).toEqual([])
    expect(screen.queryByRole('button', { name: 'Last 30 days' })).not.toBeInTheDocument()
    await person.click(screen.getByRole('button', { name: 'Try again' }))
    await loaded()
    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^3,140$/)
  })
})

describe('a sorter with nothing to report on (404)', () => {
  it('says reports are not available when pipeline status is answered 404, and asks for no report', async () => {
    const fetchMock = serve({ [PIPELINE]: () => jsonResponse(404, TENANT_NOT_FOUND) })

    renderApp(CENTRAL_REPORTS)

    expect(await screen.findByText(UNAVAILABLE)).toHaveRole('note')
    expect(reportRequests(fetchMock)).toEqual([])
    expect(screen.getByRole('heading', { level: 2, name: 'Central Library AMH' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'Reports' })).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/Page not found|Organization or branch not found/)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Try again/ })).not.toBeInTheDocument()
  })

  it.each(KINDS)('says so in place of all four sections when the %s report is answered 404', async (_kind, route) => {
    serve({ [route]: () => jsonResponse(404, TENANT_NOT_FOUND) })

    renderApp(CENTRAL_REPORTS)

    expect(await screen.findByText(UNAVAILABLE)).toHaveRole('note')
    await pass(200)
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/3,140|Could not load|Organization or branch not found/)
    // The way back to Live Today, and the range, are still offered.
    expect(screen.getByRole('link', { name: 'Live Today' })).toBeInTheDocument()
    expect(preset('Last 30 days')).toBeInTheDocument()
  })
})

describe('an expired session (401)', () => {
  it('returns to the sign-in form at the same address when pipeline status is answered 401', async () => {
    const fetchMock = serve({ [PIPELINE]: () => jsonResponse(401, NOT_AUTHENTICATED) })

    renderApp(CENTRAL_REPORTS, { retries: true })

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByText('alice@example.test')).not.toBeInTheDocument()
    expect(screen.getByTestId('address')).toHaveTextContent(CENTRAL_REPORTS)
    expect(reportRequests(fetchMock)).toEqual([])
  })

  it.each(KINDS)('returns to the sign-in form when the %s report is answered 401, and asks it once', async (kind, route) => {
    const fetchMock = serve({ [route]: () => jsonResponse(401, NOT_AUTHENTICATED) })

    renderApp(CENTRAL_REPORTS, { retries: true })

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
    expect(screen.queryByText('3,140')).not.toBeInTheDocument()
    expect(screen.getByTestId('address')).toHaveTextContent(CENTRAL_REPORTS)
    await pass(500)
    expect(requestsFor(fetchMock, kind)).toHaveLength(1)
  })

  it('returns to the sign-in form when a later range is answered 401', async () => {
    serve({ [OVERVIEW]: (url) => (url.includes(DEFAULT_RANGE) ? answer('overview')(url) : jsonResponse(401, NOT_AUTHENTICATED)) })
    const person = user()
    renderApp(CENTRAL_REPORTS)
    await loaded()

    await person.click(preset('Last 7 days'))

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByText('3,140')).not.toBeInTheDocument()
  })

  it('shows the next person nothing that was loaded before they signed in', async () => {
    const later = deferred<Response>()
    serve({
      [OVERVIEW]: inTurn(answer('overview'), () => jsonResponse(401, NOT_AUTHENTICATED), () => later.promise),
      'POST /api/auth/login': () => jsonResponse(200, { id: 8, email: 'bob@example.test', full_name: 'Bob Example' }),
    })
    const person = user()
    renderApp(CENTRAL_REPORTS)
    await loaded()
    await person.click(preset('Last 7 days'))
    await screen.findByRole('button', { name: 'Sign in' })

    await person.type(screen.getByLabelText('Email'), 'bob@example.test')
    await person.type(screen.getByLabelText('Password'), 'pw{Enter}')

    const overview = await screen.findByRole('region', { name: 'Overview' })
    expect(within(overview).getByText('Loading…')).toBeInTheDocument()
    expect(overview).not.toHaveTextContent(/3,140|760/)
    // Back on the default range: the last person's choice of range went with them.
    expect(preset('Last 30 days')).toHaveAttribute('aria-pressed', 'true')
  })
})

describe('nothing from one question is shown under another', () => {
  it('shows nothing of the old range while the new one loads', async () => {
    const week = deferred<Response>()
    serve({ [OVERVIEW]: (url) => (url.includes(DEFAULT_RANGE) ? answer('overview')(url) : week.promise) })
    const person = user()
    renderApp(CENTRAL_REPORTS)
    await loaded()
    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^3,140$/)

    await person.click(preset('Last 7 days'))

    // The line that says what is shown has changed, so nothing of the 30 days may still be under it.
    expect(shown()).toContain('7 days')
    const overview = section('Overview')
    expect(within(overview).getByText('Loading…')).toBeInTheDocument()
    expect(overview).not.toHaveTextContent('3,140')
    expect(within(overview).queryByRole('img')).not.toBeInTheDocument()
    await waitFor(() => expect(metric('Volume & capacity', 'Check-ins')).toHaveTextContent(/^760$/))
    expect(main()).not.toHaveTextContent('3,140')

    week.resolve(jsonResponse(200, reportBody.overview(REPORT, '2026-09-29', '2026-10-05')))
    await waitFor(() => expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^760$/))
  })

  it('ignores an answer for a range that has since been left', async () => {
    const month = deferred<Response>()
    serve({ [OVERVIEW]: (url) => (url.includes(DEFAULT_RANGE) ? month.promise : answer('overview')(url)) })
    const person = user()
    renderApp(CENTRAL_REPORTS)
    await screen.findByRole('img', { name: /^Bar chart of rejects/ })

    await person.click(preset('Last 7 days'))
    await waitFor(() => expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^760$/))
    month.resolve(jsonResponse(200, reportBody.overview(REPORT, FROM, TO)))
    await pass(500)

    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^760$/)
    expect(main()).not.toHaveTextContent('3,140')
  })

  it('asks again on returning to a range, and shows nothing kept from before', async () => {
    const second = deferred<Response>()
    const fetchMock = serve({
      [OVERVIEW]: (url) => (url.includes(DEFAULT_RANGE) && requestsFor(fetchMock, 'overview').length > 2 ? second.promise : answer('overview')(url)),
    })
    const person = user()
    renderApp(CENTRAL_REPORTS)
    await loaded()
    await person.click(preset('Last 7 days'))
    await loaded()

    await person.click(preset('Last 30 days'))

    expect(within(section('Overview')).getByText('Loading…')).toBeInTheDocument()
    expect(section('Overview')).not.toHaveTextContent(/3,140|760/)
    expect(requestsFor(fetchMock, 'overview')).toEqual([
      `overview?${DEFAULT_RANGE}`,
      'overview?from=2026-09-29&to=2026-10-05',
      `overview?${DEFAULT_RANGE}`,
    ])
    second.resolve(jsonResponse(200, reportBody.overview(REPORT, FROM, TO)))
    await waitFor(() => expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^3,140$/))
  })

  it('shows nothing of one sorter under another, and starts the other on the default range', async () => {
    const east = deferred<Response>()
    const eastApi = livePath('northbridge', 'east-side')
    const fetchMock = serve({ [`GET ${eastApi}/reports/overview?from=*&to=*`]: () => east.promise })
    const person = user()
    renderApp(CENTRAL_REPORTS)
    await loaded()
    await person.click(preset('Last 7 days'))
    await loaded()

    await person.click(screen.getByRole('link', { name: 'Northbridge Library' }))
    await person.click(await screen.findByRole('link', { name: /East Side AMH/ }))
    await person.click(await screen.findByRole('link', { name: 'Reports' }))

    expect(await screen.findByRole('heading', { level: 2, name: 'East Side AMH' })).toBeInTheDocument()
    await waitFor(() => expect(metric('Volume & capacity', 'Check-ins')).toHaveTextContent(/^210$/))
    expect(preset('Last 30 days')).toHaveAttribute('aria-pressed', 'true')
    expect(shown()).toContain('Sep 6, 2026 to Oct 5, 2026: 30 days')
    expect(within(section('Overview')).getByText('Loading…')).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/3,140|760|Westside|Library Express|Central/)
    expect(requestedUrls(fetchMock).filter((url) => url.includes('east-side/reports/')).every((url) => url.endsWith(DEFAULT_RANGE))).toBe(true)

    east.resolve(jsonResponse(200, reportBody.overview(EAST, FROM, TO)))
    await waitFor(() => expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^210$/))
    expect(screen.getByText('No transit destinations are configured for this sorter.')).toBeInTheDocument()
    expect(rows('Where check-ins went')).toEqual([['East Side (home)', '210', '100.0%']])
  })

  it('asks again on coming back from Live Today, on the default range', async () => {
    const fetchMock = serve()
    const person = user()
    renderApp(CENTRAL_REPORTS)
    await loaded()
    await person.click(preset('Last 7 days'))
    await loaded()

    await person.click(screen.getByRole('link', { name: 'Live Today' }))
    await screen.findByRole('img', { name: /^Bar chart of check-ins in each hour/ })
    fetchMock.mockClear()
    await person.click(screen.getByRole('link', { name: 'Reports' }))
    await loaded()

    expect(preset('Last 30 days')).toHaveAttribute('aria-pressed', 'true')
    expect(new Set(reportRequests(fetchMock))).toEqual(new Set(KINDS.map(([kind]) => `${kind}?${DEFAULT_RANGE}`)))
  })
})

describe('reading the reports without seeing or pointing', () => {
  beforeEach(async () => {
    serve()
    renderApp(CENTRAL_REPORTS)
    await loaded()
  })

  it('names every chart and describes it with the sentence beside it', () => {
    const charts = screen.getAllByRole('img')

    expect(charts.map((chart) => chart.getAttribute('aria-label'))).toEqual([
      'Bar chart of check-ins on each day of the range',
      'Bar chart of average check-ins for each day of the week',
      'Bar chart of average check-ins in each hour of the day',
      'Bar chart of check-ins sent to transit destinations on each day of the range',
      'Bar chart of rejects on each day of the range',
    ])
    for (const chart of charts) {
      const summary = document.getElementById(chart.getAttribute('aria-describedby') ?? '')
      expect(summary?.textContent?.length).toBeGreaterThan(20)
      // The drawing itself offers nothing to focus on or hover over.
      expect(chart.querySelector('[tabindex], a, button, title')).toBeNull()
      expect(chart.querySelector('svg')).toHaveAttribute('aria-hidden', 'true')
    }
  })

  it('gives every chart a table of the same figures, opened and closed by a button that says which', async () => {
    const person = user()
    const toggles = screen.getAllByRole('button', { name: /^Show table: / })
    expect(toggles.map((toggle) => toggle.textContent)).toEqual([
      'Show table: Daily check-ins',
      'Show table: Typical week',
      'Show table: Typical day',
      'Show table: Daily transit',
      'Show table: Daily rejects',
    ])
    const toggle = toggles[1]
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('table', { name: 'Typical week' })).not.toBeInTheDocument()

    toggle.focus()
    await person.keyboard('{Enter}')

    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(toggle).toHaveTextContent('Hide table: Typical week')
    expect(toggle).toHaveFocus()
    const table = screen.getByRole('table', { name: 'Typical week' })
    expect(document.getElementById(toggle.getAttribute('aria-controls') ?? '')).toContainElement(table)
    expect(within(table).getAllByRole('rowheader')).toHaveLength(7)
    expect(within(table).getAllByRole('columnheader')).toHaveLength(4)

    await person.keyboard(' ')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('table', { name: 'Typical week' })).not.toBeInTheDocument()
  })

  it('labels the range controls', () => {
    const presets = screen.getByRole('group', { name: 'Date range presets' })
    expect(within(presets).getAllByRole('button').map((button) => button.textContent)).toEqual(['Last 7 days', 'Last 30 days', 'Last 90 days'])
    const custom = screen.getByRole('form', { name: 'Custom date range' })
    expect(within(custom).getByLabelText('From')).toHaveAttribute('type', 'date')
    expect(within(custom).getByLabelText('To')).toHaveAttribute('type', 'date')
    expect(within(custom).getByRole('button', { name: 'Apply dates' })).toHaveAttribute('type', 'submit')
  })

  it('applies a custom range from the keyboard', async () => {
    const person = user()
    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-09-01' } })
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2026-09-14' } })

    screen.getByLabelText('To').focus()
    await person.keyboard('{Enter}')
    await loaded()

    expect(shown()).toContain('Sep 1, 2026 to Sep 14, 2026: 14 days')
  })

  it('names each section as a region, and each data table by its heading', () => {
    expect(screen.getAllByRole('region').map((region) => region.getAttribute('aria-labelledby'))).toEqual([
      'report-overview-heading',
      'report-volume-heading',
      'report-routing-heading',
      'report-reliability-heading',
    ])
    expect(screen.getByRole('table', { name: 'Where check-ins went' })).toBeInTheDocument()
    expect(screen.getByRole('table', { name: 'Reject reasons' })).toBeInTheDocument()
    expect(within(screen.getByRole('table', { name: 'Where check-ins went' })).getAllByRole('rowheader')).toHaveLength(3)
  })

  it('has no element that depends on a pointer, and disables nothing', () => {
    expect(main().querySelectorAll('[title]')).toHaveLength(0)
    expect(main().querySelectorAll('[disabled]')).toHaveLength(0)
    expect(main().querySelectorAll('[tabindex]:not([tabindex="-1"])')).toHaveLength(0)
  })
})
