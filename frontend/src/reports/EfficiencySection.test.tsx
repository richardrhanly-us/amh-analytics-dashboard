import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  efficiencyBody,
  efficiencyPaths,
  efficiencyRoutes,
  type EfficiencyStore,
  efficiencyStore,
  emptyStore,
  FORBIDDEN,
  invalidSettings,
  STORED_INVALID,
} from '../test/efficiency.ts'
import {
  ALICE,
  type ApiRoutes,
  deferred,
  type FetchMock,
  jsonResponse,
  NORTHBRIDGE_DETAIL,
  NOT_AUTHENTICATED,
  reportRoutes,
  requestedUrls,
  serveApi,
  TENANT_NOT_FOUND,
} from '../test/http.ts'
import { renderApp } from '../test/render.tsx'

/**
 * A sorter's Efficiency report and the form beside it, as an owner or admin
 * of the organization sees them on the sorter's Reports page -- and as
 * everyone else does not.
 */

const CENTRAL_REPORTS = '/organizations/northbridge/sorters/central/reports'
const PATHS = efficiencyPaths('northbridge', 'central')
// 1:50 PM on Monday 5 October 2026 in Chicago: the fixtures' "today". The default range is the 30 days ending then.
const NOW = '2026-10-05T18:50:00Z'
const DEFAULT_RANGE = 'from=2026-09-06&to=2026-10-05'

const SERVER_ERROR = () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' })
const NOT_A_FIGURE = /NaN|Infinity|undefined|\bnull\b|\[object/
// What Efficiency must never be called, anywhere a person can read.
const OVERCLAIMS =
  /hours saved|staff hours saved|labor savings|payroll savings|budget savings|net savings|\bROI\b|return on investment|payback|break-?even|net operational value|\bFTE/i

const ORGANIZATION_LABOR = 'Hourly labor rate (USD per hour)'
const ORGANIZATION_MANUAL = 'Manual processing rate assumption (items per staff labor-hour)'
const SORTER_LABOR = 'Labor rate override (USD per hour)'
const SORTER_MANUAL = 'Manual processing rate assumption override (items per staff labor-hour)'
const ONE_TIME = 'One-time cost (USD)'
const RECURRING = 'Recurring annual cost (USD per year)'
const IN_SERVICE = 'In-service date'

let fetchMock: FetchMock

/** A signed-in user at Northbridge -- an admin of it unless `role` says otherwise -- whose Central sorter answers from `store`. */
function serve(store: EfficiencyStore = efficiencyStore(), overrides: ApiRoutes = {}, role = 'admin'): EfficiencyStore {
  fetchMock = serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    'GET /api/organizations/northbridge': () => jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role }),
    ...reportRoutes('northbridge', 'central'),
    ...reportRoutes('northbridge', 'east-side'),
    ...efficiencyRoutes('northbridge', 'central', store, () => fetchMock),
    ...overrides,
  })
  return store
}

const main = () => screen.getByRole('main')
const efficiency = () => screen.getByRole('region', { name: 'Efficiency' })
const within_ = () => within(efficiency())
/** Every request about Efficiency, as "METHOD path-after-the-organization". */
const efficiencyRequests = () =>
  fetchMock.mock.calls
    .map(([url, init]) => `${init?.method ?? 'GET'} ${String(url)}`)
    .filter((request) => request.includes('efficiency'))
    .map((request) => request.replace('/api/organizations/northbridge', ''))
const puts = () => fetchMock.mock.calls.filter(([, init]) => init?.method === 'PUT').map(([url, init]) => [String(url), JSON.parse(String(init?.body))])

function metric(label: string): HTMLElement {
  return within_().getByText(label, { selector: 'dt' }).nextElementSibling as HTMLElement
}
const note = (label: string) => metric(label).nextElementSibling?.textContent ?? null
function detail(label: string): string {
  const row = within(within_().getByRole('table', { name: 'Assumptions and details' })).getByRole('rowheader', { name: label })
  return row.nextElementSibling?.textContent ?? ''
}

const user = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })
const pass = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms))

/** The sorter's Reports page with every section, Efficiency included, loaded. */
async function page() {
  renderApp(CENTRAL_REPORTS)
  await screen.findByRole('img', { name: /^Bar chart of rejects/ })
  await waitFor(() => expect(main()).not.toHaveTextContent('Loading…'))
}

async function openSettings() {
  fireEvent.click(within_().getByRole('button', { name: 'Efficiency settings' }))
  await screen.findByLabelText(ORGANIZATION_LABOR)
  await screen.findByLabelText(SORTER_LABOR)
}
const field = (label: string) => screen.getByLabelText(label) as HTMLInputElement
const type = (label: string, value: string) => fireEvent.change(field(label), { target: { value } })
const organizationForm = () => screen.getByRole('form', { name: 'Organization defaults' })
const sorterForm = () => screen.getByRole('form', { name: 'This sorter' })
const save = (form: HTMLElement) => fireEvent.click(within(form).getByRole('button', { name: /^Save / }))
/** The sentence under a field saying what is wrong with it. */
const problemOf = (label: string) => document.getElementById(`${field(label).id}-problem`)?.textContent ?? null

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'], shouldAdvanceTime: true })
  vi.setSystemTime(new Date(NOW))
})

afterEach(() => {
  vi.useRealTimers()
})

// =====================================================================================================================
// Who sees it
// =====================================================================================================================

