import { act, screen, waitFor, within } from '@testing-library/react'
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
  serveApi,
  TENANT_NOT_FOUND,
  textResponse,
} from '../test/http.ts'
import { renderApp } from '../test/render.tsx'
import { REFRESH_INTERVAL_MS } from './useLiveToday.ts'

const CENTRAL = '/organizations/northbridge/branches/central'
const API = livePath('northbridge', 'central')
const PIPELINE = `GET ${API}/pipeline-status`
const CHECKIN_COUNT = `GET ${API}/checkins/count?date=*`
const BY_HOUR = `GET ${API}/checkins/by-hour?date=*`
const REJECT_COUNT = `GET ${API}/rejects/count?date=*`
const BY_REASON = `GET ${API}/rejects/by-reason?date=*`

// 1:50 PM on Monday 5 October 2026 in Chicago (CDT, UTC-5).
const NOW = '2026-10-05T18:50:00Z'
const TODAY = '2026-10-05'

const SERVER_ERROR = () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' })
const UNAVAILABLE = 'Live dashboard data is not available for this branch yet.'

const hoursWith = (counts: Record<number, number>) => Array.from({ length: 24 }, (_, hour) => counts[hour] ?? 0)
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
  await screen.findByRole('table', { name: 'Hourly check-ins' })
  await waitFor(() => expect(refreshButton()).toBeEnabled())
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
      'checkins/by-hour',
      'checkins/count',
      'pipeline-status',
      'rejects/by-reason',
      'rejects/count',
    ])
    expect(liveRequests(fetchMock)[0]).toBe(`${API}/pipeline-status`)
  })

  it('asks all four dated reads for the same date, exactly once each', async () => {
    const fetchMock = serve()

    renderApp(CENTRAL)
    await loaded()

    expect(liveRequests(fetchMock)).toHaveLength(5)
    expect(datesAsked(fetchMock)).toEqual([TODAY, TODAY, TODAY, TODAY])
    expect(liveRequests(fetchMock).slice(1).sort()).toEqual([
      `${API}/checkins/by-hour?date=${TODAY}`,
      `${API}/checkins/count?date=${TODAY}`,
      `${API}/rejects/by-reason?date=${TODAY}`,
      `${API}/rejects/count?date=${TODAY}`,
    ])
  })

  it.each([
    // The same instant, 9:30 PM Monday in Chicago: three zones, two dates.
    ['America/Chicago', '2026-10-05', 'Monday, October 5, 2026', 'Current hour (9 PM)'],
    ['Asia/Tokyo', '2026-10-06', 'Tuesday, October 6, 2026', 'Current hour (11 AM)'],
    ['Pacific/Honolulu', '2026-10-05', 'Monday, October 5, 2026', 'Current hour (4 PM)'],
  ])('takes today and the hour from the product zone %s, not from this machine', async (timezone, date, dateText, hourLabel) => {
    vi.setSystemTime(new Date('2026-10-06T02:30:00Z'))
    const fetchMock = serve({}, live({ timezone }))

    renderApp(CENTRAL)
    await loaded()

    expect(datesAsked(fetchMock)).toEqual([date, date, date, date])
    expect(main()).toHaveTextContent(`${dateText} (${timezone})`)
    expect(screen.getByText(hourLabel, { selector: 'dt' })).toBeInTheDocument()
  })

  it('asks for nothing dated, and guesses no date, when pipeline status cannot be read', async () => {
    const fetchMock = serve({ [PIPELINE]: SERVER_ERROR })

    renderApp(CENTRAL)

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    await pass(1000)
    expect(liveRequests(fetchMock)).toEqual([`${API}/pipeline-status`])
    expect(datesAsked(fetchMock)).toEqual([])
    expect(main()).not.toHaveTextContent(/2026|Today/)
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
    expect(metric('Check-ins')).toHaveTextContent(/^120$/)
    expect(metric('Current hour (1 PM)')).toHaveTextContent(/^17$/)
    expect(metric('Busiest hour')).toHaveTextContent(/^11 AM \(40\)$/)
    expect(metric('Rejects')).toHaveTextContent(/^6$/)
    expect(metric('Reject rate')).toHaveTextContent(/^5\.0%$/)
  })

  it('shows zero for a current hour in which nothing has happened', async () => {
    vi.setSystemTime(new Date('2026-10-05T20:10:00Z'))
    serve()

    renderApp(CENTRAL)
    await loaded()

    expect(metric('Current hour (3 PM)')).toHaveTextContent(/^0$/)
  })

  it('gives the earliest hour when hours tie for busiest', async () => {
    serve({}, live({ hours: hoursWith({ 14: 40, 9: 40, 11: 40 }) }))

    renderApp(CENTRAL)
    await loaded()

    expect(metric('Busiest hour')).toHaveTextContent(/^9 AM \(40\)$/)
  })

  it('shows no busiest hour and no reject rate on a day with no check-ins', async () => {
    serve({}, live({ hours: hoursWith({}), reasons: [0, 0, 0, 0, 0, 0, 0, 0] }))

    renderApp(CENTRAL)
    await loaded()

    expect(metric('Check-ins')).toHaveTextContent(/^0$/)
    expect(metric('Busiest hour')).toHaveTextContent('No check-ins yet')
    expect(metric('Rejects')).toHaveTextContent(/^0$/)
    expect(metric('Reject rate')).toHaveTextContent('Not available (no check-ins)')
    expect(main()).not.toHaveTextContent(/NaN|Infinity|12 AM \(0\)/)
  })

  it('shows no reject rate, rather than infinity, for rejects without check-ins', async () => {
    serve({}, live({ hours: hoursWith({}), reasons: [4, 0, 0, 0, 0, 0, 0, 0] }))

    renderApp(CENTRAL)
    await loaded()

    expect(metric('Rejects')).toHaveTextContent(/^4$/)
    expect(metric('Reject rate')).toHaveTextContent('Not available (no check-ins)')
    expect(main()).not.toHaveTextContent(/NaN|Infinity|%/)
  })

  it('lists all 24 hours in a table and marks the current one in words', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()

    const hourly = rows('Hourly check-ins')
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

  it('draws no chart', async () => {
    serve()

    renderApp(CENTRAL)
    await loaded()

    for (const role of ['img', 'progressbar', 'meter', 'figure']) {
      expect(screen.queryByRole(role)).not.toBeInTheDocument()
    }
    expect(document.querySelector('svg, canvas')).toBeNull()
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
    expect(liveRequests(fetchMock)).toHaveLength(5)

    fixture = live({ state: 'degraded', hours: hoursWith({ 9: 20, 10: 25, 11: 40, 12: 18, 13: 30 }), reasons: [3, 0, 1, 0, 0, 2, 0, 4] })
    await pass(10_000)
    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(10))

    const second = liveRequests(fetchMock).slice(5)
    expect(second[0]).toBe(`${API}/pipeline-status`)
    expect(endpoints(second.slice(1)).sort()).toEqual(['checkins/by-hour', 'checkins/count', 'rejects/by-reason', 'rejects/count'])
    await waitFor(() => expect(metric('Check-ins')).toHaveTextContent(/^133$/))
    expect(metric('Status')).toHaveTextContent(/^Degraded$/)
    expect(metric('Current hour (1 PM)')).toHaveTextContent(/^30$/)
    expect(metric('Rejects')).toHaveTextContent(/^10$/)
    expect(rows('Top reject reasons')[0]).toEqual(['Unknown', '4'])
  })

  it('keeps refreshing, interval after interval', async () => {
    const fetchMock = serve()

    renderApp(CENTRAL)
    await loaded()
    await pass(REFRESH_INTERVAL_MS * 3 + 5000)

    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(20))
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
    expect(liveRequests(fetchMock)).toHaveLength(5)
    expect(metric('Check-ins')).toHaveTextContent(/^120$/)
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

    await waitFor(() => expect(metric('Check-ins')).toHaveTextContent(/^200$/))
    const second = liveRequests(fetchMock).slice(5)
    expect(second[0]).toBe(`${API}/pipeline-status`)
    expect(endpoints(second).sort()).toEqual(['checkins/by-hour', 'checkins/count', 'pipeline-status', 'rejects/by-reason', 'rejects/count'])
    expect(main()).toHaveTextContent('Automatic refresh is paused.')

    await pass(REFRESH_INTERVAL_MS * 2)
    expect(liveRequests(fetchMock)).toHaveLength(10)
  })

  it('refreshes from the button while running too', async () => {
    const fetchMock = serve()
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(refreshButton())

    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(10))
    expect(main()).toHaveTextContent('Refreshes automatically every 3 minutes.')
  })

  it('starts refreshing by itself again when resumed', async () => {
    const fetchMock = serve()
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(screen.getByRole('button', { name: 'Pause automatic refresh' }))
    await pass(REFRESH_INTERVAL_MS * 2)
    expect(liveRequests(fetchMock)).toHaveLength(5)

    await person.click(screen.getByRole('button', { name: 'Resume automatic refresh' }))
    expect(main()).toHaveTextContent('Refreshes automatically every 3 minutes.')
    await pass(REFRESH_INTERVAL_MS + 5000)

    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(10))
  })

  it('shows that it is refreshing and starts no second refresh on top of the first', async () => {
    const slow = deferred<Response>()
    const fetchMock = serve({ [PIPELINE]: inTurn(() => jsonResponse(200, liveBody.pipeline(LIVE)), () => slow.promise) })
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(refreshButton())

    const busy = await screen.findByRole('button', { name: 'Refreshing…' })
    expect(busy).toBeDisabled()
    await person.click(busy)
    await person.click(busy)
    await pass(1000)
    expect(liveRequests(fetchMock)).toHaveLength(6)
    // What was already loaded stays on screen while the refresh runs.
    expect(metric('Check-ins')).toHaveTextContent(/^120$/)

    slow.resolve(jsonResponse(200, liveBody.pipeline(LIVE)))
    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(10))
    await waitFor(() => expect(refreshButton()).toBeEnabled())
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
    expect(datesAsked(fetchMock)).toEqual(['2026-10-05', '2026-10-05', '2026-10-05', '2026-10-05'])
    expect(main()).toHaveTextContent('Monday, October 5, 2026')
    expect(screen.getByText('Current hour (11 PM)', { selector: 'dt' })).toBeInTheDocument()

    await pass(REFRESH_INTERVAL_MS + 5000)

    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(10))
    expect(datesAsked(fetchMock).slice(4)).toEqual(['2026-10-06', '2026-10-06', '2026-10-06', '2026-10-06'])
    await waitFor(() => expect(main()).toHaveTextContent('Tuesday, October 6, 2026'))
    expect(main()).not.toHaveTextContent('Monday, October 5, 2026')
    await waitFor(() => expect(screen.getByText('Current hour (12 AM)', { selector: 'dt' })).toBeInTheDocument())
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

    await waitFor(() => expect(datesAsked(fetchMock).slice(4)).toEqual(['2026-10-06', '2026-10-06', '2026-10-06', '2026-10-06']))
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
    expect(metric('Check-ins')).toHaveTextContent(/^120$/)
    await person.click(within(main()).getByRole('link', { name: 'Northbridge Library' }))
    await person.click(await within(main()).findByRole('link', { name: 'East Side Branch' }))

    expect(await screen.findByText('Loading live data…')).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/120|11 AM \(40\)|Item not found/)

    eastPipeline.resolve(jsonResponse(200, liveBody.pipeline(LIVE)))
    await loaded()
    expect(metric('Check-ins')).toHaveTextContent(/^7$/)
    expect(metric('Busiest hour')).toHaveTextContent(/^8 AM \(7\)$/)
    const east = liveRequests(fetchMock).filter((url) => url.includes('/east-side/'))
    expect(east).toHaveLength(5)
    expect(liveRequests(fetchMock).filter((url) => url.includes('/central/'))).toHaveLength(5)
  })

  it('starts the other branch running even if the first was paused', async () => {
    serve()
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(screen.getByRole('button', { name: 'Pause automatic refresh' }))
    await person.click(within(main()).getByRole('link', { name: 'Northbridge Library' }))
    await person.click(await within(main()).findByRole('link', { name: 'East Side Branch' }))
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
    await screen.findByRole('heading', { level: 3, name: 'Branches' })
    await pass(REFRESH_INTERVAL_MS * 2)

    expect(liveRequests(fetchMock)).toHaveLength(5)
  })

  it('loads a branch afresh when its page is opened again', async () => {
    let fixture = LIVE
    const fetchMock = serve({}, () => fixture)
    const person = user()

    renderApp(CENTRAL)
    await loaded()
    await person.click(within(main()).getByRole('link', { name: 'Northbridge Library' }))
    fixture = live({ hours: hoursWith({ 13: 300 }) })
    await person.click(await within(main()).findByRole('link', { name: 'Central Branch' }))
    await loaded()

    expect(metric('Check-ins')).toHaveTextContent(/^300$/)
    expect(liveRequests(fetchMock)).toHaveLength(10)
  })
})

