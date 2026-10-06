import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  ALICE,
  type ApiRoutes,
  deferred,
  type FetchMock,
  jsonResponse,
  LIVE,
  liveBody,
  type LiveFixture,
  livePath,
  liveRoutes,
  networkFailure,
  NORTHBRIDGE_DETAIL,
  NOT_AUTHENTICATED,
  requestedUrls,
  RIVERSIDE_DETAIL,
  type RoutedTo,
  serveApi,
  TENANT_NOT_FOUND,
  textResponse,
} from '../test/http.ts'
import { renderApp } from '../test/render.tsx'
import { REFRESH_INTERVAL_MS } from './useLiveToday.ts'

const CENTRAL = '/organizations/northbridge/sorters/central'
const API = livePath('northbridge', 'central')
const PIPELINE = `GET ${API}/pipeline-status`
const CHECKIN_COUNT = `GET ${API}/checkins/count?date=*`
const BY_HOUR = `GET ${API}/checkins/by-hour?date=*`
const BY_DESTINATION = `GET ${API}/checkins/by-destination?date=*`
const REJECT_COUNT = `GET ${API}/rejects/count?date=*`
const BY_REASON = `GET ${API}/rejects/by-reason?date=*`

// 1:50 PM on Monday 5 October 2026 in Chicago (CDT, UTC-5).
const NOW = '2026-10-05T18:50:00Z'
const TODAY = '2026-10-05'

const SERVER_ERROR = () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' })
const UNAVAILABLE = 'Live dashboard data is not available for this sorter yet.'

const hoursWith = (counts: Record<number, number>) => Array.from({ length: 24 }, (_, hour) => counts[hour] ?? 0)
/** The icon drawn for each pipeline state, as the tests come across them. */
const ICON_SHAPES: Record<string, string> = {}
const live = (changes: Partial<LiveFixture>): LiveFixture => ({ ...LIVE, ...changes })

/** A signed-in user at a branch whose five reads answer `fixture`, unless a test replaces a route. */
function serve(overrides: ApiRoutes = {}, fixture: LiveFixture | (() => LiveFixture) = LIVE): FetchMock {
  return serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    'GET /api/organizations/northbridge': () => jsonResponse(200, NORTHBRIDGE_DETAIL),
    'GET /api/organizations/riverside': () => jsonResponse(200, RIVERSIDE_DETAIL),
    ...liveRoutes('northbridge', 'central', fixture),
    ...liveRoutes('northbridge', 'east-side', live({ hours: hoursWith({ 8: 7 }), reasons: [0, 0, 0, 0, 0, 0, 0, 0] })),
    ...liveRoutes('riverside', 'main', fixture),
    ...overrides,
  })
}

/** Answers first with each of `replies` in turn, then keeps giving the last one. */
function inTurn(...replies: Array<(url: string) => Response | Promise<Response>>) {
  let call = 0
  return (url: string) => replies[Math.min(call++, replies.length - 1)](url)
}

const main = () => screen.getByRole('main')
const liveRequests = (fetchMock: FetchMock) => requestedUrls(fetchMock).filter((url) => url.includes('/branches/'))
const datesAsked = (fetchMock: FetchMock) =>
  liveRequests(fetchMock)
    .map((url) => new URL(url, 'http://test.invalid').searchParams.get('date'))
    .filter((date) => date !== null)
/** The endpoint part of each live request: "pipeline-status", "checkins/count", ... */
const endpoints = (urls: string[]) => urls.map((url) => url.split('/branches/')[1].split('/').slice(1).join('/').split('?')[0])

/** The value shown beside a summary label. */
function metric(label: string | RegExp): HTMLElement {
  return screen.getByText(label, { selector: 'dt' }).nextElementSibling as HTMLElement
}
/** The line under a summary figure -- which hour it is, or why there is no figure -- or null. */
function note(label: string): string | null {
  return metric(label).nextElementSibling?.textContent ?? null
}
const chart = () => screen.getByRole('img', { name: 'Bar chart of check-ins in each hour of the day' })
/** The drawn height of each hour's bar, in the chart's own units (0 to 100). */
const barHeights = () =>
  Array.from(chart().querySelectorAll('rect[data-hour]')).map((bar) => Number(bar.getAttribute('height')))
/** The hourly table's body rows, opening the table first if it is closed. */
function hourlyRows(): string[][] {
  const show = screen.queryByRole('button', { name: 'Show hourly table' })
  if (show !== null) {
    fireEvent.click(show)
  }
  return rows('Hourly check-ins')
}
/** A table's body rows as [first cell, second cell] text. */
function rows(name: string): string[][] {
  return within(screen.getByRole('table', { name }))
    .getAllByRole('row')
    .slice(1)
    .map((row) => Array.from(row.children).map((cell) => cell.textContent ?? ''))
}

const user = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })
const refreshButton = () => screen.getByRole('button', { name: /^Refresh/ })
/** Lets `ms` of the app's time pass, timers and all. */
const pass = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms))
/** Waits until the dashboard has loaded every section. */
async function loaded() {
  await screen.findByRole('img', { name: /^Bar chart of check-ins/ })
  await waitFor(() => expect(refreshButton()).not.toHaveAttribute('aria-disabled', 'true'))
}

beforeEach(() => {
  // The clock is the test's: "now" is fixed, and the refresh timer runs only when a test lets time pass.
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'], shouldAdvanceTime: true })
  vi.setSystemTime(new Date(NOW))
})

afterEach(() => {
  vi.useRealTimers()
})

describe('the order things are asked in', () => {
  it('asks for pipeline status alone, and shows no figures, until it answers', async () => {
    const pipeline = deferred<Response>()
    const fetchMock = serve({ [PIPELINE]: () => pipeline.promise })

    renderApp(CENTRAL)

    expect(await screen.findByText('Loading live data…')).toHaveRole('status')
    await pass(1000)
    expect(liveRequests(fetchMock)).toEqual([`${API}/pipeline-status`])
    expect(main()).not.toHaveTextContent(/\b0\b|Check-ins|Rejects/)

    pipeline.resolve(jsonResponse(200, liveBody.pipeline(LIVE)))
    await loaded()
    expect(endpoints(liveRequests(fetchMock)).sort()).toEqual([
      'checkins/by-destination',
      'checkins/by-hour',
      'checkins/count',
      'pipeline-status',
      'rejects/by-reason',
      'rejects/count',
    ])
    expect(liveRequests(fetchMock)[0]).toBe(`${API}/pipeline-status`)
  })

  it('asks all five dated reads for the same date, exactly once each', async () => {
    const fetchMock = serve()

    renderApp(CENTRAL)
    await loaded()

    expect(liveRequests(fetchMock)).toHaveLength(6)
    expect(datesAsked(fetchMock)).toEqual([TODAY, TODAY, TODAY, TODAY, TODAY])
    expect(liveRequests(fetchMock).slice(1).sort()).toEqual([
      `${API}/checkins/by-destination?date=${TODAY}`,
      `${API}/checkins/by-hour?date=${TODAY}`,
      `${API}/checkins/count?date=${TODAY}`,
      `${API}/rejects/by-reason?date=${TODAY}`,
      `${API}/rejects/count?date=${TODAY}`,
    ])
  })

  it.each([
    // The same instant, 9:30 PM Monday in Chicago: three zones, two dates.
    ['America/Chicago', '2026-10-05', 'Monday, October 5, 2026', '9–10 PM'],
    ['Asia/Tokyo', '2026-10-06', 'Tuesday, October 6, 2026', '11 AM–12 PM'],
    ['Pacific/Honolulu', '2026-10-05', 'Monday, October 5, 2026', '4–5 PM'],
  ])('takes today and the hour from the product zone %s, not from this machine', async (timezone, date, dateText, hourRange) => {
    vi.setSystemTime(new Date('2026-10-06T02:30:00Z'))
    const fetchMock = serve({}, live({ timezone }))

    renderApp(CENTRAL)
    await loaded()

    expect(datesAsked(fetchMock)).toEqual([date, date, date, date, date])
    expect(main()).toHaveTextContent(`${dateText} (${timezone})`)
    expect(note('Current hour')).toBe(hourRange)
    expect(screen.getByText(/^Busiest hour: .* Hours are in /)).toHaveTextContent(`Hours are in ${timezone} time.`)
  })

  it('asks for nothing dated, and guesses no date, when pipeline status cannot be read', async () => {
    const fetchMock = serve({ [PIPELINE]: SERVER_ERROR })

    renderApp(CENTRAL)

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    await pass(1000)
    expect(liveRequests(fetchMock)).toEqual([`${API}/pipeline-status`])
    expect(datesAsked(fetchMock)).toEqual([])
    expect(main()).not.toHaveTextContent(/2026|October|Monday/)
  })
})

