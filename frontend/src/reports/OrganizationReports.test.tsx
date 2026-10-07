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
  networkFailure,
  NORTHBRIDGE,
  NORTHBRIDGE_DETAIL,
  NOT_AUTHENTICATED,
  ORGANIZATION_NOT_FOUND,
  organizationReportBody,
  organizationReportRoutes,
  organizationReportsPath,
  type OrganizationSorterFixture,
  REPORT,
  type ReportFixture,
  reportRoutes,
  requestedUrls,
  RIVERSIDE,
  RIVERSIDE_DETAIL,
  serveApi,
} from '../test/http.ts'
import { renderApp } from '../test/render.tsx'

const ORGANIZATION = '/organizations/northbridge'
const REPORTS = `${ORGANIZATION}/reports`
const API = organizationReportsPath('northbridge')
const OVERVIEW = `GET ${API}/overview?from=*&to=*`
const NETWORK = `GET ${API}/routing-network?from=*&to=*`
const RELIABILITY = `GET ${API}/reliability?from=*&to=*`
const CENTRAL_PIPELINE = `GET ${livePath('northbridge', 'central')}/pipeline-status`
const KINDS = [
  ['overview', OVERVIEW, ['Overview', 'Sorter comparison']],
  ['routing-network', NETWORK, ['Routing network']],
  ['reliability', RELIABILITY, ['System reliability']],
] as const
const SECTIONS = ['Overview', 'Sorter comparison', 'Routing network', 'System reliability']

// 1:50 PM on Monday 5 October 2026 in Chicago (CDT, UTC-5): the fixtures' "today".
const NOW = '2026-10-05T18:50:00Z'
const DEFAULT_RANGE = 'from=2026-09-06&to=2026-10-05'
// Monday to Sunday, ending before today.
const WEEK: [string, string] = ['2026-09-28', '2026-10-04']

