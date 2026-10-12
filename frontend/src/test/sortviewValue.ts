import type { ApiRoutes, FetchMock } from './http.ts'
import { jsonResponse } from './http.ts'

/**
 * Test helpers for SortView Value: what the API stores for an organization,
 * and what it answers from that -- the settings read, the replacement and the
 * report -- shaped exactly as the real API shapes them. A test changes what
 * is "stored" and every answer follows.
 *
 * The arithmetic is the API's formulas, done here in plain numbers: good
 * enough for figures a test then reads off the page, and no part of the app.
 */

type Text = string | null

export interface ValueStore {
  labor_rate: Text
  subscription_annual_cost: Text
  reporting: { tasks_per_month: Text; baseline_minutes_per_task: Text; current_minutes_per_task: Text; basis: Text; as_of: Text }
  troubleshooting: { incidents_per_month: Text; staff_per_incident: Text; baseline_minutes: Text; current_minutes: Text; basis: Text; as_of: Text }
  /** The organization's Efficiency default labor rate: what the labor rate falls back on. Not part of the block. */
  efficiency_labor_rate: Text
}

type Changes = Partial<Omit<ValueStore, 'reporting' | 'troubleshooting'>> & {
  reporting?: Partial<ValueStore['reporting']>
  troubleshooting?: Partial<ValueStore['troubleshooting']>
}

/**
 * Everything set. Reporting: 12 tasks a month, 90 minutes each before and 15 now -- 18.00 and 3.00 hours, 15.00
 * recovered. Troubleshooting: 4 incidents of 2 staff, 45 minutes each before and 20 now -- 6.00 and 2.67 hours, 3.33
 * recovered. At 32.50 an hour: 487.50 and 108.33 a month; 595.83 together, 7,150.00 a year, against 4,800.00:
 * 2,350.00 net, a return of 49.0%, and 8.1 months of value to cover a year's cost.
 */
export function valueStore(changes: Changes = {}): ValueStore {
  return {
    labor_rate: '32.50',
    subscription_annual_cost: '4800.00',
    efficiency_labor_rate: null,
    ...changes,
    reporting: {
      tasks_per_month: '12.0',
      baseline_minutes_per_task: '90.0',
      current_minutes_per_task: '15.0',
      basis: 'estimate',
      as_of: '2026-09-01',
      ...changes.reporting,
    },
    troubleshooting: {
      incidents_per_month: '4.0',
      staff_per_incident: '2.0',
      baseline_minutes: '45.0',
      current_minutes: '20.0',
      basis: 'measured',
      as_of: '2026-09-15',
      ...changes.troubleshooting,
    },
  }
}

/** Nothing set anywhere. */
export const emptyValueStore = (): ValueStore =>
  valueStore({
    labor_rate: null,
    subscription_annual_cost: null,
    reporting: { tasks_per_month: null, baseline_minutes_per_task: null, current_minutes_per_task: null, basis: null, as_of: null },
    troubleshooting: { incidents_per_month: null, staff_per_incident: null, baseline_minutes: null, current_minutes: null, basis: null, as_of: null },
  })

/** A decimal the way the API stores it: with exactly `places` decimal places. */
function canonical(text: Text, places: number): Text {
  if (text === null) {
    return null
  }
  const [whole, fraction = ''] = text.split('.')
  return `${whole}.${fraction.padEnd(places, '0')}`
}

/** A figure the way the API writes it: rounded half up to `places`, and a zero never with a minus sign. */
function written(value: number | null, places: number): Text {
  if (value === null) {
    return null
  }
  const scale = 10 ** places
  const rounded = (Math.sign(value) * Math.round(Math.abs(value) * scale + 1e-7)) / scale
  return (rounded === 0 ? 0 : rounded).toFixed(places)
}

const number = (text: Text) => (text === null ? null : Number(text))
const product = (...factors: Array<number | null>) => (factors.some((factor) => factor === null) ? null : (factors as number[]).reduce((result, factor) => result * factor, 1))
const sum = (first: number | null, second: number | null) => (first === null || second === null ? null : first + second)

function settings(store: ValueStore) {
  const source = store.labor_rate !== null ? 'sortview_value' : store.efficiency_labor_rate !== null ? 'organization_efficiency' : null
  return {
    labor_rate: {
      sortview_value: store.labor_rate,
      organization_efficiency: store.efficiency_labor_rate,
      effective: store.labor_rate ?? store.efficiency_labor_rate,
      source,
    },
    subscription_annual_cost: store.subscription_annual_cost,
    reporting: { ...store.reporting },
    troubleshooting: { ...store.troubleshooting },
  }
}

function kindOfWork(volume: number | null, baselineMinutes: Text, currentMinutes: Text, rate: number | null) {
  const hours = (minutes: Text) => {
    const total = product(volume, number(minutes))
    return total === null ? null : total / 60
  }
  const baseline = hours(baselineMinutes)
  const current = hours(currentMinutes)
  const recovered = baseline === null || current === null ? null : baseline - current
  return { baseline, current, recovered, monthly: product(recovered, rate) }
}

