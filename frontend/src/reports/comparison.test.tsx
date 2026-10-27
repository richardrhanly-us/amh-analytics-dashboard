import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { FeatureEntitlement } from '../api/organizations.ts'
import { efficiencyRoutes, efficiencyStore } from '../test/efficiency.ts'
import {
  ALICE,
  type ApiRoutes,
  deferred,
  type FetchMock,
  jsonResponse,
  livePath,
  liveRoutes,
  NORTHBRIDGE,
  NORTHBRIDGE_DETAIL,
  organizationReportBody,
  organizationReportRoutes,
  organizationReportsPath,
  REPORT,
  reportBody,
  type ReportFixture,
  reportRoutes,
  requestedUrls,
  serveApi,
} from '../test/http.ts'
import { renderApp } from '../test/render.tsx'
import { valueRoutes, valueStore } from '../test/sortviewValue.ts'

/**
 * Reports R10B: an Operations Overview set beside the period before it.
 *
 * The comparison is asked for in the address (`compare=previous`), is read as
 * one more Overview report over the previous period and nothing else, and is
 * shown as one line of words on each Overview card. It is never read for a
 * period the plan does not reach, and nothing that becomes of it changes the
 * report it is set beside.
 */

// 1:50 PM on Monday 5 October 2026 in Chicago: the fixtures' "today".
const NOW = '2026-10-05T18:50:00Z'
// The range a page opens on, and the 30 days before it.
const DEFAULT_RANGE = 'from=2026-09-06&to=2026-10-05'
const PREVIOUS_RANGE = 'from=2026-08-07&to=2026-09-05'
const SORTER = '/organizations/northbridge/sorters/central'
const SORTER_OPERATIONS = `${SORTER}/reports/operations`
const SORTER_EFFICIENCY = `${SORTER}/reports/efficiency`
const ORG_OPERATIONS = '/organizations/northbridge/reports/operations'
const ORG_VALUE = '/organizations/northbridge/reports/value'
const SORTER_OVERVIEW = `GET ${livePath('northbridge', 'central')}/reports/overview?from=*&to=*`
const ORG_OVERVIEW = `GET ${organizationReportsPath('northbridge')}/overview?from=*&to=*`
const COMPARE = 'Compare with previous period'

/**
 * Ten check-ins a day from 6 September, eight a day before; one reject every day. A tenth of a day's check-ins
 * goes to Westside, rounded down: one a day now, none before.
 *
 *   the 30 days to 5 October      300 check-ins, 30 in transit (10.0%), 30 rejects (10.0%), 30 active days
 *   the 30 days to 5 September    240 check-ins,  0 in transit,         30 rejects (12.5%), 30 active days
 */
const STEP: ReportFixture = { ...REPORT, checkins: (date) => (date >= '2026-09-06' ? 10 : 8), rejects: () => 1 }
/** The same now, and nothing at all before 6 September. */
const NEW: ReportFixture = { ...REPORT, checkins: (date) => (date >= '2026-09-06' ? 10 : 0), rejects: (date) => (date >= '2026-09-06' ? 1 : 0) }

const on = (limit_value: number | null = null): FeatureEntitlement => ({ enabled: true, limit_value })

let fetchMock: FetchMock

/** An admin of Northbridge whose plan lets a report start `history` days back (null: no limit), its sorters answering `fixture`. */
function serve(fixture: ReportFixture = STEP, history: number | null = null, overrides: ApiRoutes = {}): FetchMock {
  const sorters = NORTHBRIDGE_DETAIL.sorters.map((sorter) => ({ ...sorter, report: fixture }))
  fetchMock = serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    'GET /api/organizations': () => jsonResponse(200, [NORTHBRIDGE]),
    'GET /api/organizations/northbridge': () =>
      jsonResponse(200, { ...NORTHBRIDGE_DETAIL, entitlements: { transits: on(), history_days: on(history) } }),
    ...liveRoutes('northbridge', 'central'),
    ...reportRoutes('northbridge', 'central', fixture),
    ...organizationReportRoutes('northbridge', sorters),
    ...efficiencyRoutes('northbridge', 'central', efficiencyStore(), () => fetchMock),
    ...valueRoutes('northbridge', valueStore(), () => fetchMock),
    ...overrides,
  })
  return fetchMock
}

