import { type ApiRoutes, datesBetween, type FetchMock, jsonResponse, livePath, REPORT } from './http.ts'

/**
 * Test helpers for Efficiency: what the API stores for an organization and
 * one of its sorters, and what it answers from that -- the two settings
 * reads, the two replacements and the report -- shaped exactly as the real
 * API shapes them. A test changes what is "stored" and every answer follows.
 *
 * The arithmetic is the API's formulas, done here in plain numbers: good
 * enough for figures a test then reads off the page, and no part of the app.
 */

type Text = string | null

export interface EfficiencyStore {
  organization: { labor_rate: Text; manual_items_per_hour: Text }
  sorter: { labor_rate: Text; manual_items_per_hour: Text; one_time_cost: Text; recurring_annual_cost: Text; in_service_date: Text }
  /** Check-ins on a date. The sorter's other reports use the same rhythm (REPORT). */
  checkins: (date: string) => number
}

/** An organization with both defaults, and a sorter with its costs and a long-past in-service date. */
export function efficiencyStore(changes: { organization?: Partial<EfficiencyStore['organization']>; sorter?: Partial<EfficiencyStore['sorter']> } = {}): EfficiencyStore {
  return {
    organization: { labor_rate: '18.00', manual_items_per_hour: '40.0', ...changes.organization },
    sorter: {
      labor_rate: null,
      manual_items_per_hour: null,
      one_time_cost: '125000.00',
      recurring_annual_cost: '7300.00',
      in_service_date: '2020-11-20',
      ...changes.sorter,
    },
    checkins: REPORT.checkins,
  }
}

/** Nothing configured anywhere. */
export const emptyStore = (): EfficiencyStore =>
  efficiencyStore({
    organization: { labor_rate: null, manual_items_per_hour: null },
    sorter: { one_time_cost: null, recurring_annual_cost: null, in_service_date: null },
  })

/** A decimal the way the API stores it: with exactly `places` decimal places. */
function canonical(text: Text, places: number): Text {
  if (text === null) {
    return null
  }
  const [whole, fraction = ''] = text.split('.')
  return `${whole}.${fraction.padEnd(places, '0')}`
}

function rate(organization: Text, sorter: Text) {
  const source = sorter !== null ? 'sorter' : organization !== null ? 'organization' : null
  return { organization, sorter, effective: sorter ?? organization, source }
}

export const efficiencyBody = {
  organization: (store: EfficiencyStore) => ({ efficiency: { ...store.organization } }),
  sorter: (store: EfficiencyStore) => ({
    efficiency: {
      labor_rate: rate(store.organization.labor_rate, store.sorter.labor_rate),
      manual_items_per_hour: rate(store.organization.manual_items_per_hour, store.sorter.manual_items_per_hour),
      one_time_cost: store.sorter.one_time_cost,
      recurring_annual_cost: store.sorter.recurring_annual_cost,
      in_service_date: store.sorter.in_service_date,
    },
  }),
  report: (store: EfficiencyStore, from: string, to: string) => {
    const dates = datesBetween(from, to)
    const inService = dates.filter((date) => store.sorter.in_service_date === null || date >= store.sorter.in_service_date)
    const counted = inService.reduce((sum, date) => sum + store.checkins(date), 0)
    const manual = store.sorter.manual_items_per_hour ?? store.organization.manual_items_per_hour
    const labor = store.sorter.labor_rate ?? store.organization.labor_rate
    const annual = store.sorter.recurring_annual_cost

    const hours = manual === null ? null : counted / Number(manual)
    const value = hours === null || labor === null ? null : hours * Number(labor)
    const cost = annual === null ? null : (Number(annual) * inService.length) / 365
    const sourced = (organization: Text, sorter: Text) =>
      sorter !== null ? { value: sorter, source: 'sorter' } : organization !== null ? { value: organization, source: 'organization' } : null
    const assumptions = {
      manual_items_per_hour: sourced(store.organization.manual_items_per_hour, store.sorter.manual_items_per_hour),
      labor_rate: sourced(store.organization.labor_rate, store.sorter.labor_rate),
      recurring_annual_cost: annual,
      one_time_cost: store.sorter.one_time_cost,
      in_service_date: store.sorter.in_service_date,
    }
    return {
      range: { from, to, days: dates.length, timezone: REPORT.timezone, includes_today: to >= REPORT.today },
      currency: 'USD',
      checkin_count: dates.reduce((sum, date) => sum + store.checkins(date), 0),
      assumptions,
      results: {
        in_service_days: inService.length,
        in_service_checkin_count: counted,
        staff_time_equivalent_hours: hours === null ? null : hours.toFixed(2),
        labor_value_equivalent: value === null ? null : value.toFixed(2),
        recurring_cost: cost === null ? null : cost.toFixed(2),
        net_operational_value: value === null || cost === null ? null : (Number(value.toFixed(2)) - Number(cost.toFixed(2))).toFixed(2),
        recurring_cost_per_item: cost === null || counted === 0 ? null : (cost / counted).toFixed(4),
      },
      missing: (['manual_items_per_hour', 'labor_rate', 'recurring_annual_cost', 'in_service_date'] as const).filter((name) => assumptions[name] === null),
    }
  },
}

