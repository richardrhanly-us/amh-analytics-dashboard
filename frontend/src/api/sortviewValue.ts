import { ApiError, apiRequest, isApiError, unexpectedResponse } from './client.ts'
import { CALENDAR_DATE, record, segment } from './liveToday.ts'
import { dayNumber } from './reports.ts'

/**
 * SortView Value: what an organization has ENTERED about its reporting and
 * troubleshooting work -- before SortView and now -- what SortView costs it,
 * and the estimate the API works out from that. For an organization's owners
 * and admins only: the API answers 403 to anyone else.
 *
 * EVERY FIGURE IS TEXT. A rate, a count, a number of minutes or hours, an
 * amount of money, a percentage and a number of months each arrive as a
 * decimal written out in full ("32.50", "12.0", "-121.7") and are kept as
 * that text: nothing here turns one into a JavaScript number, and a JSON
 * number where text belongs is refused, since it has already been through a
 * binary float. `null` is "not set" or "cannot be worked out" -- never the
 * same as zero. A result may be negative.
 *
 * NOTHING IS CALCULATED IN THE BROWSER. The report's results are the API's.
 *
 * There is no date range: nothing here is counted from a sorter's events.
 */

export type Basis = 'estimate' | 'measured'
export type LaborRateSource = 'sortview_value' | 'organization_efficiency'

/** The labor rate, three ways: this block's own, the organization's Efficiency default, and which of them applies. */
export interface ValueLaborRate {
  sortview_value: string | null
  organization_efficiency: string | null
  effective: string | null
  source: LaborRateSource | null
}

export interface ReportingAssumptions {
  tasks_per_month: string | null
  baseline_minutes_per_task: string | null
  current_minutes_per_task: string | null
  basis: Basis | null
  as_of: string | null
}

export interface TroubleshootingAssumptions {
  incidents_per_month: string | null
  staff_per_incident: string | null
  baseline_minutes: string | null
  current_minutes: string | null
  basis: Basis | null
  as_of: string | null
}

/** What an organization has entered, exactly as stored, and the labor rate that applies. */
export interface SortViewValueSettings {
  labor_rate: ValueLaborRate
  subscription_annual_cost: string | null
  reporting: ReportingAssumptions
  troubleshooting: TroubleshootingAssumptions
}

/** One kind of work in a month, before and now, and what the difference is worth. Any of them may be negative but the first two. */
export interface CategoryResults {
  baseline_hours_per_month: string | null
  current_hours_per_month: string | null
  recovered_hours_per_month: string | null
  monthly_value: string | null
  annual_value: string | null
}

export interface SortViewValueResults {
  reporting: CategoryResults
  troubleshooting: CategoryResults
  /** The six below are the organization's, and are null unless BOTH kinds of work have theirs. */
  recovered_hours_per_month: string | null
  monthly_value: string | null
  annual_value: string | null
  net_annual_value: string | null
  roi_percent: string | null
  /** Months of `monthly_value` it takes to cover a year of the subscription. */
  payback_months: string | null
}

/** Every input the estimate uses, in the order the API lists the ones that are missing. */
export const VALUE_INPUTS = [
  'labor_rate',
  'subscription_annual_cost',
  'reporting.tasks_per_month',
  'reporting.baseline_minutes_per_task',
  'reporting.current_minutes_per_task',
  'troubleshooting.incidents_per_month',
  'troubleshooting.staff_per_incident',
  'troubleshooting.baseline_minutes',
  'troubleshooting.current_minutes',
] as const
export type ValueInput = (typeof VALUE_INPUTS)[number]

export interface SortViewValueReport {
  currency: 'USD'
  assumptions: SortViewValueSettings
  results: SortViewValueResults
  missing: ValueInput[]
}

