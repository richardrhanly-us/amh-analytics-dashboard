import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { FeatureEntitlement } from '../api/organizations.ts'
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
  type ReportFixture,
  reportRoutes,
  requestedUrls,
  serveApi,
} from '../test/http.ts'
import { renderApp } from '../test/render.tsx'

/**
 * Reports R9D2: a range longer than 92 days. Its daily figures are SHOWN a
 * calendar month at a time -- and only shown: every card, average and
 * "busiest day" is still the days'. The Holds report keeps 92 days and says
 * so instead of asking. "Year to date" is offered when the plan's window
 * reaches 1 January.
 */

// 1:50 PM on Monday 5 October 2026 in Chicago: the fixtures' "today".
const NOW = '2026-10-05T18:50:00Z'
const SORTER_REPORTS = '/organizations/northbridge/sorters/central/reports'
const ORG_REPORTS = '/organizations/northbridge/reports'
const HOLDS = `GET ${livePath('northbridge', 'central')}/reports/holds?from=*&to=*`
const HOLDS_NOTE = 'Holds reporting is currently available for ranges up to 92 days.'

// Ten check-ins and one reject every day: a month's figures are its days times ten, and times one.
const STEADY: ReportFixture = { ...REPORT, checkins: () => 10, rejects: () => 1 }

const on = (limit_value: number | null = null): FeatureEntitlement => ({ enabled: true, limit_value })

function holds(url: string): Response {
  const params = new URL(url, 'http://test.invalid').searchParams
  const [from, to] = [params.get('from'), params.get('to')]
  const days = (Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000 + 1
  return jsonResponse(200, {
    range: { from, to, days, timezone: 'America/Chicago', includes_today: to === '2026-10-05' },
    public_hold_count: 41,
    ill_hold_count: 3,
  })
}

function serve(entitlements: Record<string, FeatureEntitlement> = { transits: on(), history_days: on() }, overrides: ApiRoutes = {}): FetchMock {
  const sorters = NORTHBRIDGE_DETAIL.sorters.map((sorter) => ({ ...sorter, report: STEADY }))
  return serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    'GET /api/organizations': () => jsonResponse(200, [{ ...NORTHBRIDGE, role: 'viewer' }]),
    'GET /api/organizations/northbridge': () => jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role: 'viewer', entitlements }),
    ...reportRoutes('northbridge', 'central', STEADY),
    ...organizationReportRoutes('northbridge', sorters),
    [HOLDS]: holds,
    ...overrides,
  })
}

const settle = () => act(() => vi.advanceTimersByTimeAsync(1_000))
const region = (name: string) => screen.getByRole('region', { name })
const headings = (name: string) => within(region(name)).getAllByRole('heading', { level: 5 }).map((heading) => heading.textContent)
const metric = (section: string, label: string) =>
  within(region(section)).getByText(label, { selector: 'dt' }).nextElementSibling?.textContent
const presetNames = () =>
  within(screen.getByRole('group', { name: 'Date range presets' })).getAllByRole('button').map((button) => button.textContent)

async function loaded(path = SORTER_REPORTS) {
  renderApp(path)
  await screen.findByRole('region', { name: path === ORG_REPORTS ? 'System reliability' : 'Reliability' })
  await waitFor(() => expect(screen.getByRole('main')).not.toHaveTextContent('Loading…'))
}

async function applyDates(from: string, to: string) {
  fireEvent.change(screen.getByLabelText('From'), { target: { value: from } })
  fireEvent.change(screen.getByLabelText('To'), { target: { value: to } })
  fireEvent.click(screen.getByRole('button', { name: 'Apply dates' }))
  await settle()
  await waitFor(() => expect(screen.getByRole('main')).not.toHaveTextContent('Loading…'))
}

/** A chart's table, opened, as its rows of cells (the header row first). */
function table(heading: string): string[][] {
  fireEvent.click(screen.getByRole('button', { name: `Show table: ${heading}` }))
  return within(screen.getByRole('table', { name: heading }))
    .getAllByRole('row')
    .map((row) => Array.from(row.children).map((cell) => cell.textContent ?? ''))
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'], shouldAdvanceTime: true })
  vi.setSystemTime(new Date(NOW))
})

afterEach(() => {
  vi.useRealTimers()
})

describe('a sorter’s reports over a long range', () => {
  it('up to 92 days, still day by day: every chart and table as before', async () => {
    serve()
    await loaded()
    await applyDates('2026-07-06', '2026-10-05')            // 92 days

    expect(headings('Overview')).toContain('Daily check-ins')
    expect(headings('Routing')).toContain('Daily transit')
    expect(headings('Reliability')).toContain('Daily rejects')
    const rows = table('Daily check-ins')
    expect(rows[0][0]).toBe('Date')
    expect(rows).toHaveLength(93)
    expect(rows[1]).toEqual(['Mon, Jul 6', '10', '1'])
  })

  it('over 92 days, a month at a time -- exact sums, partial months said -- and every card unchanged', async () => {
    const fetchMock = serve()
    await loaded()
    await applyDates('2026-06-28', '2026-10-05')            // 100 days

    expect(requestedUrls(fetchMock).some((url) => url.includes('/reports/overview?from=2026-06-28&to=2026-10-05'))).toBe(true)
    expect(headings('Overview')).toContain('Check-ins by month')
    expect(headings('Routing')).toContain('Transit by month')
    expect(headings('Reliability')).toContain('Rejects by month')
    expect(screen.getByRole('img', { name: 'Bar chart of check-ins in each month of the range' })).toBeInTheDocument()
    expect(table('Check-ins by month')).toEqual([
      ['Month', 'Check-ins', 'Rejects'],
      ['Jun 2026 (from Jun 28)', '30', '3'],
      ['Jul 2026', '310', '31'],
      ['Aug 2026', '310', '31'],
      ['Sep 2026', '300', '30'],
      ['Oct 2026 (to Oct 5)', '50', '5'],
    ])
    expect(table('Rejects by month')[0]).toEqual(['Month', 'Rejects', 'Check-ins'])
    // The cards are the API's totals and the days' own figures, never the months'.
    expect(metric('Overview', 'Check-ins')).toBe('1,000')
    expect(metric('Overview', 'Average per day')).toBe('10.0')
    expect(metric('Overview', 'Active days')).toBe('100')
    expect(metric('Reliability', 'Rejects')).toBe('100')
    // And what is not a series of days is not grouped at all.
    expect(headings('Volume & capacity')).toEqual(expect.arrayContaining(['Typical week', 'Typical day']))
  })
})

