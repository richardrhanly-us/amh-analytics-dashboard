import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { FeatureEntitlement } from '../api/organizations.ts'
import { efficiencyRoutes, efficiencyStore } from '../test/efficiency.ts'
import {
  ALICE,
  type ApiRoutes,
  type FetchMock,
  jsonResponse,
  livePath,
  NORTHBRIDGE,
  NORTHBRIDGE_DETAIL,
  organizationReportRoutes,
  REPORT,
  reportRoutes,
  requestedUrls,
  serveApi,
} from '../test/http.ts'
import { renderApp } from '../test/render.tsx'

/**
 * Reports R10A: a Reports page's views have addresses of their own --
 * a sorter's Operations and Efficiency, an organization's Operations and
 * Value -- and the range being reported on is kept in the address, as
 * `?from=YYYY-MM-DD&to=YYYY-MM-DD`, where every view of the page reads it.
 *
 * What each report shows is tested where it always was. This is about which
 * address shows what, and what the address's range is allowed to be.
 */

// 1:50 PM on Monday 5 October 2026 in Chicago: the fixtures' "today".
const NOW = '2026-10-05T18:50:00Z'
const DEFAULT_RANGE = 'from=2026-09-06&to=2026-10-05'
const WEEK = 'from=2026-09-29&to=2026-10-05'
const SORTER = '/organizations/northbridge/sorters/central'
const SORTER_REPORTS = `${SORTER}/reports`
const ORG = '/organizations/northbridge'
const ORG_REPORTS = `${ORG}/reports`
const PIPELINE = `${livePath('northbridge', 'central')}/pipeline-status`

const on = (limit_value: number | null = null): FeatureEntitlement => ({ enabled: true, limit_value })

let fetchMock: FetchMock

/** A signed-in member of Northbridge with `role`, whose plan lets a report start `history` days back (null: no limit). */
function serve(role = 'admin', history: number | null = null, overrides: ApiRoutes = {}): FetchMock {
  const sorters = NORTHBRIDGE_DETAIL.sorters.map((sorter) => ({ ...sorter, report: REPORT }))
  fetchMock = serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    'GET /api/organizations': () => jsonResponse(200, [{ ...NORTHBRIDGE, role }]),
    'GET /api/organizations/northbridge': () =>
      jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role, entitlements: { transits: on(), history_days: on(history) } }),
    ...reportRoutes('northbridge', 'central'),
    ...organizationReportRoutes('northbridge', sorters),
    ...efficiencyRoutes('northbridge', 'central', efficiencyStore(), () => fetchMock),
    ...overrides,
  })
  return fetchMock
}

const main = () => screen.getByRole('main')
const address = () => screen.getByTestId('address').textContent
const query = () => screen.getByTestId('query').textContent
/** Every report request made, as "kind?from=..&to=..". */
const reportRequests = () =>
  requestedUrls(fetchMock)
    .filter((url) => url.includes('/reports/'))
    .map((url) => url.split('/reports/')[1])
/** The ranges reports were asked for, each once. */
const rangesAsked = () => [...new Set(reportRequests().map((request) => request.split('?')[1]))]
const views = () => within(screen.getByRole('navigation', { name: 'Report views' }))
const view = (name: string) => views().getByRole('link', { name })
const preset = (name: string) => screen.getByRole('button', { name })
/** The address's dates are taken out in place, as a navigation of the router's own: a moment after the page shows. */
const cleared = () => waitFor(() => expect(query()).toBe(''))
const user = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })

/** Waits for the last section of a view, then for every section to have its answer. */
async function shown(lastSection: string) {
  await screen.findByRole('region', { name: lastSection })
  await waitFor(() => expect(main()).not.toHaveTextContent('Loading…'))
}
const sorterOperations = () => shown('Reliability')
const sorterEfficiency = () => shown('Efficiency')
const organizationOperations = () => shown('System reliability')

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'], shouldAdvanceTime: true })
  vi.setSystemTime(new Date(NOW))
})

afterEach(() => {
  vi.useRealTimers()
})

