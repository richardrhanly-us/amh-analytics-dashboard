import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
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
const HELP =
  'Holds for library patrons: items checked in to wait for a patron to pick them up. Items in transit to another location are not holds. Holds for the library’s own service accounts and for interlibrary loans are not counted as public holds. Only confirmed holds are counted.'

const SERVER_ERROR = () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' })
const WITH_HOLDS = { ...NORTHBRIDGE_DETAIL.entitlements, internal_workflow: { enabled: true, limit_value: null } }

/** What the confirmed counts could not include (hold-count accuracy, D): left out, nothing. */
interface Accuracy {
  partialDays?: number
  illUncertainDays?: number
  unknownAlerts?: number
}

/** The answer for whatever range `url` asks for. `daysWithData` left out: every day of the range had data. */
function holds(publicHolds: number, illHolds: number, daysWithData?: number, accuracy: Accuracy = {}) {
  return (url: string) => {
    const params = new URL(url, 'http://test.invalid').searchParams
    const [from, to] = [params.get('from'), params.get('to')]
    const days = (Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86_400_000 + 1
    return jsonResponse(200, {
      range: { from, to, days, timezone: 'America/Chicago', includes_today: to === '2026-10-05' },
      public_hold_count: publicHolds,
      ill_hold_count: illHolds,
      days_with_data: daysWithData ?? days,
      public_holds_partial_days: accuracy.partialDays ?? 0,
      ill_uncertain_days: accuracy.illUncertainDays ?? 0,
      unknown_alert_count: accuracy.unknownAlerts ?? 0,
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
/** Everything a card says under its figure, in order: its notes. */
const cardNotes = (label: string) =>
  Array.from(within(section()).getByText(label, { selector: 'dt' }).parentElement?.querySelectorAll('dd.metric-note') ?? []).map(
    (note) => note.textContent,
  )
const accuracyNote = () => within(section()).queryAllByRole('note').find((note) => note.classList.contains('holds-accuracy')) ?? null
const user = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })

async function loaded() {
  renderApp(CENTRAL_REPORTS)
  await screen.findByRole('img', { name: /^Bar chart of rejects/ })
  await waitFor(() => expect(screen.getByRole('main')).not.toHaveTextContent('Loading…'))
}

/** Picks the range from `from` to `to` (inclusive) and waits for its Holds figures. */
async function pickRange(from: string, to: string) {
  fireEvent.change(screen.getByLabelText('From'), { target: { value: from } })
  fireEvent.change(screen.getByLabelText('To'), { target: { value: to } })
  fireEvent.click(screen.getByRole('button', { name: 'Apply dates' }))
  await waitFor(() => expect(screen.getByRole('main')).not.toHaveTextContent('Loading…'))
  await within(section()).findByText('Public holds')
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

/*
 * Hold-count accuracy (phase D): the two figures are CONFIRMED holds. What they could not include is said beside
 * them -- never estimated, and never shown as a zero. Which days are partial or ILL-uncertain (the legacy/v2 cutover
 * and the branch's corrected-from date) is the server's to work out, and is tested there
 * (tests/test_hold_report_service.py, tests/test_hold_report_postgres.py); here, every shape of its answer is shown.
 */
describe('what the confirmed figures could not include', () => {
  it('says nothing more when every day is confirmed: no label and no note', async () => {
    serve({ overrides: { [HOLDS]: holds(41, 3) } })
    await loaded()
    await within(section()).findByText('Public holds')

    expect([cardNotes('Public holds'), cardNotes('Interlibrary loan (ILL) holds')]).toEqual([[], []])
    expect(accuracyNote()).toBeNull()
    expect(section()).not.toHaveTextContent(/partial|not recognized|missed or added/i)
  })

  it('labels Public holds Partial and says how many of the range’s days are affected, keeping the confirmed figure', async () => {
    serve({ overrides: { [HOLDS]: holds(41, 3, undefined, { partialDays: 12 }) } })
    await loaded()
    await within(section()).findByText('Public holds')

    expect(metric('Public holds')).toBe('41')
    expect(cardNotes('Public holds')).toEqual(['Partial'])
    expect(cardNotes('Interlibrary loan (ILL) holds')).toEqual([])
    expect(accuracyNote()).toHaveTextContent(
      'Public holds are partial. On 12 of the 30 days in this date range, this sorter’s collector could not tell holds for pickup at another location apart from items in transit to it, so those holds are not included. They are not available, not zero.',
    )
    // Only the Partial explanation: nothing about ILL or unknown alerts.
    expect(accuracyNote()?.querySelectorAll('p')).toHaveLength(1)
  })

  it('never shows a partial zero as a bare zero: it is labelled Partial and explained', async () => {
    serve({ overrides: { [HOLDS]: holds(0, 0, undefined, { partialDays: 1 }) } })
    await loaded()
    await within(section()).findByText('Public holds')

    expect(metric('Public holds')).toBe('0')
    expect(cardNotes('Public holds')).toEqual(['Partial'])
    expect(accuracyNote()).toHaveTextContent('They are not available, not zero.')
  })

  it('says “this day” for a one-day range', async () => {
    serve({ overrides: { [HOLDS]: holds(5, 1, undefined, { partialDays: 1, illUncertainDays: 1 }) } })
    await loaded()
    await pickRange('2026-10-01', '2026-10-01')

    await waitFor(() => expect(accuracyNote()).toHaveTextContent('Public holds are partial. On this day, this sorter’s collector'))
    expect(accuracyNote()).toHaveTextContent('ILL holds only: on this day, collected before')
    expect(accuracyNote()).not.toHaveTextContent(/of the \d+ days/)
  })

  it('puts the ILL note on the ILL figure only', async () => {
    serve({ overrides: { [HOLDS]: holds(41, 3, undefined, { illUncertainDays: 7 }) } })
    await loaded()
    await within(section()).findByText('Public holds')

    expect(cardNotes('Interlibrary loan (ILL) holds')).toEqual(['May miss or add a few'])
    expect(cardNotes('Public holds')).toEqual([])
    expect(metric('Interlibrary loan (ILL) holds')).toBe('3')
    expect(accuracyNote()).toHaveTextContent(
      'ILL holds only: on 7 of the 30 days in this date range, collected before this sorter’s collector was updated, a few interlibrary loan holds could be missed or added.',
    )
    expect(accuracyNote()).not.toHaveTextContent(/partial/i)
  })

  it.each([
    [1, '1 item had an alert type that was not recognized, so it is not included in either figure.'],
    [1234, '1,234 items had an alert type that was not recognized, so they are not included in either figure.'],
  ])('says %i unrecognized alert(s) were left out of both figures, without labelling either', async (unknownAlerts, sentence) => {
    serve({ overrides: { [HOLDS]: holds(41, 3, undefined, { unknownAlerts }) } })
    await loaded()
    await within(section()).findByText('Public holds')

    expect(accuracyNote()).toHaveTextContent(sentence)
    expect([metric('Public holds'), metric('Interlibrary loan (ILL) holds')]).toEqual(['41', '3'])
    expect([cardNotes('Public holds'), cardNotes('Interlibrary loan (ILL) holds')]).toEqual([[], []])
  })

  it('across the cutover and the corrected-from date: only the days the server flags, each note once, in one note', async () => {
    // 90 days: legacy days (accurate), then uncorrected v2 days (8 partial, 20 with holds), then days from the
    // corrected-from date (accurate). The server counts only the flagged days; the figures stay the confirmed sums.
    serve({ overrides: { [HOLDS]: holds(600, 40, 85, { partialDays: 8, illUncertainDays: 20, unknownAlerts: 2 }) } })
    await loaded()
    await user().click(screen.getByRole('button', { name: 'Last 90 days' }))
    await waitFor(() => expect(accuracyNote()).toHaveTextContent('On 8 of the 90 days in this date range'))

    expect([metric('Public holds'), metric('Interlibrary loan (ILL) holds')]).toEqual(['600', '40'])
    expect(Array.from(accuracyNote()?.querySelectorAll('p') ?? []).map((p) => p.textContent?.split(/[.:]/)[0])).toEqual([
      'Public holds are partial',
      'ILL holds only',
      '2 items had an alert type that was not recognized, so they are not included in either figure',
    ])
    expect(accuracyNote()).toHaveTextContent('on 20 of the 90 days in this date range')
    // The no-data note is its own, and still says missing days are not zero.
    expect(within(section()).getByText(/Sorter records were found on 85 of the 90 days/)).toHaveAttribute('role', 'note')
  })

  it('shows no label once the range is entirely from the corrected-from date, and asks again for each range', async () => {
    let answer = holds(41, 3, undefined, { partialDays: 4, illUncertainDays: 4 })
    serve({ overrides: { [HOLDS]: (url) => answer(url) } })
    await loaded()
    await waitFor(() => expect(cardNotes('Public holds')).toEqual(['Partial']))

    answer = holds(9, 1)
    await user().click(screen.getByRole('button', { name: 'Last 7 days' }))
    await waitFor(() => expect(metric('Public holds')).toBe('9'))

    expect([cardNotes('Public holds'), cardNotes('Interlibrary loan (ILL) holds')]).toEqual([[], []])
    expect(accuracyNote()).toBeNull()
  })

  it('shows the figures unlabelled from a server that does not send the accuracy fields yet', async () => {
    serve({
      overrides: {
        [HOLDS]: () =>
          jsonResponse(200, {
            range: { from: '2026-09-06', to: '2026-10-05', days: 30, timezone: 'America/Chicago', includes_today: true },
            public_hold_count: 41,
            ill_hold_count: 3,
            days_with_data: 30,
          }),
      },
    })
    await loaded()
    await within(section()).findByText('Public holds')

    expect([metric('Public holds'), metric('Interlibrary loan (ILL) holds')]).toEqual(['41', '3'])
    expect(accuracyNote()).toBeNull()
  })

  it('shows no figure, label or note when the answer is not one it can trust, and offers to try again', async () => {
    serve({ overrides: { [HOLDS]: holds(41, 3, 10, { partialDays: 11 }) } })
    await loaded()

    await within(section()).findByRole('button', { name: 'Try again: Holds' })
    expect(within(section()).queryByText('Public holds')).not.toBeInTheDocument()
    expect(section()).not.toHaveTextContent(/partial|not recognized/i)
  })

  it('is read in order by a screen reader: each label belongs to its figure, and the explanations are one note', async () => {
    serve({ overrides: { [HOLDS]: holds(41, 3, undefined, { partialDays: 2, illUncertainDays: 2, unknownAlerts: 1 }) } })
    await loaded()
    await within(section()).findByText('Public holds')

    // Each label is a <dd> of its own card's <dt>: "Public holds, 41, Partial"; "ILL holds, 3, May miss or add a few".
    for (const [label, figure, note] of [
      ['Public holds', '41', 'Partial'],
      ['Interlibrary loan (ILL) holds', '3', 'May miss or add a few'],
    ]) {
      const card = within(section()).getByText(label, { selector: 'dt' }).parentElement as HTMLElement
      expect(Array.from(card.children).map((child) => [child.tagName, child.textContent])).toEqual([
        ['DT', label],
        ['DD', figure],
        ['DD', note],
      ])
    }
    // The explanations follow the figures, as one note, and are not a live region: a report is read, not announced.
    const note = accuracyNote() as HTMLElement
    expect(note.tagName).toBe('DIV')
    expect(note.compareDocumentPosition(within(section()).getByText('Public holds'))).toBe(Node.DOCUMENT_POSITION_PRECEDING)
    expect(note).not.toHaveAttribute('aria-live')
    expect(note.querySelectorAll('p')).toHaveLength(3)
  })
})
