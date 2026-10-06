import { isValidTimeZone } from '../time/productTime.ts'
import { ApiError, apiRequest, unexpectedResponse } from './client.ts'

/**
 * A branch's operational reads for one day: what its pipeline last reported,
 * and its check-ins and rejects. A branch is named by its organization's slug
 * and its own -- the API takes no id, and returns none.
 *
 * The API never decides which day "today" is: every dated read is told the
 * calendar date, which the caller works out in the product's time zone (the
 * `timezone` that pipeline-status returns).
 *
 * Each response is checked against its documented shape and used whole or
 * not at all.
 */

export const PIPELINE_STATES = ['ok', 'degraded', 'failed', 'unknown'] as const
export type PipelineState = (typeof PIPELINE_STATES)[number]

/** The reject reason codes, in the one order the API lists them. */
export const REJECT_REASONS = [
  'item_not_found',
  'ils_acs_failure',
  'rfid_collision',
  'configuration_error',
  'routing_error',
  'communication_error',
  'other',
  'unknown',
] as const
export type RejectReason = (typeof REJECT_REASONS)[number]

export interface PipelineStatus {
  /** The IANA zone the product presents times in, and in which a calendar day is a day. */
  timezone: string
  /** What the pipeline last reported. Not a judgement of how recent that report is. */
  state: PipelineState
  /** When that report was received (an ISO-8601 instant, UTC), or null if nothing has been reported. */
  last_reported_at: string | null
}

export interface CheckinCount {
  date: string
  timezone: string
  checkin_count: number
}

export interface CheckinHourCount {
  /** The wall-clock hour, 0 to 23. */
  hour: number
  checkin_count: number
}

export interface CheckinsByHour {
  date: string
  timezone: string
  /** Always 24 entries, hours 0 to 23 in order, zero where nothing happened. */
  hours: CheckinHourCount[]
}

/** Check-ins the sorter routed to one of the site's configured destinations. */
export interface RoutingDestinationCount {
  /** Identifies the destination: a lower-case slug, unique within one answer. */
  key: string
  /** What the site calls the destination, as configured. */
  label: string
  checkin_count: number
}

/**
 * One day's check-ins by where the sorter routed them. A destination is a
 * routing outcome of this sorter site -- not a site or a sorter of its own.
 */
export interface CheckinsByDestination {
  date: string
  timezone: string
  /** Every check-in of the day: `home.checkin_count + transit_count + other_count`. */
  checkin_count: number
  /** Kept at the site itself. */
  home: { label: string; checkin_count: number }
  /** The site's configured destinations, in configured order, zero where there were none. */
  transit: RoutingDestinationCount[]
  /** The sum of `transit`. */
  transit_count: number
  /** Neither home nor a configured destination. */
  other_count: number
}

export interface RejectCount {
  date: string
  timezone: string
  reject_count: number
}

export interface RejectReasonCount {
  reason: RejectReason
  reject_count: number
}

export interface RejectsByReason {
  date: string
  timezone: string
  /** Always eight entries, one for each reason code in REJECT_REASONS order, zero where there were none. */
  reasons: RejectReasonCount[]
}

export const CALENDAR_DATE = /^\d{4}-\d{2}-\d{2}$/
export const DESTINATION_KEY = /^[a-z0-9][a-z0-9_]{0,63}$/
const LABEL_MAX_LENGTH = 200

// The validators below are shared with the range reports (api/reports.ts).
export function record(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw unexpectedResponse(200)
  }
  return value as Record<string, unknown>
}

export function count(value: unknown): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    throw unexpectedResponse(200)
  }
  return value
}

export function timezone(value: unknown): string {
  if (!isValidTimeZone(value)) {
    throw unexpectedResponse(200)
  }
  return value
}

/** A configured label: text with something in it, and not absurdly long. */
export function label(value: unknown): string {
  if (typeof value !== 'string' || value.trim() === '' || value.length > LABEL_MAX_LENGTH) {
    throw unexpectedResponse(200)
  }
  return value
}

/** The `date` and `timezone` every dated answer repeats. The date must be the one that was asked for. */
function day(body: Record<string, unknown>, requestedDate: string): { date: string; timezone: string } {
  if (body.date !== requestedDate) {
    throw unexpectedResponse(200)
  }
  return { date: requestedDate, timezone: timezone(body.timezone) }
}

function entries(value: unknown, length: number): Record<string, unknown>[] {
  if (!Array.isArray(value) || value.length !== length) {
    throw unexpectedResponse(200)
  }
  return value.map(record)
}

/** One path segment. "." and ".." are path navigation to a browser, so no request is made for them. */
function segment(slug: string): string {
  if (slug === '' || slug === '.' || slug === '..') {
    throw new ApiError(404, 'tenant_not_found', 'Organization or branch not found.')
  }
  return encodeURIComponent(slug)
}

export function branchPath(orgSlug: string, branchSlug: string): string {
  return `/api/organizations/${segment(orgSlug)}/branches/${segment(branchSlug)}`
}

