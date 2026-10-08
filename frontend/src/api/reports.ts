import { apiRequest, unexpectedResponse } from './client.ts'
import {
  branchPath,
  CALENDAR_DATE,
  count,
  DESTINATION_KEY,
  label,
  record,
  REJECT_REASONS,
  timezone,
  type RejectReason,
  type RoutingDestinationCount,
} from './liveToday.ts'

/**
 * A sorter's range reports: counts over a run of calendar days. A sorter is
 * read by its host branch -- the scope its collector uploads under -- so
 * these take an organization's slug and that branch's, like the Live Today
 * reads.
 *
 * THE API SENDS COUNTS AND NOTHING ELSE. Every figure is a whole number;
 * rates, averages and "busiest" are worked out from them (reports/derive.ts).
 * Every list is full length: one entry per date of the range, 24 hours, eight
 * reject reasons, every configured destination -- with zeros, never gaps. The
 * one list that is not is the bin report's `bins`: which bins a sorter has is
 * not known, so it holds only the bins that were observed.
 *
 * Each response is checked against that shape, AND against its own
 * arithmetic, and is used whole or not at all: an answer whose parts do not
 * add up to its totals cannot all be shown as true.
 */

export interface ReportRange {
  /** First and last calendar date, both included, in `timezone`. */
  from: string
  to: string
  /** How many calendar dates that is. */
  days: number
  /** The product's IANA zone: the one in which a day is a day. */
  timezone: string
  /** The range reaches the product's current date, whose counts are not final yet. */
  includes_today: boolean
}

export interface OverviewReport {
  range: ReportRange
  checkin_count: number
  /** Dates in the range with at least one check-in. */
  active_days: number
  home_count: number
  transit_count: number
  other_count: number
  reject_count: number
  days: Array<{ date: string; checkin_count: number; reject_count: number }>
}

export interface VolumeReport {
  range: ReportRange
  checkin_count: number
  days: Array<{ date: string; checkin_count: number }>
  /** Always 24, hours 0 to 23. Each count is the TOTAL for that wall-clock hour across the range. */
  hours: Array<{ hour: number; checkin_count: number }>
}

export interface RoutingReport {
  range: ReportRange
  checkin_count: number
  home: { label: string; checkin_count: number }
  /** The sorter's configured destinations, in configured order. Outcomes of this sorter, not sorters. */
  transit: RoutingDestinationCount[]
  transit_count: number
  other_count: number
  /** `transit_counts[n]` is the day's count for `transit[n]`. */
  days: Array<{ date: string; checkin_count: number; home_count: number; transit_counts: number[]; other_count: number }>
}

export interface ReliabilityReport {
  range: ReportRange
  checkin_count: number
  reject_count: number
  /** Always the eight reason codes, in REJECT_REASONS order. */
  reasons: Array<{ reason: RejectReason; reject_count: number }>
  days: Array<{ date: string; checkin_count: number; reject_count: number }>
}

/** One sort bin that check-ins of the range were logged in. */
export interface BinVolumeBin {
  /** The bin's number as the sorter logs it, without leading zeros: "0", "4", "12". An identifier, not a count. */
  key: string
  checkin_count: number
  /** Always 24, hours 0 to 23. Each count is the TOTAL for that wall-clock hour across the range. */
  hours: number[]
}

/**
 * Check-ins by the physical sort bin each was logged in. A bin is where an
 * item went on the sorter: it says nothing of how full the bin was, where
 * the item was routed, or what it was.
 */
export interface BinVolumeReport {
  range: ReportRange
  /** Every check-in of the range: `known_bin_count + unknown_bin_count`. */
  checkin_count: number
  known_bin_count: number
  /** Check-ins whose logged bin was missing or was not a bin number. Not a bin, and not in `bins`. */
  unknown_bin_count: number
  /**
   * Only the bins that were OBSERVED in the range, in numeric order. A bin
   * that is not here had no check-ins in the range -- or does not exist:
   * which bins a sorter has is not known, so a missing bin is not a zero.
   */
  bins: BinVolumeBin[]
}

