import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { efficiencyRoutes, efficiencyStore } from '../test/efficiency.ts'
import {
  ALICE,
  type ApiRoutes,
  deferred,
  type FetchMock,
  jsonResponse,
  liveRoutes,
  NORTHBRIDGE,
  NORTHBRIDGE_DETAIL,
  NOT_AUTHENTICATED,
  organizationReportRoutes,
  REPORT,
  reportRoutes,
  serveApi,
} from '../test/http.ts'
import { renderApp } from '../test/render.tsx'
import {
  emptyValueStore,
  invalidValueSettings,
  VALUE_FORBIDDEN,
  VALUE_READ_ONLY,
  VALUE_STORED_INVALID,
  valueBody,
  valuePaths,
  valueRoutes,
  type ValueStore,
  valueStore,
} from '../test/sortviewValue.ts'
import { DISCLOSURE } from './sortviewValueText.ts'

/**
 * Reports R10G: the Value view of an organization's reports -- SortView
 * Value, an estimate from assumptions the organization entered, for its
 * owners and admins.
 *
 * Every figure on the page is the API's. A figure that cannot be worked out
 * is "Not available", never 0; one below zero is written as it is.
 */

// 1:50 PM on Monday 5 October 2026 in Chicago: the fixtures' "today". The Value view itself reads no clock.
const NOW = '2026-10-05T18:50:00Z'
const ORG_VALUE = '/organizations/northbridge/reports/value'
const PATHS = valuePaths('northbridge')
const REPORT_URL = '/api/organizations/northbridge/reports/sortview-value'
const SETTINGS_URL = '/api/organizations/northbridge/settings/sortview-value'
const SERVER_ERROR = () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' })

const LABOR = 'SortView Value labor rate (USD per hour)'
const COST = 'Annual subscription cost (USD per year)'
const TASKS = 'Reporting tasks per month'
const REPORTING_BASIS = 'Basis of the reporting figures'
const REPORTING_AS_OF = 'Reporting figures as of'
const STAFF = 'Staff per troubleshooting incident'

let fetchMock: FetchMock
let print: ReturnType<typeof vi.fn>

interface Served {
  role?: string
  access_mode?: string
  exports?: boolean
}

/** A member of Northbridge, whose SortView Value is answered from `store`. */
function serve(store: ValueStore = valueStore(), overrides: ApiRoutes = {}, { role = 'admin', access_mode = 'full', exports = false }: Served = {}): FetchMock {
  const entitlements = { ...NORTHBRIDGE_DETAIL.entitlements, ...(exports ? { exports: { enabled: true, limit_value: null } } : {}) }
  fetchMock = serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    'GET /api/organizations': () => jsonResponse(200, [{ ...NORTHBRIDGE, role, access_mode }]),
    'GET /api/organizations/northbridge': () => jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role, access_mode, entitlements }),
    ...liveRoutes('northbridge', 'central'),
    ...reportRoutes('northbridge', 'central'),
    ...organizationReportRoutes('northbridge', NORTHBRIDGE_DETAIL.sorters.map((sorter) => ({ ...sorter, report: REPORT }))),
    ...efficiencyRoutes('northbridge', 'central', efficiencyStore(), () => fetchMock),
    ...valueRoutes('northbridge', store, () => fetchMock),
    ...overrides,
  })
  return fetchMock
}

const main = () => screen.getByRole('main')
const value = () => screen.getByRole('region', { name: 'SortView Value' })
const within_ = () => within(value())
const card = (label: string) => within_().getByText(label, { selector: 'dt' }).parentElement as HTMLElement
const metric = (label: string) => card(label).querySelector('.metric-value')?.textContent
const note = (label: string) => card(label).querySelector('.metric-note')?.textContent
const cells = (table: string, row: string) =>
  within(within(within_().getByRole('table', { name: table })).getByRole('rowheader', { name: row }).closest('tr') as HTMLElement)
    .getAllByRole('cell')
    .map((cell) => cell.textContent)
const kind = (row: string) => cells('By kind of work', row)
const assumption = (row: string) => cells('Assumptions used', row)[0]
/** Every request about SortView Value, in order: its method and its address. */
const valueRequests = () =>
  fetchMock.mock.calls.map(([url, init]) => `${init?.method ?? 'GET'} ${String(url)}`).filter((request) => request.includes('sortview-value'))
const puts = () => valueRequests().filter((request) => request.startsWith('PUT '))
const field = (label: string) => screen.getByLabelText(label) as HTMLInputElement
const type = (label: string, typed: string) => fireEvent.change(field(label), { target: { value: typed } })
const form = () => screen.getByRole('form', { name: 'Assumptions' })
const save = () => fireEvent.click(within(form()).getByRole('button', { name: 'Save assumptions' }))
const toggle = (name = 'Edit assumptions') => within_().getByRole('button', { name })

async function page(path = ORG_VALUE) {
  renderApp(path)
  await screen.findByRole('region', { name: 'SortView Value' })
  await waitFor(() => expect(value()).not.toHaveTextContent('Loading…'))
}

