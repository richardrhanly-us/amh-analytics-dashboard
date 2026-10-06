import { ApiError, apiRequest, isApiError, unexpectedResponse } from './client.ts'
import { branchPath, CALENDAR_DATE, count, record, segment } from './liveToday.ts'
import { dayNumber, range, type ReportRange } from './reports.ts'

/**
 * Efficiency: what an organization has ASSUMED about handling items by hand,
 * what a sorter costs to keep, and what the API works out from the two for a
 * range of days. For an organization's owners and admins only -- the API
 * answers 403 to anyone else.
 *
 * EVERY FIGURE THAT IS NOT A COUNT IS TEXT. A rate, an amount of money and a
 * number of hours each arrive as a decimal written out in full ("12.5",
 * "20.00", "1225.30") and are kept as that text: nothing here turns one into
 * a JavaScript number, and a JSON number where text belongs is refused, since
 * it has already been through a binary float. `null` is "not set" or "cannot
 * be worked out" -- never the same as zero.
 *
 * NOTHING IS CALCULATED IN THE BROWSER. The report's results are the API's.
 *
 * A sorter is read and written by its host branch, like its other reports.
 */

export type RateSource = 'organization' | 'sorter'

/** The assumptions a report can find missing, in the order the API lists them. */
export const USED_ASSUMPTIONS = ['manual_items_per_hour', 'labor_rate', 'recurring_annual_cost', 'in_service_date'] as const
export type UsedAssumption = (typeof USED_ASSUMPTIONS)[number]

export interface AssumedRate {
  value: string
  source: RateSource
}

export interface EfficiencyReport {
  range: ReportRange
  currency: 'USD'
  /** Every check-in of the range: the count the sorter's other reports give. */
  checkin_count: number
  assumptions: {
    manual_items_per_hour: AssumedRate | null
    labor_rate: AssumedRate | null
    recurring_annual_cost: string | null
    one_time_cost: string | null
    in_service_date: string | null
  }
  results: {
    /** Days of the range on or after the in-service date, and the check-ins on them: what the figures are about. */
    in_service_days: number
    in_service_checkin_count: number
    staff_time_equivalent_hours: string | null
    labor_value_equivalent: string | null
    recurring_cost: string | null
    /** May be negative. */
    net_operational_value: string | null
    recurring_cost_per_item: string | null
  }
  missing: UsedAssumption[]
}

/** An organization's defaults, as stored. */
export interface OrganizationEfficiencySettings {
  labor_rate: string | null
  manual_items_per_hour: string | null
}

/** One rate for one sorter: the organization's default, the sorter's own, and which of them applies. */
export interface SorterRateSetting {
  organization: string | null
  sorter: string | null
  effective: string | null
  source: RateSource | null
}

export interface SorterEfficiencySettings {
  labor_rate: SorterRateSetting
  manual_items_per_hour: SorterRateSetting
  one_time_cost: string | null
  recurring_annual_cost: string | null
  in_service_date: string | null
}

/** What a PUT sends: the whole block for its level. null clears a field. Text exactly as it is to be stored. */
export type OrganizationEfficiencyInput = Record<'labor_rate' | 'manual_items_per_hour', string | null>
export type SorterEfficiencyInput = Record<
  'labor_rate' | 'manual_items_per_hour' | 'one_time_cost' | 'recurring_annual_cost' | 'in_service_date',
  string | null
>

/** One field the API refused, and the stable code for why. Never the value that was sent. */
export interface SettingProblem {
  field: string
  code: string
}

export const EFFICIENCY_SETTINGS_INVALID = 'efficiency_settings_invalid'
export const INVALID_EFFICIENCY_SETTINGS = 'invalid_efficiency_settings'

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

const money = (value: unknown) => decimal(value, 2)
const manualRate = (value: unknown) => decimal(value, 1)

function calendarDate(value: unknown): string {
  if (typeof value !== 'string' || !CALENDAR_DATE.test(value) || dayNumber(value) === null) {
    throw unexpectedResponse(200)
  }
  return value
}

function rateSource(value: unknown): RateSource {
  if (value !== 'organization' && value !== 'sorter') {
    throw unexpectedResponse(200)
  }
  return value
}