// =====================================================================================================================
// Which address shows what
// =====================================================================================================================

describe('a sorter’s report views', () => {
  it('the address reports used to have leads to Operations, in place, with its query', async () => {
    serve()

    renderApp(`${SORTER_REPORTS}?${WEEK}`)
    await sorterOperations()

    expect(address()).toBe(`${SORTER_REPORTS}/operations`)
    expect(query()).toBe(`?${WEEK}`)
    expect(rangesAsked()).toEqual([WEEK])
  })

  it('the sorter’s Reports link leads to Operations and stays current in either view', async () => {
    serve()
    const person = user()
    renderApp(SORTER)

    await person.click(within(await screen.findByRole('navigation', { name: 'Sorter views' })).getByRole('link', { name: 'Reports' }))
    await sorterOperations()

    expect(address()).toBe(`${SORTER_REPORTS}/operations`)
    expect(query()).toBe('')
    const reports = () => within(screen.getByRole('navigation', { name: 'Sorter views' })).getByRole('link', { name: 'Reports' })
    expect(reports()).toHaveAttribute('aria-current', 'page')
    await person.click(view('Efficiency'))
    await sorterEfficiency()
    expect(reports()).toHaveAttribute('aria-current', 'page')
    // Following the redirect did not leave an entry that bounces: one step back is the sorter's Live Today.
    await person.click(screen.getByRole('button', { name: 'browser-back' }))
    await person.click(screen.getByRole('button', { name: 'browser-back' }))
    expect(address()).toBe(SORTER)
  })

  it('Operations is the operational reports, and says it is the current view', async () => {
    serve()

    renderApp(`${SORTER_REPORTS}/operations`)
    await sorterOperations()

    expect(screen.getAllByRole('heading', { level: 4 }).map((heading) => heading.textContent)).toEqual([
      'Overview',
      'Volume & capacity',
      'Routing',
      'Bin volume',
      'Reliability',
    ])
    expect(views().getAllByRole('link').map((link) => link.textContent)).toEqual(['Operations', 'Efficiency'])
    expect(view('Operations')).toHaveAttribute('aria-current', 'page')
    expect(view('Efficiency')).not.toHaveAttribute('aria-current')
    expect(reportRequests().sort()).toEqual(['bins', 'overview', 'reliability', 'routing', 'volume'].map((kind) => `${kind}?${DEFAULT_RANGE}`))
  })

  it('Efficiency is the Efficiency report alone, under the same heading and range control', async () => {
    serve()

    renderApp(`${SORTER_REPORTS}/efficiency`)
    await sorterEfficiency()

    expect(screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent)).toEqual(['Reports'])
    expect(screen.getAllByRole('heading', { level: 4 }).map((heading) => heading.textContent)).toEqual(['Efficiency'])
    expect(view('Efficiency')).toHaveAttribute('aria-current', 'page')
    expect(view('Operations')).not.toHaveAttribute('aria-current')
    expect(preset('Last 30 days')).toHaveAttribute('aria-pressed', 'true')
    expect(reportRequests()).toEqual([`efficiency?${DEFAULT_RANGE}`])
  })

  it('an address that is not one of the views is the not-found page, and asks the sorter nothing', async () => {
    serve()

    renderApp(`${SORTER_REPORTS}/value`)

    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument()
    expect(requestedUrls(fetchMock).filter((url) => url.includes('/branches/'))).toEqual([])
  })
})