async function openForm() {
  fireEvent.click(toggle())
  await screen.findByLabelText(LABOR)
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'], shouldAdvanceTime: true })
  vi.setSystemTime(new Date(NOW))
  print = vi.fn()
  window.print = print as unknown as typeof window.print
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

// =====================================================================================================================
// Who it is for
// =====================================================================================================================

describe('who sees SortView Value', () => {
  it.each(['owner', 'admin'])('an %s is shown the report, from one request', async (role) => {
    serve(valueStore(), {}, { role })

    await page()

    expect(screen.getByRole('heading', { level: 3, name: 'Organization Reports' })).toBeInTheDocument()
    expect(within_().getByRole('heading', { level: 4, name: 'SortView Value' })).toBeInTheDocument()
    expect(metric('Annual estimated value')).toBe('$7,150.00')
    expect(valueRequests()).toEqual([`GET ${REPORT_URL}`])
  })

  it.each(['manager', 'viewer', 'superuser', ''])('a member with the role %j is told who it is for, and nothing is asked for', async (role) => {
    serve(valueStore(), {}, { role })

    renderApp(ORG_VALUE)

    expect(await screen.findByText('SortView Value is available to this organization’s owners and administrators.')).toHaveRole('note')
    expect(screen.queryByRole('region', { name: 'SortView Value' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /assumptions/ })).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/\$|ROI|Payback|hours/)
    expect(valueRequests()).toEqual([])
  })

  it('shows a contained refusal, and nothing of the report, when the API says 403 after all', async () => {
    serve(valueStore(), { [PATHS.report]: () => jsonResponse(403, VALUE_FORBIDDEN) })

    await page()

    expect(within_().getByRole('note')).toHaveTextContent('SortView Value is available to this organization’s owners and administrators.')
    expect(value()).not.toHaveTextContent(/\$|hours|Try again|You do not have permission/)
    expect(within_().queryByRole('button')).not.toBeInTheDocument()
    expect(valueRequests()).toEqual([`GET ${REPORT_URL}`])
  })
})

// =====================================================================================================================
// What it says
// =====================================================================================================================

describe('a complete SortView Value report', () => {
  beforeEach(async () => {
    serve()
    await page()
  })

  it('says, in plain sight, that the figures are estimates from the organization’s own assumptions', () => {
    expect(within_().getByText(DISCLOSURE)).toBeVisible()
    expect(value()).toHaveTextContent(
      'SortView Value figures are estimates based on workflow assumptions entered by your organization. They are intended for planning and evaluation and do not represent guaranteed cost savings.',
    )
    expect(value()).not.toHaveTextContent(/operational data|hours saved|time saved|money saved/i)
  })

  it('shows the seven headline figures, each as the API wrote it', () => {
    expect(within_().getAllByRole('term').map((term) => term.textContent)).toEqual([
      'Recovered hours per month',
      'Monthly estimated value',
      'Annual estimated value',
      'Subscription annual cost',
      'Net estimated annual value',
      'Estimated ROI',
      'Payback period',
    ])
    expect(metric('Recovered hours per month')).toBe('18.33 hours')
    expect(metric('Monthly estimated value')).toBe('$595.83')
    expect(metric('Annual estimated value')).toBe('$7,150.00')
    expect(metric('Subscription annual cost')).toBe('$4,800.00')
    expect(metric('Net estimated annual value')).toBe('$2,350.00')
    expect(metric('Estimated ROI')).toBe('49.0%')
    expect(metric('Payback period')).toBe('8.1 months')
    expect(note('Payback period')).toBe('Months of estimated value needed to cover one year of subscription cost.')
    // Nothing is missing, so nothing says anything is.
    expect(value()).not.toHaveTextContent(/Not available|not set/i)
    expect(within_().queryByRole('note')).not.toBeInTheDocument()
  })

  it('breaks the estimate down by kind of work, in a table with headers for its columns and rows', () => {
    const table = within_().getByRole('table', { name: 'By kind of work' })

    expect(within(table).getAllByRole('columnheader').map((header) => header.textContent)).toEqual([
      'Kind of work',
      'Baseline hours/month',
      'Current hours/month',
      'Recovered hours/month',
      'Monthly value',
      'Annual value',
    ])
    expect(within(table).getAllByRole('rowheader').map((header) => header.textContent)).toEqual(['Reporting', 'Troubleshooting'])
    expect(kind('Reporting')).toEqual(['18.00 hours', '3.00 hours', '15.00 hours', '$487.50', '$5,850.00'])
    expect(kind('Troubleshooting')).toEqual(['6.00 hours', '2.67 hours', '3.33 hours', '$108.33', '$1,300.00'])
    expect(value()).not.toHaveTextContent('Current time is higher than the baseline')
  })

  it('lists the assumptions used: whose the labor rate is, and what each kind of work’s figures are and as of when', () => {
    expect(assumption('Labor rate')).toBe('$32.50 per hour (set for SortView Value)')
    expect(assumption('Annual subscription cost')).toBe('$4,800.00 per year')
    expect(assumption('Reporting: tasks per month')).toBe('12.0 tasks')
    expect(assumption('Reporting: baseline minutes per task')).toBe('90.0 minutes')
    expect(assumption('Reporting: current minutes per task')).toBe('15.0 minutes')
    expect(assumption('Reporting: basis of these figures')).toBe('Estimate, as of Sep 1, 2026')
    expect(assumption('Troubleshooting: incidents per month')).toBe('4.0 incidents')
    expect(assumption('Troubleshooting: staff per incident')).toBe('2.0 staff')
    expect(assumption('Troubleshooting: baseline minutes per person')).toBe('45.0 minutes')
    expect(assumption('Troubleshooting: current minutes per person')).toBe('20.0 minutes')
    expect(assumption('Troubleshooting: basis of these figures')).toBe('Measured, as of Sep 15, 2026')
  })

  it('has no range of days, nothing to compare, and nothing to download', () => {
    expect(screen.queryByRole('button', { name: 'Last 30 days' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('From')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('To')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Compare/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /CSV/ })).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/previous period|Download/i)
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })

  it('names its headings in order, and marks nothing by colour', () => {
    expect(within(main()).getAllByRole('heading').map((heading) => `${heading.tagName} ${heading.textContent}`)).toEqual(
      expect.arrayContaining(['H3 Organization Reports', 'H4 SortView Value', 'H5 Estimated value', 'H5 By kind of work', 'H5 Assumptions used']),
    )
    expect(within_().getAllByRole('heading').map((heading) => heading.textContent)).toEqual(['SortView Value', 'Estimated value', 'By kind of work', 'Assumptions used'])
    expect(value().querySelector('[style], .negative, .positive, .good, .bad, [aria-live]')).toBeNull()
    expect(within_().queryByRole('alert')).not.toBeInTheDocument()
  })

  it('keeps nothing in the browser’s storage', () => {
    expect(localStorage).toHaveLength(0)
    expect(sessionStorage).toHaveLength(0)
  })
})