const main = () => screen.getByRole('main')
const query = () => screen.getByTestId('query').textContent
/** Every report request made, as "kind?from=..&to=..". */
const reportRequests = () =>
  requestedUrls(fetchMock)
    .filter((url) => url.includes('/reports/'))
    .map((url) => url.split('/reports/')[1])
const overview = () => screen.getByRole('region', { name: 'Overview' })
const toggle = () => screen.getByRole('button', { name: COMPARE })
const user = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })

/** The card with `label` in the Overview: its figure, and the line saying how it changed (null when it has none). */
function card(label: string): { value: string; change: string | null } {
  const term = within(overview()).getByText(label, { selector: 'dt' })
  const metric = term.parentElement as HTMLElement
  return {
    value: metric.querySelector('.metric-value')?.textContent ?? '',
    change: metric.querySelector('.metric-change')?.textContent ?? null,
  }
}
const changeLines = () => Array.from(main().querySelectorAll('.metric-change')).map((line) => line.textContent)

/** Waits for the last section of a view, then for every section -- and every comparison -- to have its answer. */
async function shown(lastSection: string) {
  await screen.findByRole('region', { name: lastSection })
  await waitFor(() => expect(main()).not.toHaveTextContent(/Loading…|Comparing…/))
}
const sorterShown = () => shown('Reliability')
const organizationShown = () => shown('System reliability')

/** Answers for the previous period with `previous`, and for any other range as the fixture does. */
const previousOverview = (previous: (url: string) => Response | Promise<Response>, fixture: ReportFixture = STEP) => ({
  [SORTER_OVERVIEW]: (url: string) => {
    const params = new URL(url, 'http://test.invalid').searchParams
    return url.includes(PREVIOUS_RANGE)
      ? previous(url)
      : jsonResponse(200, reportBody.overview(fixture, params.get('from') ?? '', params.get('to') ?? ''))
  },
})
const SERVER_ERROR = () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' })

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'], shouldAdvanceTime: true })
  vi.setSystemTime(new Date(NOW))
})

afterEach(() => {
  vi.useRealTimers()
})

// =====================================================================================================================
// A sorter's Overview
// =====================================================================================================================