describe('the figures', () => {
  it('shows the pipeline state and when it last reported, in the product zone', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()

    expect(metric('Status')).toHaveTextContent(/^OK$/)
    const reported = within(metric('Last reported')).getByText(/Oct 5, 2026, 1:45\sPM CDT/)
    expect(reported.tagName).toBe('TIME')
    expect(reported).toHaveAttribute('datetime', '2026-10-05T18:45:03Z')
  })

  it.each([
    ['ok', 'OK'],
    ['degraded', 'Degraded'],
    ['failed', 'Failed'],
    ['unknown', 'Unknown'],
  ])('shows the pipeline state %s in words', async (state, label) => {
    serve({}, live({ state }))

    renderApp(CENTRAL)
    await loaded()

    expect(metric('Status')).toHaveTextContent(new RegExp(`^${label}$`))
  })

  it('says so when the pipeline has never reported, and judges no report by its age', async () => {
    serve({}, live({ state: 'unknown', last_reported_at: null }))

    renderApp(CENTRAL)
    await loaded()

    expect(metric('Last reported')).toHaveTextContent('Nothing reported yet')
    expect(main()).not.toHaveTextContent(/Invalid Date|stale|out of date|\bago\b/i)
  })

  it('shows an old report exactly as reported, with no staleness judgement', async () => {
    serve({}, live({ state: 'ok', last_reported_at: '2026-09-01T15:00:00Z' }))

    renderApp(CENTRAL)
    await loaded()

    expect(metric('Status')).toHaveTextContent(/^OK$/)
    expect(metric('Last reported')).toHaveTextContent(/Sep 1, 2026, 10:00\sAM CDT/)
    expect(main()).not.toHaveTextContent(/stale|out of date|\bago\b/i)
  })

  it('shows today, check-ins, the current hour, the busiest hour, rejects and the reject rate', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()

    expect(main()).toHaveTextContent('Monday, October 5, 2026 (America/Chicago)')
    expect(metric('Check-ins today')).toHaveTextContent(/^120$/)
    expect(metric('Current hour')).toHaveTextContent(/^17$/)
    expect(note('Current hour')).toBe('1–2 PM')
    expect(metric('Busiest hour')).toHaveTextContent(/^40$/)
    expect(note('Busiest hour')).toBe('11 AM–12 PM')
    expect(note('Check-ins today')).toBeNull()
    expect(note('Rejects today')).toBeNull()
    expect(note('Reject rate')).toBeNull()
    expect(screen.getAllByRole('term').map((term) => term.textContent)).toEqual([
      'Status',
      'Last reported',
      'Check-ins today',
      'Current hour',
      'Busiest hour',
      'Total transit',
      'Westside',
      'Library Express',
      'Rejects today',
      'Reject rate',
    ])
    expect(metric('Rejects today')).toHaveTextContent(/^6$/)
    expect(metric('Reject rate')).toHaveTextContent(/^5\.0%$/)
  })

  it('shows zero for a current hour in which nothing has happened', async () => {
    vi.setSystemTime(new Date('2026-10-05T20:10:00Z'))
    serve()

    renderApp(CENTRAL)
    await loaded()

    expect(metric('Current hour')).toHaveTextContent(/^0$/)
    expect(note('Current hour')).toBe('3–4 PM')
  })

  it('gives the earliest hour when hours tie for busiest', async () => {
    serve({}, live({ hours: hoursWith({ 14: 40, 9: 40, 11: 40 }) }))

    renderApp(CENTRAL)
    await loaded()

    expect(metric('Busiest hour')).toHaveTextContent(/^40$/)
    expect(note('Busiest hour')).toBe('9–10 AM')
    // The chart's own summary names the same hour: one rule, in one place.
    expect(screen.getByText(/^Busiest hour: /)).toHaveTextContent('Busiest hour: 9–10 AM, 40 check-ins.')
  })

  it('shows no busiest hour and no reject rate on a day with no check-ins', async () => {
    serve({}, live({ hours: hoursWith({}), reasons: [0, 0, 0, 0, 0, 0, 0, 0] }))

    renderApp(CENTRAL)
    await loaded()

    expect(metric('Check-ins today')).toHaveTextContent(/^0$/)
    expect(metric('Busiest hour')).toHaveTextContent(/^No check-ins yet$/)
    expect(note('Busiest hour')).toBeNull()
    expect(metric('Rejects today')).toHaveTextContent(/^0$/)
    expect(metric('Reject rate')).toHaveTextContent(/^Not available$/)
    expect(note('Reject rate')).toBe('No check-ins yet')
    expect(main()).not.toHaveTextContent(/NaN|Infinity|Busiest hour: /)
  })

  it('shows no reject rate, rather than infinity, for rejects without check-ins', async () => {
    serve({}, live({ hours: hoursWith({}), reasons: [4, 0, 0, 0, 0, 0, 0, 0] }))

    renderApp(CENTRAL)
    await loaded()

    expect(metric('Rejects today')).toHaveTextContent(/^4$/)
    expect(metric('Reject rate')).toHaveTextContent(/^Not available$/)
    expect(note('Reject rate')).toBe('No check-ins yet')
    expect(main()).not.toHaveTextContent(/NaN|Infinity|%/)
  })

  it('lists all 24 hours in a table and marks the current one in words', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()

    const hourly = hourlyRows()
    expect(hourly).toHaveLength(24)
    expect(hourly[0]).toEqual(['12 AM', '0'])
    expect(hourly[11]).toEqual(['11 AM', '40'])
    expect(hourly[13]).toEqual(['1 PM (current hour)', '17'])
    expect(hourly[23]).toEqual(['11 PM', '0'])
    expect(hourly.filter(([hour]) => hour.includes('current hour'))).toHaveLength(1)
  })

  it('lists the reject reasons that occurred, most frequent first, by plain name', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()

    expect(rows('Top reject reasons')).toEqual([
      ['Item not found', '3'],
      ['Communication error', '2'],
      ['RFID collision', '1'],
    ])
    expect(main()).not.toHaveTextContent(/item_not_found|rfid_collision/)
  })

  it('says there were no rejects instead of showing an empty table', async () => {
    serve({}, live({ reasons: [0, 0, 0, 0, 0, 0, 0, 0] }))

    renderApp(CENTRAL)
    await loaded()

    expect(screen.getByText('No rejects today.')).toBeInTheDocument()
    expect(screen.queryByRole('table', { name: 'Top reject reasons' })).not.toBeInTheDocument()
  })
})

describe('refreshing', () => {
  it('refreshes everything by itself every three minutes, pipeline status first', async () => {
    let fixture = LIVE
    const fetchMock = serve({}, () => fixture)

    renderApp(CENTRAL)
    await loaded()
    expect(REFRESH_INTERVAL_MS).toBe(180_000)
    expect(main()).toHaveTextContent('Refreshes automatically every 3 minutes.')

    await pass(REFRESH_INTERVAL_MS - 5000)
    expect(liveRequests(fetchMock)).toHaveLength(6)

    fixture = live({ state: 'degraded', hours: hoursWith({ 9: 20, 10: 25, 11: 40, 12: 18, 13: 30 }), reasons: [3, 0, 1, 0, 0, 2, 0, 4] })
    await pass(10_000)
    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(12))

    const second = liveRequests(fetchMock).slice(6)
    expect(second[0]).toBe(`${API}/pipeline-status`)
    expect(endpoints(second.slice(1)).sort()).toEqual(['checkins/by-destination', 'checkins/by-hour', 'checkins/count', 'rejects/by-reason', 'rejects/count'])
    await waitFor(() => expect(metric('Check-ins today')).toHaveTextContent(/^133$/))
    expect(metric('Status')).toHaveTextContent(/^Degraded$/)
    expect(metric('Current hour')).toHaveTextContent(/^30$/)
    expect(metric('Rejects today')).toHaveTextContent(/^10$/)
    expect(rows('Top reject reasons')[0]).toEqual(['Unknown', '4'])
  })

  it('keeps refreshing, interval after interval', async () => {
    const fetchMock = serve()

    renderApp(CENTRAL)
    await loaded()
    await pass(REFRESH_INTERVAL_MS * 3 + 5000)

    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(24))
  })

  it('stops refreshing by itself while paused, and says so', async () => {
    const fetchMock = serve()
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(screen.getByRole('button', { name: 'Pause automatic refresh' }))

    expect(main()).toHaveTextContent('Automatic refresh is paused.')
    expect(screen.getByRole('button', { name: 'Resume automatic refresh' })).toBeInTheDocument()
    await pass(REFRESH_INTERVAL_MS * 3)
    expect(liveRequests(fetchMock)).toHaveLength(6)
    expect(metric('Check-ins today')).toHaveTextContent(/^120$/)
  })

  it('refreshes everything from the button while paused, and stays paused', async () => {
    let fixture = LIVE
    const fetchMock = serve({}, () => fixture)
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(screen.getByRole('button', { name: 'Pause automatic refresh' }))
    fixture = live({ hours: hoursWith({ 13: 200 }) })
    await person.click(refreshButton())

    await waitFor(() => expect(metric('Check-ins today')).toHaveTextContent(/^200$/))
    const second = liveRequests(fetchMock).slice(6)
    expect(second[0]).toBe(`${API}/pipeline-status`)
    expect(endpoints(second).sort()).toEqual(['checkins/by-destination', 'checkins/by-hour', 'checkins/count', 'pipeline-status', 'rejects/by-reason', 'rejects/count'])
    expect(main()).toHaveTextContent('Automatic refresh is paused.')

    await pass(REFRESH_INTERVAL_MS * 2)
    expect(liveRequests(fetchMock)).toHaveLength(12)
  })

  it('refreshes from the button while running too', async () => {
    const fetchMock = serve()
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(refreshButton())

    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(12))
    expect(main()).toHaveTextContent('Refreshes automatically every 3 minutes.')
  })

  it('starts refreshing by itself again when resumed', async () => {
    const fetchMock = serve()
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(screen.getByRole('button', { name: 'Pause automatic refresh' }))
    await pass(REFRESH_INTERVAL_MS * 2)
    expect(liveRequests(fetchMock)).toHaveLength(6)

    await person.click(screen.getByRole('button', { name: 'Resume automatic refresh' }))
    expect(main()).toHaveTextContent('Refreshes automatically every 3 minutes.')
    await pass(REFRESH_INTERVAL_MS + 5000)

    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(12))
  })

  it('shows that it is refreshing and starts no second refresh on top of the first', async () => {
    const slow = deferred<Response>()
    const fetchMock = serve({ [PIPELINE]: inTurn(() => jsonResponse(200, liveBody.pipeline(LIVE)), () => slow.promise) })
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(refreshButton())

    const busy = await screen.findByRole('button', { name: 'Refreshing…' })
    expect(busy).toHaveAttribute('aria-disabled', 'true')
    await person.click(busy)
    await person.click(busy)
    await pass(1000)
    expect(liveRequests(fetchMock)).toHaveLength(7)
    // What was already loaded stays on screen while the refresh runs.
    expect(metric('Check-ins today')).toHaveTextContent(/^120$/)

    slow.resolve(jsonResponse(200, liveBody.pipeline(LIVE)))
    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(12))
    await waitFor(() => expect(refreshButton()).not.toHaveAttribute('aria-disabled', 'true'))
  })

  it('says when the figures were last updated, in the product zone', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()

    expect(main()).toHaveTextContent(/Last updated Oct 5, 2026, 1:50\sPM CDT\./)
  })

  it('moves to the new day on the first refresh after midnight in the product zone', async () => {
    // 11:58 PM Monday in Chicago.
    vi.setSystemTime(new Date('2026-10-06T04:58:00Z'))
    const fetchMock = serve()

    renderApp(CENTRAL)
    await loaded()
    expect(datesAsked(fetchMock)).toEqual(['2026-10-05', '2026-10-05', '2026-10-05', '2026-10-05', '2026-10-05'])
    expect(main()).toHaveTextContent('Monday, October 5, 2026')
    expect(note('Current hour')).toBe('11 PM–12 AM')

    await pass(REFRESH_INTERVAL_MS + 5000)

    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(12))
    expect(datesAsked(fetchMock).slice(5)).toEqual(['2026-10-06', '2026-10-06', '2026-10-06', '2026-10-06', '2026-10-06'])
    await waitFor(() => expect(main()).toHaveTextContent('Tuesday, October 6, 2026'))
    expect(main()).not.toHaveTextContent('Monday, October 5, 2026')
    await waitFor(() => expect(note('Current hour')).toBe('12–1 AM'))
  })

  it('moves to the new day on a manual refresh after midnight as well', async () => {
    vi.setSystemTime(new Date('2026-10-06T04:59:30Z'))
    const fetchMock = serve()
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(screen.getByRole('button', { name: 'Pause automatic refresh' }))
    await pass(60_000)
    await person.click(refreshButton())

    await waitFor(() => expect(datesAsked(fetchMock).slice(5)).toEqual(['2026-10-06', '2026-10-06', '2026-10-06', '2026-10-06', '2026-10-06']))
    await waitFor(() => expect(main()).toHaveTextContent('Tuesday, October 6, 2026'))
  })
})