describe('who sees Efficiency', () => {
  it.each(['owner', 'admin'])('an %s sees it as the sixth section, after the five every member has', async (role) => {
    serve(efficiencyStore(), {}, role)

    await page()

    expect(screen.getAllByRole('region').map((region) => region.getAttribute('aria-labelledby'))).toEqual([
      'report-overview-heading',
      'report-volume-heading',
      'report-routing-heading',
      'report-bins-heading',
      'report-reliability-heading',
      'report-efficiency-heading',
    ])
    expect(within_().getByRole('heading', { level: 4, name: 'Efficiency' })).toBeInTheDocument()
    expect(within_().getByRole('button', { name: 'Efficiency settings' })).toBeInTheDocument()
    expect(efficiencyRequests()).toEqual([`GET /branches/central/reports/efficiency?${DEFAULT_RANGE}`])
  })

  it.each(['manager', 'viewer', 'superuser', ''])('a member with the role %j does not see it, and nothing about it is asked for', async (role) => {
    serve(efficiencyStore(), {}, role)

    await page()
    await pass(500)

    expect(screen.queryByRole('region', { name: 'Efficiency' })).not.toBeInTheDocument()
    // The five every member has, Bin volume among them.
    expect(screen.getAllByRole('region')).toHaveLength(5)
    expect(screen.getByRole('region', { name: 'Bin volume' })).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/Efficiency|labor|assumption|\$/i)
    expect(efficiencyRequests()).toEqual([])
  })

  it('shows a contained refusal, and nothing of the report, when the API says 403 after all', async () => {
    serve(efficiencyStore(), { [PATHS.report]: () => jsonResponse(403, FORBIDDEN) })

    await page()

    expect(within_().getByRole('note')).toHaveTextContent('Efficiency is available to this organization’s owners and administrators.')
    expect(efficiency()).not.toHaveTextContent(/\$|hours|assumption|Try again/)
    expect(within_().queryByRole('button')).not.toBeInTheDocument()
    expect(efficiencyRequests()).toHaveLength(1)
    // The five reports every member has are untouched.
    expect(screen.getAllByRole('region')).toHaveLength(6)
    expect(within(screen.getByRole('region', { name: 'Overview' })).getByText('3,140')).toBeInTheDocument()
    expect(screen.queryByText('Could not load.')).not.toBeInTheDocument()
  })
})

// =====================================================================================================================
// The report
// =====================================================================================================================