describe('a suspended organization', () => {
  it('still shows live data and can still refresh', async () => {
    const fetchMock = serve()
    const person = user()

    renderApp('/organizations/riverside/branches/main')
    await loaded()

    expect(screen.getByText(/currently suspended/)).toBeInTheDocument()
    expect(metric('Check-ins')).toHaveTextContent(/^120$/)
    expect(screen.getByRole('button', { name: 'Pause automatic refresh' })).toBeEnabled()
    await person.click(refreshButton())
    await waitFor(() => expect(liveRequests(fetchMock)).toHaveLength(10))
  })
})

describe('a branch with no live data (404)', () => {
  it('says live data is not available, and does not call the branch missing', async () => {
    const fetchMock = serve({ [PIPELINE]: () => jsonResponse(404, TENANT_NOT_FOUND) })

    renderApp(CENTRAL, { retries: true })

    expect(await screen.findByText(UNAVAILABLE)).toHaveRole('note')
    expect(screen.getByRole('heading', { level: 2, name: 'Central Branch' })).toBeInTheDocument()
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
    expect(screen.getByRole('heading', { level: 2, name: 'Central Branch' })).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('keeps the suspended notice and the page around it', async () => {
    serve({ [`GET ${livePath('riverside', 'main')}/pipeline-status`]: () => jsonResponse(404, TENANT_NOT_FOUND) })

    renderApp('/organizations/riverside/branches/main')

    expect(await screen.findByText(UNAVAILABLE)).toBeInTheDocument()
    expect(screen.getByText(/currently suspended/)).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Riverside Main' })).toBeInTheDocument()
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
    expect(asked).toBeLessThanOrEqual(5)
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
    expect(screen.getByRole('heading', { level: 2, name: 'Central Branch' })).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(UNAVAILABLE)

    await person.click(screen.getByRole('button', { name: 'Try again' }))

    await loaded()
    expect(metric('Check-ins')).toHaveTextContent(/^120$/)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(liveRequests(fetchMock)).toHaveLength(6)
  })

  it('keeps the sections that loaded when one read fails, and says which could not load', async () => {
    serve({ [BY_REASON]: SERVER_ERROR })

    renderApp(CENTRAL)
    await loaded()

    expect(await screen.findByRole('alert')).toHaveTextContent('Some live data could not be loaded. Use Refresh to try again.')
    expect(metric('Status')).toHaveTextContent(/^OK$/)
    expect(metric('Check-ins')).toHaveTextContent(/^120$/)
    expect(metric('Rejects')).toHaveTextContent(/^6$/)
    expect(metric('Reject rate')).toHaveTextContent(/^5\.0%$/)
    expect(rows('Hourly check-ins')).toHaveLength(24)
    const reasons = screen.getByRole('heading', { level: 3, name: 'Top reject reasons' }).parentElement as HTMLElement
    expect(reasons).toHaveTextContent('Could not load.')
    expect(screen.queryByRole('table', { name: 'Top reject reasons' })).not.toBeInTheDocument()
    expect(screen.getAllByRole('alert')).toHaveLength(1)
  })

  it('shows no figure that depends on a read that failed, and never a zero in its place', async () => {
    serve({ [CHECKIN_COUNT]: SERVER_ERROR, [BY_HOUR]: () => jsonResponse(200, { hours: 'none' }) })

    renderApp(CENTRAL)
    await screen.findByRole('alert')
    await waitFor(() => expect(metric('Rejects')).toHaveTextContent(/^6$/))

    expect(metric('Check-ins')).toHaveTextContent('Could not load')
    expect(metric(/^Current hour/)).toHaveTextContent('Could not load')
    expect(metric('Busiest hour')).toHaveTextContent('Could not load')
    expect(metric('Reject rate')).toHaveTextContent('Could not load')
    expect(main()).not.toHaveTextContent(/NaN|Infinity|%/)
    expect(screen.queryByRole('table', { name: 'Hourly check-ins' })).not.toBeInTheDocument()
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
    expect(metric('Check-ins')).toHaveTextContent(/^120$/)
    expect(metric('Status')).toHaveTextContent(/^OK$/)
    expect(rows('Hourly check-ins')).toHaveLength(24)

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
    expect(liveRequests(fetchMock)).toHaveLength(6)
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