describe('the Holds report over a long range', () => {
  const withHolds = { transits: on(), history_days: on(), internal_workflow: on() }

  it('up to 92 days is asked for and shown as before', async () => {
    const fetchMock = serve(withHolds)
    await loaded()
    await applyDates('2026-07-06', '2026-10-05')            // 92 days

    await within(region('Holds')).findByText('Public holds')
    expect(metric('Holds', 'Public holds')).toBe('41')
    expect(requestedUrls(fetchMock).some((url) => url.includes('/reports/holds?from=2026-07-06&to=2026-10-05'))).toBe(true)
  })

  it('over 92 days is not asked for: the section says so, and every other report is there', async () => {
    const fetchMock = serve(withHolds)
    await loaded()
    await applyDates('2026-06-28', '2026-10-05')            // 100 days

    expect(region('Holds')).toHaveTextContent(HOLDS_NOTE)
    expect(within(region('Holds')).queryByText('Public holds')).not.toBeInTheDocument()
    expect(within(region('Holds')).queryByRole('button', { name: /Try again/ })).not.toBeInTheDocument()
    expect(requestedUrls(fetchMock).filter((url) => url.includes('/reports/holds?from=2026-06-28'))).toEqual([])
    expect(metric('Overview', 'Check-ins')).toBe('1,000')
    // Not a plan's limit: the note does not say the plan, the organization or its history decides it.
    expect(region('Holds')).not.toHaveTextContent(/plan|subscription|history|organization/i)

    // Back to a short range, and it is asked for again.
    await userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) }).click(screen.getByRole('button', { name: 'Last 30 days' }))
    await within(region('Holds')).findByText('Public holds')
  })
})

describe('year to date', () => {
  it.each([
    ['no history limit', on(null)],
    ['730 days of history', on(730)],
    ['3,650 days of history', on(3650)],
  ])('with %s, is offered and reports from 1 January', async (_label, history) => {
    const fetchMock = serve({ transits: on(), history_days: history })
    await loaded()

    expect(presetNames()).toEqual(['Last 7 days', 'Last 30 days', 'Last 90 days', 'Year to date'])
    fireEvent.click(screen.getByRole('button', { name: 'Year to date' }))
    await settle()
    await waitFor(() => expect(screen.getByRole('main')).not.toHaveTextContent('Loading…'))

    expect(screen.getByRole('button', { name: 'Year to date' })).toHaveAttribute('aria-pressed', 'true')
    expect(requestedUrls(fetchMock).some((url) => url.includes('/reports/overview?from=2026-01-01&to=2026-10-05'))).toBe(true)
    expect(screen.getByText(/Showing Jan 1, 2026 to Oct 5, 2026: 278 days/)).toBeInTheDocument()
    expect(headings('Overview')).toContain('Check-ins by month')
  })

  it.each([
    ['90 days of history', on(90), ['Last 7 days', 'Last 30 days', 'Last 90 days']],
    ['30 days of history', on(30), ['Last 7 days', 'Last 30 days']],
    ['no history feature', undefined, ['Last 7 days', 'Last 30 days']],
  ])('with %s, is not offered: 1 January is outside the window, and it is never shortened to fit', async (_label, history, presets) => {
    serve(history === undefined ? { transits: on() } : { transits: on(), history_days: history })
    await loaded()

    expect(presetNames()).toEqual(presets)
  })
})

describe('an organization’s reports over a long range', () => {
  it('show its daily figures a month at a time, with its totals unchanged', async () => {
    serve()
    await loaded(ORG_REPORTS)
    const before = metric('Overview', 'Check-ins')
    await applyDates('2026-06-28', '2026-10-05')            // 100 days

    expect(headings('Overview')).toContain('Check-ins by month')
    expect(headings('System reliability')).toContain('Rejects by month')
    const rows = table('Check-ins by month')
    expect(rows[0]).toEqual(['Month', 'Check-ins', 'Rejects'])
    expect(rows.slice(1).map((row) => row[0])).toEqual(['Jun 2026 (from Jun 28)', 'Jul 2026', 'Aug 2026', 'Sep 2026', 'Oct 2026 (to Oct 5)'])
    // Two sorters, ten a day each, for 100 days.
    expect(metric('Overview', 'Check-ins')).toBe('2,000')
    expect(before).toBe('600')                                 // the 30 days it opened on
  })
})