describe('whose the labor rate is', () => {
  it('says so when it is the organization’s Efficiency default', async () => {
    serve(valueStore({ labor_rate: null, efficiency_labor_rate: '20.00' }))

    await page()

    expect(assumption('Labor rate')).toBe('$20.00 per hour (organization Efficiency default)')
    expect(metric('Monthly estimated value')).toBe('$366.67')
    expect(within_().queryByRole('note')).not.toBeInTheDocument()
  })

  it('is SortView Value’s own when both are set', async () => {
    serve(valueStore({ efficiency_labor_rate: '20.00' }))

    await page()

    expect(assumption('Labor rate')).toBe('$32.50 per hour (set for SortView Value)')
    expect(metric('Monthly estimated value')).toBe('$595.83')
  })
})

// =====================================================================================================================
// What it does not know
// =====================================================================================================================

describe('when assumptions are missing', () => {
  it('with nothing set: says what is not set, shows no figure and no zero in its place', async () => {
    serve(emptyValueStore())

    await page()

    const missing = within_().getByRole('note')
    expect(missing).toHaveTextContent('Some assumptions are not set, so some figures cannot be estimated. Not set:')
    expect(within(missing).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'Labor rate. A SortView Value labor rate can be entered, or the organization Efficiency default can supply it.',
      'Annual subscription cost',
      'Reporting tasks per month',
      'Baseline minutes per reporting task',
      'Current minutes per reporting task',
      'Troubleshooting incidents per month',
      'Staff per troubleshooting incident',
      'Baseline minutes per person per incident',
      'Current minutes per person per incident',
    ])
    for (const label of ['Recovered hours per month', 'Monthly estimated value', 'Annual estimated value', 'Subscription annual cost', 'Net estimated annual value', 'Estimated ROI', 'Payback period']) {
      expect(metric(label)).toBe('Not available')
    }
    expect(note('Recovered hours per month')).toBe('Needs both kinds of work to be complete.')
    expect(note('Subscription annual cost')).toBe('Not entered.')
    expect(kind('Reporting')).toEqual(Array(5).fill('Not available'))
    expect(kind('Troubleshooting')).toEqual(Array(5).fill('Not available'))
    expect(assumption('Labor rate')).toBe('Not available. A SortView Value labor rate can be entered, or the organization Efficiency default can supply it.')
    expect(assumption('Annual subscription cost')).toBe('Not set')
    expect(assumption('Reporting: basis of these figures')).toBe('Not set')
    // Not a figure anywhere: no amount, no percentage, no number of hours.
    expect(value()).not.toHaveTextContent(/\$\d|\d%|\d hours|\d months/)
    // And it is still a report, with the way to enter what is missing.
    expect(toggle()).toBeInTheDocument()
  })

  it('with no labor rate: the hours, and no value', async () => {
    serve(valueStore({ labor_rate: null }))

    await page()

    expect(within(within_().getByRole('note')).getAllByRole('listitem')).toHaveLength(1)
    expect(metric('Recovered hours per month')).toBe('18.33 hours')
    expect(metric('Monthly estimated value')).toBe('Not available')
    expect(note('Monthly estimated value')).toBe('Needs a labor rate.')
    expect(metric('Subscription annual cost')).toBe('$4,800.00')
    expect(metric('Estimated ROI')).toBe('Not available')
    expect(note('Payback period')).toBe('Needs a labor rate.')
    expect(kind('Reporting')).toEqual(['18.00 hours', '3.00 hours', '15.00 hours', 'Not available', 'Not available'])
  })

  it('with one kind of work incomplete: the other’s figures, and no total', async () => {
    serve(valueStore({ troubleshooting: { staff_per_incident: null } }))

    await page()

    expect(within(within_().getByRole('note')).getAllByRole('listitem').map((item) => item.textContent)).toEqual(['Staff per troubleshooting incident'])
    expect(kind('Reporting')).toEqual(['18.00 hours', '3.00 hours', '15.00 hours', '$487.50', '$5,850.00'])
    expect(kind('Troubleshooting')).toEqual(Array(5).fill('Not available'))
    expect(metric('Recovered hours per month')).toBe('Not available')
    expect(metric('Annual estimated value')).toBe('Not available')
    expect(note('Annual estimated value')).toBe('Needs both kinds of work to be complete.')
    expect(assumption('Troubleshooting: staff per incident')).toBe('Not set')
  })

  it('with no subscription cost: the value, and nothing set against it', async () => {
    serve(valueStore({ subscription_annual_cost: null }))

    await page()

    expect(metric('Annual estimated value')).toBe('$7,150.00')
    expect(metric('Subscription annual cost')).toBe('Not available')
    expect(metric('Net estimated annual value')).toBe('Not available')
    expect(note('Net estimated annual value')).toBe('Needs the annual subscription cost.')
    expect(metric('Estimated ROI')).toBe('Not available')
    expect(metric('Payback period')).toBe('Not available')
  })

  it('a subscription cost of zero is a cost of zero, and has no return on it', async () => {
    serve(valueStore({ subscription_annual_cost: '0.00' }))

    await page()

    expect(metric('Subscription annual cost')).toBe('$0.00')
    expect(metric('Net estimated annual value')).toBe('$7,150.00')
    expect(metric('Estimated ROI')).toBe('Not available')
    expect(note('Estimated ROI')).toBe('Not available for a subscription cost of zero.')
    expect(metric('Payback period')).toBe('0.0 months')
    expect(assumption('Annual subscription cost')).toBe('$0.00 per year')
    // Nothing is missing: zero was entered.
    expect(within_().queryByRole('note')).not.toBeInTheDocument()
  })
})