describe('an organization’s report views', () => {
  it('the address reports used to have leads to Operations, in place, with its query', async () => {
    serve()

    renderApp(`${ORG_REPORTS}?${WEEK}`)
    await organizationOperations()

    expect(address()).toBe(`${ORG_REPORTS}/operations`)
    expect(query()).toBe(`?${WEEK}`)
    expect(rangesAsked()).toEqual([WEEK])
  })

  it('Operations is the organization’s reports, and says it is the current view', async () => {
    serve()

    renderApp(`${ORG_REPORTS}/operations`)
    await organizationOperations()

    expect(screen.getAllByRole('heading', { level: 4 }).map((heading) => heading.textContent)).toEqual([
      'Overview',
      'Sorter comparison',
      'Routing network',
      'System reliability',
    ])
    expect(views().getAllByRole('link').map((link) => link.textContent)).toEqual(['Operations', 'Value'])
    expect(view('Operations')).toHaveAttribute('aria-current', 'page')
    expect(reportRequests().sort()).toEqual(['overview', 'reliability', 'routing-network'].map((kind) => `${kind}?${DEFAULT_RANGE}`))
  })

  it.each(['owner', 'admin'])('Value has an address and says it is not built yet: no figure, no form and nothing asked, for an %s', async (role) => {
    serve(role)

    renderApp(`${ORG_REPORTS}/value`)

    expect(await screen.findByText('SortView Value reporting will be available in a later stage.')).toHaveRole('note')
    expect(screen.getByRole('heading', { level: 3, name: 'Organization Reports' })).toBeInTheDocument()
    expect(view('Value')).toHaveAttribute('aria-current', 'page')
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
    expect(screen.queryByRole('form')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Last 30 days' })).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/\$|\d+(\.\d+)?%|ROI|payback|hours/i)
    // The organization itself, and nothing else: no report, no settings, not even the product's day.
    expect(requestedUrls(fetchMock).filter((url) => url.startsWith('/api/organizations/northbridge/'))).toEqual([])
  })

  it.each(['manager', 'viewer', ''])('Value is not offered to a member with the role %j, who is told so at its address', async (role) => {
    serve(role)

    renderApp(`${ORG_REPORTS}/value`)

    expect(await screen.findByText('SortView Value is available to this organization’s owners and administrators.')).toHaveRole('note')
    expect(screen.queryByRole('navigation', { name: 'Report views' })).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent('later stage')
    expect(requestedUrls(fetchMock).filter((url) => url.startsWith('/api/organizations/northbridge/'))).toEqual([])
  })

  it.each(['manager', 'viewer', ''])('a member with the role %j has Operations as before, with no strip of views', async (role) => {
    serve(role)

    renderApp(`${ORG_REPORTS}/operations`)
    await organizationOperations()

    expect(screen.queryByRole('navigation', { name: 'Report views' })).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/Value/)
  })

  it('an address that is not one of the views is the not-found page', async () => {
    serve()

    renderApp(`${ORG_REPORTS}/efficiency`)

    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument()
  })
})

// =====================================================================================================================
// The range is the address's
// =====================================================================================================================