export function efficiencyPaths(orgSlug: string, branchSlug: string) {
  return {
    report: `GET ${livePath(orgSlug, branchSlug)}/reports/efficiency?from=*&to=*`,
    organization: `/api/organizations/${orgSlug}/settings/efficiency`,
    sorter: `${livePath(orgSlug, branchSlug)}/settings/efficiency`,
  }
}

const rangeIn = (url: string): [string, string] => {
  const params = new URL(url, 'http://test.invalid').searchParams
  return [params.get('from') ?? '', params.get('to') ?? '']
}

/** The body of the request being answered: the last one the fetch mock recorded. */
export function lastBody(fetchMock: FetchMock): Record<string, Text> {
  const init = fetchMock.mock.calls.at(-1)?.[1]
  return JSON.parse(String(init?.body)) as Record<string, Text>
}

/**
 * Routes for one sorter's Efficiency: the report and the two pairs of
 * settings routes, all answering from `store`. A PUT replaces what is stored
 * for its level -- written the way the API writes it -- and answers with it.
 * `fetchMock` is read lazily, so the routes can be built before it exists.
 */
export function efficiencyRoutes(orgSlug: string, branchSlug: string, store: EfficiencyStore, fetchMock: () => FetchMock): ApiRoutes {
  const paths = efficiencyPaths(orgSlug, branchSlug)
  return {
    [paths.report]: (url) => jsonResponse(200, efficiencyBody.report(store, ...rangeIn(url))),
    [`GET ${paths.organization}`]: () => jsonResponse(200, efficiencyBody.organization(store)),
    [`GET ${paths.sorter}`]: () => jsonResponse(200, efficiencyBody.sorter(store)),
    [`PUT ${paths.organization}`]: () => {
      const body = lastBody(fetchMock())
      store.organization = { labor_rate: canonical(body.labor_rate, 2), manual_items_per_hour: canonical(body.manual_items_per_hour, 1) }
      return jsonResponse(200, efficiencyBody.organization(store))
    },
    [`PUT ${paths.sorter}`]: () => {
      const body = lastBody(fetchMock())
      store.sorter = {
        labor_rate: canonical(body.labor_rate, 2),
        manual_items_per_hour: canonical(body.manual_items_per_hour, 1),
        one_time_cost: canonical(body.one_time_cost, 2),
        recurring_annual_cost: canonical(body.recurring_annual_cost, 2),
        in_service_date: body.in_service_date,
      }
      return jsonResponse(200, efficiencyBody.sorter(store))
    },
  }
}

export const FORBIDDEN = { code: 'forbidden', message: 'You do not have permission to manage these settings.' }
export const STORED_INVALID = { code: 'efficiency_settings_invalid', message: 'The stored efficiency settings could not be read.' }
export const invalidSettings = (...problems: Array<[field: string, code: string]>) => ({
  code: 'invalid_efficiency_settings',
  message: 'The efficiency settings are not valid.',
  problems: problems.map(([field, code]) => ({ field, code })),
})