describe('results below zero', () => {
  it('are written as they are, with a minus sign and a plain sentence, and are not marked as a fault', async () => {
    serve(valueStore({ reporting: { current_minutes_per_task: '120.0' } }))

    await page()

    expect(kind('Reporting')).toEqual(['18.00 hours', '24.00 hours', '-6.00 hours', '-$195.00', '-$2,340.00'])
    expect(kind('Troubleshooting')).toEqual(['6.00 hours', '2.67 hours', '3.33 hours', '$108.33', '$1,300.00'])
    expect(within_().getByText('Reporting: Current time is higher than the baseline for this work.')).toBeInTheDocument()
    expect(value()).not.toHaveTextContent('Troubleshooting: Current time is higher')
    expect(metric('Recovered hours per month')).toBe('-2.67 hours')
    expect(metric('Monthly estimated value')).toBe('-$86.67')
    expect(metric('Annual estimated value')).toBe('-$1,040.00')
    expect(metric('Net estimated annual value')).toBe('-$5,840.00')
    expect(metric('Estimated ROI')).toBe('-121.7%')
    expect(metric('Payback period')).toBe('Not available')
    expect(note('Payback period')).toBe('The estimated monthly value is zero or less, so it does not cover the subscription cost.')
    // Not an error, not a warning, and no colour: the same cards as any other figure.
    expect(within_().queryByRole('alert')).not.toBeInTheDocument()
    expect(card('Net estimated annual value').className).toBe('metric metric-value')
    expect(value().querySelector('[style], .negative, .error-message')).toBeNull()
    expect(value()).not.toHaveTextContent(/loss|warning|error/i)
  })
})

// =====================================================================================================================
// When it cannot be shown
// =====================================================================================================================