describe('changing branch', () => {
  it('starts the other branch from loading, never showing the first branch under it', async () => {
    const eastPipeline = deferred<Response>()
    const fetchMock = serve({ [`GET ${livePath('northbridge', 'east-side')}/pipeline-status`]: () => eastPipeline.promise })
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    expect(metric('Check-ins today')).toHaveTextContent(/^120$/)
    await person.click(within(main()).getByRole('link', { name: 'Northbridge Library' }))
    await person.click(await within(main()).findByRole('link', { name: 'East Side AMH' }))

    expect(await screen.findByText('Loading live data…')).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/120|11 AM–12 PM|Item not found/)

    eastPipeline.resolve(jsonResponse(200, liveBody.pipeline(LIVE)))
    await loaded()
    expect(metric('Check-ins today')).toHaveTextContent(/^7$/)
    expect(metric('Busiest hour')).toHaveTextContent(/^7$/)
    expect(note('Busiest hour')).toBe('8–9 AM')
    const east = liveRequests(fetchMock).filter((url) => url.includes('/east-side/'))
    expect(east).toHaveLength(6)
    expect(liveRequests(fetchMock).filter((url) => url.includes('/central/'))).toHaveLength(6)
  })

  it('starts the other branch running even if the first was paused', async () => {
    serve()
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(screen.getByRole('button', { name: 'Pause automatic refresh' }))
    await person.click(within(main()).getByRole('link', { name: 'Northbridge Library' }))
    await person.click(await within(main()).findByRole('link', { name: 'East Side AMH' }))
    await loaded()

    expect(screen.getByRole('button', { name: 'Pause automatic refresh' })).toBeInTheDocument()
    expect(main()).toHaveTextContent('Refreshes automatically every 3 minutes.')
  })

  it('stops refreshing a branch once its page is left', async () => {
    const fetchMock = serve()
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(within(main()).getByRole('link', { name: 'Northbridge Library' }))
    await screen.findByRole('heading', { level: 3, name: 'Sorting machines' })
    await pass(REFRESH_INTERVAL_MS * 2)

    expect(liveRequests(fetchMock)).toHaveLength(6)
  })

  it('loads a branch afresh when its page is opened again', async () => {
    let fixture = LIVE
    const fetchMock = serve({}, () => fixture)
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(within(main()).getByRole('link', { name: 'Northbridge Library' }))
    fixture = live({ hours: hoursWith({ 13: 300 }) })
    await person.click(await within(main()).findByRole('link', { name: 'Central Library AMH' }))
    await loaded()

    expect(metric('Check-ins today')).toHaveTextContent(/^300$/)
    expect(liveRequests(fetchMock)).toHaveLength(12)
  })
})

describe('a suspended organization', () => {
  it('still shows live data and can still refresh', async () => {
    const fetchMock = serve()
    const person = user()

    renderApp('/organizations/riverside/sorters/main')
    await loaded()

    expect(screen.getByText(/currently suspended/)).toBeInTheDocument()
    expect(metric('Check-ins today')).toHaveTextContent(/^120$/)
    expect(screen.getByRole('button', { name: 'Pause automatic refresh' })).not.toHaveAttribute('aria-disabled', 'true')
    await person.click(refreshButton())
    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(12))
  })
})

describe('a branch with no live data (404)', () => {
  it('says live data is not available, and does not call the branch missing', async () => {
    const fetchMock = serve({ [PIPELINE]: () => jsonResponse(404, TENANT_NOT_FOUND) })

    renderApp(CENTRAL, { retries: true })

    expect(await screen.findByText(UNAVAILABLE)).toHaveRole('note')
    expect(screen.getByRole('heading', { level: 2, name: 'Central Library AMH' })).toBeInTheDocument()
    expect(within(main()).getByRole('link', { name: 'Northbridge Library' })).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/Page not found|not found|mapp|operational|tenant/i)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await pass(10_000)
    // Asked once: not retried, and no dated read was attempted.
    expect(liveRequests(fetchMock)).toEqual([`${API}/pipeline-status`])
  })

  it('says the same when a dated read is the one answered 404', async () => {
    serve({ [REJECT_COUNT]: () => jsonResponse(404, TENANT_NOT_FOUND) })

    renderApp(CENTRAL)

    expect(await screen.findByText(UNAVAILABLE)).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Central Library AMH' })).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('keeps the suspended notice and the page around it', async () => {
    serve({ [`GET ${livePath('riverside', 'main')}/pipeline-status`]: () => jsonResponse(404, TENANT_NOT_FOUND) })

    renderApp('/organizations/riverside/sorters/main')

    expect(await screen.findByText(UNAVAILABLE)).toBeInTheDocument()
    expect(screen.getByText(/currently suspended/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Riverside Main AMH' })).toBeInTheDocument()
  })
})

describe('an expired session (401)', () => {
  it('returns to the sign-in form at the same address when pipeline status is answered 401', async () => {
    const fetchMock = serve({ [PIPELINE]: () => jsonResponse(401, NOT_AUTHENTICATED) })

    renderApp(CENTRAL, { retries: true })

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByText('alice@example.test')).not.toBeInTheDocument()
    expect(screen.getByTestId('address')).toHaveTextContent(CENTRAL)
    await pass(REFRESH_INTERVAL_MS * 2)
    expect(liveRequests(fetchMock)).toEqual([`${API}/pipeline-status`])
  })

  it.each([
    ['checkins/count', CHECKIN_COUNT],
    ['checkins/by-hour', BY_HOUR],
    ['checkins/by-destination', BY_DESTINATION],
    ['rejects/count', REJECT_COUNT],
    ['rejects/by-reason', BY_REASON],
  ])('returns to the sign-in form when %s is answered 401, and asks nothing more', async (_name, route) => {
    const fetchMock = serve({ [route]: () => jsonResponse(401, NOT_AUTHENTICATED) })

    renderApp(CENTRAL, { retries: true })

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    const asked = liveRequests(fetchMock).length
    await pass(REFRESH_INTERVAL_MS * 2)
    expect(liveRequests(fetchMock)).toHaveLength(asked)
    expect(asked).toBeLessThanOrEqual(6)
  })

  it('returns to the sign-in form when a later refresh is answered 401', async () => {
    serve({ [PIPELINE]: inTurn(() => jsonResponse(200, liveBody.pipeline(LIVE)), () => jsonResponse(401, NOT_AUTHENTICATED)) })
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(refreshButton())

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByText('120')).not.toBeInTheDocument()
  })

  it('shows the next person nothing that was loaded before they signed in', async () => {
    const pipeline = deferred<Response>()
    serve({
      [PIPELINE]: inTurn(() => jsonResponse(200, liveBody.pipeline(LIVE)), () => jsonResponse(401, NOT_AUTHENTICATED), () => pipeline.promise),
      'POST /api/auth/login': () => jsonResponse(200, { id: 8, email: 'bob@example.test', full_name: 'Bob Example' }),
    })
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(refreshButton())
    await screen.findByRole('button', { name: 'Sign in' })
    await person.type(screen.getByLabelText('Email'), 'bob@example.test')
    await person.type(screen.getByLabelText('Password'), 'pw{Enter}')

    expect(await screen.findByText('Loading live data…')).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/120|Item not found/)
  })
})