export const valueBody = {
  settings: (store: ValueStore) => ({ sortview_value: settings(store) }),
  report: (store: ValueStore) => {
    const assumptions = settings(store)
    const rate = number(assumptions.labor_rate.effective)
    const cost = number(store.subscription_annual_cost)
    const reporting = kindOfWork(number(store.reporting.tasks_per_month), store.reporting.baseline_minutes_per_task, store.reporting.current_minutes_per_task, rate)
    const troubleshooting = kindOfWork(
      product(number(store.troubleshooting.incidents_per_month), number(store.troubleshooting.staff_per_incident)),
      store.troubleshooting.baseline_minutes,
      store.troubleshooting.current_minutes,
      rate,
    )
    const hours = sum(reporting.recovered, troubleshooting.recovered)
    const monthly = sum(reporting.monthly, troubleshooting.monthly)
    const annual = product(monthly, 12)
    const net = annual === null || cost === null ? null : annual - cost
    const results = (kind: typeof reporting) => ({
      baseline_hours_per_month: written(kind.baseline, 2),
      current_hours_per_month: written(kind.current, 2),
      recovered_hours_per_month: written(kind.recovered, 2),
      monthly_value: written(kind.monthly, 2),
      annual_value: written(product(kind.monthly, 12), 2),
    })
    const inputs: Array<[string, Text]> = [
      ['labor_rate', assumptions.labor_rate.effective],
      ['subscription_annual_cost', store.subscription_annual_cost],
      ['reporting.tasks_per_month', store.reporting.tasks_per_month],
      ['reporting.baseline_minutes_per_task', store.reporting.baseline_minutes_per_task],
      ['reporting.current_minutes_per_task', store.reporting.current_minutes_per_task],
      ['troubleshooting.incidents_per_month', store.troubleshooting.incidents_per_month],
      ['troubleshooting.staff_per_incident', store.troubleshooting.staff_per_incident],
      ['troubleshooting.baseline_minutes', store.troubleshooting.baseline_minutes],
      ['troubleshooting.current_minutes', store.troubleshooting.current_minutes],
    ]
    return {
      currency: 'USD',
      assumptions,
      results: {
        reporting: results(reporting),
        troubleshooting: results(troubleshooting),
        recovered_hours_per_month: written(hours, 2),
        monthly_value: written(monthly, 2),
        annual_value: written(annual, 2),
        net_annual_value: written(net, 2),
        roi_percent: net === null || cost === null || cost === 0 ? null : written((net / cost) * 100, 1),
        payback_months: monthly === null || cost === null || monthly <= 0 ? null : written(cost / monthly, 1),
      },
      missing: inputs.filter(([, value]) => value === null).map(([name]) => name),
    }
  },
}

export function valuePaths(orgSlug: string) {
  return {
    report: `GET /api/organizations/${orgSlug}/reports/sortview-value`,
    settings: `/api/organizations/${orgSlug}/settings/sortview-value`,
  }
}

type Block = Record<string, Text | Record<string, Text>>

/** The body of the request being answered: the last one the fetch mock recorded. */
export function lastValueBody(fetchMock: FetchMock): Block {
  const init = fetchMock.mock.calls.at(-1)?.[1]
  return JSON.parse(String(init?.body)) as Block
}

/**
 * Routes for one organization's SortView Value: the report and the settings
 * pair, all answering from `store`. A PUT replaces what is stored -- written
 * the way the API writes it -- and answers with it. `fetchMock` is read
 * lazily, so the routes can be built before it exists.
 */
export function valueRoutes(orgSlug: string, store: ValueStore, fetchMock: () => FetchMock): ApiRoutes {
  const paths = valuePaths(orgSlug)
  return {
    [paths.report]: () => jsonResponse(200, valueBody.report(store)),
    [`GET ${paths.settings}`]: () => jsonResponse(200, valueBody.settings(store)),
    [`PUT ${paths.settings}`]: () => {
      const body = lastValueBody(fetchMock())
      const reporting = body.reporting as Record<string, Text>
      const troubleshooting = body.troubleshooting as Record<string, Text>
      store.labor_rate = canonical(body.labor_rate as Text, 2)
      store.subscription_annual_cost = canonical(body.subscription_annual_cost as Text, 2)
      store.reporting = {
        tasks_per_month: canonical(reporting.tasks_per_month, 1),
        baseline_minutes_per_task: canonical(reporting.baseline_minutes_per_task, 1),
        current_minutes_per_task: canonical(reporting.current_minutes_per_task, 1),
        basis: reporting.basis,
        as_of: reporting.as_of,
      }
      store.troubleshooting = {
        incidents_per_month: canonical(troubleshooting.incidents_per_month, 1),
        staff_per_incident: canonical(troubleshooting.staff_per_incident, 1),
        baseline_minutes: canonical(troubleshooting.baseline_minutes, 1),
        current_minutes: canonical(troubleshooting.current_minutes, 1),
        basis: troubleshooting.basis,
        as_of: troubleshooting.as_of,
      }
      return jsonResponse(200, valueBody.settings(store))
    },
  }
}

export const VALUE_FORBIDDEN = { code: 'forbidden', message: 'You do not have permission to manage these settings.' }
export const VALUE_READ_ONLY = { code: 'organization_read_only', message: "This organization's settings cannot be changed." }
export const VALUE_STORED_INVALID = { code: 'sortview_value_settings_invalid', message: 'The stored SortView Value settings could not be read.' }
export const invalidValueSettings = (...problems: Array<[field: string, code: string]>) => ({
  code: 'invalid_sortview_value_settings',
  message: 'The SortView Value settings are not valid.',
  problems: problems.map(([field, code]) => ({ field, code })),
})