describe('a fully configured sorter', () => {
  beforeEach(async () => {
    serve()
    await page()
  })

  it('shows what was observed and what is estimated, apart', () => {
    // 3,140 check-ins in 30 days. 3140 / 40 = 78.50 h; x 18.00 = 1,413.00; 7300 / 365 * 30 = 600.00.
    const observed = within_().getByRole('heading', { level: 5, name: 'Observed' })
    const estimated = within_().getByRole('heading', { level: 5, name: 'Estimated from assumptions' })
    expect(observed.compareDocumentPosition(estimated) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    const groups = Array.from(efficiency().querySelectorAll('dl.metrics')).map((group) => Array.from(group.querySelectorAll('dt')).map((term) => term.textContent))
    expect(groups).toEqual([
      ['Items processed', 'Recurring cost for this period'],
      ['Estimated manual-workload equivalent', 'Estimated labor-value equivalent'],
    ])
    expect(metric('Items processed')).toHaveTextContent(/^3,140$/)
    expect(note('Items processed')).toBe('Over 30 days')
    expect(metric('Recurring cost for this period')).toHaveTextContent(/^\$600\.00$/)
    expect(note('Recurring cost for this period')).toBe('For 30 days in service')
    expect(metric('Estimated manual-workload equivalent')).toHaveTextContent(/^78\.50 hours$/)
    expect(metric('Estimated labor-value equivalent')).toHaveTextContent(/^\$1,413\.00$/)
  })

  it('says, in plain sight, that the figures are estimates and what each one is not', () => {
    const text = efficiency().textContent ?? ''

    expect(text).toContain('These figures are estimates based on configured assumptions.')
    expect(text).toContain(
      'Estimated from the configured manual processing rate. This represents the manual processing workload that the selected volume would require at that assumed rate; it is not a measurement of staff hours actually saved.',
    )
    expect(text).toContain(
      'Estimated manual-workload equivalent multiplied by the configured labor rate. This is not a budget or payroll saving; staff time may have been redirected to other work.',
    )
    // Text on the page, not something that appears on hover.
    for (const caveat of efficiency().querySelectorAll('.estimate-caveat')) {
      expect(caveat.tagName).toBe('P')
      expect(caveat).toBeVisible()
    }
    expect(efficiency().querySelectorAll('.estimate-caveat')).toHaveLength(2)
    expect(efficiency().querySelectorAll('[title], [data-tooltip], [role="tooltip"]')).toHaveLength(0)
  })

  it('lists the assumptions behind the figures, and whose each rate is', () => {
    expect(detail('Items processed in the selected range')).toBe('3,140')
    expect(detail('Recurring cost per processed item')).toBe('$0.1911')
    expect(detail('Configured manual processing rate (an assumption)')).toBe('40.0 items per staff labor-hour (organization default)')
    expect(detail('Configured labor rate')).toBe('$18.00 per hour (organization default)')
    expect(detail('Configured recurring annual cost')).toBe('$7,300.00 per year')
    expect(detail('In-service date')).toBe('Nov 20, 2020')
  })

  it('claims nothing it cannot support', () => {
    expect(efficiency().textContent).not.toMatch(OVERCLAIMS)
    expect(main().textContent).not.toMatch(OVERCLAIMS)
    // The API's net figure (813.00 here) is not shown, and neither is the one-time cost.
    expect(efficiency()).not.toHaveTextContent(/813\.00|125,000|Net\b/)
    expect(efficiency().textContent).not.toMatch(NOT_A_FIGURE)
  })

  it('leaves the other four reports exactly as they are', () => {
    expect(within(screen.getByRole('region', { name: 'Overview' })).getByText('Check-ins', { selector: 'dt' }).nextElementSibling).toHaveTextContent(/^3,140$/)
    for (const name of ['Overview', 'Volume & capacity', 'Routing', 'Reliability']) {
      expect(within(screen.getByRole('region', { name })).getAllByRole('img').length).toBeGreaterThan(0)
    }
  })
})

describe('whose rate applies', () => {
  it('says so when the sorter has its own', async () => {
    serve(efficiencyStore({ sorter: { labor_rate: '22.00', manual_items_per_hour: '50.0' } }))

    await page()

    expect(detail('Configured manual processing rate (an assumption)')).toBe('50.0 items per staff labor-hour (set for this sorter)')
    expect(detail('Configured labor rate')).toBe('$22.00 per hour (set for this sorter)')
    // 3140 / 50 = 62.80 h; x 22 = 1,381.60.
    expect(metric('Estimated manual-workload equivalent')).toHaveTextContent(/^62\.80 hours$/)
    expect(metric('Estimated labor-value equivalent')).toHaveTextContent(/^\$1,381\.60$/)
  })
})

describe('assumptions that are not there', () => {
  it('with no manual rate assumption: the count and the cost, and no estimate', async () => {
    serve(efficiencyStore({ organization: { manual_items_per_hour: null } }))

    await page()

    expect(metric('Items processed')).toHaveTextContent(/^3,140$/)
    expect(metric('Recurring cost for this period')).toHaveTextContent(/^\$600\.00$/)
    expect(metric('Estimated manual-workload equivalent')).toHaveTextContent(/^Not available$/)
    expect(note('Estimated manual-workload equivalent')).toBe('Configure a manual processing rate assumption to estimate equivalent manual workload.')
    expect(metric('Estimated labor-value equivalent')).toHaveTextContent(/^Not available$/)
    expect(note('Estimated labor-value equivalent')).toBe('Needs a manual processing rate assumption.')
    expect(detail('Configured manual processing rate (an assumption)')).toBe('Not configured')
    expect(detail('Recurring cost per processed item')).toBe('$0.1911')
  })

  it('with no labor rate: the workload estimate, and no labor-value estimate', async () => {
    serve(efficiencyStore({ organization: { labor_rate: null } }))

    await page()

    expect(metric('Estimated manual-workload equivalent')).toHaveTextContent(/^78\.50 hours$/)
    expect(metric('Estimated labor-value equivalent')).toHaveTextContent(/^Not available$/)
    expect(note('Estimated labor-value equivalent')).toBe('Configure a labor rate to estimate a labor-value equivalent.')
    expect(detail('Configured labor rate')).toBe('Not configured')
  })

  it('with no recurring cost: both estimates, and no cost or cost per item', async () => {
    serve(efficiencyStore({ sorter: { recurring_annual_cost: null } }))

    await page()

    expect(metric('Estimated manual-workload equivalent')).toHaveTextContent(/^78\.50 hours$/)
    expect(metric('Estimated labor-value equivalent')).toHaveTextContent(/^\$1,413\.00$/)
    expect(metric('Recurring cost for this period')).toHaveTextContent(/^Not available$/)
    expect(note('Recurring cost for this period')).toBe('Configure a recurring annual cost to show this.')
    expect(detail('Recurring cost per processed item')).toBe('Not available')
    expect(detail('Configured recurring annual cost')).toBe('Not configured')
  })

  it('with no in-service date: the full range is used, and that is said', async () => {
    serve(efficiencyStore({ sorter: { in_service_date: null } }))

    await page()

    expect(within_().getByText('No in-service date is configured, so the full selected range is used.')).toBeInTheDocument()
    expect(metric('Items processed')).toHaveTextContent(/^3,140$/)
    expect(detail('In-service date')).toBe('Not configured')
    expect(metric('Recurring cost for this period')).toHaveTextContent(/^\$600\.00$/)
  })

  it('with nothing configured at all: still a report, with the count and how to begin', async () => {
    serve(emptyStore())

    await page()

    expect(metric('Items processed')).toHaveTextContent(/^3,140$/)
    expect(within_().getByText(/No Efficiency assumptions are configured yet, so only the processing count can be shown\./)).toHaveRole('note')
    for (const label of ['Recurring cost for this period', 'Estimated manual-workload equivalent', 'Estimated labor-value equivalent']) {
      expect(metric(label)).toHaveTextContent(/^Not available$/)
    }
    expect(efficiency()).not.toHaveTextContent(/\$|Could not load/)
    expect(efficiency().textContent).not.toMatch(NOT_A_FIGURE)
    expect(within_().getByRole('button', { name: 'Efficiency settings' })).toBeInTheDocument()
  })

  it('a cost that was set to zero is a cost of zero, not a missing one', async () => {
    serve(efficiencyStore({ sorter: { recurring_annual_cost: '0.00', one_time_cost: '0.00' } }))

    await page()

    expect(metric('Recurring cost for this period')).toHaveTextContent(/^\$0\.00$/)
    expect(detail('Recurring cost per processed item')).toBe('$0.0000')
    expect(detail('Configured recurring annual cost')).toBe('$0.00 per year')
    expect(efficiency()).not.toHaveTextContent('Configure a recurring annual cost')
  })
})

describe('little or nothing processed', () => {
  it('shows zero workload and labor value, the cost that still applies, and makes no claim', async () => {
    serve({ ...efficiencyStore(), checkins: () => 0 })

    await page()

    expect(metric('Items processed')).toHaveTextContent(/^0$/)
    expect(metric('Estimated manual-workload equivalent')).toHaveTextContent(/^0\.00 hours$/)
    expect(metric('Estimated labor-value equivalent')).toHaveTextContent(/^\$0\.00$/)
    expect(metric('Recurring cost for this period')).toHaveTextContent(/^\$600\.00$/)
    expect(detail('Recurring cost per processed item')).toBe('Not available')
    expect(efficiency().textContent).not.toMatch(OVERCLAIMS)
    expect(efficiency()).not.toHaveTextContent(/-\$|loss|negative/i)
  })
})

describe('a range that starts before the in-service date', () => {
  it('counts the items from that date on, says how many of the range that is, and keeps the range’s own count in view', async () => {
    serve(efficiencyStore({ sorter: { in_service_date: '2026-09-28' } }))

    await page()

    // 28 September to 5 October: 8 days, 860 of the range's 3,140. 860 / 40 = 21.50 h; 7300 / 365 * 8 = 160.00.
    // The card stays a figure; the sentence that reconciles it with the range is under the Observed cards.
    expect(metric('Items processed')).toHaveTextContent(/^860$/)
    expect(note('Items processed')).toBe('In-service processing')
    const sentence = within_().getByText('860 of 3,140 processed items occurred on or after the configured in-service date.')
    expect(sentence.tagName).toBe('P')
    expect(sentence.closest('dl')).toBeNull()
    const observed = efficiency().querySelectorAll('dl.metrics')[0]
    expect(observed.nextElementSibling).toBe(sentence)
    expect(observed.textContent).not.toContain('occurred on or after')
    expect(detail('Items processed in the selected range')).toBe('3,140')
    expect(metric('Estimated manual-workload equivalent')).toHaveTextContent(/^21\.50 hours$/)
    expect(metric('Recurring cost for this period')).toHaveTextContent(/^\$160\.00$/)
    expect(note('Recurring cost for this period')).toBe('For 8 days in service')
    expect(detail('In-service date')).toBe('Sep 28, 2026')
    // The earlier items are not called wrong, and are not hidden.
    expect(efficiency()).not.toHaveTextContent(/invalid|incorrect|excluded|ignored|error/i)
  })

  it('says nothing about it when every item of the range counts', async () => {
    serve()

    await page()

    expect(efficiency()).not.toHaveTextContent('occurred on or after')
    expect(efficiency()).not.toHaveTextContent('In-service processing')
    expect(note('Items processed')).toBe('Over 30 days')
  })
})

// =====================================================================================================================
// When it cannot be shown
// =====================================================================================================================

describe('when the Efficiency report cannot be loaded', () => {
  it('says so in its own section, with a way to try again, and leaves the other four alone', async () => {
    serve(efficiencyStore(), { [PATHS.report]: SERVER_ERROR })

    await page()

    expect(within_().getByText('Could not load.')).toBeInTheDocument()
    expect(screen.getAllByText('Could not load.')).toHaveLength(1)
    expect(efficiency()).not.toHaveTextContent(/\$|Internal server error/)
    for (const name of ['Overview', 'Volume & capacity', 'Routing', 'Reliability']) {
      expect(within(screen.getByRole('region', { name })).getAllByRole('img').length).toBeGreaterThan(0)
    }
    expect(within_().getByRole('button', { name: 'Try again: Efficiency' })).toBeInTheDocument()
  })

  it('tries only Efficiency again, then moves focus to its heading', async () => {
    let calls = 0
    const store = efficiencyStore()
    serve(store, { [PATHS.report]: (url) => (calls++ === 0 ? SERVER_ERROR() : jsonResponse(200, efficiencyBody.report(store, ...(url.match(/\d{4}-\d{2}-\d{2}/g) as [string, string])))) })
    await page()
    const before = requestedUrls(fetchMock).length

    fireEvent.click(within_().getByRole('button', { name: 'Try again: Efficiency' }))

    await waitFor(() => expect(metric('Items processed')).toHaveTextContent(/^3,140$/))
    expect(requestedUrls(fetchMock).slice(before).every((url) => url.includes('/reports/efficiency'))).toBe(true)
    expect(within_().getByRole('heading', { level: 4, name: 'Efficiency' })).toHaveFocus()
  })

  it('says what to do when the stored settings cannot be read, shows no figure, and still offers the form', async () => {
    serve(efficiencyStore(), { [PATHS.report]: () => jsonResponse(500, STORED_INVALID) })

    await page()

    expect(within_().getByRole('note')).toHaveTextContent(
      'The Efficiency settings stored for this sorter or its organization could not be read, so no figures can be shown. Open Efficiency settings below and save them again to repair this.',
    )
    expect(efficiency()).not.toHaveTextContent(/\$|hours|The stored efficiency settings could not be read\./)
    expect(within_().getByRole('button', { name: 'Efficiency settings' })).toBeInTheDocument()
    // Asked once: the same answer would come back however often it was asked.
    await pass(5000)
    expect(efficiencyRequests()).toHaveLength(1)
    expect(screen.getAllByRole('region')).toHaveLength(6)
  })

  it('says it is not available when there is no such report for the sorter', async () => {
    serve(efficiencyStore(), { [PATHS.report]: () => jsonResponse(404, TENANT_NOT_FOUND) })

    await page()

    expect(within_().getByText('Efficiency is not available for this sorter yet.')).toBeInTheDocument()
    expect(within_().queryByRole('button')).not.toBeInTheDocument()
    expect(screen.getAllByRole('region')).toHaveLength(6)
  })

  it('treats a malformed answer as a failure and shows none of it', async () => {
    const body = efficiencyBody.report(efficiencyStore(), '2026-09-06', '2026-10-05')
    serve(efficiencyStore(), { [PATHS.report]: () => jsonResponse(200, { ...body, results: { ...body.results, labor_value_equivalent: 1413 } }) })

    await page()

    expect(within_().getByText('Could not load.')).toBeInTheDocument()
    expect(efficiency()).not.toHaveTextContent(/1,413|78\.50/)
  })

  it('returns to the sign-in form when the session has ended', async () => {
    serve(efficiencyStore(), { [PATHS.report]: () => jsonResponse(401, NOT_AUTHENTICATED) })

    renderApp(CENTRAL_REPORTS)

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
    expect(screen.getByTestId('address')).toHaveTextContent(CENTRAL_REPORTS)
  })

  it('does not hold up the other reports while it is still loading', async () => {
    const slow = deferred<Response>()
    serve(efficiencyStore(), { [PATHS.report]: () => slow.promise })

    renderApp(CENTRAL_REPORTS)
    await screen.findByRole('img', { name: /^Bar chart of rejects/ })

    expect(efficiency()).toHaveTextContent('Loading…')
    expect(within(screen.getByRole('region', { name: 'Overview' })).getByText('3,140')).toBeInTheDocument()
    slow.resolve(jsonResponse(200, efficiencyBody.report(efficiencyStore(), '2026-09-06', '2026-10-05')))
    await waitFor(() => expect(metric('Items processed')).toHaveTextContent(/^3,140$/))
  })
})

describe('a new range', () => {
  it('asks for Efficiency over the new range and shows nothing of the old one meanwhile', async () => {
    const store = efficiencyStore()
    const slow = deferred<Response>()
    serve(store, {
      [PATHS.report]: (url) => (url.includes(DEFAULT_RANGE) ? jsonResponse(200, efficiencyBody.report(store, '2026-09-06', '2026-10-05')) : slow.promise),
    })
    await page()
    expect(metric('Items processed')).toHaveTextContent(/^3,140$/)

    await user().click(screen.getByRole('button', { name: 'Last 7 days' }))

    expect(efficiency()).toHaveTextContent('Loading…')
    expect(efficiency()).not.toHaveTextContent(/3,140|1,413/)
    expect(efficiencyRequests().at(-1)).toBe('GET /branches/central/reports/efficiency?from=2026-09-29&to=2026-10-05')

    slow.resolve(jsonResponse(200, efficiencyBody.report(store, '2026-09-29', '2026-10-05')))
    // Tuesday to Monday: 760 check-ins.
    await waitFor(() => expect(metric('Items processed')).toHaveTextContent(/^760$/))
    expect(note('Items processed')).toBe('Over 7 days')
  })
})

// =====================================================================================================================
// The settings
// =====================================================================================================================

describe('the Efficiency settings', () => {
  it('are closed until asked for, and nothing is read until they are opened', async () => {
    serve()
    await page()

    const toggle = within_().getByRole('button', { name: 'Efficiency settings' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(within_().queryByRole('form')).not.toBeInTheDocument()
    expect(screen.queryByLabelText(ORGANIZATION_LABOR)).not.toBeInTheDocument()
    expect(efficiencyRequests()).toHaveLength(1)

    await openSettings()

    expect(within_().getByRole('button', { name: 'Hide Efficiency settings' })).toHaveAttribute('aria-expanded', 'true')
    expect(efficiencyRequests().slice(1).sort()).toEqual(['GET /branches/central/settings/efficiency', 'GET /settings/efficiency'])

    fireEvent.click(within_().getByRole('button', { name: 'Hide Efficiency settings' }))
    expect(within_().queryByRole('form')).not.toBeInTheDocument()
  })

  it('opens and closes from the keyboard', async () => {
    serve()
    await page()
    const person = user()

    within_().getByRole('button', { name: 'Efficiency settings' }).focus()
    await person.keyboard('{Enter}')
    await screen.findByLabelText(ORGANIZATION_LABOR)
    await person.keyboard(' ')

    expect(screen.queryByLabelText(ORGANIZATION_LABOR)).not.toBeInTheDocument()
  })

  it('are two forms, kept apart: the organization’s defaults and this sorter’s own figures', async () => {
    serve(efficiencyStore({ sorter: { labor_rate: '22.00' } }))
    await page()
    await openSettings()

    expect(within_().getAllByRole('form').map((form) => form.getAttribute('aria-labelledby'))).toEqual(['efficiency-organization-heading', 'efficiency-sorter-heading'])
    expect(within(organizationForm()).getAllByRole('textbox').map((input) => (input as HTMLInputElement).value)).toEqual(['18.00', '40.0'])
    expect([field(SORTER_LABOR).value, field(SORTER_MANUAL).value, field(ONE_TIME).value, field(RECURRING).value, field(IN_SERVICE).value]).toEqual([
      '22.00', '', '125000.00', '7300.00', '2020-11-20',
    ])
    expect(organizationForm()).toHaveTextContent('Organization defaults apply to sorting machines that do not have their own override.')
    expect(within(organizationForm()).getByRole('button', { name: 'Save organization defaults' })).toHaveAttribute('type', 'submit')
    expect(within(sorterForm()).getByRole('button', { name: 'Save this sorter' })).toHaveAttribute('type', 'submit')
  })

  it('calls the manual processing rate an assumption wherever it is entered, and suggests no rate of its own', async () => {
    serve(emptyStore())
    await page()
    await openSettings()

    expect(field(ORGANIZATION_MANUAL)).toHaveAccessibleDescription(
      /Items per staff labor-hour used to estimate equivalent manual workload\..*This value is an assumption used for workload estimates\. It should ideally be based on local observation or a documented workflow study\./,
    )
    expect(field(ORGANIZATION_MANUAL)).toHaveAccessibleDescription(/SortView supplies no rate of its own/)
    expect(field(SORTER_MANUAL)).toHaveAccessibleDescription(/An assumption, not a measurement\./)
    // Nothing is filled in, and nothing is offered as a starting value.
    for (const input of screen.getAllByRole('textbox') as HTMLInputElement[]) {
      expect(input.value).toBe('')
      expect(input).not.toHaveAttribute('placeholder')
    }
    expect(efficiency().textContent).not.toMatch(/measured productivity|staff productivity|processing speed/i)
  })

  it('says, for each rate, what the default is, what the override is and which is in effect', async () => {
    serve(efficiencyStore({ sorter: { labor_rate: '22.00' } }))
    await page()
    await openSettings()

    expect(field(SORTER_LABOR)).toHaveAccessibleDescription(
      /Organization default: \$18\.00 per hour\. This sorter’s override: \$22\.00 per hour\. In effect: \$22\.00 per hour, this sorter’s own\./,
    )
    expect(field(SORTER_MANUAL)).toHaveAccessibleDescription(
      /Organization default: 40\.0 items per staff labor-hour\. This sorter’s override: none\. In effect: 40\.0 items per staff labor-hour, inherited from the organization default\./,
    )
  })

  it('says the estimate is unavailable when neither the sorter nor the organization has a rate', async () => {
    serve(emptyStore())
    await page()
    await openSettings()

    expect(field(SORTER_MANUAL)).toHaveAccessibleDescription(
      /Organization default: none set\. This sorter’s override: none\. No manual processing rate assumption is configured, so the estimate is unavailable until an assumption is configured\./,
    )
    expect(field(SORTER_LABOR)).toHaveAccessibleDescription(/No labor rate is configured, so the estimate is unavailable until an assumption is configured\./)
  })
})

describe('saving the organization’s defaults', () => {
  it('sends exactly the two fields as typed, then shows what was stored and works the figures out again', async () => {
    serve()
    await page()
    await openSettings()

    type(ORGANIZATION_LABOR, ' 20 ')
    type(ORGANIZATION_MANUAL, '50')
    // Nothing changes while it is only typed.
    expect(metric('Estimated labor-value equivalent')).toHaveTextContent(/^\$1,413\.00$/)
    save(organizationForm())

    expect(await within(organizationForm()).findByRole('status')).toHaveTextContent('Saved. The Efficiency figures are being worked out again.')
    expect(puts()).toEqual([['/api/organizations/northbridge/settings/efficiency', { labor_rate: '20', manual_items_per_hour: '50' }]])
    // What the API stored, not what was typed.
    expect([field(ORGANIZATION_LABOR).value, field(ORGANIZATION_MANUAL).value]).toEqual(['20.00', '50.0'])
    // 3140 / 50 = 62.80 h; x 20 = 1,256.00.
    await waitFor(() => expect(metric('Estimated labor-value equivalent')).toHaveTextContent(/^\$1,256\.00$/))
    expect(metric('Estimated manual-workload equivalent')).toHaveTextContent(/^62\.80 hours$/)
    // The report and this sorter's view of the defaults were asked for again; the sorter's own form was not saved.
    const after = efficiencyRequests().slice(efficiencyRequests().indexOf('PUT /settings/efficiency') + 1).sort()
    expect(after).toEqual([`GET /branches/central/reports/efficiency?${DEFAULT_RANGE}`, 'GET /branches/central/settings/efficiency'])
    await waitFor(() => expect(field(SORTER_MANUAL)).toHaveAccessibleDescription(/Organization default: 50\.0 items per staff labor-hour/))
  })

  it('clears a default that is left blank', async () => {
    serve()
    await page()
    await openSettings()

    type(ORGANIZATION_MANUAL, '')
    save(organizationForm())

    await within(organizationForm()).findByRole('status')
    expect(puts()[0][1]).toEqual({ labor_rate: '18.00', manual_items_per_hour: null })
    await waitFor(() => expect(metric('Estimated manual-workload equivalent')).toHaveTextContent(/^Not available$/))
  })

  it('does not touch what is being typed into the sorter’s form', async () => {
    serve()
    await page()
    await openSettings()

    type(ONE_TIME, '99999.99')
    type(ORGANIZATION_LABOR, '19')
    save(organizationForm())
    await within(organizationForm()).findByRole('status')
    await waitFor(() => expect(field(SORTER_LABOR)).toHaveAccessibleDescription(/Organization default: \$19\.00 per hour/))

    expect(field(ONE_TIME).value).toBe('99999.99')
    expect(puts().map(([url]) => url)).toEqual(['/api/organizations/northbridge/settings/efficiency'])
    expect(within(sorterForm()).queryByRole('status')).not.toBeInTheDocument()
  })
})

describe('saving this sorter’s figures', () => {
  it('sends exactly the five fields, then asks for this sorter’s report again', async () => {
    serve()
    await page()
    await openSettings()

    type(SORTER_LABOR, '22.5')
    type(SORTER_MANUAL, '50')
    type(RECURRING, '3650')
    type(IN_SERVICE, '2026-09-28')
    save(sorterForm())

    expect(await within(sorterForm()).findByRole('status')).toHaveTextContent('Saved.')
    expect(puts()).toEqual([
      [
        '/api/organizations/northbridge/branches/central/settings/efficiency',
        { labor_rate: '22.5', manual_items_per_hour: '50', one_time_cost: '125000.00', recurring_annual_cost: '3650', in_service_date: '2026-09-28' },
      ],
    ])
    expect([field(SORTER_LABOR).value, field(SORTER_MANUAL).value, field(RECURRING).value]).toEqual(['22.50', '50.0', '3650.00'])
    // 860 items from 28 September. 860 / 50 = 17.20 h; x 22.50 = 387.00; 3650 / 365 * 8 = 80.00.
    await waitFor(() => expect(metric('Estimated labor-value equivalent')).toHaveTextContent(/^\$387\.00$/))
    expect(metric('Recurring cost for this period')).toHaveTextContent(/^\$80\.00$/)
    expect(detail('Configured labor rate')).toBe('$22.50 per hour (set for this sorter)')
    expect(efficiencyRequests().at(-1)).toBe(`GET /branches/central/reports/efficiency?${DEFAULT_RANGE}`)
    // The organization's defaults were neither saved nor asked for again.
    expect(efficiencyRequests().filter((request) => request.endsWith(' /settings/efficiency'))).toEqual(['GET /settings/efficiency'])
  })

  it('a blank override goes back to the organization’s default', async () => {
    serve(efficiencyStore({ sorter: { labor_rate: '22.00', manual_items_per_hour: '50.0' } }))
    await page()
    await openSettings()

    type(SORTER_LABOR, '')
    type(SORTER_MANUAL, '   ')
    save(sorterForm())

    await within(sorterForm()).findByRole('status')
    expect(puts()[0][1]).toMatchObject({ labor_rate: null, manual_items_per_hour: null })
    await waitFor(() => expect(detail('Configured labor rate')).toBe('$18.00 per hour (organization default)'))
    expect(field(SORTER_LABOR)).toHaveAccessibleDescription(/This sorter’s override: none\. In effect: \$18\.00 per hour, inherited from the organization default\./)
    expect(field(SORTER_MANUAL).value).toBe('')
  })

  it('keeps an explicit zero as zero, and a blank cost as not known', async () => {
    serve()
    await page()
    await openSettings()

    type(RECURRING, '0')
    type(ONE_TIME, '')
    save(sorterForm())

    await within(sorterForm()).findByRole('status')
    expect(puts()[0][1]).toMatchObject({ recurring_annual_cost: '0', one_time_cost: null })
    expect([field(RECURRING).value, field(ONE_TIME).value]).toEqual(['0.00', ''])
    await waitFor(() => expect(metric('Recurring cost for this period')).toHaveTextContent(/^\$0\.00$/))
    expect(detail('Recurring cost per processed item')).toBe('$0.0000')
  })

  it('a blank in-service date clears it', async () => {
    serve()
    await page()
    await openSettings()

    type(IN_SERVICE, '')
    save(sorterForm())

    await within(sorterForm()).findByRole('status')
    expect(puts()[0][1]).toMatchObject({ in_service_date: null })
    await waitFor(() => expect(within_().getByText('No in-service date is configured, so the full selected range is used.')).toBeInTheDocument())
  })
})

describe('a value that cannot be saved', () => {
  it.each([
    [ORGANIZATION_LABOR, '17.567', 'Use at most 2 decimal places. The value is not rounded for you.'],
    [ORGANIZATION_LABOR, '0', 'Enter an amount above 0, up to 1,000.'],
    [ORGANIZATION_LABOR, '$18.00', 'Enter digits only, with a decimal point if needed: no symbols, commas or spaces.'],
    [ORGANIZATION_MANUAL, '45.25', 'Use at most 1 decimal place. The value is not rounded for you.'],
    [ORGANIZATION_MANUAL, '0.5', 'Enter a rate from 1 to 1,000.'],
  ])('is refused before anything is sent: %s = %j', async (label, typed, sentence) => {
    serve()
    await page()
    await openSettings()

    type(label, typed)
    save(organizationForm())

    expect(problemOf(label)).toBe(sentence)
    expect(field(label)).toHaveAttribute('aria-invalid', 'true')
    expect(field(label)).toHaveAccessibleDescription(new RegExp(sentence.replace(/[.$]/g, '\\$&')))
    expect(within(organizationForm()).getByRole('alert')).toHaveTextContent('Nothing was saved. Check the fields marked below.')
    // What was typed is still there, untouched; nothing was sent and no figure moved.
    expect(field(label).value).toBe(typed)
    expect(puts()).toEqual([])
    expect(metric('Estimated labor-value equivalent')).toHaveTextContent(/^\$1,413\.00$/)
    expect(within(organizationForm()).queryByRole('status')).not.toBeInTheDocument()
  })

  it('refuses an in-service date after the product’s today, and offers no later date', async () => {
    serve()
    await page()
    await openSettings()

    expect(field(IN_SERVICE)).toHaveAttribute('max', '2026-10-05')
    type(IN_SERVICE, '2026-10-06')
    type(ONE_TIME, '100000000.01')
    save(sorterForm())

    expect(problemOf(IN_SERVICE)).toBe('The in-service date cannot be after today.')
    expect(problemOf(ONE_TIME)).toBe('Enter an amount from 0 to 100,000,000.')
    expect(puts()).toEqual([])

    type(IN_SERVICE, '2026-10-05')
    type(ONE_TIME, '100000000')
    save(sorterForm())
    await within(sorterForm()).findByRole('status')
    expect(problemOf(IN_SERVICE)).toBeNull()
    expect(field(IN_SERVICE)).toHaveAttribute('aria-invalid', 'false')
  })

  it('takes today from the product’s zone, not this machine’s or UTC', async () => {
    // 10:30 PM on 5 October in Chicago; already the 6th in UTC.
    vi.setSystemTime(new Date('2026-10-06T03:30:00Z'))
    serve()
    await page()
    await openSettings()

    expect(field(IN_SERVICE)).toHaveAttribute('max', '2026-10-05')
    type(IN_SERVICE, '2026-10-06')
    save(sorterForm())

    expect(problemOf(IN_SERVICE)).toBe('The in-service date cannot be after today.')
  })

  it('shows what the API says about each field, by its code, when the API refuses', async () => {
    serve(efficiencyStore(), {
      [`PUT ${PATHS.sorter}`]: () => jsonResponse(422, invalidSettings(['recurring_annual_cost', 'out_of_range'], ['in_service_date', 'in_the_future'])),
    })
    await page()
    await openSettings()

    save(sorterForm())

    await waitFor(() => expect(problemOf(RECURRING)).toBe('Enter an amount from 0 to 10,000,000.'))
    expect(problemOf(IN_SERVICE)).toBe('The in-service date cannot be after today.')
    expect(within(sorterForm()).getByRole('alert')).toHaveTextContent('Nothing was saved. Check the fields marked below.')
    expect(within(sorterForm()).queryByRole('status')).not.toBeInTheDocument()
    expect(metric('Recurring cost for this period')).toHaveTextContent(/^\$600\.00$/)
  })

  it.each([
    ['a refusal', () => jsonResponse(403, FORBIDDEN), 'Nothing was saved. You do not have permission to manage these settings.'],
    [
      'a suspended organization',
      () => jsonResponse(403, { code: 'organization_read_only', message: "This organization's settings cannot be changed." }),
      "Nothing was saved. This organization's settings cannot be changed.",
    ],
    ['a server error', SERVER_ERROR, 'Nothing was saved. Internal server error.'],
    [
      'stored defaults that cannot be read',
      () => jsonResponse(500, STORED_INVALID),
      'Nothing was saved: the organization defaults that are stored could not be read. Save the organization defaults first, then save this again.',
    ],
  ])('says nothing was saved, and why, for %s', async (_label, reply, sentence) => {
    serve(efficiencyStore(), { [`PUT ${PATHS.sorter}`]: reply })
    await page()
    await openSettings()

    type(RECURRING, '3650')
    save(sorterForm())

    await waitFor(() => expect(within(sorterForm()).getByRole('alert')).toHaveTextContent(sentence))
    expect(field(RECURRING).value).toBe('3650')
    expect(metric('Recurring cost for this period')).toHaveTextContent(/^\$600\.00$/)
    expect(puts()).toHaveLength(1)
  })

  it('returns to the sign-in form when the session has ended', async () => {
    serve(efficiencyStore(), { [`PUT ${PATHS.organization}`]: () => jsonResponse(401, NOT_AUTHENTICATED) })
    await page()
    await openSettings()

    save(organizationForm())

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('form', { name: 'Organization defaults' })).not.toBeInTheDocument()
  })

  it('does not send a second save while one is on its way', async () => {
    const slow = deferred<Response>()
    serve(efficiencyStore(), { [`PUT ${PATHS.organization}`]: () => slow.promise })
    await page()
    await openSettings()

    save(organizationForm())
    const button = await within(organizationForm()).findByRole('button', { name: 'Saving…' })
    expect(button).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(button)

    expect(puts()).toHaveLength(1)
    slow.resolve(jsonResponse(200, efficiencyBody.organization(efficiencyStore())))
    await within(organizationForm()).findByRole('status')
  })
})

describe('settings that are stored but cannot be read', () => {
  it('still offers the forms, empty, so that saving can repair them', async () => {
    const store = efficiencyStore()
    let repaired = false
    serve(store, {
      [PATHS.report]: (url) => (repaired ? jsonResponse(200, efficiencyBody.report(store, ...(url.match(/\d{4}-\d{2}-\d{2}/g) as [string, string]))) : jsonResponse(500, STORED_INVALID)),
      [`GET ${PATHS.organization}`]: () => (repaired ? jsonResponse(200, efficiencyBody.organization(store)) : jsonResponse(500, STORED_INVALID)),
      [`GET ${PATHS.sorter}`]: () => (repaired ? jsonResponse(200, efficiencyBody.sorter(store)) : jsonResponse(500, STORED_INVALID)),
      [`PUT ${PATHS.organization}`]: () => {
        repaired = true
        return jsonResponse(200, efficiencyBody.organization(store))
      },
    })
    await page()
    await openSettings()

    expect(within(organizationForm()).getByRole('note')).toHaveTextContent('What is stored here could not be read, so the fields start empty. Saving replaces it.')
    expect(field(ORGANIZATION_LABOR).value).toBe('')
    expect(within(sorterForm()).getByRole('note')).toBeInTheDocument()

    type(ORGANIZATION_LABOR, '18')
    type(ORGANIZATION_MANUAL, '40')
    save(organizationForm())

    // Saved: the report can be read again, and so can this sorter's settings.
    await waitFor(() => expect(metric('Estimated labor-value equivalent')).toHaveTextContent(/^\$1,413\.00$/))
    await waitFor(() => expect(field(ONE_TIME).value).toBe('125000.00'))
  })

  it('says so, with a way to try again, when the settings simply could not be loaded', async () => {
    let calls = 0
    serve(efficiencyStore(), { [`GET ${PATHS.organization}`]: () => (calls++ === 0 ? SERVER_ERROR() : jsonResponse(200, efficiencyBody.organization(efficiencyStore()))) })
    await page()
    fireEvent.click(within_().getByRole('button', { name: 'Efficiency settings' }))
    await screen.findByLabelText(SORTER_LABOR)

    const retry = await within_().findByRole('button', { name: 'Try again' })
    expect(screen.queryByLabelText(ORGANIZATION_LABOR)).not.toBeInTheDocument()
    fireEvent.click(retry)

    expect(await screen.findByLabelText(ORGANIZATION_LABOR)).toHaveValue('18.00')
  })
})

// =====================================================================================================================
// Without seeing or pointing
// =====================================================================================================================

describe('reading and using Efficiency without seeing or pointing', () => {
  beforeEach(async () => {
    serve()
    await page()
    await openSettings()
  })

  it('names the section, its groups, its table and its forms, with headings in order', () => {
    expect(efficiency().tagName).toBe('SECTION')
    expect(Array.from(efficiency().querySelectorAll('h4, h5, h6')).map((heading) => `${heading.tagName} ${heading.textContent}`)).toEqual([
      'H4 Efficiency',
      'H5 Observed',
      'H5 Estimated from assumptions',
      'H5 Assumptions and details',
      'H6 Organization defaults',
      'H6 This sorter',
    ])
    const details = within_().getByRole('table', { name: 'Assumptions and details' })
    for (const row of within(details).getAllByRole('row').slice(1)) {
      expect(row.firstElementChild).toHaveAttribute('scope', 'row')
    }
  })

  it('gives every field a label and a description, and disables nothing', () => {
    const inputs = Array.from(efficiency().querySelectorAll('input'))
    expect(inputs).toHaveLength(7)
    for (const input of inputs) {
      expect(input).toHaveAccessibleName()
      expect(input).toHaveAccessibleDescription()
      expect(input).toHaveAttribute('aria-invalid', 'false')
    }
    expect(inputs.map((input) => input.type)).toEqual(['text', 'text', 'text', 'text', 'text', 'text', 'date'])
    expect(efficiency().querySelectorAll('[disabled], [title], [onclick], [role="button"], [tabindex]:not([tabindex="-1"])')).toHaveLength(0)
  })

  it('saves from the keyboard', async () => {
    const person = user()

    field(ORGANIZATION_LABOR).focus()
    await person.clear(field(ORGANIZATION_LABOR))
    await person.keyboard('19.25{Enter}')

    expect(await within(organizationForm()).findByRole('status')).toHaveTextContent('Saved.')
    expect(puts()[0][1]).toEqual({ labor_rate: '19.25', manual_items_per_hour: '40.0' })
  })

  it('keeps nothing in the browser’s storage', () => {
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.length).toBe(0)
  })
})