function assumedRate(value: unknown, parse: (value: unknown) => string): AssumedRate {
  const rate = record(value)
  return { value: parse(rate.value), source: rateSource(rate.source) }
}

/** The `efficiency` object every settings answer is wrapped in. */
function block(body: unknown): Record<string, unknown> {
  return record(record(body).efficiency)
}

function parseOrganization(body: unknown): OrganizationEfficiencySettings {
  const efficiency = block(body)
  return {
    labor_rate: optional(efficiency.labor_rate, money),
    manual_items_per_hour: optional(efficiency.manual_items_per_hour, manualRate),
  }
}

/** A rate's four parts, which must agree: the effective value is the sorter's if it has one, else the organization's. */
function rateSetting(value: unknown, parse: (value: unknown) => string): SorterRateSetting {
  const setting = record(value)
  const parsed = {
    organization: optional(setting.organization, parse),
    sorter: optional(setting.sorter, parse),
    effective: optional(setting.effective, parse),
    source: optional(setting.source, rateSource),
  }
  const expected = parsed.sorter !== null ? 'sorter' : parsed.organization !== null ? 'organization' : null
  if (parsed.source !== expected || parsed.effective !== (expected === null ? null : parsed[expected])) {
    throw unexpectedResponse(200)
  }
  return parsed
}

function parseSorter(body: unknown): SorterEfficiencySettings {
  const efficiency = block(body)
  return {
    labor_rate: rateSetting(efficiency.labor_rate, money),
    manual_items_per_hour: rateSetting(efficiency.manual_items_per_hour, manualRate),
    one_time_cost: optional(efficiency.one_time_cost, money),
    recurring_annual_cost: optional(efficiency.recurring_annual_cost, money),
    in_service_date: optional(efficiency.in_service_date, calendarDate),
  }
}

// --- the report ------------------------------------------------------------------------------------------------------

/** GET .../reports/efficiency?from=YYYY-MM-DD&to=YYYY-MM-DD */
export async function getEfficiencyReport(
  orgSlug: string,
  branchSlug: string,
  from: string,
  to: string,
  signal?: AbortSignal,
): Promise<EfficiencyReport> {
  if (dayNumber(from) === null || dayNumber(to) === null) {
    // A bug in the caller, not something a server said.
    throw new Error('A report range must be two calendar dates in the form YYYY-MM-DD.')
  }
  const path = `${branchPath(orgSlug, branchSlug)}/reports/efficiency?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`
  const body = record(await apiRequest(path, { signal }))
  const reportRange = range(body, from, to)
  const assumptions = record(body.assumptions)
  const results = record(body.results)

  if (body.currency !== 'USD' || !Array.isArray(body.missing)) {
    throw unexpectedResponse(200)
  }
  const report: EfficiencyReport = {
    range: reportRange,
    currency: 'USD',
    checkin_count: count(body.checkin_count),
    assumptions: {
      manual_items_per_hour: optional(assumptions.manual_items_per_hour, (value) => assumedRate(value, manualRate)),
      labor_rate: optional(assumptions.labor_rate, (value) => assumedRate(value, money)),
      recurring_annual_cost: optional(assumptions.recurring_annual_cost, money),
      one_time_cost: optional(assumptions.one_time_cost, money),
      in_service_date: optional(assumptions.in_service_date, calendarDate),
    },
    results: {
      in_service_days: count(results.in_service_days),
      in_service_checkin_count: count(results.in_service_checkin_count),
      staff_time_equivalent_hours: optional(results.staff_time_equivalent_hours, money),
      labor_value_equivalent: optional(results.labor_value_equivalent, money),
      recurring_cost: optional(results.recurring_cost, money),
      net_operational_value: optional(results.net_operational_value, (value) => decimal(value, 2, true)),
      recurring_cost_per_item: optional(results.recurring_cost_per_item, (value) => decimal(value, 4)),
    },
    missing: body.missing as UsedAssumption[],
  }

  // What is listed as missing must be exactly what is missing, in the one order; and the counted days and
  // check-ins cannot be more than the range has.
  const { assumptions: assumed, results: worked } = report
  const unset = USED_ASSUMPTIONS.filter((name) => assumed[name] === null)
  if (
    report.missing.length !== unset.length ||
    report.missing.some((name, index) => name !== unset[index]) ||
    worked.in_service_days > reportRange.days ||
    worked.in_service_checkin_count > report.checkin_count ||
    (assumed.in_service_date === null &&
      (worked.in_service_days !== reportRange.days || worked.in_service_checkin_count !== report.checkin_count)) ||
    // A figure exists exactly when what it is made from is known.
    (worked.staff_time_equivalent_hours === null) !== (assumed.manual_items_per_hour === null) ||
    (worked.labor_value_equivalent === null) !== (assumed.manual_items_per_hour === null || assumed.labor_rate === null) ||
    (worked.recurring_cost === null) !== (assumed.recurring_annual_cost === null) ||
    (worked.net_operational_value === null) !== (worked.labor_value_equivalent === null || worked.recurring_cost === null) ||
    (worked.recurring_cost_per_item === null) !== (worked.recurring_cost === null || worked.in_service_checkin_count === 0)
  ) {
    throw unexpectedResponse(200)
  }
  return report
}