const SERVER_ERROR = () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' })
const NO_SORTERS = 'No sorting machines are registered for this organization yet.'
const NOT_FOUND_TEXT = 'This page does not exist, or you do not have access to it.'
const NOT_A_FIGURE = /NaN|Infinity|undefined|null|\[object/
const MISSING = '—Not available'

const report = (changes: Partial<ReportFixture>): ReportFixture => ({ ...REPORT, ...changes })
/**
 * Central: 760 check-ins and 38 rejects in the week (5.0%); a tenth go to a destination that happens to be called
 * what the other sorter is called, a twentieth to Westside.
 */
const CENTRAL = report({ home: 'Central', transit: [['east_side', 'East Side AMH', 0.1], ['westside', 'Westside', 0.05]] })
/** East Side: 50 check-ins and 5 rejects every day (10.0%); a fifth go to Westside, a tenth to Library Express. */
const EAST = report({ checkins: () => 50, rejects: () => 5, home: 'East Side', transit: [['westside', 'Westside', 0.2], ['library_express', 'Library Express', 0.1]] })
const QUIET = report({ checkins: () => 0, rejects: () => 0 })

/** Northbridge's two sorters, reporting `central` and `east` -- or, for null, registered with nothing to read. */
const sorters = (central: ReportFixture | null = CENTRAL, east: ReportFixture | null = EAST): OrganizationSorterFixture[] => [
  { ...NORTHBRIDGE_DETAIL.sorters[0], report: central },
  { ...NORTHBRIDGE_DETAIL.sorters[1], report: east },
]
const RIVERSIDE_SORTERS: OrganizationSorterFixture[] = [{ ...RIVERSIDE_DETAIL.sorters[0], report: REPORT }]

/** A signed-in user at an organization whose reports answer for `fixture`, unless a test replaces a route. */
function serve(overrides: ApiRoutes = {}, fixture: OrganizationSorterFixture[] = sorters()): FetchMock {
  return serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    'GET /api/organizations': () => jsonResponse(200, [NORTHBRIDGE, RIVERSIDE]),
    'GET /api/organizations/northbridge': () => jsonResponse(200, NORTHBRIDGE_DETAIL),
    'GET /api/organizations/riverside': () => jsonResponse(200, RIVERSIDE_DETAIL),
    // A sorter's own reports, for the tests that follow a link to them.
    ...reportRoutes('northbridge', 'central'),
    ...organizationReportRoutes('northbridge', fixture),
    ...organizationReportRoutes('riverside', RIVERSIDE_SORTERS),
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
const answer = (kind: keyof typeof organizationReportBody, fixture = sorters()) => (url: string) =>
  jsonResponse(200, organizationReportBody[kind](fixture, ...rangeIn(url)))

const main = () => screen.getByRole('main')
/** Each organization report request made, as "kind?from=..&to=..". */
const reportRequests = (fetchMock: FetchMock, orgSlug = 'northbridge') =>
  requestedUrls(fetchMock)
    .filter((url) => url.startsWith(`${organizationReportsPath(orgSlug)}/`))
    .map((url) => url.split('/reports/')[1])
const pipelineRequests = (fetchMock: FetchMock) => requestedUrls(fetchMock).filter((url) => url.endsWith('/pipeline-status'))

const section = (name: string) => screen.getByRole('region', { name })
function metric(sectionName: string, label: string): HTMLElement {
  return within(section(sectionName)).getByText(label, { selector: 'dt' }).nextElementSibling as HTMLElement
}
function note(sectionName: string, label: string): string | null {
  return metric(sectionName, label).nextElementSibling?.textContent ?? null
}
const table = (name: string) => screen.getByRole('table', { name })
/** A table's body rows, cell by cell. */
function rows(name: string): string[][] {
  return within(table(name))
    .getAllByRole('row')
    .slice(1)
    .map((row) => Array.from(row.children).map((cell) => cell.textContent ?? ''))
}
const columns = (name: string) =>
  within(table(name))
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
function applyDates(from: string, to: string) {
  fireEvent.change(screen.getByLabelText('From'), { target: { value: from } })
  fireEvent.change(screen.getByLabelText('To'), { target: { value: to } })
  fireEvent.click(screen.getByRole('button', { name: 'Apply dates' }))
}
/** Waits until every section shows its content. */
async function loaded() {
  await screen.findByRole('region', { name: 'System reliability' })
  await waitFor(() => {
    expect(main()).not.toHaveTextContent('Loading…')
    expect(screen.queryByText('Loading reports…')).not.toBeInTheDocument()
  })
}
/** The organization's reports for the week, loaded. */
async function week(path = REPORTS) {
  renderApp(path)
  await loaded()
  applyDates(...WEEK)
  await loaded()
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'], shouldAdvanceTime: true })
  vi.setSystemTime(new Date(NOW))
})

afterEach(() => {
  vi.useRealTimers()
})

describe('arriving at an organization’s reports', () => {
  it('is reached from the organization by one link, above its sorting machines', async () => {
    serve()
    const person = user()

    renderApp(ORGANIZATION)
    const link = await screen.findByRole('link', { name: 'Organization Reports' })
    expect(link).toHaveAttribute('href', REPORTS)
    // Not one of the machines: the list of sorting machines is still exactly the machines.
    expect(within(screen.getByRole('list', { name: 'Sorting machines' })).getAllByRole('link').map((machine) => machine.textContent)).toEqual([
      'Central Library AMH',
      'East Side AMH',
    ])
    await person.click(link)

    expect(screen.getByTestId('address')).toHaveTextContent(REPORTS)
    await loaded()
    expect(screen.getByRole('heading', { level: 2, name: 'Northbridge Library' })).toHaveFocus()
    expect(screen.getByRole('heading', { level: 3, name: 'Organization Reports' })).toBeInTheDocument()
  })

  it('has a breadcrumb of the organization list, the organization, then Reports', async () => {
    serve()

    renderApp(REPORTS)
    await loaded()

    const breadcrumb = within(screen.getByRole('navigation', { name: 'Breadcrumb' }))
    expect(breadcrumb.getAllByRole('listitem').map((step) => step.textContent)).toEqual(['Organizations', 'Northbridge Library', 'Reports'])
    expect(breadcrumb.getByRole('link', { name: 'Organizations' })).toHaveAttribute('href', '/organizations')
    expect(breadcrumb.getByRole('link', { name: 'Northbridge Library' })).toHaveAttribute('href', ORGANIZATION)
    expect(breadcrumb.getByText('Reports')).toHaveAttribute('aria-current', 'page')
    // It is the organization's page: no sorter's views are offered on it. ("Account" is the header's own.)
    expect(screen.getAllByRole('navigation').map((nav) => nav.getAttribute('aria-label'))).toEqual(['Account', 'Breadcrumb'])
  })

  it('reads the product’s day from a sorter first, then asks for the three reports over the last 30 days', async () => {
    const pipeline = deferred<Response>()
    const fetchMock = serve({ [CENTRAL_PIPELINE]: () => pipeline.promise })

    renderApp(REPORTS)

    expect(await screen.findByText('Loading reports…')).toHaveRole('status')
    await pass(1000)
    expect(reportRequests(fetchMock)).toEqual([])
    expect(screen.queryByRole('button', { name: 'Last 30 days' })).not.toBeInTheDocument()

    pipeline.resolve(jsonResponse(200, { timezone: 'America/Chicago', state: 'ok', last_reported_at: null }))
    await loaded()
    expect(reportRequests(fetchMock).sort()).toEqual([`overview?${DEFAULT_RANGE}`, `reliability?${DEFAULT_RANGE}`, `routing-network?${DEFAULT_RANGE}`])
    // One sorter answered: no other is asked what day it is.
    expect(pipelineRequests(fetchMock)).toEqual([`${livePath('northbridge', 'central')}/pipeline-status`])
  })

  it('opens on the last 30 days, ending today, and says today is not over', async () => {
    serve()

    renderApp(REPORTS)
    await loaded()

    expect(preset('Last 30 days')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText('From')).toHaveValue('2026-09-06')
    expect(screen.getByLabelText('To')).toHaveValue('2026-10-05')
    expect(shown()).toBe(
      'Showing Sep 6, 2026 to Oct 5, 2026: 30 days, in America/Chicago time. This range includes today, which is not over yet: its figures will still rise.',
    )
  })

  it('takes today from the product’s zone, not this machine’s or UTC', async () => {
    // 10:30 PM on 5 October in Chicago. In UTC -- and anywhere east of it -- it is already the 6th.
    vi.setSystemTime(new Date('2026-10-06T03:30:00Z'))
    const fetchMock = serve()

    renderApp(REPORTS)
    await loaded()

    expect(new Set(reportRequests(fetchMock).map((request) => request.split('?')[1]))).toEqual(new Set([DEFAULT_RANGE]))
    expect(screen.getByLabelText('To')).toHaveAttribute('max', '2026-10-05')
  })

  it('has four sections, in order, each under the organization', async () => {
    serve()

    renderApp(REPORTS)
    await loaded()

    expect(screen.getAllByRole('heading').map((heading) => `${heading.tagName} ${heading.textContent}`)).toEqual([
      'H1 SortView',
      'H2 Northbridge Library',
      'H3 Organization Reports',
      'H4 Overview',
      'H5 Daily check-ins',
      'H4 Sorter comparison',
      'H4 Routing network',
      'H5 Routing by sorter',
      'H5 Destination totals',
      'H5 Routing matrix',
      'H4 System reliability',
      'H5 Daily rejects',
      'H5 Reject reasons',
      'H5 Rejects by sorter',
    ])
    expect(screen.getAllByRole('region').map((region) => region.getAttribute('aria-labelledby'))).toEqual([
      'org-overview-heading',
      'org-comparison-heading',
      'org-routing-heading',
      'org-reliability-heading',
    ])
    expect(main()).not.toHaveTextContent(/Volume & capacity|Typical week|Busiest hour/)
  })

  it('shows a suspended organization’s reports as usual, with the notice', async () => {
    serve()

    renderApp('/organizations/riverside/reports')
    await loaded()

    expect(screen.getByRole('note')).toHaveTextContent('currently suspended')
    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^3,140$/)
  })
})