function datedPath(orgSlug: string, branchSlug: string, endpoint: string, date: string): string {
  if (!CALENDAR_DATE.test(date)) {
    // A bug in the caller, not something a server said.
    throw new Error('A date must be a calendar date in the form YYYY-MM-DD.')
  }
  return `${branchPath(orgSlug, branchSlug)}/${endpoint}?date=${encodeURIComponent(date)}`
}

/** GET .../pipeline-status */
export async function getPipelineStatus(orgSlug: string, branchSlug: string, signal?: AbortSignal): Promise<PipelineStatus> {
  const body = record(await apiRequest(`${branchPath(orgSlug, branchSlug)}/pipeline-status`, { signal }))
  const { state, last_reported_at } = body
  if (!PIPELINE_STATES.includes(state as PipelineState)) {
    throw unexpectedResponse(200)
  }
  if (last_reported_at !== null && (typeof last_reported_at !== 'string' || Number.isNaN(Date.parse(last_reported_at)))) {
    throw unexpectedResponse(200)
  }
  return { timezone: timezone(body.timezone), state: state as PipelineState, last_reported_at }
}

/** GET .../checkins/count?date=YYYY-MM-DD */
export async function getCheckinCount(
  orgSlug: string,
  branchSlug: string,
  date: string,
  signal?: AbortSignal,
): Promise<CheckinCount> {
  const body = record(await apiRequest(datedPath(orgSlug, branchSlug, 'checkins/count', date), { signal }))
  return { ...day(body, date), checkin_count: count(body.checkin_count) }
}

/** GET .../checkins/by-hour?date=YYYY-MM-DD */
export async function getCheckinsByHour(
  orgSlug: string,
  branchSlug: string,
  date: string,
  signal?: AbortSignal,
): Promise<CheckinsByHour> {
  const body = record(await apiRequest(datedPath(orgSlug, branchSlug, 'checkins/by-hour', date), { signal }))
  const hours = entries(body.hours, 24).map((entry, index) => {
    if (entry.hour !== index) {
      throw unexpectedResponse(200)
    }
    return { hour: index, checkin_count: count(entry.checkin_count) }
  })
  return { ...day(body, date), hours }
}

/** GET .../checkins/by-destination?date=YYYY-MM-DD */
export async function getCheckinsByDestination(
  orgSlug: string,
  branchSlug: string,
  date: string,
  signal?: AbortSignal,
): Promise<CheckinsByDestination> {
  const body = record(await apiRequest(datedPath(orgSlug, branchSlug, 'checkins/by-destination', date), { signal }))
  const home = record(body.home)
  if (!Array.isArray(body.transit)) {
    throw unexpectedResponse(200)
  }
  const transit = body.transit.map(record).map((entry) => {
    if (typeof entry.key !== 'string' || !DESTINATION_KEY.test(entry.key)) {
      throw unexpectedResponse(200)
    }
    return { key: entry.key, label: label(entry.label), checkin_count: count(entry.checkin_count) }
  })
  const answer = {
    ...day(body, date),
    checkin_count: count(body.checkin_count),
    home: { label: label(home.label), checkin_count: count(home.checkin_count) },
    transit,
    transit_count: count(body.transit_count),
    other_count: count(body.other_count),
  }
  // Two destinations with one key could not be told apart, and figures that do not add up cannot all be shown
  // as true: either way the answer is used whole or not at all.
  const transitTotal = transit.reduce((total, entry) => total + entry.checkin_count, 0)
  if (
    new Set(transit.map((entry) => entry.key)).size !== transit.length ||
    transitTotal !== answer.transit_count ||
    answer.home.checkin_count + answer.transit_count + answer.other_count !== answer.checkin_count
  ) {
    throw unexpectedResponse(200)
  }
  return answer
}

/** GET .../rejects/count?date=YYYY-MM-DD */
export async function getRejectCount(
  orgSlug: string,
  branchSlug: string,
  date: string,
  signal?: AbortSignal,
): Promise<RejectCount> {
  const body = record(await apiRequest(datedPath(orgSlug, branchSlug, 'rejects/count', date), { signal }))
  return { ...day(body, date), reject_count: count(body.reject_count) }
}

/** GET .../rejects/by-reason?date=YYYY-MM-DD */
export async function getRejectsByReason(
  orgSlug: string,
  branchSlug: string,
  date: string,
  signal?: AbortSignal,
): Promise<RejectsByReason> {
  const body = record(await apiRequest(datedPath(orgSlug, branchSlug, 'rejects/by-reason', date), { signal }))
  const reasons = entries(body.reasons, REJECT_REASONS.length).map((entry, index) => {
    // Exactly the eight known codes, in their fixed order: a code this app does not know is not shown as one.
    if (entry.reason !== REJECT_REASONS[index]) {
      throw unexpectedResponse(200)
    }
    return { reason: REJECT_REASONS[index], reject_count: count(entry.reject_count) }
  })
  return { ...day(body, date), reasons }
}