describe('the range in the address', () => {
  it('a range that is chosen is written to the address, and no dates are written for the range the page opens on', async () => {
    serve()
    const person = user()
    renderApp(`${SORTER_REPORTS}/operations`)
    await sorterOperations()
    expect(query()).toBe('')

    await person.click(preset('Last 7 days'))
    await sorterOperations()
    expect(query()).toBe(`?${WEEK}`)
    // Choosing a range is not arriving at a page: the address is the same page's, and focus stays on what was used.
    expect(address()).toBe(`${SORTER_REPORTS}/operations`)
    expect(preset('Last 7 days')).toHaveFocus()

    await person.click(preset('Last 30 days'))
    await sorterOperations()
    expect(query()).toBe('')
    expect(rangesAsked()).toEqual([DEFAULT_RANGE, WEEK])
  })

  it('Year to date and typed dates are written as their two dates', async () => {
    serve()
    const person = user()
    renderApp(`${SORTER_REPORTS}/operations`)
    await sorterOperations()

    await person.click(preset('Year to date'))
    await sorterOperations()
    expect(query()).toBe('?from=2026-01-01&to=2026-10-05')

    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-09-01' } })
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2026-09-14' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply dates' }))
    await sorterOperations()
    expect(query()).toBe('?from=2026-09-01&to=2026-09-14')
  })

  it('an address opened directly is the range it says: the same requests, boxes and pressed preset', async () => {
    serve()

    renderApp(`${SORTER_REPORTS}/operations?${WEEK}`)
    await sorterOperations()

    expect(rangesAsked()).toEqual([WEEK])
    expect(screen.getByLabelText('From')).toHaveValue('2026-09-29')
    expect(screen.getByLabelText('To')).toHaveValue('2026-10-05')
    expect(preset('Last 7 days')).toHaveAttribute('aria-pressed', 'true')
    expect(preset('Last 30 days')).toHaveAttribute('aria-pressed', 'false')
    expect(query()).toBe(`?${WEEK}`)
  })

  it('an address with the dates of Year to date shows that preset as chosen', async () => {
    serve()

    renderApp(`${ORG_REPORTS}/operations?from=2026-01-01&to=2026-10-05`)
    await organizationOperations()

    expect(preset('Year to date')).toHaveAttribute('aria-pressed', 'true')
    expect(rangesAsked()).toEqual(['from=2026-01-01&to=2026-10-05'])
  })

  it('goes with the person between a sorter’s views, and the sorter’s day is read once', async () => {
    serve()
    const person = user()
    renderApp(`${SORTER_REPORTS}/operations`)
    await sorterOperations()
    await person.click(preset('Last 7 days'))
    await sorterOperations()

    expect(view('Efficiency')).toHaveAttribute('href', `${SORTER_REPORTS}/efficiency?${WEEK}`)
    await person.click(view('Efficiency'))
    await sorterEfficiency()

    expect(address()).toBe(`${SORTER_REPORTS}/efficiency`)
    expect(query()).toBe(`?${WEEK}`)
    expect(reportRequests().at(-1)).toBe(`efficiency?${WEEK}`)
    expect(preset('Last 7 days')).toHaveAttribute('aria-pressed', 'true')

    expect(view('Operations')).toHaveAttribute('href', `${SORTER_REPORTS}/operations?${WEEK}`)
    await person.click(view('Operations'))
    await sorterOperations()
    expect(query()).toBe(`?${WEEK}`)
    expect(screen.getByLabelText('From')).toHaveValue('2026-09-29')
    // The views are one page: moving between them does not read the product's day again.
    expect(requestedUrls(fetchMock).filter((url) => url === PIPELINE)).toHaveLength(1)
  })

  it('goes with the person between an organization’s views', async () => {
    serve()
    const person = user()
    renderApp(`${ORG_REPORTS}/operations`)
    await organizationOperations()
    await person.click(preset('Last 7 days'))
    await organizationOperations()

    await person.click(view('Value'))
    expect(await screen.findByText('SortView Value reporting will be available in a later stage.')).toBeInTheDocument()
    expect(address()).toBe(`${ORG_REPORTS}/value`)
    expect(query()).toBe(`?${WEEK}`)

    await person.click(view('Operations'))
    await organizationOperations()
    expect(query()).toBe(`?${WEEK}`)
    expect(preset('Last 7 days')).toHaveAttribute('aria-pressed', 'true')
    expect(rangesAsked()).toEqual([DEFAULT_RANGE, WEEK])
  })

  it('Back and Forward move between the ranges that were chosen, and the date boxes follow', async () => {
    serve()
    const person = user()
    renderApp(`${SORTER_REPORTS}/operations`)
    await sorterOperations()
    await person.click(preset('Last 7 days'))
    await sorterOperations()

    await person.click(screen.getByRole('button', { name: 'browser-back' }))
    await sorterOperations()
    expect(query()).toBe('')
    expect(preset('Last 30 days')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText('From')).toHaveValue('2026-09-06')

    await person.click(screen.getByRole('button', { name: 'browser-forward' }))
    await sorterOperations()
    expect(query()).toBe(`?${WEEK}`)
    expect(preset('Last 7 days')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText('From')).toHaveValue('2026-09-29')
  })

  it('stays with the person when the sorter’s Reports link is used from within its reports', async () => {
    serve()
    const person = user()
    const reports = () => within(screen.getByRole('navigation', { name: 'Sorter views' })).getByRole('link', { name: 'Reports' })
    renderApp(`${SORTER_REPORTS}/efficiency?ref=email&${WEEK}`)
    await sorterEfficiency()

    expect(reports()).toHaveAttribute('href', `${SORTER_REPORTS}?ref=email&${WEEK}`)
    await person.click(reports())
    await sorterOperations()

    // The link leads to the reports' first view, by the same redirect, which keeps the query.
    expect(address()).toBe(`${SORTER_REPORTS}/operations`)
    expect(query()).toBe(`?ref=email&${WEEK}`)
    expect(preset('Last 7 days')).toHaveAttribute('aria-pressed', 'true')
    expect(rangesAsked()).toEqual([WEEK])
  })

  it('is not carried to Live Today, and the Reports link from there opens on the rolling default', async () => {
    serve()
    const person = user()
    const sorterViews = () => within(screen.getByRole('navigation', { name: 'Sorter views' }))
    renderApp(`${SORTER_REPORTS}/operations?${WEEK}`)
    await sorterOperations()

    expect(sorterViews().getByRole('link', { name: 'Live Today' })).toHaveAttribute('href', SORTER)
    await person.click(sorterViews().getByRole('link', { name: 'Live Today' }))
    await waitFor(() => expect(address()).toBe(SORTER))
    expect(query()).toBe('')

    // No range in the address, so none is made up for the link.
    expect(sorterViews().getByRole('link', { name: 'Reports' })).toHaveAttribute('href', SORTER_REPORTS)
    fetchMock.mockClear()
    await person.click(sorterViews().getByRole('link', { name: 'Reports' }))
    await sorterOperations()
    expect(address()).toBe(`${SORTER_REPORTS}/operations`)
    expect(query()).toBe('')
    expect(preset('Last 30 days')).toHaveAttribute('aria-pressed', 'true')
    expect(rangesAsked()).toEqual([DEFAULT_RANGE])
  })

  it('goes with the person from the organization’s sorter comparison into that sorter’s reports', async () => {
    serve()
    const person = user()
    renderApp(`${ORG_REPORTS}/operations`)
    await organizationOperations()
    await person.click(preset('Last 7 days'))
    await organizationOperations()
    const comparison = () => within(screen.getByRole('table', { name: 'Sorter comparison' }))

    expect(comparison().getAllByRole('link').map((link) => link.getAttribute('href'))).toEqual([
      `${SORTER_REPORTS}?${WEEK}`,
      `/organizations/northbridge/sorters/east-side/reports?${WEEK}`,
    ])
    fetchMock.mockClear()
    await person.click(comparison().getByRole('link', { name: 'Central Library AMH: sorter reports' }))
    await sorterOperations()

    expect(address()).toBe(`${SORTER_REPORTS}/operations`)
    expect(query()).toBe(`?${WEEK}`)
    expect(preset('Last 7 days')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText('From')).toHaveValue('2026-09-29')
    expect(rangesAsked()).toEqual([WEEK])
  })

  it('the sorter comparison’s links carry no dates when the address has none, and the sorter opens on the rolling default', async () => {
    serve()
    const person = user()
    renderApp(`${ORG_REPORTS}/operations`)
    await organizationOperations()
    const comparison = within(screen.getByRole('table', { name: 'Sorter comparison' }))

    expect(comparison.getAllByRole('link').map((link) => link.getAttribute('href'))).toEqual([
      SORTER_REPORTS,
      '/organizations/northbridge/sorters/east-side/reports',
    ])
    fetchMock.mockClear()
    await person.click(comparison.getByRole('link', { name: 'Central Library AMH: sorter reports' }))
    await sorterOperations()

    expect(address()).toBe(`${SORTER_REPORTS}/operations`)
    expect(query()).toBe('')
    expect(rangesAsked()).toEqual([DEFAULT_RANGE])
  })

  it('leaves the rest of the query alone', async () => {
    serve()
    const person = user()
    renderApp(`${SORTER_REPORTS}/operations?ref=email&from=nonsense`)
    await sorterOperations()
    await waitFor(() => expect(query()).toBe('?ref=email'))

    await person.click(preset('Last 7 days'))
    await sorterOperations()
    expect(query()).toBe(`?ref=email&${WEEK}`)
  })
})