describe('failures', () => {
  it.each([
    ['a server error', SERVER_ERROR],
    ['a crash page', () => textResponse(502, 'Traceback: customer_id=41 branch_id=7')],
    ['a network failure', () => Promise.reject(networkFailure())],
    ['a malformed answer', () => jsonResponse(200, { ...liveBody.pipeline(LIVE), state: 'stale' })],
    ['an invalid time zone', () => jsonResponse(200, { ...liveBody.pipeline(LIVE), timezone: 'Central Time' })],
    ['a rate limit', () => jsonResponse(429, { error: 'Rate limit exceeded: 60 per 1 minute' })],
    ['a validation error', () => jsonResponse(422, { code: 'validation_error', detail: [{ loc: ['query', 'date'], msg: 'bad' }] })],
  ])('shows a safe message when pipeline status fails with %s, and the dashboard after trying again', async (_label, fail) => {
    const fetchMock = serve({ [PIPELINE]: inTurn(fail, () => jsonResponse(200, liveBody.pipeline(LIVE))) })
    const person = user()

    renderApp(CENTRAL)

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).not.toMatch(/Traceback|customer_id|41|Failed to fetch|stale|Central Time|60 per|\[object|loc/)
    expect(alert.textContent?.length).toBeLessThan(120)
    expect(screen.getByRole('heading', { level: 2, name: 'Central Library AMH' })).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(UNAVAILABLE)

    await person.click(screen.getByRole('button', { name: 'Try again' }))

    await loaded()
    expect(metric('Check-ins today')).toHaveTextContent(/^120$/)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(liveRequests(fetchMock)).toHaveLength(7)
  })

  it('keeps the sections that loaded when one read fails, and says which could not load', async () => {
    serve({ [BY_REASON]: SERVER_ERROR })

    renderApp(CENTRAL)
    await loaded()

    expect(await screen.findByRole('alert')).toHaveTextContent('Some live data could not be loaded. Use Refresh to try again.')
    expect(metric('Status')).toHaveTextContent(/^OK$/)
    expect(metric('Check-ins today')).toHaveTextContent(/^120$/)
    expect(metric('Rejects today')).toHaveTextContent(/^6$/)
    expect(metric('Reject rate')).toHaveTextContent(/^5\.0%$/)
    expect(hourlyRows()).toHaveLength(24)
    const reasons = screen.getByRole('heading', { level: 3, name: 'Top reject reasons' }).parentElement as HTMLElement
    expect(reasons).toHaveTextContent('Could not load.')
    expect(screen.queryByRole('table', { name: 'Top reject reasons' })).not.toBeInTheDocument()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
  })

  it('shows no figure that depends on a read that failed, and never a zero in its place', async () => {
    serve({ [CHECKIN_COUNT]: SERVER_ERROR, [BY_HOUR]: () => jsonResponse(200, { hours: 'none' }) })

    renderApp(CENTRAL)
    await screen.findByRole('alert')
    await waitFor(() => expect(metric('Rejects today')).toHaveTextContent(/^6$/))

    expect(metric('Check-ins today')).toHaveTextContent('Could not load')
    expect(metric('Current hour')).toHaveTextContent('Could not load')
    expect(note('Current hour')).toBeNull()
    expect(metric('Busiest hour')).toHaveTextContent('Could not load')
    expect(metric('Reject rate')).toHaveTextContent('Could not load')
    expect(metric('Reject rate')).not.toHaveTextContent('%')
    expect(main()).not.toHaveTextContent(/NaN|Infinity/)
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Show hourly table' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'Hourly check-ins' }).parentElement).toHaveTextContent('Could not load.')
    expect(rows('Top reject reasons')).toHaveLength(3)
  })

  it('recovers the failed section on the next refresh', async () => {
    serve({ [BY_REASON]: inTurn(SERVER_ERROR, (url) => jsonResponse(200, liveBody.rejectsByReason(LIVE, new URL(url, 'http://t.invalid').searchParams.get('date')))) })
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await screen.findByRole('alert')
    await person.click(refreshButton())

    await waitFor(() => expect(rows('Top reject reasons')).toHaveLength(3))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  })

  it('keeps showing the last good figures, and says so, when a refresh fails', async () => {
    serve({
      [PIPELINE]: inTurn(() => jsonResponse(200, liveBody.pipeline(LIVE)), SERVER_ERROR, () => jsonResponse(200, liveBody.pipeline(LIVE))),
    })
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(refreshButton())

    expect(await screen.findByRole('alert')).toHaveTextContent('The latest refresh failed. Some of what is shown may be out of date.')
    expect(metric('Check-ins today')).toHaveTextContent(/^120$/)
    expect(metric('Status')).toHaveTextContent(/^OK$/)
    expect(hourlyRows()).toHaveLength(24)

    await person.click(refreshButton())
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  })
})

describe('retries', () => {
  it('tries a failing read three times in all, then reports it', async () => {
    const fetchMock = serve({ [PIPELINE]: SERVER_ERROR })

    renderApp(CENTRAL, { retries: true })

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(liveRequests(fetchMock)).toEqual([`${API}/pipeline-status`, `${API}/pipeline-status`, `${API}/pipeline-status`])
  })

  it('gets past a failure that clears up, without showing an error', async () => {
    const fetchMock = serve({
      [PIPELINE]: inTurn(() => Promise.reject(networkFailure()), () => jsonResponse(200, liveBody.pipeline(LIVE))),
    })

    renderApp(CENTRAL, { retries: true })
    await loaded()

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(liveRequests(fetchMock)).toHaveLength(7)
  })

  it.each([
    ['a rate limit', () => jsonResponse(429, { error: 'Rate limit exceeded' })],
    ['a validation error', () => jsonResponse(422, { code: 'validation_error', detail: [] })],
    ['a malformed answer', () => jsonResponse(200, { state: 'ok' })],
  ])('asks only once after %s', async (_label, fail) => {
    const fetchMock = serve({ [PIPELINE]: fail })

    renderApp(CENTRAL, { retries: true })

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    await pass(10_000)
    expect(liveRequests(fetchMock)).toEqual([`${API}/pipeline-status`])
  })
})