describe('when the SortView Value report cannot be loaded', () => {
  it('says it is loading until the answer arrives', async () => {
    const slow = deferred<Response>()
    serve(valueStore(), { [PATHS.report]: () => slow.promise })

    renderApp(ORG_VALUE)

    await waitFor(() => expect(value()).toHaveTextContent('Loading…'))
    expect(value()).not.toHaveTextContent(/\$|Not available/)
    expect(within_().queryByRole('button')).not.toBeInTheDocument()
    slow.resolve(jsonResponse(200, valueBody.report(valueStore())))
    await waitFor(() => expect(metric('Estimated ROI')).toBe('49.0%'))
  })

  it('says so, with a way to try again that then moves focus to its heading', async () => {
    let calls = 0
    serve(valueStore(), { [PATHS.report]: () => (calls++ === 0 ? SERVER_ERROR() : jsonResponse(200, valueBody.report(valueStore()))) })
    await page()

    expect(within_().getByText('Could not load.')).toBeInTheDocument()
    expect(value()).not.toHaveTextContent(/\$|Internal server error|assumptions/)
    fireEvent.click(within_().getByRole('button', { name: 'Try again: SortView Value' }))

    await waitFor(() => expect(metric('Annual estimated value')).toBe('$7,150.00'))
    expect(within_().getByRole('heading', { level: 4, name: 'SortView Value' })).toHaveFocus()
    expect(valueRequests()).toEqual([`GET ${REPORT_URL}`, `GET ${REPORT_URL}`])
  })

  it('treats a malformed answer as a failure and shows none of it', async () => {
    const body = valueBody.report(valueStore())
    serve(valueStore(), { [PATHS.report]: () => jsonResponse(200, { ...body, results: { ...body.results, annual_value: 7150 } }) })

    await page()

    expect(within_().getByText('Could not load.')).toBeInTheDocument()
    expect(value()).not.toHaveTextContent(/7,150|595\.83/)
  })

  it('returns to the sign-in form when the session has ended', async () => {
    serve(valueStore(), { [PATHS.report]: () => jsonResponse(401, NOT_AUTHENTICATED) })

    renderApp(ORG_VALUE)

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('region')).not.toBeInTheDocument()
    expect(screen.getByTestId('address')).toHaveTextContent(ORG_VALUE)
  })

  it('says what to do when the stored assumptions cannot be read, and saving them again repairs it', async () => {
    const store = valueStore()
    let repaired = false
    serve(store, {
      [PATHS.report]: () => (repaired ? jsonResponse(200, valueBody.report(store)) : jsonResponse(500, VALUE_STORED_INVALID)),
      [`GET ${PATHS.settings}`]: () => (repaired ? jsonResponse(200, valueBody.settings(store)) : jsonResponse(500, VALUE_STORED_INVALID)),
      [`PUT ${PATHS.settings}`]: () => {
        repaired = true
        return jsonResponse(200, valueBody.settings(store))
      },
    })
    await page()

    expect(within_().getByRole('note')).toHaveTextContent(
      'The SortView Value assumptions stored for this organization could not be read, so no figures can be shown. Open the assumptions below and save them again to repair this.',
    )
    expect(within_().getByText(DISCLOSURE)).toBeInTheDocument()
    expect(value()).not.toHaveTextContent(/\$|hours|The stored SortView Value settings could not be read\./)
    // Asked once: the same answer would come back however often it was asked.
    expect(valueRequests()).toEqual([`GET ${REPORT_URL}`])

    await openForm()
    expect(within(form()).getByRole('note')).toHaveTextContent('What is stored here could not be read, so the fields start empty. Saving replaces it.')
    expect(field(LABOR).value).toBe('')
    type(LABOR, '32.5')
    save()

    await waitFor(() => expect(metric('Annual estimated value')).toBe('$7,150.00'))
    expect(field(LABOR).value).toBe('32.50')
    expect(field(TASKS).value).toBe('12.0')
  })
})

// =====================================================================================================================
// Entering the assumptions
// =====================================================================================================================

describe('the assumptions panel', () => {
  it('is closed until asked for, and nothing is read until it is opened', async () => {
    serve()
    await page()

    expect(toggle()).toHaveAttribute('aria-expanded', 'false')
    expect(toggle()).toHaveAttribute('aria-controls', 'sortview-value-assumptions')
    expect(document.getElementById('sortview-value-assumptions')).not.toBeVisible()
    expect(screen.queryByRole('form')).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(valueRequests()).toEqual([`GET ${REPORT_URL}`])

    await openForm()

    expect(toggle('Hide assumptions')).toHaveAttribute('aria-expanded', 'true')
    expect(valueRequests()).toEqual([`GET ${REPORT_URL}`, `GET ${SETTINGS_URL}`])
  })

  it('opens and closes from the keyboard', async () => {
    serve()
    await page()
    const person = userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })

    toggle().focus()
    await person.keyboard('{Enter}')
    expect(await screen.findByLabelText(LABOR)).toBeInTheDocument()
    expect(toggle('Hide assumptions')).toHaveFocus()

    await person.keyboard(' ')
    expect(screen.queryByRole('form')).not.toBeInTheDocument()
    expect(toggle()).toHaveFocus()
  })

  it('is one form of three groups, filled with what is stored, every field labelled and described', async () => {
    serve(valueStore({ efficiency_labor_rate: '20.00' }))
    await page()
    await openForm()

    expect(within(form()).getByRole('heading', { level: 5, name: 'Assumptions' })).toBeInTheDocument()
    expect(within(form()).getAllByRole('group').map((group) => group.querySelector('legend')?.textContent)).toEqual(['Labor rate and subscription', 'Reporting', 'Troubleshooting'])
    const controls = [...form().querySelectorAll<HTMLInputElement | HTMLSelectElement>('input, select')]
    expect(controls.map((control) => control.value)).toEqual(['32.50', '4800.00', '12.0', '90.0', '15.0', 'estimate', '2026-09-01', '4.0', '2.0', '45.0', '20.0', 'measured', '2026-09-15'])
    for (const control of controls) {
      expect(control.labels).toHaveLength(1)
      expect(document.getElementById(control.getAttribute('aria-describedby') as string)).not.toBeEmptyDOMElement()
      expect(control).toHaveAttribute('aria-invalid', 'false')
      expect(control).not.toBeDisabled()
      expect(control).not.toBeRequired()
    }
    expect(field(REPORTING_AS_OF).type).toBe('date')
    expect(field(LABOR).type).toBe('text')
    expect(within(field(REPORTING_BASIS)).getAllByRole('option').map((option) => option.textContent)).toEqual(['Not set', 'Estimate', 'Measured'])
    expect(field(LABOR)).toHaveAccessibleDescription(/Leave blank to use the organization Efficiency default\..*Organization Efficiency default: \$20\.00 per hour\./)
    // No rate, count or duration is suggested anywhere.
    expect(form().querySelector('[placeholder]')).toBeNull()
  })

  it('leaves the labor rate blank when the rate in effect is the Efficiency default', async () => {
    serve(valueStore({ labor_rate: null, efficiency_labor_rate: '20.00' }))
    await page()
    await openForm()

    expect(field(LABOR).value).toBe('')
    expect(form()).toHaveTextContent('Organization Efficiency default: $20.00 per hour.')
  })

  it('says there is no Efficiency default when there is none', async () => {
    serve()
    await page()
    await openForm()

    expect(form()).toHaveTextContent('Organization Efficiency default: none set.')
  })

  it('says so, with a way to try again, when the stored assumptions could not be loaded', async () => {
    let calls = 0
    serve(valueStore(), { [`GET ${PATHS.settings}`]: () => (calls++ === 0 ? SERVER_ERROR() : jsonResponse(200, valueBody.settings(valueStore()))) })
    await page()
    fireEvent.click(toggle())

    const retry = await within_().findByRole('button', { name: 'Try again' })
    expect(screen.queryByRole('form')).not.toBeInTheDocument()
    // The report is still there: only the form is not.
    expect(metric('Annual estimated value')).toBe('$7,150.00')
    fireEvent.click(retry)

    expect(await screen.findByLabelText(LABOR)).toHaveValue('32.50')
  })
})