/** What a PUT sends: the whole block. Null is "not set"; a kind of work is always there, with nulls. */
export interface SortViewValueInput {
  labor_rate: string | null
  subscription_annual_cost: string | null
  reporting: Record<keyof ReportingAssumptions, string | null>
  troubleshooting: Record<keyof TroubleshootingAssumptions, string | null>
}

/** One field the API refused, and the stable code for why. Never the value that was sent. */
export interface ValueSettingProblem {
  field: string
  code: string
}

export const SORTVIEW_VALUE_SETTINGS_INVALID = 'sortview_value_settings_invalid'
export const INVALID_SORTVIEW_VALUE_SETTINGS = 'invalid_sortview_value_settings'

// --- checking what arrives -------------------------------------------------------------------------------------------

/** A decimal written out with exactly `places` decimal places, and a minus sign only where one is allowed. */
function decimal(value: unknown, places: number, signed = false): string {
  const pattern = new RegExp(`^${signed ? '-?' : ''}(0|[1-9][0-9]*)\\.[0-9]{${places}}$`)
  if (typeof value !== 'string' || !pattern.test(value)) {
    throw unexpectedResponse(200)
  }
  return value
}

function optional<T>(value: unknown, parse: (value: unknown) => T): T | null {
  return value === null ? null : parse(value)
}

const money = (value: unknown) => optional(value, (inner) => decimal(inner, 2))
const signedMoney = (value: unknown) => optional(value, (inner) => decimal(inner, 2, true))
const onePlace = (value: unknown) => optional(value, (inner) => decimal(inner, 1))
const hours = (value: unknown) => optional(value, (inner) => decimal(inner, 2))
const signedHours = (value: unknown) => optional(value, (inner) => decimal(inner, 2, true))

function basis(value: unknown): Basis | null {
  if (value !== null && value !== 'estimate' && value !== 'measured') {
    throw unexpectedResponse(200)
  }
  return value
}

function calendarDate(value: unknown): string | null {
  if (value === null) {
    return null
  }
  if (typeof value !== 'string' || !CALENDAR_DATE.test(value) || dayNumber(value) === null) {
    throw unexpectedResponse(200)
  }
  return value
}

function laborRate(value: unknown): ValueLaborRate {
  const rate = record(value)
  const parsed: ValueLaborRate = {
    sortview_value: money(rate.sortview_value),
    organization_efficiency: money(rate.organization_efficiency),
    effective: money(rate.effective),
    source: rate.source === null ? null : (rate.source as LaborRateSource),
  }
  // The rate in effect is this block's own if it has one, else the Efficiency default, else none -- and says which.
  const expected: LaborRateSource | null =
    parsed.sortview_value !== null ? 'sortview_value' : parsed.organization_efficiency !== null ? 'organization_efficiency' : null
  if (parsed.source !== expected || parsed.effective !== (expected === null ? null : parsed[expected])) {
    throw unexpectedResponse(200)
  }
  return parsed
}

function parseSettings(value: unknown): SortViewValueSettings {
  const settings = record(value)
  const reporting = record(settings.reporting)
  const troubleshooting = record(settings.troubleshooting)
  return {
    labor_rate: laborRate(settings.labor_rate),
    subscription_annual_cost: money(settings.subscription_annual_cost),
    reporting: {
      tasks_per_month: onePlace(reporting.tasks_per_month),
      baseline_minutes_per_task: onePlace(reporting.baseline_minutes_per_task),
      current_minutes_per_task: onePlace(reporting.current_minutes_per_task),
      basis: basis(reporting.basis),
      as_of: calendarDate(reporting.as_of),
    },
    troubleshooting: {
      incidents_per_month: onePlace(troubleshooting.incidents_per_month),
      staff_per_incident: onePlace(troubleshooting.staff_per_incident),
      baseline_minutes: onePlace(troubleshooting.baseline_minutes),
      current_minutes: onePlace(troubleshooting.current_minutes),
      basis: basis(troubleshooting.basis),
      as_of: calendarDate(troubleshooting.as_of),
    },
  }
}