describe('the hourly chart', () => {
  it('draws one bar for each of the 24 hours, each as tall as its share of the busiest', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()

    const heights = barHeights()
    expect(heights).toHaveLength(24)
    // 20, 25, 40, 18 and 17 check-ins against an axis that tops out at 40.
    expect(heights.slice(9, 14)).toEqual([50, 62.5, 100, 45, 42.5])
    expect(heights.filter((height) => height === 0)).toHaveLength(19)
    expect(Array.from(chart().querySelectorAll('rect[data-hour]')).map((bar) => bar.getAttribute('data-hour'))).toEqual(
      Array.from({ length: 24 }, (_, hour) => String(hour)),
    )
  })

  it('is one named image, described in words, with the product zone', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()

    expect(screen.getAllByRole('img')).toEqual([chart()])
    expect(chart()).toHaveAccessibleDescription(
      'Busiest hour: 11 AM–12 PM, 40 check-ins. Current hour: 1–2 PM, 17 check-ins. Hours are in America/Chicago time.',
    )
    // The description is a sentence anyone can read, not text kept for screen readers alone.
    expect(screen.getByText(/^Busiest hour: 11 AM–12 PM/)).toBeVisible()
  })

  it('keeps the drawing out of the accessibility tree and out of the tab order', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()

    const drawing = chart().querySelector('svg') as SVGElement
    expect(drawing).toHaveAttribute('aria-hidden', 'true')
    expect(drawing).toHaveAttribute('focusable', 'false')
    expect(chart().querySelectorAll('a, button, input, [tabindex], [onclick], title')).toHaveLength(0)
    expect(chart()).not.toHaveAttribute('tabindex')
    // Everything else in the chart is decoration around the drawing, hidden the same way.
    for (const part of Array.from(chart().children)) {
      expect(part.getAttribute('aria-hidden') === 'true' || part.querySelector('svg[aria-hidden="true"]') !== null).toBe(true)
    }
  })

  it('stretches to its container instead of having a size of its own', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()

    const drawing = chart().querySelector('svg') as SVGElement
    expect(drawing).toHaveAttribute('viewBox', '0 0 24 100')
    expect(drawing).toHaveAttribute('preserveAspectRatio', 'none')
    expect(drawing).not.toHaveAttribute('width')
    expect(drawing).not.toHaveAttribute('height')
  })

  it('marks the current hour with a word over its column, not with colour alone', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()

    const now = within(chart()).getByText('Now')
    // 1 PM is the fourteenth of the 24 columns.
    expect(now.style.gridColumn).toBe('14')
    expect(within(chart()).getAllByText('Now')).toHaveLength(1)
    expect(chart().querySelector('rect[data-current-hour]')).toHaveAttribute('x', '13')
    expect(chart()).toHaveAccessibleDescription(/Current hour: 1–2 PM, 17 check-ins\./)
  })

  it('labels the axis every few hours rather than all 24', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()

    const labels = Array.from(chart().querySelectorAll('.chart-x span')).map((label) => label.textContent)
    expect(labels).toEqual(['12 AM', '3 AM', '6 AM', '9 AM', '12 PM', '3 PM', '6 PM', '9 PM'])
    expect(Array.from(chart().querySelectorAll('.chart-y span')).map((label) => label.textContent)).toEqual(['0', '10', '20', '30', '40'])
  })

  it('shows an empty plot that says so on a day with no check-ins', async () => {
    serve({}, live({ hours: hoursWith({}), reasons: [0, 0, 0, 0, 0, 0, 0, 0] }))

    renderApp(CENTRAL)
    await loaded()

    expect(barHeights()).toEqual(Array.from({ length: 24 }, () => 0))
    expect(within(chart()).getByText('No check-ins yet today')).toBeInTheDocument()
    expect(chart()).toHaveAccessibleDescription(
      'No check-ins yet today. Current hour: 1–2 PM, 0 check-ins. Hours are in America/Chicago time.',
    )
    expect(Array.from(chart().querySelectorAll('.chart-y span')).map((label) => label.textContent)).toEqual(['0', '1'])
    expect(within(chart()).getByText('Now')).toBeInTheDocument()
    expect(hourlyRows().map(([, checkins]) => checkins)).toEqual(Array.from({ length: 24 }, () => '0'))
    expect(main()).not.toHaveTextContent(/NaN|Infinity/)
  })

  it('fits one very large hour, and still shows the small ones beside it', async () => {
    serve({}, live({ hours: hoursWith({ 10: 1_000_000, 11: 3, 13: 1 }) }))

    renderApp(CENTRAL)
    await loaded()

    const heights = barHeights()
    expect(Math.max(...heights)).toBe(100)
    expect(heights[10]).toBe(100)
    // Too small to draw to scale, so drawn at the smallest height a bar may have -- not left out like a zero.
    expect(heights[11]).toBeGreaterThan(0)
    expect(heights[11]).toBeLessThan(5)
    expect(heights[13]).toBeGreaterThan(0)
    expect(heights[12]).toBe(0)
    expect(Array.from(chart().querySelectorAll('.chart-y span')).map((label) => label.textContent)).toEqual(['0', '500K', '1M'])
    expect(metric('Busiest hour')).toHaveTextContent(/^1,000,000$/)
    expect(hourlyRows()[10]).toEqual(['10 AM', '1,000,000'])
    expect(hourlyRows()[11]).toEqual(['11 AM', '3'])
  })

  it('gives every exact figure in a table that opens and closes from a button', async () => {
    serve()
    const person = user()

    renderApp(CENTRAL)
    await loaded()

    const toggle = screen.getByRole('button', { name: 'Show hourly table' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('table', { name: 'Hourly check-ins' })).not.toBeInTheDocument()

    await person.click(toggle)

    const open = screen.getByRole('button', { name: 'Hide hourly table' })
    expect(open).toHaveAttribute('aria-expanded', 'true')
    const table = screen.getByRole('table', { name: 'Hourly check-ins' })
    expect(document.getElementById(open.getAttribute('aria-controls') ?? '')).toContainElement(table)
    expect(rows('Hourly check-ins')).toHaveLength(24)
    const current = within(table).getByRole('row', { current: true })
    expect(current).toHaveTextContent('1 PM (current hour)17')

    await person.click(open)
    expect(screen.queryByRole('table', { name: 'Hourly check-ins' })).not.toBeInTheDocument()
  })

  it('keeps the table open, with new figures, across a refresh', async () => {
    let fixture = LIVE
    serve({}, () => fixture)
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(screen.getByRole('button', { name: 'Show hourly table' }))
    fixture = live({ hours: hoursWith({ 13: 55 }) })
    await person.click(refreshButton())

    await waitFor(() => expect(rows('Hourly check-ins')[13]).toEqual(['1 PM (current hour)', '55']))
    expect(barHeights()[13]).toBeGreaterThan(90)
  })
})

describe('the pipeline panel', () => {
  it.each([
    ['ok', 'OK'],
    ['degraded', 'Degraded'],
    ['failed', 'Failed'],
    ['unknown', 'Unknown'],
  ])('gives the state %s an icon of its own beside its word', async (state, label) => {
    serve({}, live({ state }))

    renderApp(CENTRAL)
    await loaded()

    const status = metric('Status')
    expect(status).toHaveTextContent(new RegExp(`^${label}$`))
    const icon = status.querySelector('svg') as SVGElement
    expect(icon).toHaveAttribute('aria-hidden', 'true')
    // The icon's shape, which no other state shares: so the state shows without its colour.
    const shape = Array.from(icon.querySelectorAll('path'))
      .map((path) => `${path.getAttribute('d')} ${path.getAttribute('stroke-dasharray') ?? 'solid'}`)
      .join(' | ')
    expect(ICON_SHAPES[state]).toBeUndefined()
    ICON_SHAPES[state] = shape
    expect(Object.values(ICON_SHAPES).filter((other) => other === shape)).toHaveLength(1)
  })

  it('keeps the state and the time of the report as two separate facts', async () => {
    serve({}, live({ state: 'failed', last_reported_at: '2026-09-01T15:00:00Z' }))

    renderApp(CENTRAL)
    await loaded()

    expect(metric('Status')).toHaveTextContent(/^Failed$/)
    expect(metric('Status').querySelector('time')).toBeNull()
    expect(metric('Last reported')).toHaveTextContent(/^Sep 1, 2026, 10:00\sAM CDT$/)
  })
})

describe('the first load', () => {
  it('holds the page with empty blocks that say nothing, and announces the loading in words', async () => {
    const pipeline = deferred<Response>()
    serve({ [PIPELINE]: () => pipeline.promise })

    renderApp(CENTRAL)

    expect(await screen.findByText('Loading live data…')).toHaveRole('status')
    expect(screen.getAllByRole('status')).toHaveLength(1)
    const outline = document.querySelector('.skeleton') as HTMLElement
    expect(outline).toHaveAttribute('aria-hidden', 'true')
    expect(outline.textContent).toBe('')
    expect(screen.getByRole('heading', { level: 2, name: 'Central Library AMH' })).toBeInTheDocument()
    expect(within(main()).getByRole('link', { name: 'Northbridge Library' })).toBeInTheDocument()
    expect(main().textContent).not.toMatch(/\d/)
    expect(screen.queryByRole('term')).not.toBeInTheDocument()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(within(main()).queryByRole('button')).not.toBeInTheDocument()
  })

  it('says each figure is loading, and shows no number for it, until its own read answers', async () => {
    const byHour = deferred<Response>()
    serve({ [BY_HOUR]: () => byHour.promise })

    renderApp(CENTRAL)
    await waitFor(() => expect(metric('Check-ins today')).toHaveTextContent(/^120$/))

    expect(metric('Current hour')).toHaveTextContent(/^Loading…$/)
    expect(metric('Busiest hour')).toHaveTextContent(/^Loading…$/)
    expect(note('Current hour')).toBeNull()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'Hourly check-ins' }).parentElement).toHaveTextContent(/Loading…$/)

    byHour.resolve(jsonResponse(200, liveBody.checkinsByHour(LIVE, TODAY)))
    await loaded()
    expect(metric('Current hour')).toHaveTextContent(/^17$/)
  })
})

describe('what is said aloud', () => {
  /** The polite live region on the loaded dashboard. */
  const announcer = () => screen.getByRole('status')

  it('says nothing until the person does something', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()

    expect(announcer()).toBeEmptyDOMElement()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('confirms pausing and resuming', async () => {
    serve()
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(screen.getByRole('button', { name: 'Pause automatic refresh' }))
    expect(announcer()).toHaveTextContent(/^Automatic refresh paused\.$/)

    await person.click(screen.getByRole('button', { name: 'Resume automatic refresh' }))
    expect(announcer()).toHaveTextContent(/^Automatic refresh resumed\.$/)
  })

  it('confirms a refresh the person asked for, once the new data is in', async () => {
    const slow = deferred<Response>()
    serve({ [PIPELINE]: inTurn(() => jsonResponse(200, liveBody.pipeline(LIVE)), () => slow.promise) })
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await pass(60_000)
    await person.click(refreshButton())
    await screen.findByRole('button', { name: 'Refreshing…' })
    expect(announcer()).toBeEmptyDOMElement()

    slow.resolve(jsonResponse(200, liveBody.pipeline(LIVE)))

    await waitFor(() => expect(announcer()).toHaveTextContent(/^Live data refreshed\.$/))
    await waitFor(() => expect(refreshButton()).not.toHaveAttribute('aria-disabled', 'true'))
  })

  it('leaves a failed refresh to the alert, and does not also call it refreshed', async () => {
    serve({ [PIPELINE]: inTurn(() => jsonResponse(200, liveBody.pipeline(LIVE)), SERVER_ERROR) })
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(refreshButton())

    expect(await screen.findByRole('alert')).toHaveTextContent('The latest refresh failed.')
    expect(announcer()).toBeEmptyDOMElement()
  })

  it('does not announce a timed refresh, or the figures it brings', async () => {
    let fixture = LIVE
    const fetchMock = serve({}, () => fixture)

    renderApp(CENTRAL)
    await loaded()
    fixture = live({ hours: hoursWith({ 13: 300 }) })
    await pass(REFRESH_INTERVAL_MS + 5000)
    await waitFor(() => expect(metric('Check-ins today')).toHaveTextContent(/^300$/))
    await pass(REFRESH_INTERVAL_MS)
    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(18))

    expect(announcer()).toBeEmptyDOMElement()
    // Nothing that changes with a refresh sits in a live region: not the figures, not the time of the update.
    const liveRegions = Array.from(document.querySelectorAll('[role="status"], [role="alert"], [role="log"], [aria-live]'))
    expect(liveRegions).toEqual([announcer()])
    expect(screen.getByText(/Last updated/).closest('[role="status"], [aria-live]')).toBeNull()
  })

  it('shows whether automatic refresh is on, and when the data was last updated, without a countdown', async () => {
    serve()
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    const status = screen.getByText(/Refreshes automatically every 3 minutes\./).closest('p') as HTMLElement
    expect(status).toHaveTextContent(/^Refreshes automatically every 3 minutes\. Last updated Oct 5, 2026, 1:50\sPM CDT\.$/)
    expect(within(status).getByText(/1:50\sPM CDT/)).toHaveAttribute('datetime', '2026-10-05T18:50:00.000Z')

    // A minute passes with no refresh: nothing on the page counts it down.
    const before = main().textContent
    await pass(60_000)
    expect(main().textContent).toBe(before)

    await person.click(screen.getByRole('button', { name: 'Pause automatic refresh' }))
    expect(status).toHaveTextContent(/^Automatic refresh is paused\. Last updated Oct 5, 2026, 1:50\sPM CDT\.$/)
    // The interval is not something the person can set.
    for (const role of ['spinbutton', 'slider', 'combobox', 'textbox', 'radio', 'checkbox', 'timer', 'progressbar']) {
      expect(screen.queryByRole(role)).not.toBeInTheDocument()
    }
  })

  it('names the parts that failed where they are, and leaves the rest alone', async () => {
    serve({ [CHECKIN_COUNT]: SERVER_ERROR })

    renderApp(CENTRAL)
    await loaded()
    await screen.findByRole('alert')

    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(screen.getByRole('alert')).toHaveTextContent(/^Some live data could not be loaded\. Use Refresh to try again\.$/)
    const failed = screen.getAllByRole('term').filter((term) => /^Could not load$/.test(term.nextElementSibling?.textContent ?? ''))
    expect(failed.map((term) => term.textContent)).toEqual(['Check-ins today', 'Reject rate'])
    expect(metric('Current hour')).toHaveTextContent(/^17$/)
    expect(metric('Rejects today')).toHaveTextContent(/^6$/)
    expect(barHeights()).toHaveLength(24)
    expect(refreshButton()).not.toHaveAttribute('aria-disabled', 'true')
  })
})