/**
 * Holds handled by a sorter over the range: two counts and nothing else.
 * Each item is counted once over the whole range, by its latest record, so
 * the counts of a range are not the sums of its days. Holds for the
 * library's own service accounts are in neither count. Only for a plan that
 * has it.
 */
export interface HoldsReport {
  range: ReportRange
  /** Holds for library patrons: not interlibrary loans, and not the library's own service accounts. */
  public_hold_count: number
  /** Holds for interlibrary loans. */
  ill_hold_count: number
}

export const REPORT_KINDS = ['overview', 'volume', 'routing', 'bins', 'reliability', 'holds'] as const

/** The API's code for a range that starts before the organization's plan lets a report reach (R9C). */
export const RANGE_BEFORE_HISTORY = 'range_before_history'
export type ReportKind = (typeof REPORT_KINDS)[number]

const MS_PER_DAY = 86_400_000

/** A real calendar date's day number, or null. A calendar date has no zone: this is arithmetic, not a clock. */
export function dayNumber(date: unknown): number | null {
  if (typeof date !== 'string' || !CALENDAR_DATE.test(date)) {
    return null
  }
  const [year, month, day] = date.split('-').map(Number)
  const utc = Date.UTC(year, month - 1, day)
  // Date rolls an impossible date over into a real one (30 February). That is not the date that was written.
  return new Date(utc).toISOString().slice(0, 10) === date ? utc / MS_PER_DAY : null
}

export function list(value: unknown, length: number): Record<string, unknown>[] {
  if (!Array.isArray(value) || value.length !== length) {
    throw unexpectedResponse(200)
  }
  return value.map(record)
}

export const sum = (values: number[]) => values.reduce((total, value) => total + value, 0)

export function agrees(...pairs: Array<[number, number]>): void {
  if (pairs.some(([part, whole]) => part !== whole)) {
    throw unexpectedResponse(200)
  }
}

/** The range every report repeats. It must be the range that was asked for, and its own arithmetic must hold. */
export function range(body: Record<string, unknown>, from: string, to: string): ReportRange {
  const value = record(body.range)
  const first = dayNumber(from)
  const last = dayNumber(to)
  if (
    first === null ||
    last === null ||
    value.from !== from ||
    value.to !== to ||
    value.days !== last - first + 1 ||
    typeof value.includes_today !== 'boolean'
  ) {
    throw unexpectedResponse(200)
  }
  return { from, to, days: value.days, timezone: timezone(value.timezone), includes_today: value.includes_today }
}

/** One entry per date of the range, in order, each parsed by `parse`. */
export function days<T>(value: unknown, reportRange: ReportRange, parse: (entry: Record<string, unknown>) => T): Array<T & { date: string }> {
  const first = dayNumber(reportRange.from) as number
  return list(value, reportRange.days).map((entry, index) => {
    if (dayNumber(entry.date) !== first + index) {
      throw unexpectedResponse(200)
    }
    return { date: entry.date as string, ...parse(entry) }
  })
}

/** Exactly the eight known codes, in their fixed order: a code this app does not know is not shown as one. */
export function reasons(value: unknown): Array<{ reason: RejectReason; reject_count: number }> {
  return list(value, REJECT_REASONS.length).map((entry, index) => {
    if (entry.reason !== REJECT_REASONS[index]) {
      throw unexpectedResponse(200)
    }
    return { reason: REJECT_REASONS[index], reject_count: count(entry.reject_count) }
  })
}

function reportPath(orgSlug: string, branchSlug: string, kind: ReportKind, from: string, to: string): string {
  if (dayNumber(from) === null || dayNumber(to) === null) {
    // A bug in the caller, not something a server said.
    throw new Error('A report range must be two calendar dates in the form YYYY-MM-DD.')
  }
  return `${branchPath(orgSlug, branchSlug)}/reports/${kind}?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`
}