describe('a sorter’s Overview beside the previous period', () => {
  it('is not compared until asked: no line on any card, and the previous period is not read', async () => {
    serve()

    renderApp(SORTER_OPERATIONS)
    await sorterShown()

    expect(toggle()).toHaveAttribute('aria-pressed', 'false')
    expect(toggle()).toHaveAttribute('aria-disabled', 'false')
    expect(changeLines()).toEqual([])
    expect(within(overview()).queryByRole('note')).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/Compared with|Comparing/)
    expect(reportRequests().filter((request) => !request.endsWith(DEFAULT_RANGE))).toEqual([])
    expect(query()).toBe('')
  })

  it('once asked, reads the previous period’s Overview and nothing else of it, and says so in the address', async () => {
    serve()
    const person = user()
    renderApp(SORTER_OPERATIONS)
    await sorterShown()
    fetchMock.mockClear()

    await person.click(toggle())
    await sorterShown()

    expect(query()).toBe('?compare=previous')
    expect(toggle()).toHaveAttribute('aria-pressed', 'true')
    // Choosing it is not arriving at a page: focus stays on what was used.
    expect(toggle()).toHaveFocus()
    // One request: the same report, for the period before. Nothing that was already shown is asked for again.
    expect(reportRequests()).toEqual([`overview?${PREVIOUS_RANGE}`])
  })

  it('says how each Overview figure changed: counts as a percentage of the previous one, rates in points', async () => {
    serve()

    renderApp(`${SORTER_OPERATIONS}?compare=previous`)
    await sorterShown()

    expect(card('Check-ins')).toEqual({ value: '300', change: 'Up 25.0% from 240' })
    expect(card('Average per day')).toEqual({ value: '10.0', change: 'Up 25.0% from 8.0' })
    expect(card('Active days')).toEqual({ value: '30', change: 'No change from 30' })
    // Nothing went to transit before: there is no percentage of nothing, and none is made up. The rate has a change.
    expect(card('In transit')).toEqual({ value: '30', change: 'Percentage change not available: 0 in the previous period; rate up 10.0 points' })
    // The same number of rejects against more check-ins: the count did not move, the rate did. 10.0% from 12.5%.
    expect(card('Rejects')).toEqual({ value: '30', change: 'No change from 30; rate down 2.5 points' })
    // The one card that is not a figure of the period has nothing to compare.
    expect(card('Busiest day').change).toBeNull()
    expect(changeLines()).toHaveLength(5)
  })

  it('says which period it is compared with, and that a range reaching today may be partial beside it', async () => {
    serve()

    renderApp(`${SORTER_OPERATIONS}?compare=previous`)
    await sorterShown()

    expect(within(overview()).getByRole('note')).toHaveTextContent(
      'Compared with the previous period, Aug 7, 2026 to Sep 5, 2026. This range includes today, which is not over yet: it may be partial beside the previous period, which is complete.',
    )
  })

  it('a range that ended before today is compared with the same number of days before it, with nothing said about today', async () => {
    serve()

    // 14 days, 1 to 14 September; the 14 days before are 18 to 31 August.
    renderApp(`${SORTER_OPERATIONS}?from=2026-09-01&to=2026-09-14&compare=previous`)
    await sorterShown()

    expect(reportRequests().filter((request) => request.startsWith('overview?')).sort()).toEqual([
      'overview?from=2026-08-18&to=2026-08-31',
      'overview?from=2026-09-01&to=2026-09-14',
    ])
    expect(within(overview()).getByRole('note')).toHaveTextContent(/^Compared with the previous period, Aug 18, 2026 to Aug 31, 2026\.$/)
    // 5 days of 8 and 9 of 10 against 14 days of 8: 130 from 112.
    expect(card('Check-ins')).toEqual({ value: '130', change: 'Up 16.1% from 112' })
  })

  it('changes only the Overview’s cards: no other section has a line, a note or a request of its own', async () => {
    serve()

    renderApp(`${SORTER_OPERATIONS}?compare=previous`)
    await sorterShown()

    for (const name of ['Volume & capacity', 'Routing', 'Bin volume', 'Reliability']) {
      const section = screen.getByRole('region', { name })
      expect(section.querySelector('.metric-change')).toBeNull()
      expect(section).not.toHaveTextContent(/previous period/i)
    }
    expect(reportRequests().filter((request) => request.endsWith(PREVIOUS_RANGE))).toEqual([`overview?${PREVIOUS_RANGE}`])
  })

  it('has no percentage, and invents none, when the previous period had nothing at all', async () => {
    serve(NEW)

    renderApp(`${SORTER_OPERATIONS}?compare=previous`)
    await sorterShown()

    expect(card('Check-ins')).toEqual({ value: '300', change: 'Percentage change not available: 0 in the previous period' })
    expect(card('Average per day')).toEqual({ value: '10.0', change: 'Percentage change not available: 0.0 in the previous period' })
    expect(card('Active days')).toEqual({ value: '30', change: 'Percentage change not available: 0 in the previous period' })
    // A rate needs check-ins to be a rate of. The previous period had none: its rate does not exist, and is not 0%.
    expect(card('Rejects')).toEqual({ value: '30', change: 'Percentage change not available: 0 in the previous period; rate change not available' })
    expect(card('In transit')).toEqual({ value: '30', change: 'Percentage change not available: 0 in the previous period; rate change not available' })
    expect(main()).not.toHaveTextContent(/NaN|Infinity|undefined/)
  })

  it('keeps "Not available" for a figure with no denominator, in both periods', async () => {
    serve({ ...REPORT, checkins: () => 0, rejects: () => 0 })

    renderApp(`${SORTER_OPERATIONS}?compare=previous`)
    await sorterShown()

    expect(card('Check-ins')).toEqual({ value: '0', change: 'Percentage change not available: 0 in the previous period' })
    expect(within(overview()).getByText('Rejects', { selector: 'dt' }).parentElement).toHaveTextContent('Reject rate not available')
    expect(card('Rejects').change).toBe('Percentage change not available: 0 in the previous period; rate change not available')
  })

  it('shows the report at once and says it is comparing while the previous period is on its way', async () => {
    const slow = deferred<Response>()
    serve(STEP, null, previousOverview(() => slow.promise))

    renderApp(`${SORTER_OPERATIONS}?compare=previous`)
    await screen.findByRole('region', { name: 'Reliability' })
    await waitFor(() => expect(main()).not.toHaveTextContent('Loading…'))

    expect(card('Check-ins')).toEqual({ value: '300', change: 'Comparing…' })
    expect(card('Rejects')).toEqual({ value: '30', change: 'Comparing…' })

    slow.resolve(jsonResponse(200, reportBody.overview(STEP, '2026-08-07', '2026-09-05')))
    await waitFor(() => expect(card('Check-ins').change).toBe('Up 25.0% from 240'))
  })

  it('can be switched off again: the lines go, the address says so, and nothing is asked for', async () => {
    serve()
    const person = user()
    renderApp(`${SORTER_OPERATIONS}?from=2026-09-01&to=2026-09-14&compare=previous`)
    await sorterShown()
    fetchMock.mockClear()

    await person.click(toggle())

    await waitFor(() => expect(query()).toBe('?from=2026-09-01&to=2026-09-14'))
    expect(toggle()).toHaveAttribute('aria-pressed', 'false')
    expect(changeLines()).toEqual([])
    expect(within(overview()).queryByRole('note')).not.toBeInTheDocument()
    expect(card('Check-ins').value).toBe('130')
    expect(reportRequests()).toEqual([])
  })
})

