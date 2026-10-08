import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  ALICE,
  type ApiRoutes,
  type FetchMock,
  jsonResponse,
  livePath,
  NORTHBRIDGE_DETAIL,
  reportRoutes,
  requestedUrls,
  serveApi,
} from '../test/http.ts'
import { renderApp } from '../test/render.tsx'

const CENTRAL_REPORTS = '/organizations/northbridge/sorters/central/reports'
const HOLDS = `GET ${livePath('northbridge', 'central')}/reports/holds?from=*&to=*`
const RELIABILITY = `GET ${livePath('northbridge', 'central')}/reports/reliability?from=*&to=*`
// 1:50 PM on Monday 5 October 2026 in Chicago: the fixture's "today". The default range is the last 30 days.
const NOW = '2026-10-05T18:50:00Z'
const DEFAULT_RANGE = 'from=2026-09-06&to=2026-10-05'
const HELP = 'Holds for library patrons. Holds for the library’s own service accounts and for interlibrary loans are not counted as public holds.'

const SERVER_ERROR = () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' })
const WITH_HOLDS = { ...NORTHBRIDGE_DETAIL.entitlements, internal_workflow: { enabled: true, limit_value: null } }

/** The answer for whatever range `url` asks for. */
function holds(publicHolds: number, illHolds: number) {
  return (url: string) => {
    const params = new URL(url, 'http://test.invalid').searchParams
    const [from, to] = [params.get('from'), params.get('to')]
    const days = (Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000 + 1
    return jsonResponse(200, {
      range: { from, to, days, timezone: 'America/Chicago', includes_today: to === '2026-10-05' },
      public_hold_count: publicHolds,
      ill_hold_count: illHolds,
    })
  }
}

/** A viewer at Northbridge's Central sorter, on a plan with Holds or without it. */
function serve({ plan = true, role = 'viewer', overrides = {} }: { plan?: boolean; role?: string; overrides?: ApiRoutes } = {}): FetchMock {
  return serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    'GET /api/organizations/northbridge': () =>
      jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role, entitlements: plan ? WITH_HOLDS : NORTHBRIDGE_DETAIL.entitlements }),
    ...reportRoutes('northbridge', 'central'),
    [HOLDS]: holds(41, 3),
    ...overrides,
  })
}

const holdsRequests = (fetchMock: FetchMock) => requestedUrls(fetchMock).filter((url) => url.includes('/reports/holds'))
const section = () => screen.getByRole('region', { name: 'Holds' })
const metric = (label: string) => within(section()).getByText(label, { selector: 'dt' }).nextElementSibling?.textContent
const user = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })

async function loaded() {
  renderApp(CENTRAL_REPORTS)
  await screen.findByRole('img', { name: /^Bar chart of rejects/ })
  await waitFor(() => expect(screen.getByRole('main')).not.toHaveTextContent('Loading…'))
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'], shouldAdvanceTime: true })
  vi.setSystemTime(new Date(NOW))
})

afterEach(() => {
  vi.useRealTimers()
})

describe('whose plan has it', () => {
  it.each(['owner', 'admin', 'manager', 'viewer'])('shows a %s the two counts and what they mean, after the five reports', async (role) => {
    const fetchMock = serve({ role })

    await loaded()
    await within(section()).findByText('Public holds')

    expect(metric('Public holds')).toBe('41')
    expect(metric('Interlibrary loan (ILL) holds')).toBe('3')
    expect(section()).toHaveTextContent(HELP)
    expect(holdsRequests(fetchMock)).toEqual([`/api/organizations/northbridge/branches/central/reports/holds?${DEFAULT_RANGE}`])
    const headings = screen.getAllByRole('heading', { level: 4 }).map((heading) => heading.textContent)
    expect(headings.indexOf('Holds')).toBe(headings.indexOf('Reliability') + 1)
  })

  it('shows zero as zero', async () => {
    serve({ overrides: { [HOLDS]: holds(0, 0) } })

    await loaded()
    await within(section()).findByText('Public holds')

    expect([metric('Public holds'), metric('Interlibrary loan (ILL) holds')]).toEqual(['0', '0'])
  })

  it('says nothing of patrons, items, destinations or service accounts', async () => {
    serve()

    await loaded()
    await within(section()).findByText('Public holds')

    expect(section().textContent?.replace(HELP, '')).not.toMatch(/patron|barcode|destination|staff|programming|collection|branch services|transit/i)
  })

  it('asks again for a new range, and shows nothing of the old one meanwhile', async () => {
    let answer = holds(41, 3)
    const fetchMock = serve({ overrides: { [HOLDS]: (url) => answer(url) } })
    await loaded()
    await within(section()).findByText('Public holds')
    answer = holds(9, 1)

    await user().click(screen.getByRole('button', { name: 'Last 7 days' }))
    await waitFor(() => expect(metric('Public holds')).toBe('9'))

    expect(metric('Interlibrary loan (ILL) holds')).toBe('1')
    expect(holdsRequests(fetchMock).at(-1)).toBe('/api/organizations/northbridge/branches/central/reports/holds?from=2026-09-29&to=2026-10-05')
  })

  it('fails on its own, says so with a way to try again, and leaves the five reports alone', async () => {
    let answer: (url: string) => Response = SERVER_ERROR
    serve({ overrides: { [HOLDS]: (url) => answer(url) } })
    await loaded()

    const retry = await within(section()).findByRole('button', { name: 'Try again: Holds' })
    expect(within(section()).queryByText('Public holds')).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Reliability' })).not.toHaveTextContent('Try again')

    answer = holds(41, 3)
    await user().click(retry)

    await waitFor(() => expect(metric('Public holds')).toBe('41'))
    expect(screen.getByRole('heading', { name: 'Holds' })).toHaveFocus()
  })

  it('is not taken down by another report failing', async () => {
    serve({ overrides: { [RELIABILITY]: SERVER_ERROR } })

    renderApp(CENTRAL_REPORTS)
    await screen.findByRole('button', { name: 'Try again: Reliability' })
    await waitFor(() => expect(metric('Public holds')).toBe('41'))
  })
})

describe('whose plan does not have it', () => {
  it.each(['owner', 'viewer'])('shows a %s no Holds section and never asks for it', async (role) => {
    const fetchMock = serve({ plan: false, role })

    await loaded()
    await act(() => vi.advanceTimersByTimeAsync(1_000))

    expect(screen.queryByRole('region', { name: 'Holds' })).not.toBeInTheDocument()
    expect(screen.queryByText(/holds/i)).not.toBeInTheDocument()
    expect(holdsRequests(fetchMock)).toEqual([])
  })

  it('treats a feature that is listed but switched off as not there', async () => {
    const fetchMock = serveApi({
      'GET /api/auth/session': () => jsonResponse(200, ALICE),
      'GET /api/organizations/northbridge': () =>
        jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role: 'viewer', entitlements: { internal_workflow: { enabled: false, limit_value: null } } }),
      ...reportRoutes('northbridge', 'central'),
      [HOLDS]: holds(41, 3),
    })

    await loaded()

    expect(screen.queryByRole('region', { name: 'Holds' })).not.toBeInTheDocument()
    expect(holdsRequests(fetchMock)).toEqual([])
  })
})
