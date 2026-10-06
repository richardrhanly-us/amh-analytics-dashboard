import { apiRequest, unexpectedResponse } from './client.ts'
import { count, DESTINATION_KEY, label, record, segment, type RejectReasonCount, type RoutingDestinationCount } from './liveToday.ts'
import { parseSorter, parseSorterIdentity, type SorterSummary } from './organizations.ts'
import { agrees, dayNumber, days, range, reasons, sum, type ReportRange } from './reports.ts'

/**
 * An organization's range reports: its sorters' counts over a run of
 * calendar days, added up. Read by the organization's slug alone.
 *
 * The figures are PROCESSING EVENTS across the organization's sorters, not
 * distinct items: an item two sorters each handled is counted at both.
 *
 * As for a sorter's reports (api/reports.ts), THE API SENDS COUNTS AND
 * NOTHING ELSE, every list is full length, and each response is checked
 * against its shape AND its own arithmetic and used whole or not at all. A
 * rate for the organization is one total over another (reports/derive.ts),
 * never an average of the sorters' rates -- which is why none is sent.
 *
 * A sorter that is registered but has no data to read yet is `available:
 * false`, with zeros that stand for "nothing could be read", not for "nothing
 * happened". It adds nothing to a total.
 */

export type SorterIdentity = Pick<SorterSummary, 'slug' | 'name' | 'host_branch'>

/** One sorter's headline counts, beside what the organization's own list says about it. */
export interface OrganizationSorterOverview extends SorterSummary {
  available: boolean
  checkin_count: number
  /** Dates in the range on which THIS sorter had a check-in. */
  active_days: number
  transit_count: number
  reject_count: number
}

export interface OrganizationOverviewReport {
  range: ReportRange
  /** `home_count + transit_count + other_count` is `checkin_count`. */
  totals: { checkin_count: number; home_count: number; transit_count: number; other_count: number; reject_count: number }
  /** Every sorter of the organization, in the organization's order. */
  sorters: OrganizationSorterOverview[]
  days: Array<{ date: string; checkin_count: number; reject_count: number }>
}

/** One sorter and where IT routed its check-ins. `transit` is its own configured destinations, in its own order. */
export interface RoutingNetworkSource {
  sorter: SorterIdentity
  checkin_count: number
  home: { label: string; checkin_count: number }
  transit_count: number
  other_count: number
  transit: RoutingDestinationCount[]
}

/**
 * Check-ins routed to one destination `key`, across the sources that have it
 * configured. Sources share an entry because their keys are equal, and that
 * is all it means: a destination is a routing outcome, never a sorter, and
 * nothing says a place exists by that key.
 */
export interface RoutingNetworkDestination extends RoutingDestinationCount {
  /** How many sources have the key configured, whether or not they routed anything to it. */
  source_count: number
}

export interface RoutingNetworkReport {
  range: ReportRange
  totals: { checkin_count: number; transit_count: number }
  /** Only the sorters that have data to read. */
  sources: RoutingNetworkSource[]
  destinations: RoutingNetworkDestination[]
}

export interface OrganizationSorterReliability {
  sorter: SorterIdentity
  available: boolean
  checkin_count: number
  reject_count: number
  /** Always the eight reason codes, in REJECT_REASONS order. */
  reasons: RejectReasonCount[]
}

export interface OrganizationReliabilityReport {
  range: ReportRange
  totals: { checkin_count: number; reject_count: number; reasons: RejectReasonCount[] }
  sorters: OrganizationSorterReliability[]
  days: Array<{ date: string; checkin_count: number; reject_count: number }>
}

export const ORGANIZATION_REPORT_KINDS = ['overview', 'routing-network', 'reliability'] as const
export type OrganizationReportKind = (typeof ORGANIZATION_REPORT_KINDS)[number]

/** A list of any length, each entry an object. */
function records(value: unknown): Record<string, unknown>[] {
  if (!Array.isArray(value)) {
    throw unexpectedResponse(200)
  }
  return value.map(record)
}