describe('trying again after pipeline status could not be read', () => {
  it('starts one retry, not two, when Try again is clicked twice at once', async () => {
    const slow = deferred<Response>()
    const fetchMock = serve({ [PIPELINE]: inTurn(SERVER_ERROR, () => slow.promise) })

    renderApp(CENTRAL)
    await screen.findByRole('alert')
    expect(liveRequests(fetchMock)).toHaveLength(1)
    const retry = screen.getByRole('button', { name: 'Try again' })
    expect(retry).not.toHaveAttribute('disabled')
    // Two activations before the page has had a chance to redraw.
    act(() => {
      fireEvent.click(retry)
      fireEvent.click(retry)
    })
    await pass(1000)

    expect(liveRequests(fetchMock)).toEqual([`${API}/pipeline-status`, `${API}/pipeline-status`])
    // While the retry runs the page is loading again: there is no button left to press a third time.
    expect(screen.getByText('Loading live data…')).toHaveRole('status')
    expect(screen.queryByRole('button', { name: /Try again|Trying again/ })).not.toBeInTheDocument()

    slow.resolve(jsonResponse(200, liveBody.pipeline(LIVE)))
    await loaded()
    expect(metric('Check-ins today')).toHaveTextContent(/^120$/)
    expect(liveRequests(fetchMock)).toHaveLength(7)
  })

  it('starts one retry from the keyboard, however often the key is pressed', async () => {
    const slow = deferred<Response>()
    const fetchMock = serve({ [PIPELINE]: inTurn(SERVER_ERROR, () => slow.promise) })
    const person = user()

    renderApp(CENTRAL)
    await screen.findByRole('alert')
    screen.getByRole('button', { name: 'Try again' }).focus()
    await person.keyboard('{Enter}')
    await person.keyboard('{Enter}')
    await person.keyboard(' ')
    await pass(1000)

    expect(liveRequests(fetchMock)).toEqual([`${API}/pipeline-status`, `${API}/pipeline-status`])

    slow.resolve(jsonResponse(200, liveBody.pipeline(LIVE)))
    await loaded()
    expect(liveRequests(fetchMock)).toHaveLength(7)
  })

  it('offers Try again once more, and asks once more, when the retry fails too', async () => {
    const fetchMock = serve({ [PIPELINE]: SERVER_ERROR })
    const person = user()

    renderApp(CENTRAL)
    await screen.findByRole('alert')
    await person.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(2))

    const again = await screen.findByRole('button', { name: 'Try again' })
    expect(again).not.toHaveAttribute('aria-disabled', 'true')
    await person.click(again)
    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(3))
  })
})

describe('today in three groups', () => {
  /** The labels of the figures in one named group, in order. */
  const figuresIn = (name: string) =>
    within(screen.getByRole('group', { name }))
      .getAllByRole('term')
      .map((term) => term.textContent)

  it('groups the figures as Operations, Routing and Rejects, in that order', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()
    await screen.findByText('Westside', { selector: 'dt' })

    const today = screen.getByRole('region', { name: 'Today' })
    expect(within(today).getAllByRole('group').map((group) => group.getAttribute('aria-labelledby'))).toEqual([
      'operations-heading',
      'routing-heading',
      'rejects-heading',
    ])
    expect(within(today).getAllByRole('heading', { level: 4 }).map((heading) => heading.textContent)).toEqual([
      'Operations',
      'Routing',
      'Rejects',
    ])
    expect(figuresIn('Operations')).toEqual(['Check-ins today', 'Current hour', 'Busiest hour'])
    expect(figuresIn('Routing')).toEqual(['Total transit', 'Westside', 'Library Express'])
    expect(figuresIn('Rejects')).toEqual(['Rejects today', 'Reject rate'])
    expect(today).toHaveTextContent('Monday, October 5, 2026 (America/Chicago)')
  })

  it('says what the page is: this sorter site, not everything that belongs to the branch', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()

    const heading = screen.getByRole('heading', { level: 2, name: 'Central Library AMH' })
    expect(heading).toBeInTheDocument()
    expect(screen.getByText('Live activity for this sorter, at Central Branch')).toHaveClass('page-context')
    expect(screen.getByRole('group', { name: 'Routing' })).toHaveTextContent('Where this sorter sent today’s check-ins.')
    expect(main()).not.toHaveTextContent(/installation|machine|Tech Logic|UltraSort/i)
  })

  it('keeps the meanings of the operations and rejects figures', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()
    await screen.findByText('Westside', { selector: 'dt' })

    expect(metric('Check-ins today')).toHaveTextContent(/^120$/)
    expect(metric('Current hour')).toHaveTextContent(/^17$/)
    expect(note('Current hour')).toBe('1–2 PM')
    expect(metric('Busiest hour')).toHaveTextContent(/^40$/)
    expect(note('Busiest hour')).toBe('11 AM–12 PM')
    expect(metric('Rejects today')).toHaveTextContent(/^6$/)
    expect(metric('Reject rate')).toHaveTextContent(/^5\.0%$/)
  })

  it('takes the current hour from the clock, not from the latest hour with activity', async () => {
    // 3:10 PM: the last check-in was in the 1 PM hour, and the current hour is still the 3 PM one.
    vi.setSystemTime(new Date('2026-10-05T20:10:00Z'))
    serve()

    renderApp(CENTRAL)
    await loaded()

    expect(note('Current hour')).toBe('3–4 PM')
    expect(metric('Current hour')).toHaveTextContent(/^0$/)
  })
})