// --- the settings ----------------------------------------------------------------------------------------------------

function organizationSettingsPath(orgSlug: string): string {
  return `/api/organizations/${segment(orgSlug)}/settings/efficiency`
}

function sorterSettingsPath(orgSlug: string, branchSlug: string): string {
  return `${branchPath(orgSlug, branchSlug)}/settings/efficiency`
}

/** GET /api/organizations/{org_slug}/settings/efficiency */
export async function getOrganizationEfficiencySettings(orgSlug: string, signal?: AbortSignal): Promise<OrganizationEfficiencySettings> {
  return parseOrganization(await apiRequest(organizationSettingsPath(orgSlug), { signal }))
}

/** PUT /api/organizations/{org_slug}/settings/efficiency. Replaces both defaults; answers with what is then stored. */
export async function putOrganizationEfficiencySettings(
  orgSlug: string,
  settings: OrganizationEfficiencyInput,
): Promise<OrganizationEfficiencySettings> {
  const body: OrganizationEfficiencyInput = {
    labor_rate: settings.labor_rate,
    manual_items_per_hour: settings.manual_items_per_hour,
  }
  return parseOrganization(await apiRequest(organizationSettingsPath(orgSlug), { method: 'PUT', body }))
}

/** GET .../branches/{branch_slug}/settings/efficiency */
export async function getSorterEfficiencySettings(
  orgSlug: string,
  branchSlug: string,
  signal?: AbortSignal,
): Promise<SorterEfficiencySettings> {
  return parseSorter(await apiRequest(sorterSettingsPath(orgSlug, branchSlug), { signal }))
}

/** PUT .../branches/{branch_slug}/settings/efficiency. Replaces the sorter's whole block; answers with what is then stored. */
export async function putSorterEfficiencySettings(
  orgSlug: string,
  branchSlug: string,
  settings: SorterEfficiencyInput,
): Promise<SorterEfficiencySettings> {
  const body: SorterEfficiencyInput = {
    labor_rate: settings.labor_rate,
    manual_items_per_hour: settings.manual_items_per_hour,
    one_time_cost: settings.one_time_cost,
    recurring_annual_cost: settings.recurring_annual_cost,
    in_service_date: settings.in_service_date,
  }
  return parseSorter(await apiRequest(sorterSettingsPath(orgSlug, branchSlug), { method: 'PUT', body }))
}

/**
 * The fields the API refused in a PUT, if that is what this failure is: a 422
 * `invalid_efficiency_settings` with a well-formed list. Anything else -- any
 * other failure, or a list that is not one -- is null. Only the field name
 * and the code of each are kept.
 */
export function settingProblems(error: unknown): SettingProblem[] | null {
  if (!isApiError(error) || !(error instanceof ApiError) || error.status !== 422 || error.code !== INVALID_EFFICIENCY_SETTINGS) {
    return null
  }
  const body = error.body
  if (typeof body !== 'object' || body === null || !Array.isArray((body as { problems?: unknown }).problems)) {
    return null
  }
  const problems: SettingProblem[] = []
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