function category(value: unknown): CategoryResults {
  const results = record(value)
  return {
    baseline_hours_per_month: hours(results.baseline_hours_per_month),
    current_hours_per_month: hours(results.current_hours_per_month),
    recovered_hours_per_month: signedHours(results.recovered_hours_per_month),
    monthly_value: signedMoney(results.monthly_value),
    annual_value: signedMoney(results.annual_value),
  }
}

/** The value of one input the estimate uses, by its dotted name. The labor rate is the one in effect. */
function input(settings: SortViewValueSettings, name: ValueInput): string | null {
  if (name === 'labor_rate') {
    return settings.labor_rate.effective
  }
  if (name === 'subscription_annual_cost') {
    return settings.subscription_annual_cost
  }
  const [kind, field] = name.split('.') as ['reporting' | 'troubleshooting', string]
  return (settings[kind] as unknown as Record<string, string | null>)[field]
}

/**
 * Whether a report's figures are there exactly when what they are made from is: nothing is worked out here, only
 * which figures exist. A report that says otherwise is not one this app shows.
 */
function consistent(report: SortViewValueReport): boolean {
  const { assumptions, results, missing } = report
  const unset = VALUE_INPUTS.filter((name) => input(assumptions, name) === null)
  if (missing.length !== unset.length || missing.some((name, index) => name !== unset[index])) {
    return false
  }
  const known = (name: ValueInput) => input(assumptions, name) !== null
  const rate = known('labor_rate')
  const cost = assumptions.subscription_annual_cost

  const kinds: Array<[CategoryResults, boolean, boolean]> = [
    [
      results.reporting,
      known('reporting.tasks_per_month') && known('reporting.baseline_minutes_per_task'),
      known('reporting.tasks_per_month') && known('reporting.current_minutes_per_task'),
    ],
    [
      results.troubleshooting,
      known('troubleshooting.incidents_per_month') && known('troubleshooting.staff_per_incident') && known('troubleshooting.baseline_minutes'),
      known('troubleshooting.incidents_per_month') && known('troubleshooting.staff_per_incident') && known('troubleshooting.current_minutes'),
    ],
  ]
  for (const [kind, baseline, current] of kinds) {
    if (
      (kind.baseline_hours_per_month !== null) !== baseline ||
      (kind.current_hours_per_month !== null) !== current ||
      (kind.recovered_hours_per_month !== null) !== (baseline && current) ||
      (kind.monthly_value !== null) !== (baseline && current && rate) ||
      (kind.annual_value !== null) !== (kind.monthly_value !== null)
    ) {
      return false
    }
  }

  const bothHours = results.reporting.recovered_hours_per_month !== null && results.troubleshooting.recovered_hours_per_month !== null
  const bothValues = results.reporting.monthly_value !== null && results.troubleshooting.monthly_value !== null
  return (
    (results.recovered_hours_per_month !== null) === bothHours &&
    (results.monthly_value !== null) === bothValues &&
    (results.annual_value !== null) === bothValues &&
    (results.net_annual_value !== null) === (bothValues && cost !== null) &&
    // A return on a cost of zero does not exist.
    (results.roi_percent !== null) === (bothValues && cost !== null && cost !== '0.00') &&
    // A payback needs a value and a cost; with both it can still be absent, when the value covers none of the cost.
    (results.payback_months === null || (bothValues && cost !== null))
  )
}

// --- the settings ----------------------------------------------------------------------------------------------------

function settingsPath(orgSlug: string): string {
  return `/api/organizations/${segment(orgSlug)}/settings/sortview-value`
}

/** The `sortview_value` object every settings answer is wrapped in. */
function settingsBody(body: unknown): SortViewValueSettings {
  return parseSettings(record(body).sortview_value)
}