describe('routing', () => {
  const routing = () => screen.getByRole('group', { name: 'Routing' })
  const routed = (transit: RoutedTo[], changes: Partial<NonNullable<LiveFixture['routing']>> = {}) =>
    live({ routing: { transit, ...changes } })
  /** Waits until the routing figures are on the page. */
  const shown = () => waitFor(() => expect(routing()).toHaveTextContent(/Kept at /))

  it('shows the total in transit and one figure for each configured destination, each with its share of today', async () => {
    serve({}, routed([['westside', 'Westside', 13], ['library_express', 'Library Express', 2]]))

    renderApp(CENTRAL)
    await loaded()
    await shown()

    expect(metric('Total transit')).toHaveTextContent(/^15$/)
    expect(note('Total transit')).toBe('12.5% of today')
    expect(metric('Westside')).toHaveTextContent(/^13$/)
    expect(note('Westside')).toBe('10.8% of today')
    expect(metric('Library Express')).toHaveTextContent(/^2$/)
    expect(note('Library Express')).toBe('1.7% of today')
  })

  it('accounts for the rest: what was kept at home, in words under the figures', async () => {
    serve({}, routed([['westside', 'Westside', 13]], { home: 'Central' }))

    renderApp(CENTRAL)
    await loaded()
    await shown()

    expect(routing()).toHaveTextContent('Kept at Central: 107.')
    // Home is not a transit destination and is not given a card.
    expect(within(routing()).queryByText('Central', { selector: 'dt' })).not.toBeInTheDocument()
    expect(routing()).not.toHaveTextContent(/Other routing/)
  })

  it('shows a destination with no check-ins today, at zero', async () => {
    serve({}, routed([['westside', 'Westside', 9], ['library_express', 'Library Express', 0]]))

    renderApp(CENTRAL)
    await loaded()
    await shown()

    expect(metric('Library Express')).toHaveTextContent(/^0$/)
    expect(note('Library Express')).toBe('0.0% of today')
  })

  it('shows whatever destinations the site has, in the order given, however many', async () => {
    const stops: RoutedTo[] = Array.from({ length: 12 }, (_, index) => [`stop_${index + 1}`, `Stop ${index + 1}`, index])
    serve({}, routed(stops))

    renderApp(CENTRAL)
    await loaded()
    await shown()

    const labels = within(routing()).getAllByRole('term').map((term) => term.textContent)
    expect(labels).toEqual(['Total transit', ...stops.map(([, label]) => label)])
    expect(metric('Total transit')).toHaveTextContent(/^66$/)
    expect(metric('Stop 12')).toHaveTextContent(/^11$/)
    expect(main()).not.toHaveTextContent(/Westside|Library Express/)
  })

  it('shows a long destination name whole', async () => {
    const long = 'Bartholomew-Featherstonehaugh Memorial Neighborhood Library and Community Learning Annex (East Riverside)'
    serve({}, routed([['annex', long, 4]]))

    renderApp(CENTRAL)
    await loaded()
    await shown()

    expect(screen.getByText(long, { selector: 'dt' })).toBeVisible()
    expect(metric(long)).toHaveTextContent(/^4$/)
  })

  it('shows check-ins that went neither home nor to a configured destination, plainly and without alarm', async () => {
    serve({}, routed([['westside', 'Westside', 13]], { other: 3 }))

    renderApp(CENTRAL)
    await loaded()
    await shown()

    expect(routing()).toHaveTextContent('Kept at Main: 104. Other routing: 3.')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(routing().querySelector('.metric-failed')).toBeNull()
  })

  it('shows every figure adding up to the day’s check-ins', async () => {
    serve({}, routed([['westside', 'Westside', 13], ['library_express', 'Library Express', 2]], { other: 5 }))

    renderApp(CENTRAL)
    await loaded()
    await shown()

    // 100 kept + 15 in transit + 5 other = the 120 check-ins in Operations.
    expect(routing()).toHaveTextContent('Kept at Main: 100. Other routing: 5.')
    expect(metric('Total transit')).toHaveTextContent(/^15$/)
    expect(metric('Check-ins today')).toHaveTextContent(/^120$/)
  })

  it('says so when the site has no destinations configured, and still accounts for the day', async () => {
    serve({}, routed([], { other: 2 }))

    renderApp(CENTRAL)
    await loaded()
    await shown()

    expect(routing()).toHaveTextContent('No transit destinations are configured for this sorter.')
    expect(routing()).toHaveTextContent('Kept at Main: 118. Other routing: 2.')
    expect(within(routing()).queryByRole('term')).not.toBeInTheDocument()
  })

  it('shows zeros and no percentage on a day with no check-ins', async () => {
    serve({}, live({ hours: hoursWith({}), reasons: [0, 0, 0, 0, 0, 0, 0, 0] }))

    renderApp(CENTRAL)
    await loaded()
    await shown()

    expect(metric('Total transit')).toHaveTextContent(/^0$/)
    expect(note('Total transit')).toBe('No check-ins yet')
    expect(metric('Westside')).toHaveTextContent(/^0$/)
    expect(note('Westside')).toBe('No check-ins yet')
    expect(routing()).toHaveTextContent('Kept at Main: 0.')
    expect(main()).not.toHaveTextContent(/NaN|Infinity|%/)
  })

  it('writes large figures with separators and a share to one decimal place', async () => {
    serve({}, live({ hours: hoursWith({ 10: 1_250_000, 11: 67 }), routing: { transit: [['westside', 'Westside', 137_000]], other: 1 } }))

    renderApp(CENTRAL)
    await loaded()
    await shown()

    expect(metric('Westside')).toHaveTextContent(/^137,000$/)
    expect(note('Westside')).toBe('11.0% of today')
    expect(routing()).toHaveTextContent('Kept at Main: 1,113,066. Other routing: 1.')
  })

  it('makes no destination a link, a button or anything else that could be followed', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()
    await shown()

    for (const role of ['link', 'button', 'tab', 'menuitem', 'img']) {
      expect(within(routing()).queryByRole(role)).not.toBeInTheDocument()
    }
    expect(routing().querySelectorAll('a, button, [tabindex], [onclick], [href]')).toHaveLength(0)
    // The only links on the page are still the two in the breadcrumb.
    expect(within(main()).getAllByRole('link').map((link) => link.textContent)).toEqual([
      'Organizations',
      'Northbridge Library',
      'Live Today',
      'Reports',
    ])
  })

  it('adds no live region: routing figures are read when reached, not announced', async () => {
    let fixture = LIVE
    const fetchMock = serve({}, () => fixture)

    renderApp(CENTRAL)
    await loaded()
    await shown()
    fixture = routed([['westside', 'Westside', 40]])
    await pass(REFRESH_INTERVAL_MS + 5000)
    await waitFor(() => expect(metric('Westside')).toHaveTextContent(/^40$/))

    expect(liveRequests(fetchMock)).toHaveLength(12)
    expect(routing().closest('[role="status"], [role="alert"], [aria-live]')).toBeNull()
    expect(routing().querySelector('[role="status"], [role="alert"], [aria-live]')).toBeNull()
    expect(screen.getByRole('status')).toBeEmptyDOMElement()
  })

  it('leaves the chart, the pipeline panel and the reject reasons as they were', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()
    await shown()

    expect(barHeights().slice(9, 14)).toEqual([50, 62.5, 100, 45, 42.5])
    expect(chart()).toHaveAccessibleDescription(
      'Busiest hour: 11 AM–12 PM, 40 check-ins. Current hour: 1–2 PM, 17 check-ins. Hours are in America/Chicago time.',
    )
    expect(metric('Status')).toHaveTextContent(/^OK$/)
    expect(metric('Last reported')).toHaveTextContent(/Oct 5, 2026, 1:45\sPM CDT/)
    expect(rows('Top reject reasons')).toEqual([
      ['Item not found', '3'],
      ['Communication error', '2'],
      ['RFID collision', '1'],
    ])
  })
})