describe('choosing a range', () => {
  it('a preset applies at once: new requests for all three reports, and new figures', async () => {
    const fetchMock = serve()
    const person = user()
    renderApp(REPORTS)
    await loaded()

    await person.click(preset('Last 7 days'))
    await loaded()

    expect(preset('Last 7 days')).toHaveAttribute('aria-pressed', 'true')
    expect(preset('Last 30 days')).toHaveAttribute('aria-pressed', 'false')
    const range = 'from=2026-09-29&to=2026-10-05'
    expect(reportRequests(fetchMock).slice(3).sort()).toEqual([`overview?${range}`, `reliability?${range}`, `routing-network?${range}`])
    expect(note('Overview', 'Check-ins')).toBe('Over 7 days')

    await person.click(preset('Last 90 days'))
    await loaded()
    expect(shown()).toContain('Jul 8, 2026 to Oct 5, 2026: 90 days')
    expect(bars(/organization's check-ins/)).toHaveLength(90)
  })

  it('a custom range applies when submitted; one that ends before today is not called partial', async () => {
    const fetchMock = serve()
    await week()

    expect(shown()).toBe('Showing Sep 28, 2026 to Oct 4, 2026: 7 days, in America/Chicago time.')
    expect(preset('Last 30 days')).toHaveAttribute('aria-pressed', 'false')
    expect(reportRequests(fetchMock).slice(3).sort()).toEqual([
      'overview?from=2026-09-28&to=2026-10-04',
      'reliability?from=2026-09-28&to=2026-10-04',
      'routing-network?from=2026-09-28&to=2026-10-04',
    ])
  })

  it.each([
    ['', '2026-10-04', 'Enter both a start date and an end date.'],
    ['2026-10-04', '2026-09-28', 'The start date must be on or before the end date.'],
    ['2026-09-28', '2026-10-06', 'The end date cannot be after today.'],
    ['2026-07-05', '2026-10-05', 'Choose a range of 92 days or fewer. That is the longest range available at present.'],
  ])('refuses the range %j to %j, asks nothing, and keeps the figures it had', async (from, to, problem) => {
    const fetchMock = serve()
    renderApp(REPORTS)
    await loaded()

    applyDates(from, to)

    expect(screen.getByRole('alert')).toHaveTextContent(problem)
    expect(reportRequests(fetchMock)).toHaveLength(3)
    expect(shown()).toContain('30 days')
    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^4,640$/)
  })

  it('accepts exactly 92 days', async () => {
    serve()
    renderApp(REPORTS)
    await loaded()

    applyDates('2026-07-06', '2026-10-05')
    await loaded()

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(shown()).toContain('92 days')
  })
})

describe('the figures of an organization with two sorters', () => {
  beforeEach(async () => {
    serve()
    await week()
  })

  it('overview: totals, and rates made from the totals -- not from the sorters’ rates', () => {
    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^1,110$/)
    expect(note('Overview', 'Check-ins')).toBe('Over 7 days')
    expect(metric('Overview', 'Average per day')).toHaveTextContent(/^159$/)
    expect(metric('Overview', 'In transit')).toHaveTextContent(/^219$/)
    expect(note('Overview', 'In transit')).toBe('19.7% of check-ins')
    expect(metric('Overview', 'Rejects')).toHaveTextContent(/^73$/)
    // 73 of 1,110. The sorters' own rates are 5.0% and 10.0%, whose average would be 7.5%.
    expect(note('Overview', 'Rejects')).toBe('6.6% reject rate')
    expect(main()).not.toHaveTextContent('7.5%')
    expect(metric('Overview', 'Sorting machines')).toHaveTextContent(/^2$/)
    expect(note('Overview', 'Sorting machines')).toBe('All with reporting data')
    expect(section('Overview')).toHaveTextContent('An item handled at two machines is counted at each.')
  })

  it('overview: a bar for every day, with the same figures -- and the rejects -- as a table', () => {
    expect(bars(/organization's check-ins/)).toHaveLength(7)
    expect(screen.getByText(/^1,110 check-ins across the organization over 7 days\. Busiest day: Oct 2, 2026, with 230\.$/)).toBeInTheDocument()
    expect(chartRows('Daily check-ins')).toEqual([
      ['Mon, Sep 28', '150', '10'],
      ['Tue, Sep 29', '170', '11'],
      ['Wed, Sep 30', '190', '12'],
      ['Thu, Oct 1', '210', '13'],
      ['Fri, Oct 2', '230', '14'],
      ['Sat, Oct 3', '110', '8'],
      ['Sun, Oct 4', '50', '5'],
    ])
    expect(columns('Daily check-ins')).toEqual(['Date', 'Check-ins', 'Rejects'])
  })

  it('sorter comparison: every sorter in the organization’s order, with its counts, its share and its own rates', () => {
    expect(columns('Sorter comparison')).toEqual([
      'Sorter',
      'Location',
      'Status',
      'Check-ins',
      'Share of check-ins',
      'In transit',
      'Transit rate',
      'Rejects',
      'Reject rate',
      'Active days',
      'Collectors',
    ])
    expect(rows('Sorter comparison')).toEqual([
      ['Central Library AMH: sorter reports', 'Central Branch', 'Active', '760', '68.5%', '114', '15.0%', '38', '5.0%', '6 of 7', '1'],
      ['East Side AMH: sorter reports', 'East Side Branch', 'Active', '350', '31.5%', '105', '30.0%', '35', '10.0%', '7 of 7', '1'],
    ])
  })

  it('sorter comparison: a sorter’s name is a link to that sorter’s own reports', async () => {
    const comparison = within(table('Sorter comparison'))
    expect(comparison.getAllByRole('link').map((link) => link.getAttribute('href'))).toEqual([
      '/organizations/northbridge/sorters/central/reports',
      '/organizations/northbridge/sorters/east-side/reports',
    ])

    await user().click(comparison.getByRole('link', { name: 'Central Library AMH: sorter reports' }))

    expect(screen.getByTestId('address')).toHaveTextContent('/organizations/northbridge/sorters/central/reports')
    expect(await screen.findByRole('heading', { level: 2, name: 'Central Library AMH' })).toBeInTheDocument()
    expect(await screen.findByRole('region', { name: 'Volume & capacity' })).toBeInTheDocument()
  })

  it('routing network: the organization’s transit, and each sorter’s home, transit and other', () => {
    expect(metric('Routing network', 'Check-ins')).toHaveTextContent(/^1,110$/)
    expect(metric('Routing network', 'Total transit')).toHaveTextContent(/^219$/)
    expect(metric('Routing network', 'Transit rate')).toHaveTextContent(/^19\.7%$/)
    expect(columns('Routing by sorter')).toEqual(['Sorter', 'Check-ins', 'Kept at home', 'In transit', 'Transit rate', 'Other routing'])
    expect(rows('Routing by sorter')).toEqual([
      ['Central Library AMH', '760', '646 (Central)', '114', '15.0%', '0'],
      ['East Side AMH', '350', '245 (East Side)', '105', '30.0%', '0'],
    ])
  })

  it('routing network: destination totals, in the order given, with how many sorters have each', () => {
    expect(columns('Destination totals')).toEqual(['Destination', 'Check-ins routed', 'Sorters with this destination'])
    expect(rows('Destination totals')).toEqual([
      ['East Side AMH', '76', '1 of 2'],
      // Both sorters route to a destination by this key: one column, their counts added up.
      ['Westside', '108', '2 of 2'],
      ['Library Express', '35', '1 of 2'],
    ])
  })

  it('routing network: a matrix of sorter by destination, with a dash where a sorter has no such destination', () => {
    expect(columns('Routing matrix')).toEqual(['From sorter', 'East Side AMH', 'Westside', 'Library Express'])
    expect(rows('Routing matrix')).toEqual([
      ['Central Library AMH', '76', '38', '—Not configured'],
      ['East Side AMH', '—Not configured', '70', '35'],
    ])
    const matrix = table('Routing matrix')
    expect(within(matrix).getAllByRole('columnheader').every((cell) => cell.getAttribute('scope') === 'col')).toBe(true)
    expect(within(matrix).getAllByRole('rowheader').map((cell) => [cell.tagName, cell.getAttribute('scope'), cell.textContent])).toEqual([
      ['TH', 'row', 'Central Library AMH'],
      ['TH', 'row', 'East Side AMH'],
    ])
    expect(matrix).toHaveAccessibleDescription(/A dash means that machine has no such destination\./)
    // "Other" is each sorter's own count, never a destination.
    expect(columns('Routing matrix')).not.toContain('Other')
  })

  it('routing network: a destination called what a sorter is called is still a destination, and links nowhere', () => {
    const routing = section('Routing network')
    expect(within(routing).getAllByText('East Side AMH').length).toBeGreaterThan(1)
    expect(within(routing).queryAllByRole('link')).toEqual([])
    expect(routing.querySelectorAll('a, button, [href]')).toHaveLength(0)
    expect(routing).toHaveTextContent('it is a routing outcome, not a sorting machine.')
    expect(routing).toHaveTextContent('Routing is shown for the sorting machines that have reporting data.')
  })

  it('system reliability: rejects against check-ins, by day, by reason and by sorter', () => {
    expect(metric('System reliability', 'Rejects')).toHaveTextContent(/^73$/)
    expect(metric('System reliability', 'Reject rate')).toHaveTextContent(/^6\.6%$/)
    expect(note('System reliability', 'Reject rate')).toBe('All rejects against all check-ins')
    expect(bars(/organization's rejects/)).toHaveLength(7)
    expect(chartRows('Daily rejects')[4]).toEqual(['Fri, Oct 2', '14', '230'])
    expect(rows('Reject reasons')).toEqual([
      ['Item not found', '41', '56.2%'],
      ['RFID collision', '32', '43.8%'],
    ])
    expect(columns('Rejects by sorter')).toEqual(['Sorter', 'Check-ins', 'Rejects', 'Reject rate', 'Most frequent reason'])
    expect(rows('Rejects by sorter')).toEqual([
      ['Central Library AMH', '760', '38', '5.0%', 'Item not found'],
      ['East Side AMH', '350', '35', '10.0%', 'Item not found'],
    ])
  })

  it('describes and concludes nothing: no advice, no cause, no ranking of problems', () => {
    expect(main()).not.toHaveTextContent(/attention|recommend|because|caused|due to|issue|likely|should|worst|ROI|efficien/i)
    expect(main()).not.toHaveTextContent(NOT_A_FIGURE)
  })
})

describe('an organization with one sorter', () => {
  it('is a whole report: the sorter is all of it, and the organization’s rates are the sorter’s', async () => {
    serve()
    await week('/organizations/riverside/reports')

    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^760$/)
    expect(note('Overview', 'Rejects')).toBe('5.0% reject rate')
    expect(metric('Overview', 'Sorting machines')).toHaveTextContent(/^1$/)
    expect(rows('Sorter comparison')).toEqual([
      ['Riverside Main AMH: sorter reports', 'Riverside Main', 'Active', '760', '100.0%', '114', '15.0%', '38', '5.0%', '6 of 7', '1'],
    ])
    expect(rows('Destination totals')).toEqual([
      ['Westside', '76', '1 of 1'],
      ['Library Express', '38', '1 of 1'],
    ])
    expect(rows('Routing matrix')).toEqual([['Riverside Main AMH', '76', '38']])
    expect(rows('Rejects by sorter')).toEqual([['Riverside Main AMH', '760', '38', '5.0%', 'Item not found']])
  })
})

describe('a sorter that is registered and has no data to read', () => {
  it('is listed as unavailable, with no figures and no link, and is never said to have done nothing', async () => {
    serve({}, sorters(CENTRAL, null))
    await week()

    expect(note('Overview', 'Sorting machines')).toBe('1 with reporting data')
    expect(rows('Sorter comparison')).toEqual([
      ['Central Library AMH: sorter reports', 'Central Branch', 'Active', '760', '100.0%', '114', '15.0%', '38', '5.0%', '6 of 7', '1'],
      ['East Side AMH', 'East Side Branch', 'Unavailable', MISSING, MISSING, MISSING, MISSING, MISSING, MISSING, MISSING, '1'],
    ])
    expect(within(table('Sorter comparison')).getAllByRole('link')).toHaveLength(1)
    expect(within(table('Sorter comparison')).queryByRole('link', { name: /East Side AMH/ })).not.toBeInTheDocument()
    expect(section('Sorter comparison')).toHaveTextContent('has no reporting data yet. That is not a count of zero')
    expect(main()).not.toHaveTextContent(/no activity|did nothing|\b0\.0%/i)
  })

  it('says so beside its status when it is also being set up', async () => {
    const detail = { ...NORTHBRIDGE_DETAIL, sorters: [NORTHBRIDGE_DETAIL.sorters[0], { ...NORTHBRIDGE_DETAIL.sorters[1], status: 'provisioning' }] }
    const fixture = [{ ...detail.sorters[0], report: CENTRAL }, { ...detail.sorters[1], report: null }]
    serve({ 'GET /api/organizations/northbridge': () => jsonResponse(200, detail) }, fixture)
    await week()

    expect(rows('Sorter comparison')[1][2]).toBe('Unavailable · Being set up')
  })

  it('is not a routing source, and is marked in the reliability comparison', async () => {
    serve({}, sorters(CENTRAL, null))
    await week()

    expect(rows('Routing by sorter').map((row) => row[0])).toEqual(['Central Library AMH'])
    expect(rows('Routing matrix').map((row) => row[0])).toEqual(['Central Library AMH'])
    expect(rows('Destination totals')).toEqual([
      ['East Side AMH', '76', '1 of 1'],
      ['Westside', '38', '1 of 1'],
    ])
    expect(section('Routing network')).toHaveTextContent('Routing is shown for the sorting machines that have reporting data.')
    expect(rows('Rejects by sorter')).toEqual([
      ['Central Library AMH', '760', '38', '5.0%', 'Item not found'],
      ['East Side AMH (Unavailable)', MISSING, MISSING, MISSING, MISSING],
    ])
    expect(section('System reliability')).toHaveTextContent('That is not a count of zero')
  })

  it('does not stop the product’s day being read: the next sorter is asked', async () => {
    const fetchMock = serve({}, sorters(null, EAST))

    renderApp(REPORTS)
    await loaded()

    expect(pipelineRequests(fetchMock)).toEqual([
      `${livePath('northbridge', 'central')}/pipeline-status`,
      `${livePath('northbridge', 'east-side')}/pipeline-status`,
    ])
    expect(rows('Sorter comparison').map((row) => row[2])).toEqual(['Unavailable', 'Active'])
  })

  it('says reports are not available when no sorter has data, and asks for no report', async () => {
    const fetchMock = serve({}, sorters(null, null))

    renderApp(REPORTS)

    expect(await screen.findByText('Reports are not available for this organization yet: none of its sorting machines has reporting data.')).toHaveRole('note')
    await pass(500)
    expect(reportRequests(fetchMock)).toEqual([])
    expect(screen.getByRole('heading', { level: 2, name: 'Northbridge Library' })).toBeInTheDocument()
    expect(screen.queryByText(NOT_FOUND_TEXT)).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/no activity/i)
  })
})

describe('an organization with little or nothing in a range', () => {
  it('shows a range with no activity as a report, with words where a rate has no denominator', async () => {
    serve({}, sorters(QUIET, QUIET))
    await week()

    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^0$/)
    expect(metric('Overview', 'Average per day')).toHaveTextContent(/^0\.0$/)
    expect(note('Overview', 'In transit')).toBe('Transit rate not available')
    expect(note('Overview', 'Rejects')).toBe('Reject rate not available')
    expect(screen.getAllByText('No processing activity in this range.').length).toBeGreaterThan(0)
    expect(rows('Sorter comparison')).toEqual([
      ['Central Library AMH: sorter reports', 'Central Branch', 'Active', '0', 'Not available', '0', 'Not available', '0', 'Not available', '0 of 7', '1'],
      ['East Side AMH: sorter reports', 'East Side Branch', 'Active', '0', 'Not available', '0', 'Not available', '0', 'Not available', '0 of 7', '1'],
    ])
    expect(metric('Routing network', 'Transit rate')).toHaveTextContent(/^Not available$/)
    expect(metric('System reliability', 'Reject rate')).toHaveTextContent(/^Not available$/)
    expect(within(section('System reliability')).getAllByText('No rejects in this range.').length).toBeGreaterThan(0)
    expect(rows('Rejects by sorter').map((row) => row.slice(1))).toEqual([
      ['0', '0', 'Not available', 'No rejects'],
      ['0', '0', 'Not available', 'No rejects'],
    ])
    // A report of nothing is still a report: nothing failed, and nothing is "unavailable".
    expect(main()).not.toHaveTextContent(/Could not load|Unavailable|0\.0%/)
    expect(main()).not.toHaveTextContent(NOT_A_FIGURE)
    expect(bars(/organization's check-ins/)).toHaveLength(7)
  })

  it('does the same for a single sorter with nothing', async () => {
    serveApi({
      'GET /api/auth/session': () => jsonResponse(200, ALICE),
      'GET /api/organizations/riverside': () => jsonResponse(200, RIVERSIDE_DETAIL),
      ...organizationReportRoutes('riverside', [{ ...RIVERSIDE_DETAIL.sorters[0], report: QUIET }]),
    })
    await week('/organizations/riverside/reports')

    expect(rows('Sorter comparison')[0].slice(3, 9)).toEqual(['0', 'Not available', '0', 'Not available', '0', 'Not available'])
    expect(main()).not.toHaveTextContent(NOT_A_FIGURE)
  })

  it('says an organization has no sorting machines without asking for anything, and without calling it missing', async () => {
    const fetchMock = serve({ 'GET /api/organizations/northbridge': () => jsonResponse(200, { ...NORTHBRIDGE_DETAIL, sorters: [] }) })

    renderApp(REPORTS)

    expect(await screen.findByText(NO_SORTERS)).toBeInTheDocument()
    await pass(500)
    expect(reportRequests(fetchMock)).toEqual([])
    expect(pipelineRequests(fetchMock)).toEqual([])
    expect(screen.getByRole('heading', { level: 3, name: 'Organization Reports' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Last 30 days' })).not.toBeInTheDocument()
    expect(screen.queryByText(NOT_FOUND_TEXT)).not.toBeInTheDocument()
  })

  it('says so in each section if the reports themselves list no sorters', async () => {
    serve({ [OVERVIEW]: answer('overview', []), [NETWORK]: answer('routingNetwork', []), [RELIABILITY]: answer('reliability', []) })

    renderApp(REPORTS)
    await loaded()

    expect(screen.getAllByText(NO_SORTERS)).toHaveLength(3)
    expect(section('Routing network')).toHaveTextContent('No sorting machine of this organization has routing data to show yet.')
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('says so when no sorter has a destination, and draws no matrix', async () => {
    serve({}, sorters(report({ transit: [] }), report({ transit: [] })))
    await week()

    expect(within(section('Routing network')).getByText('No routed destinations are configured for reporting.')).toBeInTheDocument()
    expect(screen.queryByRole('table', { name: 'Routing matrix' })).not.toBeInTheDocument()
    expect(screen.queryByRole('table', { name: 'Destination totals' })).not.toBeInTheDocument()
    expect(metric('Routing network', 'Transit rate')).toHaveTextContent(/^0\.0%$/)
    expect(rows('Routing by sorter').map((row) => row[3])).toEqual(['0', '0'])
  })

  it('keeps destinations nothing was routed to, as zeros, and says nothing went to them', async () => {
    serve({}, sorters(report({ transit: [['westside', 'Westside', 0]] }), report({ transit: [['westside', 'Westside', 0], ['depot', 'Depot', 0]] })))
    await week()

    expect(rows('Destination totals')).toEqual([
      ['Westside', '0', '2 of 2'],
      ['Depot', '0', '1 of 2'],
    ])
    expect(rows('Routing matrix')).toEqual([
      ['Central Library AMH', '0', '—Not configured'],
      ['East Side AMH', '0', '0'],
    ])
    expect(within(section('Routing network')).getByText('No check-ins were routed to a configured destination in this range.')).toBeInTheDocument()
  })

  it('shows other routing as each sorter’s own count', async () => {
    serve({}, sorters(report({ ...CENTRAL, other: 0.05 }), EAST))
    await week()

    expect(rows('Routing by sorter').map((row) => [row[0], row[5]])).toEqual([
      ['Central Library AMH', '38'],
      ['East Side AMH', '0'],
    ])
    expect(columns('Routing matrix')).toEqual(['From sorter', 'East Side AMH', 'Westside', 'Library Express'])
    expect(rows('Destination totals').map((row) => row[0])).not.toContain('Other')
  })

  it('shows check-ins with no rejects as a 0.0% reject rate, and says there were none', async () => {
    serve({}, sorters(report({ ...CENTRAL, rejects: () => 0 }), report({ ...EAST, rejects: () => 0 })))
    await week()

    expect(metric('System reliability', 'Rejects')).toHaveTextContent(/^0$/)
    expect(metric('System reliability', 'Reject rate')).toHaveTextContent(/^0\.0%$/)
    expect(within(section('System reliability')).getAllByText('No rejects in this range.')).toHaveLength(2)
    expect(screen.queryByRole('table', { name: 'Reject reasons' })).not.toBeInTheDocument()
    expect(rows('Rejects by sorter').map((row) => row.slice(2))).toEqual([
      ['0', '0.0%', 'No rejects'],
      ['0', '0.0%', 'No rejects'],
    ])
  })
})

describe('when an organization report cannot be loaded', () => {
  it.each(KINDS)('a failed %s report says so in its own sections and leaves the others alone', async (_kind, route, failedSections) => {
    serve({ [route]: SERVER_ERROR })

    renderApp(REPORTS)
    await loaded()

    for (const name of SECTIONS) {
      const failed = (failedSections as readonly string[]).includes(name)
      expect(within(section(name)).queryByText('Could not load.') !== null).toBe(failed)
      expect(within(section(name)).queryByRole('button', { name: `Try again: ${name}` }) !== null).toBe(failed)
      expect(within(section(name)).queryAllByRole('table').length + within(section(name)).queryAllByRole('img').length > 0).toBe(!failed)
    }
    expect(main()).not.toHaveTextContent('Internal server error.')
    expect(preset('Last 30 days')).toHaveAttribute('aria-pressed', 'true')
  })

  it.each([
    ['a dead network', () => Promise.reject(networkFailure())],
    ['a body that is not the report', () => jsonResponse(200, { totals: { checkin_count: 12 } })],
    ['a report for another range', () => jsonResponse(200, organizationReportBody.routingNetwork(sorters(), '2026-01-01', '2026-01-30'))],
    [
      'a report whose parts do not add up',
      (url: string) => {
        const body = organizationReportBody.routingNetwork(sorters(), ...rangeIn(url))
        body.destinations[0].source_count = 2
        return jsonResponse(200, body)
      },
    ],
  ])('treats %s the same way, and shows none of what was sent', async (_label, reply) => {
    serve({ [NETWORK]: reply as (url: string) => Response })

    renderApp(REPORTS)
    await screen.findByRole('button', { name: 'Try again: Routing network' })
    await loaded()

    expect(within(section('Routing network')).queryByRole('table')).not.toBeInTheDocument()
    expect(within(section('Routing network')).queryByText('Total transit')).not.toBeInTheDocument()
    expect(screen.getAllByText('Could not load.')).toHaveLength(1)
    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^4,640$/)
  })

  it('tries only that report again, keeps the button in place meanwhile, then moves focus to the section', async () => {
    const again = deferred<Response>()
    const fetchMock = serve({ [NETWORK]: inTurn(SERVER_ERROR, () => again.promise) })
    const person = user()
    renderApp(REPORTS)
    const retry = await screen.findByRole('button', { name: 'Try again: Routing network' })
    await loaded()

    retry.focus()
    await person.keyboard('{Enter}')

    expect(within(section('Routing network')).getByText('Trying again…')).toBeInTheDocument()
    expect(retry).toHaveFocus()
    expect(retry).toHaveAttribute('aria-disabled', 'true')
    expect(reportRequests(fetchMock).map((request) => request.split('?')[0]).sort()).toEqual([
      'overview',
      'reliability',
      'routing-network',
      'routing-network',
    ])

    again.resolve(answer('routingNetwork')(`${API}/routing-network?${DEFAULT_RANGE}`))
    await screen.findByRole('table', { name: 'Routing matrix' })
    expect(screen.getByRole('heading', { level: 4, name: 'Routing network' })).toHaveFocus()
    expect(screen.queryByText('Could not load.')).not.toBeInTheDocument()
  })

  it('one retry of the overview brings back both of its sections, with one request', async () => {
    const fetchMock = serve({ [OVERVIEW]: inTurn(SERVER_ERROR, answer('overview')) })
    renderApp(REPORTS)
    await screen.findByRole('button', { name: 'Try again: Overview' })
    expect(screen.getByRole('button', { name: 'Try again: Sorter comparison' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Try again: Sorter comparison' }))

    await screen.findByRole('table', { name: 'Sorter comparison' })
    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^4,640$/)
    expect(reportRequests(fetchMock).filter((request) => request.startsWith('overview?'))).toHaveLength(2)
  })

  it('offers the whole page again when the product’s day could not be read', async () => {
    serve({ [CENTRAL_PIPELINE]: inTurn(SERVER_ERROR, () => jsonResponse(200, { timezone: 'America/Chicago', state: 'ok', last_reported_at: null })) })

    renderApp(REPORTS)
    fireEvent.click(await screen.findByRole('button', { name: 'Try again' }))

    await loaded()
    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^4,640$/)
  })
})

describe('an organization the API will not return reports for (404)', () => {
  it.each(KINDS)('shows the not-found page, at the same address, when the %s report is answered 404', async (_kind, route) => {
    serve({ [route]: () => jsonResponse(404, ORGANIZATION_NOT_FOUND) })

    renderApp(REPORTS, { retries: true })

    expect(await screen.findByText(NOT_FOUND_TEXT)).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('Page not found')
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/Northbridge|Central Library AMH|4,640/)
    expect(screen.getByTestId('address')).toHaveTextContent(REPORTS)
  })

  it('shows the same page for an organization that is not there at all, and asks for no report', async () => {
    const fetchMock = serve({ 'GET /api/organizations/northbridge': () => jsonResponse(404, ORGANIZATION_NOT_FOUND) })

    renderApp(REPORTS)

    expect(await screen.findByText(NOT_FOUND_TEXT)).toBeInTheDocument()
    expect(reportRequests(fetchMock)).toEqual([])
    expect(pipelineRequests(fetchMock)).toEqual([])
  })
})

describe('an expired session (401)', () => {
  it.each(KINDS)('returns to the sign-in form at the same address when the %s report is answered 401, and asks it once', async (kind, route) => {
    const fetchMock = serve({ [route]: () => jsonResponse(401, NOT_AUTHENTICATED) })

    renderApp(REPORTS, { retries: true })

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
    expect(screen.queryByText('4,640')).not.toBeInTheDocument()
    expect(screen.getByTestId('address')).toHaveTextContent(REPORTS)
    await pass(500)
    expect(reportRequests(fetchMock).filter((request) => request.startsWith(`${kind}?`))).toHaveLength(1)
  })

  it('does the same when the product’s day is answered 401', async () => {
    const fetchMock = serve({ [CENTRAL_PIPELINE]: () => jsonResponse(401, NOT_AUTHENTICATED) })

    renderApp(REPORTS, { retries: true })

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(reportRequests(fetchMock)).toEqual([])
  })
})

describe('nothing from one question is shown under another', () => {
  it('shows nothing of the old range while the new one loads', async () => {
    const slow = { overview: deferred<Response>(), network: deferred<Response>(), reliability: deferred<Response>() }
    serve({
      [OVERVIEW]: inTurn(answer('overview'), () => slow.overview.promise),
      [NETWORK]: inTurn(answer('routingNetwork'), () => slow.network.promise),
      [RELIABILITY]: inTurn(answer('reliability'), () => slow.reliability.promise),
    })
    const person = user()
    renderApp(REPORTS)
    await loaded()
    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^4,640$/)

    await person.click(preset('Last 7 days'))

    expect(shown()).toContain('7 days')
    expect(main()).not.toHaveTextContent('4,640')
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    for (const name of SECTIONS) {
      expect(section(name)).toHaveTextContent('Loading…')
    }

    const range = `${API}/x?from=2026-09-29&to=2026-10-05`
    slow.overview.resolve(answer('overview')(range))
    slow.network.resolve(answer('routingNetwork')(range))
    slow.reliability.resolve(answer('reliability')(range))
    await loaded()
    expect(note('Overview', 'Check-ins')).toBe('Over 7 days')
  })

  it('ignores an answer for a range that has since been left', async () => {
    const late = deferred<Response>()
    serve({ [OVERVIEW]: (url) => (url.includes('from=2026-09-29') ? late.promise : answer('overview')(url)) })
    const person = user()
    renderApp(REPORTS)
    await loaded()

    await person.click(preset('Last 7 days'))
    await person.click(preset('Last 30 days'))
    await loaded()
    late.resolve(answer('overview')(`${API}/overview?from=2026-09-29&to=2026-10-05`))
    await pass(100)

    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^4,640$/)
    expect(note('Overview', 'Check-ins')).toBe('Over 30 days')
  })

  it('shows nothing of one organization under another, and starts the other on the default range', async () => {
    const slow = deferred<Response>()
    const fetchMock = serve({ [`GET ${organizationReportsPath('riverside')}/overview?from=*&to=*`]: () => slow.promise })
    const person = user()
    await week()
    expect(metric('Overview', 'Check-ins')).toHaveTextContent(/^1,110$/)

    await person.click(screen.getByRole('link', { name: 'Organizations' }))
    await person.click(await screen.findByRole('link', { name: 'Riverside Library' }))
    await person.click(await screen.findByRole('link', { name: 'Organization Reports' }))
    await screen.findByRole('region', { name: 'Overview' })

    expect(screen.getByTestId('address')).toHaveTextContent('/organizations/riverside/reports')
    expect(main()).not.toHaveTextContent(/Northbridge|Central Library AMH|East Side AMH|1,110/)
    expect(section('Overview')).toHaveTextContent('Loading…')
    expect(preset('Last 30 days')).toHaveAttribute('aria-pressed', 'true')
    expect(new Set(reportRequests(fetchMock, 'riverside').map((request) => request.split('?')[1]))).toEqual(new Set([DEFAULT_RANGE]))

    slow.resolve(jsonResponse(200, organizationReportBody.overview(RIVERSIDE_SORTERS, '2026-09-06', '2026-10-05')))
    await loaded()
    expect(rows('Sorter comparison').map((row) => row[0])).toEqual(['Riverside Main AMH: sorter reports'])
  })

  it('asks again on returning to a range, and shows nothing kept from before', async () => {
    const fetchMock = serve()
    const person = user()
    renderApp(REPORTS)
    await loaded()

    await person.click(preset('Last 7 days'))
    await loaded()
    await person.click(preset('Last 30 days'))
    await loaded()

    expect(reportRequests(fetchMock).filter((request) => request === `overview?${DEFAULT_RANGE}`)).toHaveLength(2)
  })
})

describe('reading the organization’s reports without seeing or pointing', () => {
  beforeEach(async () => {
    serve()
    await week()
  })

  it('names every chart and describes it with the sentence beside it', () => {
    const charts = screen.getAllByRole('img')
    expect(charts.map((chart) => chart.getAttribute('aria-label'))).toEqual([
      "Bar chart of the organization's check-ins on each day of the range",
      "Bar chart of the organization's rejects on each day of the range",
    ])
    for (const chart of charts) {
      expect(chart).toHaveAccessibleDescription(/\d/)
    }
  })

  it('gives every chart a table of the same figures, opened and closed by a button that says which', async () => {
    const person = user()
    for (const heading of ['Daily check-ins', 'Daily rejects']) {
      const show = screen.getByRole('button', { name: `Show table: ${heading}` })
      expect(show).toHaveAttribute('aria-expanded', 'false')
      show.focus()
      await person.keyboard('{Enter}')
      expect(screen.getByRole('button', { name: `Hide table: ${heading}` })).toHaveAttribute('aria-expanded', 'true')
      expect(rows(heading)).toHaveLength(7)
    }
  })

  it('names each section as a region and each table by its heading, with a header for every row and column', () => {
    expect(SECTIONS.map((name) => section(name).tagName)).toEqual(['SECTION', 'SECTION', 'SECTION', 'SECTION'])
    const tables = ['Sorter comparison', 'Routing by sorter', 'Destination totals', 'Routing matrix', 'Reject reasons', 'Rejects by sorter']
    expect(screen.getAllByRole('table').map((found) => found.getAttribute('aria-labelledby'))).toEqual([
      'org-comparison-heading',
      'org-routing-sources-heading',
      'org-routing-destinations-heading',
      'org-routing-matrix-heading',
      'org-reasons-heading',
      'org-reliability-sorters-heading',
    ])
    for (const name of tables) {
      const found = table(name)
      expect(within(found).getAllByRole('columnheader').every((cell) => cell.tagName === 'TH' && cell.getAttribute('scope') === 'col')).toBe(true)
      for (const row of within(found).getAllByRole('row').slice(1)) {
        expect(row.firstElementChild).toHaveAttribute('scope', 'row')
      }
    }
  })

  it('lets the two wide tables scroll sideways in a box the keyboard can reach, and nothing else', async () => {
    const boxes = Array.from(document.querySelectorAll('.table-scroll'))
    expect(boxes.map((box) => [box.getAttribute('role'), box.getAttribute('tabindex'), box.getAttribute('aria-labelledby')])).toEqual([
      ['group', '0', 'org-comparison-heading'],
      ['group', '0', 'org-routing-matrix-heading'],
    ])
    expect(boxes.map((box) => box.firstElementChild?.tagName)).toEqual(['TABLE', 'TABLE'])
    expect(screen.getByRole('group', { name: 'Routing matrix' })).toBe(boxes[1])

    boxes.forEach((box) => (box as HTMLElement).focus())
    expect(boxes[1]).toHaveFocus()
    // No tab order of its own: only 0 for those two boxes, and -1 for what code may focus.
    expect(new Set(Array.from(document.querySelectorAll('[tabindex]')).map((element) => element.getAttribute('tabindex')))).toEqual(new Set(['-1', '0']))
  })

  it('tells nothing by a dash alone: each says in words what is missing', () => {
    const dashes = Array.from(main().querySelectorAll('td')).filter((cell) => cell.textContent?.startsWith('—'))
    expect(dashes).toHaveLength(2)
    for (const cell of dashes) {
      expect(cell.querySelector('[aria-hidden="true"]')).toHaveTextContent('—')
      expect(cell.querySelector('.visually-hidden')).toHaveTextContent('Not configured')
    }
  })

  it('has no element that depends on a pointer, and disables nothing', () => {
    const app = main().parentElement as HTMLElement
    expect(app.querySelectorAll('[title], [onclick], [disabled], [role="button"], [role="link"]')).toHaveLength(0)
    for (const button of within(main()).getAllByRole('button')) {
      expect(button.tagName).toBe('BUTTON')
      expect(button).toHaveAccessibleName()
    }
    for (const link of within(main()).getAllByRole('link')) {
      expect(link).toHaveAttribute('href')
      expect(link).toHaveAccessibleName()
    }
  })

  it('labels the range controls, and applies a custom range from the keyboard', async () => {
    const person = user()
    expect(screen.getByRole('group', { name: 'Date range presets' })).toBeInTheDocument()
    expect(screen.getByRole('form', { name: 'Custom date range' })).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-10-01' } })
    screen.getByLabelText('To').focus()
    await person.keyboard('{Enter}')
    await loaded()

    expect(shown()).toContain('Oct 1, 2026 to Oct 4, 2026: 4 days')
  })
})