function holds(...conditions: boolean[]): void {
  if (conditions.includes(false)) {
    throw unexpectedResponse(200)
  }
}

const distinct = (keys: string[]) => new Set(keys).size === keys.length

function available(value: unknown): boolean {
  if (typeof value !== 'boolean') {
    throw unexpectedResponse(200)
  }
  return value
}

function destination(entry: Record<string, unknown>): RoutingDestinationCount {
  if (typeof entry.key !== 'string' || !DESTINATION_KEY.test(entry.key)) {
    throw unexpectedResponse(200)
  }
  return { key: entry.key, label: label(entry.label), checkin_count: count(entry.checkin_count) }
}

function reportPath(orgSlug: string, kind: OrganizationReportKind, from: string, to: string): string {
  if (dayNumber(from) === null || dayNumber(to) === null) {
    // A bug in the caller, not something a server said.
    throw new Error('A report range must be two calendar dates in the form YYYY-MM-DD.')
  }
  return `/api/organizations/${segment(orgSlug)}/reports/${kind}?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`
}

/** GET /api/organizations/{org_slug}/reports/overview?from=YYYY-MM-DD&to=YYYY-MM-DD */
export async function getOrganizationOverviewReport(
  orgSlug: string,
  from: string,
  to: string,
  signal?: AbortSignal,
): Promise<OrganizationOverviewReport> {
  const body = record(await apiRequest(reportPath(orgSlug, 'overview', from, to), { signal }))
  const reportRange = range(body, from, to)
  const totals = record(body.totals)
  const report = {
    range: reportRange,
    totals: {
      checkin_count: count(totals.checkin_count),
      home_count: count(totals.home_count),
      transit_count: count(totals.transit_count),
      other_count: count(totals.other_count),
      reject_count: count(totals.reject_count),
    },
    sorters: records(body.sorters).map((entry) => {
      const sorter = {
        ...parseSorter(entry),
        available: available(entry.available),
        checkin_count: count(entry.checkin_count),
        active_days: count(entry.active_days),
        transit_count: count(entry.transit_count),
        reject_count: count(entry.reject_count),
      }
      holds(
        sorter.transit_count <= sorter.checkin_count,
        // A day is active when it has a check-in: no more active days than days, or than check-ins, and none without one.
        sorter.active_days <= reportRange.days,
        sorter.active_days <= sorter.checkin_count,
        sorter.active_days > 0 === sorter.checkin_count > 0,
        // A sorter that could not be read has nothing to count.
        sorter.available || sorter.checkin_count + sorter.reject_count === 0,
      )
      return sorter
    }),
    days: days(body.days, reportRange, (entry) => ({
      checkin_count: count(entry.checkin_count),
      reject_count: count(entry.reject_count),
    })),
  }
  // Two sorters with one slug could not be told apart, and two at one host would be the same figures twice.
  holds(distinct(report.sorters.map((sorter) => sorter.slug)), distinct(report.sorters.map((sorter) => sorter.host_branch.slug)))
  agrees(
    [report.totals.home_count + report.totals.transit_count + report.totals.other_count, report.totals.checkin_count],
    [sum(report.sorters.map((sorter) => sorter.checkin_count)), report.totals.checkin_count],
    [sum(report.sorters.map((sorter) => sorter.transit_count)), report.totals.transit_count],
    [sum(report.sorters.map((sorter) => sorter.reject_count)), report.totals.reject_count],
    [sum(report.days.map((day) => day.checkin_count)), report.totals.checkin_count],
    [sum(report.days.map((day) => day.reject_count)), report.totals.reject_count],
  )
  return report
}