/** GET .../reports/overview?from=YYYY-MM-DD&to=YYYY-MM-DD */
export async function getOverviewReport(
  orgSlug: string,
  branchSlug: string,
  from: string,
  to: string,
  signal?: AbortSignal,
): Promise<OverviewReport> {
  const body = record(await apiRequest(reportPath(orgSlug, branchSlug, 'overview', from, to), { signal }))
  const reportRange = range(body, from, to)
  const report = {
    range: reportRange,
    checkin_count: count(body.checkin_count),
    active_days: count(body.active_days),
    home_count: count(body.home_count),
    transit_count: count(body.transit_count),
    other_count: count(body.other_count),
    reject_count: count(body.reject_count),
    days: days(body.days, reportRange, (entry) => ({
      checkin_count: count(entry.checkin_count),
      reject_count: count(entry.reject_count),
    })),
  }
  agrees(
    [report.home_count + report.transit_count + report.other_count, report.checkin_count],
    [sum(report.days.map((day) => day.checkin_count)), report.checkin_count],
    [sum(report.days.map((day) => day.reject_count)), report.reject_count],
    [report.days.filter((day) => day.checkin_count > 0).length, report.active_days],
  )
  return report
}

/** GET .../reports/volume?from=YYYY-MM-DD&to=YYYY-MM-DD */
export async function getVolumeReport(
  orgSlug: string,
  branchSlug: string,
  from: string,
  to: string,
  signal?: AbortSignal,
): Promise<VolumeReport> {
  const body = record(await apiRequest(reportPath(orgSlug, branchSlug, 'volume', from, to), { signal }))
  const reportRange = range(body, from, to)
  const report = {
    range: reportRange,
    checkin_count: count(body.checkin_count),
    days: days(body.days, reportRange, (entry) => ({ checkin_count: count(entry.checkin_count) })),
    hours: list(body.hours, 24).map((entry, index) => {
      if (entry.hour !== index) {
        throw unexpectedResponse(200)
      }
      return { hour: index, checkin_count: count(entry.checkin_count) }
    }),
  }
  agrees(
    [sum(report.days.map((day) => day.checkin_count)), report.checkin_count],
    [sum(report.hours.map((hour) => hour.checkin_count)), report.checkin_count],
  )
  return report
}

/** GET .../reports/routing?from=YYYY-MM-DD&to=YYYY-MM-DD */
export async function getRoutingReport(
  orgSlug: string,
  branchSlug: string,
  from: string,
  to: string,
  signal?: AbortSignal,
): Promise<RoutingReport> {
  const body = record(await apiRequest(reportPath(orgSlug, branchSlug, 'routing', from, to), { signal }))
  const reportRange = range(body, from, to)
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
  const report = {
    range: reportRange,
    checkin_count: count(body.checkin_count),
    home: { label: label(home.label), checkin_count: count(home.checkin_count) },
    transit,
    transit_count: count(body.transit_count),
    other_count: count(body.other_count),
    days: days(body.days, reportRange, (entry) => {
      // One count for each destination, in the destinations' own order: a list of any other length cannot be read.
      if (!Array.isArray(entry.transit_counts) || entry.transit_counts.length !== transit.length) {
        throw unexpectedResponse(200)
      }
      const day = {
        checkin_count: count(entry.checkin_count),
        home_count: count(entry.home_count),
        transit_counts: entry.transit_counts.map(count),
        other_count: count(entry.other_count),
      }
      agrees([day.home_count + sum(day.transit_counts) + day.other_count, day.checkin_count])
      return day
    }),
  }
  if (new Set(transit.map((entry) => entry.key)).size !== transit.length) {
    throw unexpectedResponse(200)
  }
  agrees(
    [sum(transit.map((entry) => entry.checkin_count)), report.transit_count],
    [report.home.checkin_count + report.transit_count + report.other_count, report.checkin_count],
    [sum(report.days.map((day) => day.checkin_count)), report.checkin_count],
    [sum(report.days.map((day) => day.home_count)), report.home.checkin_count],
    [sum(report.days.map((day) => day.other_count)), report.other_count],
    ...transit.map((entry, slot): [number, number] => [sum(report.days.map((day) => day.transit_counts[slot])), entry.checkin_count]),
  )
  return report
}