describe('saving the assumptions', () => {
  it('sends the whole block as typed, shows what was stored, asks for the report again and moves focus to its heading', async () => {
    const store = valueStore()
    serve(store)
    await page()
    await openForm()

    type(TASKS, ' 24 ')
    type(LABOR, '')
    type(COST, '5000')
    fireEvent.change(field(REPORTING_BASIS), { target: { value: 'measured' } })
    type(REPORTING_AS_OF, '')
    // Nothing shown changes until it is saved.
    expect(metric('Recovered hours per month')).toBe('18.33 hours')
    store.efficiency_labor_rate = '20.00'
    save()

    await waitFor(() => expect(metric('Recovered hours per month')).toBe('33.33 hours'))
    expect(puts()).toEqual([`PUT ${SETTINGS_URL}`])
    expect(lastPut()).toStrictEqual({
      labor_rate: null,
      subscription_annual_cost: '5000',
      reporting: { tasks_per_month: '24', baseline_minutes_per_task: '90.0', current_minutes_per_task: '15.0', basis: 'measured', as_of: null },
      troubleshooting: { incidents_per_month: '4.0', staff_per_incident: '2.0', baseline_minutes: '45.0', current_minutes: '20.0', basis: 'measured', as_of: '2026-09-15' },
    })
    // What the API stored, as it keeps it.
    expect(field(TASKS).value).toBe('24.0')
    expect(field(COST).value).toBe('5000.00')
    expect(field(LABOR).value).toBe('')
    // The report again, once -- and every figure on the page is its answer.
    expect(valueRequests()).toEqual([`GET ${REPORT_URL}`, `GET ${SETTINGS_URL}`, `PUT ${SETTINGS_URL}`, `GET ${REPORT_URL}`])
    expect(metric('Monthly estimated value')).toBe('$666.67')
    expect(metric('Subscription annual cost')).toBe('$5,000.00')
    expect(assumption('Labor rate')).toBe('$20.00 per hour (organization Efficiency default)')
    expect(assumption('Reporting: basis of these figures')).toBe('Measured')
    expect(within(form()).getByRole('status')).toHaveTextContent('Saved. The SortView Value figures have been worked out again.')
    expect(within_().getByRole('heading', { level: 5, name: 'Estimated value' })).toHaveFocus()
    expect(within(form()).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('keeps an explicit zero as zero, and a blank as not set', async () => {
    serve()
    await page()
    await openForm()

    type(COST, '0')
    type(TASKS, '')
    save()

    await waitFor(() => expect(metric('Subscription annual cost')).toBe('$0.00'))
    expect(lastPut().subscription_annual_cost).toBe('0')
    expect((lastPut().reporting as Record<string, unknown>).tasks_per_month).toBeNull()
    expect(field(COST).value).toBe('0.00')
    expect(field(TASKS).value).toBe('')
    expect(within(within_().getByRole('note')).getAllByRole('listitem').map((item) => item.textContent)).toEqual(['Reporting tasks per month'])
  })

  it('saves from the keyboard', async () => {
    serve()
    await page()
    await openForm()
    const person = userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })

    await person.clear(field(TASKS))
    await person.type(field(TASKS), '24{Enter}')

    await waitFor(() => expect(metric('Recovered hours per month')).toBe('33.33 hours'))
    expect(puts()).toHaveLength(1)
  })

  it('checks every field before sending: says what is wrong beside each, lists them, keeps what was typed and focuses the first', async () => {
    serve()
    await page()
    await openForm()

    type(LABOR, '$32')
    type(TASKS, '12.55')
    type(STAFF, '0')
    save()

    const alert = await within(form()).findByRole('alert')
    expect(alert).toHaveTextContent('Nothing was saved. Check the fields marked below.')
    expect(within(alert).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      `${LABOR}: Enter digits only, with a decimal point if needed: no symbols, commas or spaces.`,
      `${TASKS}: Use at most 1 decimal place. The value is not rounded for you.`,
      `${STAFF}: Enter a number above 0, up to 1,000.`,
    ])
    for (const label of [LABOR, TASKS, STAFF]) {
      expect(field(label)).toHaveAttribute('aria-invalid', 'true')
    }
    expect(field(COST)).toHaveAttribute('aria-invalid', 'false')
    expect(field(STAFF)).toHaveAccessibleDescription(/Enter a number above 0, up to 1,000\./)
    expect(field(LABOR)).toHaveFocus()
    // What was typed stays as typed, nothing was sent, and no figure changed.
    expect([field(LABOR).value, field(TASKS).value, field(STAFF).value]).toEqual(['$32', '12.55', '0'])
    expect(puts()).toEqual([])
    expect(metric('Recovered hours per month')).toBe('18.33 hours')
    // The button is not disabled: saving again says the same, and moves to what is now first.
    expect(within(form()).getByRole('button', { name: 'Save assumptions' })).not.toHaveAttribute('aria-disabled', 'true')
    type(LABOR, '32')
    save()
    await waitFor(() => expect(field(TASKS)).toHaveFocus())
    expect(field(LABOR)).toHaveAttribute('aria-invalid', 'false')
    expect(puts()).toEqual([])
  })

  it('shows what the API says about each field, by its code, when the API refuses', async () => {
    serve(valueStore(), {
      [`PUT ${PATHS.settings}`]: () => jsonResponse(422, invalidValueSettings(['reporting.as_of', 'in_the_future'], ['troubleshooting.staff_per_incident', 'out_of_range'], ['results.total', 'unknown_field'])),
    })
    await page()
    await openForm()

    type(REPORTING_AS_OF, '2026-12-25')
    save()

    const alert = await within(form()).findByRole('alert')
    expect(within(alert).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      `${REPORTING_AS_OF}: The date cannot be after today.`,
      `${STAFF}: Enter a number above 0, up to 1,000.`,
    ])
    expect(field(REPORTING_AS_OF)).toHaveAttribute('aria-invalid', 'true')
    expect(field(REPORTING_AS_OF)).toHaveFocus()
    expect(field(REPORTING_AS_OF).value).toBe('2026-12-25')
    expect(form()).not.toHaveTextContent(/The SortView Value settings are not valid|unknown_field|results\.total/)
    // Nothing was stored, so the report was not asked for again.
    expect(valueRequests().filter((request) => request === `GET ${REPORT_URL}`)).toHaveLength(1)
    expect(within(form()).queryByRole('status')).not.toBeInTheDocument()
    expect(within_().getByRole('heading', { level: 5, name: 'Estimated value' })).not.toHaveFocus()
  })

  it.each<[string, () => Response]>([
    ['the server fails', SERVER_ERROR],
    ['the organization has been suspended meanwhile', () => jsonResponse(403, VALUE_READ_ONLY)],
  ])('says nothing was saved, and focuses that sentence, when %s', async (_label, answer) => {
    serve(valueStore(), { [`PUT ${PATHS.settings}`]: answer })
    await page()
    await openForm()

    type(TASKS, '24')
    save()

    const alert = await within(form()).findByRole('alert')
    expect(alert).toHaveTextContent(/^Nothing was saved\. \S/)
    expect(within(alert).queryByRole('listitem')).not.toBeInTheDocument()
    expect(alert).toHaveFocus()
    expect(field(TASKS).value).toBe('24')
    expect(form().querySelector('[aria-invalid="true"]')).toBeNull()
    expect(metric('Recovered hours per month')).toBe('18.33 hours')
  })

  it('returns to the sign-in form when the session has ended', async () => {
    serve(valueStore(), { [`PUT ${PATHS.settings}`]: () => jsonResponse(401, NOT_AUTHENTICATED) })
    await page()
    await openForm()

    save()

    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByRole('form', { name: 'Assumptions' })).not.toBeInTheDocument()
  })

  it('does not send a second save while one is on its way', async () => {
    const slow = deferred<Response>()
    serve(valueStore(), { [`PUT ${PATHS.settings}`]: () => slow.promise })
    await page()
    await openForm()

    save()
    const button = await within(form()).findByRole('button', { name: 'Saving…' })
    expect(button).toHaveAttribute('aria-disabled', 'true')
    expect(button).not.toBeDisabled()
    fireEvent.click(button)

    expect(puts()).toHaveLength(1)
    slow.resolve(jsonResponse(200, valueBody.settings(valueStore())))
    await within(form()).findByRole('status')
  })
})