// =====================================================================================================================
// When the previous period cannot be read
// =====================================================================================================================

describe('a comparison that cannot be loaded', () => {
  it('says so in one small place, with a way to ask again, and leaves the Overview and every other section as they are', async () => {
    serve(STEP, null, previousOverview(SERVER_ERROR))

    renderApp(`${SORTER_OPERATIONS}?compare=previous`)
    await sorterShown()

    expect(within(overview()).getByRole('note')).toHaveTextContent(
      'Comparison unavailable: the figures for the previous period, Aug 7, 2026 to Sep 5, 2026, could not be loaded. The figures below are not affected.',
    )
    expect(within(overview()).getByRole('button', { name: 'Try again: comparison' })).toBeInTheDocument()
    // The report itself is whole: its figures, its chart, and no card pretending to a change.
    expect(card('Check-ins')).toEqual({ value: '300', change: null })
    expect(card('Rejects')).toEqual({ value: '30', change: null })
    expect(within(overview()).getByRole('img', { name: /^Bar chart of check-ins/ })).toBeInTheDocument()
    expect(changeLines()).toEqual([])
    expect(main()).not.toHaveTextContent(/Could not load\.|Internal server error/)
    expect(screen.getAllByRole('region').map((region) => region.getAttribute('aria-labelledby'))).toEqual([
      'report-overview-heading',
      'report-volume-heading',
      'report-routing-heading',
      'report-bins-heading',
      'report-reliability-heading',
    ])
    // Still asked for in the address: it is the reading that failed, not the asking.
    expect(query()).toBe('?compare=previous')
    expect(toggle()).toHaveAttribute('aria-pressed', 'true')
  })

  it('asks for the comparison alone when told to try again, and then shows it', async () => {
    let calls = 0
    serve(STEP, null, previousOverview((url) => (calls++ === 0 ? SERVER_ERROR() : jsonResponse(200, reportBody.overview(STEP, ...(url.match(/\d{4}-\d{2}-\d{2}/g) as [string, string]))))))
    const person = user()
    renderApp(`${SORTER_OPERATIONS}?compare=previous`)
    await sorterShown()
    fetchMock.mockClear()

    await person.click(within(overview()).getByRole('button', { name: 'Try again: comparison' }))

    await waitFor(() => expect(card('Check-ins').change).toBe('Up 25.0% from 240'))
    expect(reportRequests()).toEqual([`overview?${PREVIOUS_RANGE}`])
    expect(within(overview()).queryByRole('button', { name: 'Try again: comparison' })).not.toBeInTheDocument()
  })

  it('keeps the "Try again" button, and focus on it, while the comparison is being tried again', async () => {
    const second = deferred<Response>()
    let calls = 0
    serve(STEP, null, previousOverview(() => (calls++ === 0 ? SERVER_ERROR() : second.promise)))
    const person = user()
    renderApp(`${SORTER_OPERATIONS}?compare=previous`)
    await sorterShown()
    const retry = () => within(overview()).getByRole('button', { name: 'Try again: comparison' })
    fetchMock.mockClear()

    retry().focus()
    await person.keyboard('{Enter}')

    // Still there, still focused, and saying it is trying -- not swapped out from under whoever pressed it.
    await waitFor(() => expect(within(overview()).getByText('Trying the comparison again…')).toBeInTheDocument())
    expect(retry()).toHaveFocus()
    expect(retry()).toHaveAttribute('aria-disabled', 'true')
    expect(retry()).not.toBeDisabled()
    expect(within(overview()).queryByText(/^Comparison unavailable:/)).not.toBeInTheDocument()
    expect(card('Check-ins')).toEqual({ value: '300', change: 'Comparing…' })
    // Pressing it again while it is trying asks for nothing more.
    await person.keyboard('{Enter}')
    expect(retry()).toHaveFocus()
    expect(reportRequests()).toEqual([`overview?${PREVIOUS_RANGE}`])

    second.resolve(jsonResponse(200, reportBody.overview(STEP, '2026-08-07', '2026-09-05')))

    // The button is gone with the failure; focus is on the sentence that says what the figures are compared with.
    await waitFor(() => expect(card('Check-ins').change).toBe('Up 25.0% from 240'))
    expect(within(overview()).queryByRole('button', { name: 'Try again: comparison' })).not.toBeInTheDocument()
    expect(within(overview()).getByRole('note')).toHaveFocus()
    expect(within(overview()).getByRole('note')).toHaveTextContent(/^Compared with the previous period, Aug 7, 2026 to Sep 5, 2026\./)
  })

  it('says so again, with the button still there and focused, when trying again fails too', async () => {
    serve(STEP, null, previousOverview(SERVER_ERROR))
    const person = user()
    renderApp(`${SORTER_OPERATIONS}?compare=previous`)
    await sorterShown()
    fetchMock.mockClear()

    await person.click(within(overview()).getByRole('button', { name: 'Try again: comparison' }))

    await waitFor(() => expect(reportRequests()).toEqual([`overview?${PREVIOUS_RANGE}`]))
    await waitFor(() => expect(within(overview()).getByRole('note')).toHaveTextContent(/^Comparison unavailable:/))
    expect(within(overview()).getByRole('button', { name: 'Try again: comparison' })).toHaveFocus()
    expect(within(overview()).getByRole('button', { name: 'Try again: comparison' })).toHaveAttribute('aria-disabled', 'false')
    expect(card('Check-ins')).toEqual({ value: '300', change: null })
  })

  it.each([
    ['is malformed', () => jsonResponse(200, { ...reportBody.overview(STEP, '2026-08-07', '2026-09-05'), checkin_count: '240' })],
    ['is refused as before the plan’s window', () => jsonResponse(422, { code: 'range_before_history', message: 'The selected range starts before this organization\'s available reporting window.' })],
    ['is not found', () => jsonResponse(404, { code: 'tenant_not_found', message: 'Organization or branch not found.' })],
  ])('treats an answer that %s as no comparison, and never as a failure of the report', async (_label, answer) => {
    serve(STEP, null, previousOverview(answer))

    renderApp(`${SORTER_OPERATIONS}?compare=previous`)
    await sorterShown()

    expect(within(overview()).getByRole('note')).toHaveTextContent(/^Comparison unavailable:/)
    expect(card('Check-ins')).toEqual({ value: '300', change: null })
    expect(screen.getAllByRole('region')).toHaveLength(5)
    expect(main()).not.toHaveTextContent(/Reports are not available|starts before this organization's available reporting window\. Choose/)
  })
})

// =====================================================================================================================
// The plan's window
// =====================================================================================================================

describe('a previous period the plan does not reach', () => {
  it('cannot be chosen: the choice says why, does nothing, and nothing is asked for', async () => {
    // The plan reaches back 30 days, which the range the page opens on uses in full.
    serve(STEP, 30)
    const person = user()
    renderApp(SORTER_OPERATIONS)
    await sorterShown()
    fetchMock.mockClear()

    expect(toggle()).toHaveAttribute('aria-disabled', 'true')
    expect(toggle()).toHaveAttribute('aria-pressed', 'false')
    // Unavailable, but still there to be reached and asked why.
    expect(toggle()).not.toBeDisabled()
    expect(toggle()).toHaveAccessibleDescription(
      'Not available for this range: the previous period, Aug 7, 2026 to Sep 5, 2026, starts before this organization’s available reporting window.',
    )

    await person.click(toggle())
    await person.keyboard('{Enter}')

    expect(query()).toBe('')
    expect(toggle()).toHaveAttribute('aria-pressed', 'false')
    expect(changeLines()).toEqual([])
    expect(reportRequests()).toEqual([])
  })

  it('never takes effect from the address either: no request for it, and it is taken out', async () => {
    serve(STEP, 30)

    renderApp(`${SORTER_OPERATIONS}?compare=previous`)
    await sorterShown()

    await waitFor(() => expect(query()).toBe(''))
    expect(toggle()).toHaveAttribute('aria-pressed', 'false')
    expect(changeLines()).toEqual([])
    expect(within(overview()).queryByRole('note')).not.toBeInTheDocument()
    expect(reportRequests().filter((request) => !request.endsWith(DEFAULT_RANGE))).toEqual([])
  })

  it('is all or nothing: a previous period that fits exactly is compared, and one a day too long is not shortened', async () => {
    // 30 days back from 5 October is 6 September. 15 days to today leave exactly 15 before them; 16 leave 14.
    serve(STEP, 30)

    renderApp(`${SORTER_OPERATIONS}?from=2026-09-21&to=2026-10-05&compare=previous`)
    await sorterShown()

    expect(toggle()).toHaveAttribute('aria-disabled', 'false')
    expect(reportRequests().filter((request) => request.startsWith('overview?')).sort()).toEqual([
      'overview?from=2026-09-06&to=2026-09-20',
      'overview?from=2026-09-21&to=2026-10-05',
    ])
    expect(card('Check-ins').change).toBe('No change from 150')
    expect(query()).toBe('?from=2026-09-21&to=2026-10-05&compare=previous')
  })

  it('a day too long: not compared, not shortened, and nothing before the window is asked for', async () => {
    serve(STEP, 30)

    renderApp(`${SORTER_OPERATIONS}?from=2026-09-20&to=2026-10-05&compare=previous`)
    await sorterShown()

    await waitFor(() => expect(query()).toBe('?from=2026-09-20&to=2026-10-05'))
    expect(toggle()).toHaveAttribute('aria-disabled', 'true')
    expect(toggle()).toHaveAccessibleDescription(/the previous period, Sep 4, 2026 to Sep 19, 2026, starts before/)
    expect(changeLines()).toEqual([])
    expect(reportRequests().filter((request) => !request.endsWith('from=2026-09-20&to=2026-10-05'))).toEqual([])
  })

  it('is dropped when a longer range is chosen that the plan cannot compare', async () => {
    serve(STEP, 30)
    const person = user()
    renderApp(`${SORTER_OPERATIONS}?from=2026-09-29&to=2026-10-05&compare=previous`)
    await sorterShown()
    expect(card('Check-ins').change).toBe('No change from 70')

    await person.click(screen.getByRole('button', { name: 'Last 30 days' }))
    await sorterShown()

    await waitFor(() => expect(query()).toBe(''))
    expect(toggle()).toHaveAttribute('aria-disabled', 'true')
    expect(changeLines()).toEqual([])
    expect(reportRequests().filter((request) => request.endsWith(PREVIOUS_RANGE))).toEqual([])
  })
})

// =====================================================================================================================
// The address
// =====================================================================================================================

describe('the comparison in the address', () => {
  it.each(['last-year', 'true', '', 'PREVIOUS', 'previous,previous'])('a value it does not know (%j) compares nothing and is taken out, with the rest of the query left alone', async (value) => {
    serve()

    renderApp(`${SORTER_OPERATIONS}?ref=email&from=2026-09-29&to=2026-10-05&compare=${encodeURIComponent(value)}`)
    await sorterShown()

    await waitFor(() => expect(query()).toBe('?ref=email&from=2026-09-29&to=2026-10-05'))
    expect(toggle()).toHaveAttribute('aria-pressed', 'false')
    expect(changeLines()).toEqual([])
    expect(reportRequests().filter((request) => !request.endsWith('from=2026-09-29&to=2026-10-05'))).toEqual([])
  })

  it('keeps the dates and the rest of the query when it is switched on and off', async () => {
    serve()
    const person = user()
    renderApp(`${SORTER_OPERATIONS}?ref=email&from=2026-09-29&to=2026-10-05`)
    await sorterShown()

    await person.click(toggle())
    await waitFor(() => expect(query()).toBe('?ref=email&from=2026-09-29&to=2026-10-05&compare=previous'))
    await person.click(toggle())
    await waitFor(() => expect(query()).toBe('?ref=email&from=2026-09-29&to=2026-10-05'))
  })

  it('stays on when another range is chosen, and is then about that range', async () => {
    serve()
    const person = user()
    renderApp(`${SORTER_OPERATIONS}?compare=previous`)
    await sorterShown()

    await person.click(screen.getByRole('button', { name: 'Last 7 days' }))
    await sorterShown()

    expect(query()).toBe('?compare=previous&from=2026-09-29&to=2026-10-05')
    expect(reportRequests().at(-1)).toBe('overview?from=2026-09-22&to=2026-09-28')
    expect(within(overview()).getByRole('note')).toHaveTextContent(/^Compared with the previous period, Sep 22, 2026 to Sep 28, 2026\./)
    expect(card('Check-ins')).toEqual({ value: '70', change: 'No change from 70' })
  })

  it('follows Back and Forward', async () => {
    serve()
    const person = user()
    renderApp(SORTER_OPERATIONS)
    await sorterShown()
    await person.click(toggle())
    await sorterShown()
    expect(card('Check-ins').change).toBe('Up 25.0% from 240')

    await person.click(screen.getByRole('button', { name: 'browser-back' }))
    await waitFor(() => expect(query()).toBe(''))
    expect(toggle()).toHaveAttribute('aria-pressed', 'false')
    expect(changeLines()).toEqual([])

    await person.click(screen.getByRole('button', { name: 'browser-forward' }))
    await waitFor(() => expect(query()).toBe('?compare=previous'))
    await sorterShown()
    expect(toggle()).toHaveAttribute('aria-pressed', 'true')
    expect(card('Check-ins').change).toBe('Up 25.0% from 240')
  })

  it('is not offered in the Efficiency view, which compares nothing, and comes back with the person to Operations', async () => {
    serve()
    const person = user()
    renderApp(`${SORTER_EFFICIENCY}?compare=previous`)
    await shown('Efficiency')

    expect(screen.queryByRole('button', { name: COMPARE })).not.toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Comparison' })).not.toBeInTheDocument()
    expect(reportRequests()).toEqual([`efficiency?${DEFAULT_RANGE}`])
    expect(changeLines()).toEqual([])
    // Not this view's to use, and not this view's to throw away.
    expect(query()).toBe('?compare=previous')

    await person.click(within(screen.getByRole('navigation', { name: 'Report views' })).getByRole('link', { name: 'Operations' }))
    await sorterShown()
    expect(query()).toBe('?compare=previous')
    expect(card('Check-ins').change).toBe('Up 25.0% from 240')
  })
})

// =====================================================================================================================
// An organization's Overview
// =====================================================================================================================

describe('an organization’s Overview beside the previous period', () => {
  it('reads the organization’s previous Overview alone, and says how each of its figures changed', async () => {
    serve()
    const person = user()
    renderApp(ORG_OPERATIONS)
    await organizationShown()
    expect(changeLines()).toEqual([])
    fetchMock.mockClear()

    await person.click(toggle())
    await organizationShown()

    expect(query()).toBe('?compare=previous')
    expect(reportRequests()).toEqual([`overview?${PREVIOUS_RANGE}`])
    // Two sorters, each with the sorter's own figures: twice everything, and the same rates.
    expect(card('Check-ins')).toEqual({ value: '600', change: 'Up 25.0% from 480' })
    expect(card('Average per day')).toEqual({ value: '20.0', change: 'Up 25.0% from 16.0' })
    expect(card('In transit')).toEqual({ value: '60', change: 'Percentage change not available: 0 in the previous period; rate up 10.0 points' })
    expect(card('Rejects')).toEqual({ value: '60', change: 'No change from 60; rate down 2.5 points' })
    // How many machines there are is not a figure of the period.
    expect(card('Sorting machines').change).toBeNull()
    expect(changeLines()).toHaveLength(4)
    expect(within(overview()).getByRole('note')).toHaveTextContent(/^Compared with the previous period, Aug 7, 2026 to Sep 5, 2026\. This range includes today/)
    for (const name of ['Sorter comparison', 'Routing network', 'System reliability']) {
      expect(screen.getByRole('region', { name }).querySelector('.metric-change')).toBeNull()
    }
  })

  it('leaves the organization’s reports whole when the previous period cannot be read', async () => {
    const sorters = NORTHBRIDGE_DETAIL.sorters.map((sorter) => ({ ...sorter, report: STEP }))
    serve(STEP, null, {
      [ORG_OVERVIEW]: (url) => (url.includes(PREVIOUS_RANGE) ? SERVER_ERROR() : jsonResponse(200, organizationReportBody.overview(sorters, '2026-09-06', '2026-10-05'))),
    })

    renderApp(`${ORG_OPERATIONS}?compare=previous`)
    await organizationShown()

    expect(within(overview()).getByRole('note')).toHaveTextContent(/^Comparison unavailable:/)
    expect(card('Check-ins')).toEqual({ value: '600', change: null })
    expect(screen.getAllByRole('region')).toHaveLength(4)
    expect(screen.getByRole('table', { name: 'Sorter comparison' })).toBeInTheDocument()
  })

  it('cannot be chosen for a previous period the plan does not reach, and is never asked for', async () => {
    serve(STEP, 30)

    renderApp(`${ORG_OPERATIONS}?compare=previous`)
    await organizationShown()

    await waitFor(() => expect(query()).toBe(''))
    expect(toggle()).toHaveAttribute('aria-disabled', 'true')
    expect(toggle()).toHaveAccessibleDescription(/starts before this organization’s available reporting window\./)
    expect(reportRequests().filter((request) => !request.endsWith(DEFAULT_RANGE))).toEqual([])
  })

  it('goes with the person into a sorter’s reports from the sorter comparison', async () => {
    serve()
    const person = user()
    renderApp(`${ORG_OPERATIONS}?compare=previous`)
    await organizationShown()

    const link = within(screen.getByRole('table', { name: 'Sorter comparison' })).getByRole('link', { name: 'Central Library AMH: sorter reports' })
    expect(link).toHaveAttribute('href', `${SORTER}/reports?compare=previous`)
    await person.click(link)
    await sorterShown()

    expect(query()).toBe('?compare=previous')
    expect(card('Check-ins')).toEqual({ value: '300', change: 'Up 25.0% from 240' })
  })

  it('is not offered in the Value view, where the address keeps it', async () => {
    serve()

    renderApp(`${ORG_VALUE}?from=2026-09-29&to=2026-10-05&compare=previous`)

    // R10G: the SortView Value report covers no range, and has nothing to compare.
    expect(await screen.findByRole('region', { name: 'SortView Value' })).toBeInTheDocument()
    await waitFor(() => expect(main()).not.toHaveTextContent('Loading…'))
    expect(screen.queryByRole('button', { name: COMPARE })).not.toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Comparison' })).not.toBeInTheDocument()
    expect(changeLines()).toEqual([])
    expect(main()).not.toHaveTextContent(/previous period|Compared with/i)
    // Not this view's to use, and not this view's to throw away: the address keeps all of it.
    expect(query()).toBe('?from=2026-09-29&to=2026-10-05&compare=previous')
    // One request, with no range and no comparison in it.
    expect(reportRequests()).toEqual(['sortview-value'])
  })
})

// =====================================================================================================================
// Elsewhere
// =====================================================================================================================

describe('a card that is not being compared', () => {
  it('Live Today’s cards are as they were: no line about a change, whatever the address says', async () => {
    serve()

    renderApp(`${SORTER}?compare=previous`)
    await screen.findByRole('navigation', { name: 'Sorter views' })
    await waitFor(() => expect(main().querySelectorAll('.metric-value').length).toBeGreaterThan(0))
    await waitFor(() => expect(main()).not.toHaveTextContent('Loading…'))

    expect(changeLines()).toEqual([])
    expect(main()).not.toHaveTextContent(/previous period|Comparing/i)
    expect(screen.queryByRole('button', { name: COMPARE })).not.toBeInTheDocument()
    // Its one report read is today's bins, for today alone: nothing for a previous period.
    expect(requestedUrls(fetchMock).filter((url) => url.includes('/reports/'))).toEqual([
      `${livePath('northbridge', 'central')}/reports/bins?from=${REPORT.today}&to=${REPORT.today}`,
    ])
  })
})