// =====================================================================================================================
// What the address's range may be
// =====================================================================================================================

describe('dates in the address that cannot be asked for', () => {
  it.each([
    ['not dates', 'from=yesterday&to=today'],
    ['an impossible date', 'from=2026-02-30&to=2026-03-05'],
    ['only a start date', 'from=2026-09-01'],
    ['only an end date', 'to=2026-09-14'],
    ['empty', 'from=&to='],
    ['reversed', 'from=2026-10-01&to=2026-09-01'],
    ['ending after today', 'from=2026-10-01&to=2026-10-06'],
    ['wholly in the future', 'from=2026-11-01&to=2026-11-05'],
    ['longer than the longest report', 'from=2016-09-27&to=2026-10-05'],
  ])('%s: the page opens on its own range, asks for nothing else, and takes them out of the address', async (_label, asked) => {
    serve()

    renderApp(`${SORTER_REPORTS}/operations?${asked}`)
    await sorterOperations()

    expect(rangesAsked()).toEqual([DEFAULT_RANGE])
    await cleared()
    expect(address()).toBe(`${SORTER_REPORTS}/operations`)
    expect(preset('Last 30 days')).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByLabelText('From')).toHaveValue('2026-09-06')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it.each([
    ['a sorter’s operations', `${SORTER_REPORTS}/operations`, 'Reliability'],
    ['a sorter’s Efficiency', `${SORTER_REPORTS}/efficiency`, 'Efficiency'],
    ['an organization’s operations', `${ORG_REPORTS}/operations`, 'System reliability'],
    ['the address that redirects', SORTER_REPORTS, 'Reliability'],
  ])('a range that starts before the plan’s window never becomes the range: %s', async (_label, path, lastSection) => {
    // The plan reaches back 30 days: the earliest start is 6 September.
    serve('admin', 30)

    renderApp(`${path}?from=2026-09-05&to=2026-10-05`)
    await shown(lastSection)

    expect(rangesAsked()).toEqual([DEFAULT_RANGE])
    expect(reportRequests().filter((request) => request.includes('2026-09-05'))).toEqual([])
    await cleared()
    expect(screen.getByLabelText('From')).toHaveValue('2026-09-06')
  })

  it('the earliest start the plan allows is a range that can be asked for', async () => {
    serve('admin', 30)

    renderApp(`${SORTER_REPORTS}/operations?from=2026-09-06&to=2026-09-20`)
    await sorterOperations()

    expect(rangesAsked()).toEqual(['from=2026-09-06&to=2026-09-20'])
    expect(query()).toBe('?from=2026-09-06&to=2026-09-20')
  })

  it('a plan with a shorter window opens on the whole of it, and Year to date is neither offered nor accepted', async () => {
    // Seven days back: 29 September. 1 January is far outside it.
    serve('admin', 7)

    renderApp(`${SORTER_REPORTS}/operations?from=2026-01-01&to=2026-10-05`)
    await sorterOperations()

    expect(rangesAsked()).toEqual([WEEK])
    await cleared()
    expect(screen.queryByRole('button', { name: 'Year to date' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Last 30 days' })).not.toBeInTheDocument()
  })

  it('still shows what the API says when it refuses a range this app let through', async () => {
    const refuse = () => jsonResponse(422, { code: 'range_before_history', message: "The selected range starts before this organization's available reporting window." })
    const reports = `GET ${livePath('northbridge', 'central')}/reports`
    serve('admin', null, Object.fromEntries(['overview', 'volume', 'routing', 'bins', 'reliability'].map((kind) => [`${reports}/${kind}?from=*&to=*`, refuse])))

    renderApp(`${SORTER_REPORTS}/operations?${WEEK}`)

    expect(await screen.findByText(/The selected range starts before this organization's available reporting window\./)).toHaveRole('note')
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
  })
})