describe('a suspended organization', () => {
  it('can read the report and what is stored, and is offered nothing to change it with', async () => {
    serve(valueStore(), {}, { access_mode: 'read_only' })
    await page()

    expect(metric('Annual estimated value')).toBe('$7,150.00')
    expect(within_().queryByRole('button', { name: 'Edit assumptions' })).not.toBeInTheDocument()
    fireEvent.click(toggle('View assumptions'))

    expect(await within_().findByText('This organization is suspended, so its SortView Value assumptions cannot be changed.')).toHaveRole('note')
    const facts = (document.getElementById('sortview-value-assumptions') as HTMLElement).querySelector('dl') as HTMLElement
    expect([...facts.querySelectorAll('div')].map((fact) => `${fact.querySelector('dt')?.textContent}: ${fact.querySelector('dd')?.textContent}`)).toEqual([
      'SortView Value labor rate: $32.50 per hour',
      'Annual subscription cost: $4,800.00 per year',
      'Reporting tasks per month: 12.0',
      'Baseline minutes per reporting task: 90.0',
      'Current minutes per reporting task: 15.0',
      'Reporting figures: Estimate, as of Sep 1, 2026',
      'Troubleshooting incidents per month: 4.0',
      'Staff per troubleshooting incident: 2.0',
      'Baseline minutes per person per incident: 45.0',
      'Current minutes per person per incident: 20.0',
      'Troubleshooting figures: Measured, as of Sep 15, 2026',
    ])
    expect(screen.queryByRole('form')).not.toBeInTheDocument()
    expect(value().querySelector('input, select, button[type="submit"]')).toBeNull()
    expect(valueRequests()).toEqual([`GET ${REPORT_URL}`, `GET ${SETTINGS_URL}`])
  })

  it('says "Not set" for what is not, never 0', async () => {
    serve(emptyValueStore(), {}, { access_mode: 'read_only' })
    await page()

    fireEvent.click(toggle('View assumptions'))
    await within_().findByText('This organization is suspended, so its SortView Value assumptions cannot be changed.')

    const facts = (document.getElementById('sortview-value-assumptions') as HTMLElement).querySelectorAll('dl dd')
    expect([...facts].map((fact) => fact.textContent)).toEqual(Array(11).fill('Not set'))
  })
})