/** GET /api/organizations/{org_slug}/settings/sortview-value */
export async function getSortViewValueSettings(orgSlug: string, signal?: AbortSignal): Promise<SortViewValueSettings> {
  return settingsBody(await apiRequest(settingsPath(orgSlug), { signal }))
}

/** PUT /api/organizations/{org_slug}/settings/sortview-value. Replaces the whole block; answers with what is then stored. */
export async function putSortViewValueSettings(orgSlug: string, settings: SortViewValueInput): Promise<SortViewValueSettings> {
  // Field by field: exactly these, and nothing else that happened to be on the object.
  const body: SortViewValueInput = {
    labor_rate: settings.labor_rate,
    subscription_annual_cost: settings.subscription_annual_cost,
    reporting: {
      tasks_per_month: settings.reporting.tasks_per_month,
      baseline_minutes_per_task: settings.reporting.baseline_minutes_per_task,
      current_minutes_per_task: settings.reporting.current_minutes_per_task,
      basis: settings.reporting.basis,
      as_of: settings.reporting.as_of,
    },
    troubleshooting: {
      incidents_per_month: settings.troubleshooting.incidents_per_month,
      staff_per_incident: settings.troubleshooting.staff_per_incident,
      baseline_minutes: settings.troubleshooting.baseline_minutes,
      current_minutes: settings.troubleshooting.current_minutes,
      basis: settings.troubleshooting.basis,
      as_of: settings.troubleshooting.as_of,
    },
  }
  return settingsBody(await apiRequest(settingsPath(orgSlug), { method: 'PUT', body }))
}

/**
 * The fields the API refused in a PUT, if that is what this failure is: a 422
 * `invalid_sortview_value_settings` with a well-formed list. Anything else --
 * any other failure, or a list that is not one -- is null. Only the field
 * name and the code of each are kept.
 */
export function valueSettingProblems(error: unknown): ValueSettingProblem[] | null {
  if (!isApiError(error) || !(error instanceof ApiError) || error.status !== 422 || error.code !== INVALID_SORTVIEW_VALUE_SETTINGS) {
    return null
  }
  const body = error.body
  if (typeof body !== 'object' || body === null || !Array.isArray((body as { problems?: unknown }).problems)) {
    return null
  }
  const problems: ValueSettingProblem[] = []
  for (const entry of (body as { problems: unknown[] }).problems) {
    if (typeof entry !== 'object' || entry === null) {
      return null
    }
    const { field, code } = entry as Record<string, unknown>
    if (typeof field !== 'string' || typeof code !== 'string') {
      return null
    }
    problems.push({ field, code })
  }
  return problems
}

// --- the report ------------------------------------------------------------------------------------------------------

/** GET /api/organizations/{org_slug}/reports/sortview-value */
export async function getSortViewValueReport(orgSlug: string, signal?: AbortSignal): Promise<SortViewValueReport> {
  const body = record(await apiRequest(`/api/organizations/${segment(orgSlug)}/reports/sortview-value`, { signal }))
  const results = record(body.results)

  if (body.currency !== 'USD' || !Array.isArray(body.missing) || body.missing.some((name) => !(VALUE_INPUTS as readonly unknown[]).includes(name))) {
    throw unexpectedResponse(200)
  }
  const report: SortViewValueReport = {
    currency: 'USD',
    assumptions: parseSettings(body.assumptions),
    results: {
      reporting: category(results.reporting),
      troubleshooting: category(results.troubleshooting),
      recovered_hours_per_month: signedHours(results.recovered_hours_per_month),
      monthly_value: signedMoney(results.monthly_value),
      annual_value: signedMoney(results.annual_value),
      net_annual_value: signedMoney(results.net_annual_value),
      roi_percent: optional(results.roi_percent, (value) => decimal(value, 1, true)),
      payback_months: onePlace(results.payback_months),
    },
    missing: body.missing as ValueInput[],
  }
  if (!consistent(report)) {
    throw unexpectedResponse(200)
  }
  return report
}