/** GET /api/organizations/{org_slug}/reports/routing-network?from=YYYY-MM-DD&to=YYYY-MM-DD */
export async function getOrganizationRoutingNetworkReport(
  orgSlug: string,
  from: string,
  to: string,
  signal?: AbortSignal,
): Promise<RoutingNetworkReport> {
  const body = record(await apiRequest(reportPath(orgSlug, 'routing-network', from, to), { signal }))
  const reportRange = range(body, from, to)
  const totals = record(body.totals)
  const report = {
    range: reportRange,
    totals: { checkin_count: count(totals.checkin_count), transit_count: count(totals.transit_count) },
    sources: records(body.sources).map((entry) => {
      const home = record(entry.home)
      const source = {
        sorter: parseSorterIdentity(entry.sorter),
        checkin_count: count(entry.checkin_count),
        home: { label: label(home.label), checkin_count: count(home.checkin_count) },
        transit_count: count(entry.transit_count),
        other_count: count(entry.other_count),
        transit: records(entry.transit).map(destination),
      }
      holds(distinct(source.transit.map((routed) => routed.key)))
      agrees(
        [sum(source.transit.map((routed) => routed.checkin_count)), source.transit_count],
        [source.home.checkin_count + source.transit_count + source.other_count, source.checkin_count],
      )
      return source
    }),
    destinations: records(body.destinations).map((entry) => ({ ...destination(entry), source_count: count(entry.source_count) })),
  }
  const keys = report.destinations.map((entry) => entry.key)
  holds(
    distinct(report.sources.map((source) => source.sorter.slug)),
    distinct(keys),
    // Every destination a source has is among the totals.
    report.sources.every((source) => source.transit.every((routed) => keys.includes(routed.key))),
  )
  for (const entry of report.destinations) {
    // What the sources that have this key routed to it, and how many of them there are: at least one.
    const routed = report.sources.flatMap((source) => source.transit.filter((candidate) => candidate.key === entry.key))
    holds(routed.length > 0)
    agrees([routed.length, entry.source_count], [sum(routed.map((candidate) => candidate.checkin_count)), entry.checkin_count])
  }
  agrees(
    [sum(report.sources.map((source) => source.checkin_count)), report.totals.checkin_count],
    [sum(report.sources.map((source) => source.transit_count)), report.totals.transit_count],
  )
  return report
}

/** GET /api/organizations/{org_slug}/reports/reliability?from=YYYY-MM-DD&to=YYYY-MM-DD */
export async function getOrganizationReliabilityReport(
  orgSlug: string,
  from: string,
  to: string,
  signal?: AbortSignal,
): Promise<OrganizationReliabilityReport> {
  const body = record(await apiRequest(reportPath(orgSlug, 'reliability', from, to), { signal }))
  const reportRange = range(body, from, to)
  const totals = record(body.totals)
  const report = {
    range: reportRange,
    totals: {
      checkin_count: count(totals.checkin_count),
      reject_count: count(totals.reject_count),
      reasons: reasons(totals.reasons),
    },
    sorters: records(body.sorters).map((entry) => {
      const sorter = {
        sorter: parseSorterIdentity(entry.sorter),
        available: available(entry.available),
        checkin_count: count(entry.checkin_count),
        reject_count: count(entry.reject_count),
        reasons: reasons(entry.reasons),
      }
      holds(sorter.available || sorter.checkin_count + sorter.reject_count === 0)
      agrees([sum(sorter.reasons.map((reason) => reason.reject_count)), sorter.reject_count])
      return sorter
    }),
    days: days(body.days, reportRange, (entry) => ({
      checkin_count: count(entry.checkin_count),
      reject_count: count(entry.reject_count),
    })),
  }
  holds(distinct(report.sorters.map((entry) => entry.sorter.slug)))
  agrees(
    [sum(report.totals.reasons.map((reason) => reason.reject_count)), report.totals.reject_count],
    [sum(report.sorters.map((entry) => entry.checkin_count)), report.totals.checkin_count],
    [sum(report.sorters.map((entry) => entry.reject_count)), report.totals.reject_count],
    [sum(report.days.map((day) => day.checkin_count)), report.totals.checkin_count],
    [sum(report.days.map((day) => day.reject_count)), report.totals.reject_count],
    ...report.totals.reasons.map((reason, slot): [number, number] => [
      sum(report.sorters.map((entry) => entry.reasons[slot].reject_count)),
      reason.reject_count,
    ]),
  )
  return report
}