describe('the routing read', () => {
  const routing = () => screen.getByRole('group', { name: 'Routing' })

  it('waits for pipeline status like every other dated read, and asks for the same date', async () => {
    const pipeline = deferred<Response>()
    const fetchMock = serve({ [PIPELINE]: () => pipeline.promise })

    renderApp(CENTRAL)
    await screen.findByText('Loading live data…')
    await pass(1000)
    expect(liveRequests(fetchMock)).toEqual([`${API}/pipeline-status`])

    pipeline.resolve(jsonResponse(200, liveBody.pipeline(LIVE)))
    await loaded()

    expect(liveRequests(fetchMock)).toContain(`${API}/checkins/by-destination?date=${TODAY}`)
    expect(liveRequests(fetchMock).filter((url) => url.includes('/checkins/by-destination'))).toHaveLength(1)
  })

  it('asks for the product’s date, not this machine’s', async () => {
    // 9:30 PM Monday in Chicago is already Tuesday in Tokyo.
    vi.setSystemTime(new Date('2026-10-06T02:30:00Z'))
    const fetchMock = serve({}, live({ timezone: 'Asia/Tokyo' }))

    renderApp(CENTRAL)
    await loaded()

    expect(liveRequests(fetchMock)).toContain(`${API}/checkins/by-destination?date=2026-10-06`)
  })

  it('is refreshed by the timer, by the button, and by the button while paused', async () => {
    const fetchMock = serve()
    const person = user()
    const asked = () => liveRequests(fetchMock).filter((url) => url.includes('/checkins/by-destination')).length

    renderApp(CENTRAL)
    await loaded()
    expect(asked()).toBe(1)

    await pass(REFRESH_INTERVAL_MS + 5000)
    await waitFor(() => expect(asked()).toBe(2))

    await person.click(refreshButton())
    await waitFor(() => expect(asked()).toBe(3))
    await waitFor(() => expect(refreshButton()).not.toHaveAttribute('aria-disabled', 'true'))

    await person.click(screen.getByRole('button', { name: 'Pause automatic refresh' }))
    await pass(REFRESH_INTERVAL_MS * 2)
    expect(asked()).toBe(3)
    await person.click(refreshButton())
    await waitFor(() => expect(asked()).toBe(4))
    // Six reads each time: the first load and three refreshes.
    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(24))
  })

  it('moves to the new day with the rest on the first refresh after midnight', async () => {
    vi.setSystemTime(new Date('2026-10-06T04:58:00Z'))      // 11:58 PM Monday in Chicago
    const fetchMock = serve()

    renderApp(CENTRAL)
    await loaded()
    await pass(REFRESH_INTERVAL_MS + 5000)

    await waitFor(() => expect(liveRequests(fetchMock)).toContain(`${API}/checkins/by-destination?date=2026-10-06`))
    expect(liveRequests(fetchMock).filter((url) => url.includes('/checkins/by-destination'))).toEqual([
      `${API}/checkins/by-destination?date=2026-10-05`,
      `${API}/checkins/by-destination?date=2026-10-06`,
    ])
  })

  it('starts clean on another branch, never showing the first branch’s destinations under it', async () => {
    const eastRouting = deferred<Response>()
    serve(
      {
        [`GET ${livePath('northbridge', 'east-side')}/checkins/by-destination?date=*`]: () => eastRouting.promise,
      },
      live({ routing: { transit: [['harbor_depot', 'Harbor Depot', 7]] } }),
    )
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await waitFor(() => expect(metric('Harbor Depot')).toHaveTextContent(/^7$/))
    await person.click(within(main()).getByRole('link', { name: 'Northbridge Library' }))
    await person.click(await within(main()).findByRole('link', { name: 'East Side AMH' }))
    await waitFor(() => expect(metric('Check-ins today')).toHaveTextContent(/^7$/))

    expect(metric('Total transit')).toHaveTextContent(/^Loading…$/)
    expect(main()).not.toHaveTextContent(/Harbor Depot|Kept at/)

    eastRouting.resolve(jsonResponse(200, liveBody.checkinsByDestination(live({ hours: hoursWith({ 8: 7 }), routing: { transit: [['uptown', 'Uptown', 1]] } }), TODAY)))
    await waitFor(() => expect(metric('Uptown')).toHaveTextContent(/^1$/))
    expect(main()).not.toHaveTextContent(/Harbor Depot/)
  })

  it('shows Loading, and no figure, until it answers', async () => {
    const slow = deferred<Response>()
    serve({ [BY_DESTINATION]: () => slow.promise })

    renderApp(CENTRAL)
    await waitFor(() => expect(metric('Check-ins today')).toHaveTextContent(/^120$/))

    expect(within(routing()).getAllByRole('term').map((term) => term.textContent)).toEqual(['Total transit'])
    expect(metric('Total transit')).toHaveTextContent(/^Loading…$/)
    expect(routing()).not.toHaveTextContent(/\d|Kept at|%/)
    expect(metric('Rejects today')).toHaveTextContent(/^6$/)

    slow.resolve(jsonResponse(200, liveBody.checkinsByDestination(LIVE, TODAY)))
    await waitFor(() => expect(metric('Westside')).toHaveTextContent(/^12$/))
  })

  it.each([
    ['a server error', SERVER_ERROR],
    ['a crash page', () => textResponse(502, 'Traceback: customer_id=41 branch_id=7')],
    ['a network failure', () => Promise.reject(networkFailure())],
    ['a malformed answer', () => jsonResponse(200, { ...liveBody.checkinsByDestination(LIVE, TODAY), transit_count: 99 })],
    ['a rate limit', () => jsonResponse(429, { error: 'Rate limit exceeded: 60 per 1 minute' })],
  ])('says Routing could not load after %s, and keeps everything else', async (_label, fail) => {
    serve({ [BY_DESTINATION]: fail })

    renderApp(CENTRAL)
    await loaded()

    expect(await screen.findByRole('alert')).toHaveTextContent(/^Some live data could not be loaded\. Use Refresh to try again\.$/)
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(metric('Total transit')).toHaveTextContent(/^Could not load$/)
    expect(routing()).not.toHaveTextContent(/\d|Traceback|customer_id|Failed to fetch|Rate limit|Kept at|%/)
    expect(within(routing()).getAllByRole('term')).toHaveLength(1)
    // Operations, Rejects, the chart and the reasons are untouched.
    expect(metric('Check-ins today')).toHaveTextContent(/^120$/)
    expect(metric('Current hour')).toHaveTextContent(/^17$/)
    expect(metric('Rejects today')).toHaveTextContent(/^6$/)
    expect(metric('Reject rate')).toHaveTextContent(/^5\.0%$/)
    expect(barHeights()).toHaveLength(24)
    expect(rows('Top reject reasons')).toHaveLength(3)
    expect(main()).not.toHaveTextContent(UNAVAILABLE)
  })

  it('recovers on the next refresh', async () => {
    serve({ [BY_DESTINATION]: inTurn(SERVER_ERROR, (url) => jsonResponse(200, liveBody.checkinsByDestination(LIVE, new URL(url, 'http://t.invalid').searchParams.get('date')))) })
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await screen.findByRole('alert')
    await person.click(refreshButton())

    await waitFor(() => expect(metric('Westside')).toHaveTextContent(/^12$/))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  })

  it('keeps the last good routing figures, and says so, when its refresh fails', async () => {
    serve({ [BY_DESTINATION]: inTurn((url) => jsonResponse(200, liveBody.checkinsByDestination(LIVE, new URL(url, 'http://t.invalid').searchParams.get('date'))), SERVER_ERROR) })
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await waitFor(() => expect(metric('Westside')).toHaveTextContent(/^12$/))
    await person.click(refreshButton())

    expect(await screen.findByRole('alert')).toHaveTextContent('The latest refresh failed. Some of what is shown may be out of date.')
    expect(metric('Westside')).toHaveTextContent(/^12$/)
    expect(metric('Total transit')).toHaveTextContent(/^14$/)
    expect(routing()).toHaveTextContent('Kept at Main: 106.')
  })

  it('tries a failing routing read three times in all, and a malformed one once', async () => {
    const asked = (fetchMock: FetchMock) => liveRequests(fetchMock).filter((url) => url.includes('/checkins/by-destination')).length

    const failing = serve({ [BY_DESTINATION]: SERVER_ERROR })
    const first = renderApp(CENTRAL, { retries: true })
    await screen.findByRole('alert')
    expect(asked(failing)).toBe(3)
    first.unmount()

    const malformed = serve({ [BY_DESTINATION]: () => jsonResponse(200, { transit: 'none' }) })
    renderApp(CENTRAL, { retries: true })
    await screen.findByRole('alert')
    await pass(10_000)
    expect(asked(malformed)).toBe(1)
  })

  it('gets past a routing failure that clears up, without showing an error', async () => {
    const fetchMock = serve({
      [BY_DESTINATION]: inTurn(
        () => Promise.reject(networkFailure()),
        (url) => jsonResponse(200, liveBody.checkinsByDestination(LIVE, new URL(url, 'http://t.invalid').searchParams.get('date'))),
      ),
    })

    renderApp(CENTRAL, { retries: true })
    await loaded()
    await waitFor(() => expect(metric('Westside')).toHaveTextContent(/^12$/))

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(liveRequests(fetchMock)).toHaveLength(7)
  })

  it('treats a 404 from routing as it treats one from any read: live data is not available for this branch', async () => {
    serve({ [BY_DESTINATION]: () => jsonResponse(404, TENANT_NOT_FOUND) })

    renderApp(CENTRAL, { retries: true })

    expect(await screen.findByText(UNAVAILABLE)).toHaveRole('note')
    expect(screen.getByRole('heading', { level: 2, name: 'Central Library AMH' })).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/Page not found|destination|Routing|Westside/i)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })

  it('returns to the sign-in form at the same address when a later routing refresh is answered 401', async () => {
    serve({
      [BY_DESTINATION]: inTurn(
        (url) => jsonResponse(200, liveBody.checkinsByDestination(LIVE, new URL(url, 'http://t.invalid').searchParams.get('date'))),
        () => jsonResponse(401, NOT_AUTHENTICATED),
      ),
    })
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(refreshButton())

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.getByTestId('address')).toHaveTextContent(CENTRAL)
    expect(screen.queryByText('Westside')).not.toBeInTheDocument()
  })
})

describe('the dashboard at a glance', () => {
  it('says it is live, in a word, and says it is paused while it is', async () => {
    serve()
    const person = user()
    renderApp(CENTRAL)
    await loaded()

    const badge = document.querySelector('.live-badge') as HTMLElement
    expect(badge).toHaveTextContent(/^Live$/)
    expect(badge).not.toHaveClass('live-badge-paused')
    // The mark beside the word is decoration: the word is what says it.
    expect(badge.querySelector('.live-badge-mark')).toHaveAttribute('aria-hidden', 'true')

    await person.click(screen.getByRole('button', { name: 'Pause automatic refresh' }))
    expect(badge).toHaveTextContent(/^Paused$/)
    expect(badge).toHaveClass('live-badge-paused')

    await person.click(screen.getByRole('button', { name: 'Resume automatic refresh' }))
    expect(badge).toHaveTextContent(/^Live$/)
  })

  it('puts the controls and the pipeline together at the top, with any problem between them', async () => {
    serve({ [REJECT_COUNT]: SERVER_ERROR })
    renderApp(CENTRAL)
    await loaded()

    const hero = document.querySelector('.live-hero') as HTMLElement
    expect(Array.from(hero.children).map((child) => child.className.split(' ')[0])).toEqual(['live-controls', 'error-message', 'panel'])
    expect(within(hero).getByRole('alert')).toHaveTextContent('Some live data could not be loaded.')
    expect(within(hero).getByRole('region', { name: 'Pipeline' })).toBeInTheDocument()
    expect(within(hero).getByRole('button', { name: 'Refresh' })).toBeInTheDocument()
    // It comes before today's figures.
    expect(hero.compareDocumentPosition(screen.getByRole('region', { name: 'Today' })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it.each(['ok', 'degraded', 'failed', 'unknown'])('marks the pipeline panel for the state %s, and still says the state in a word', async (state) => {
    serve({}, live({ state }))
    renderApp(CENTRAL)
    await loaded()

    const panel = screen.getByRole('region', { name: 'Pipeline' })
    expect(panel).toHaveClass('pipeline-panel', `pipeline-panel-${state}`)
    expect(panel.querySelector('.pipeline-state-label')).toHaveTextContent({ ok: 'OK', degraded: 'Degraded', failed: 'Failed', unknown: 'Unknown' }[state] as string)
  })

  it('sets today out as three named zones, in reading order, each a group with its own heading', async () => {
    serve()
    renderApp(CENTRAL)
    await loaded()

    const zones = Array.from(document.querySelectorAll('.summary-zones > .summary-group'))
    expect(zones.map((zone) => zone.className)).toEqual([
      'summary-group summary-group-operations',
      'summary-group summary-group-routing',
      'summary-group summary-group-rejects',
    ])
    expect(zones.map((zone) => [zone.getAttribute('role'), zone.querySelector('h4')?.textContent])).toEqual([
      ['group', 'Operations'],
      ['group', 'Routing'],
      ['group', 'Rejects'],
    ])
    const heading = document.querySelector('.today-heading') as HTMLElement
    expect(within(heading).getByRole('heading', { level: 3, name: 'Today' })).toBeInTheDocument()
    expect(heading).toHaveTextContent('Monday, October 5, 2026 (America/Chicago)')
  })
})