/** GET .../reports/reliability?from=YYYY-MM-DD&to=YYYY-MM-DD */
export async function getReliabilityReport(
  orgSlug: string,
  branchSlug: string,
  from: string,
  to: string,
  signal?: AbortSignal,
): Promise<ReliabilityReport> {
  const body = record(await apiRequest(reportPath(orgSlug, branchSlug, 'reliability', from, to), { signal }))
  const reportRange = range(body, from, to)
  const report = {
    range: reportRange,
    checkin_count: count(body.checkin_count),
    reject_count: count(body.reject_count),
    reasons: reasons(body.reasons),
    days: days(body.days, reportRange, (entry) => ({
      checkin_count: count(entry.checkin_count),
      reject_count: count(entry.reject_count),
    })),
  }
  agrees(
    [sum(report.reasons.map((reason) => reason.reject_count)), report.reject_count],
    [sum(report.days.map((day) => day.reject_count)), report.reject_count],
    [sum(report.days.map((day) => day.checkin_count)), report.checkin_count],
  )
  return report
}

// A bin's key: its number, one to four digits, with no leading zero. "04" and "Bin 4" are not keys.
const BIN_KEY = /^(0|[1-9][0-9]{0,3})$/

/** GET .../reports/bins?from=YYYY-MM-DD&to=YYYY-MM-DD */
export async function getBinVolumeReport(
  orgSlug: string,
  branchSlug: string,
  from: string,
  to: string,
  signal?: AbortSignal,
): Promise<BinVolumeReport> {
  const body = record(await apiRequest(reportPath(orgSlug, branchSlug, 'bins', from, to), { signal }))
  const reportRange = range(body, from, to)
  // Any number of bins, none included: how many a sorter has is not known here.
  if (!Array.isArray(body.bins)) {
    throw unexpectedResponse(200)
  }
  const bins = body.bins.map(record).map((entry) => {
    if (typeof entry.key !== 'string' || !BIN_KEY.test(entry.key) || !Array.isArray(entry.hours) || entry.hours.length !== 24) {
      throw unexpectedResponse(200)
    }
    const bin = { key: entry.key, checkin_count: count(entry.checkin_count), hours: entry.hours.map(count) }
    // A bin is listed because something was logged in it: one with nothing is not an observed bin.
    if (bin.checkin_count === 0) {
      throw unexpectedResponse(200)
    }
    agrees([sum(bin.hours), bin.checkin_count])
    return bin
  })
  // Each bin once, in the order of its number: 2 before 10.
  if (bins.some((bin, index) => index > 0 && Number(bins[index - 1].key) >= Number(bin.key))) {
    throw unexpectedResponse(200)
  }
  const report = {
    range: reportRange,
    checkin_count: count(body.checkin_count),
    known_bin_count: count(body.known_bin_count),
    unknown_bin_count: count(body.unknown_bin_count),
    bins,
  }
  agrees(
    [report.known_bin_count + report.unknown_bin_count, report.checkin_count],
    [sum(bins.map((bin) => bin.checkin_count)), report.known_bin_count],
  )
  return report
}

/** GET .../reports/holds?from=YYYY-MM-DD&to=YYYY-MM-DD -- exactly the range and two counts. */
export async function getHoldsReport(
  orgSlug: string,
  branchSlug: string,
  from: string,
  to: string,
  signal?: AbortSignal,
): Promise<HoldsReport> {
  const body = record(await apiRequest(reportPath(orgSlug, branchSlug, 'holds', from, to), { signal }))
  // Only these: anything else in the answer is dropped here and reaches no screen.
  return { range: range(body, from, to), public_hold_count: count(body.public_hold_count), ill_hold_count: count(body.ill_hold_count) }
}