// =====================================================================================================================
// Around it
// =====================================================================================================================

describe('the Value view among the reports', () => {
  it('offers Print report where the plan includes exports, and never a CSV', async () => {
    serve(valueStore(), {}, { exports: true })
    await page()

    fireEvent.click(screen.getByRole('button', { name: 'Print report' }))

    expect(print).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: /CSV/ })).not.toBeInTheDocument()
    expect(valueRequests()).toEqual([`GET ${REPORT_URL}`])
  })

  it('offers no Print report where it does not', async () => {
    serve()
    await page()

    expect(screen.queryByRole('button', { name: 'Print report' })).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/Print|CSV/)
  })

  it('keeps a range and a comparison in the address without using either, before and after a save', async () => {
    const asked = '?from=2026-09-29&to=2026-10-05&compare=previous'
    serve()
    await page(`${ORG_VALUE}${asked}`)

    expect(screen.getByTestId('query')).toHaveTextContent(asked)
    expect(metric('Annual estimated value')).toBe('$7,150.00')
    await openForm()
    type(TASKS, '24')
    save()
    await waitFor(() => expect(metric('Recovered hours per month')).toBe('33.33 hours'))

    expect(screen.getByTestId('query')).toHaveTextContent(asked)
    expect(valueRequests().every((request) => !request.includes('?'))).toBe(true)
    // And it goes with the reader to the reports that do use it.
    const views = within(screen.getByRole('navigation', { name: 'Report views' }))
    expect(views.getByRole('link', { name: 'Operations' })).toHaveAttribute('href', `/organizations/northbridge/reports/operations${asked}`)
  })

  it('leaves the organization’s Operations as they are, a view away', async () => {
    serve()
    await page()

    fireEvent.click(within(screen.getByRole('navigation', { name: 'Report views' })).getByRole('link', { name: 'Operations' }))

    expect(await screen.findByRole('region', { name: 'System reliability' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'SortView Value' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Last 30 days' })).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/Estimated ROI|Payback period|Edit assumptions/)
    expect(valueRequests()).toEqual([`GET ${REPORT_URL}`])
  })

  it('does not appear on a sorter’s Efficiency view, which asks nothing of it', async () => {
    serve()

    renderApp('/organizations/northbridge/sorters/central/reports/efficiency')

    expect(await screen.findByRole('region', { name: 'Efficiency' })).toBeInTheDocument()
    await waitFor(() => expect(main()).not.toHaveTextContent('Loading…'))
    expect(main()).not.toHaveTextContent(/SortView Value|Estimated ROI|Payback period/)
    expect(valueRequests()).toEqual([])
  })
})

/** What the one save sent. */
function lastPut(): Record<string, unknown> {
  const call = fetchMock.mock.calls.filter(([, init]) => init?.method === 'PUT').at(-1)
  return JSON.parse(String(call?.[1]?.body)) as Record<string, unknown>
}
