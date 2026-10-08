import { useQuery } from '@tanstack/react-query'

import { isApiError } from '../api/client.ts'
import { getPipelineStatus, type PipelineStatus } from '../api/liveToday.ts'
import {
  getBinVolumeReport,
  getHoldsReport,
  getOverviewReport,
  getReliabilityReport,
  getRoutingReport,
  getVolumeReport,
  RANGE_BEFORE_HISTORY,
  type BinVolumeReport,
  type HoldsReport,
  type OverviewReport,
  type ReliabilityReport,
  type ReportKind,
  type RoutingReport,
  type VolumeReport,
} from '../api/reports.ts'
import { messageFor } from '../components/errorText.ts'
import { productDate } from '../time/productTime.ts'
import type { DateRange } from './dateRange.ts'

/**
 * The product's calendar for one sorter: its time zone, and which date is
 * today there.
 *
 *   loading      not known yet
 *   ready        `timeZone` and `today` (YYYY-MM-DD)
 *   unavailable  the API answered 404 for a sorter the user can see: it has no data to report on
 *   error        it could not be read; `message` is safe to show
 *
 * A report's range is made of calendar dates in the PRODUCT's zone, and
 * "today" is the last date a range may reach -- so the zone has to be known
 * before any range can be chosen. It comes from the same read Live Today
 * takes it from (pipeline status), and today is the date in that zone at the
 * moment that answer arrived. The browser's own zone is never used.
 */
export type ProductDay =
  | { status: 'loading' }
  | { status: 'ready'; timeZone: string; today: string }
  | { status: 'unavailable' }
  | { status: 'error'; message: string }

export function useProductDay(orgSlug: string, branchSlug: string): { day: ProductDay; retry: () => void } {
  return useProductDayFrom(['reports', orgSlug, branchSlug, 'product-day'], (signal) => getPipelineStatus(orgSlug, branchSlug, signal))
}

/** The product's calendar, from whichever pipeline status `load` reads. `queryKey` says whose it is. */
export function useProductDayFrom(
  queryKey: readonly string[],
  load: (signal: AbortSignal) => Promise<PipelineStatus>,
): { day: ProductDay; retry: () => void } {
  const query = useQuery({ queryKey, queryFn: ({ signal }) => load(signal), gcTime: 0 })
  const retry = () => void query.refetch({ cancelRefetch: false })

  if (query.data !== undefined) {
    const timeZone = query.data.timezone
    return { day: { status: 'ready', timeZone, today: productDate(new Date(query.dataUpdatedAt), timeZone) }, retry }
  }
  if (isApiError(query.error) && query.error.status === 404) {
    return { day: { status: 'unavailable' }, retry }
  }
  return { day: query.isError ? { status: 'error', message: messageFor(query.error) } : { status: 'loading' }, retry }
}

/**
 * One report, for one sorter and one range.
 *
 *   loading      nothing to show yet
 *   ready        `data` is the answer for exactly this sorter and this range
 *   unavailable  the API answered 404: there is nothing to report on for this sorter
 *   before_history  the range starts before the organization's plan lets a report reach
 *   error        it could not be loaded; `message` is safe to show
 *
 * A report the organization's plan does not include is not asked for at all
 * (`enabled` false): it stays `loading` and is not shown.
 *
 * There is no "last good answer" here, on purpose. A different range, or a
 * different sorter, is a different question with its own key, and it starts
 * from `loading`: the figures of one range are never shown under another's
 * dates. Nothing is kept once the page is left (gcTime 0).
 */
export type ReportSection<T> =
  | { status: 'loading' }
  | { status: 'ready'; data: T }
  | { status: 'unavailable' }
  | { status: 'before_history' }
  | { status: 'error'; message: string }

export interface ReportRead<T> {
  section: ReportSection<T>
  /** A request for it is in flight. */
  loading: boolean
  /** Asks again. Does nothing while a request is already under way. */
  retry: () => void
}

type Load<T> = (orgSlug: string, branchSlug: string, from: string, to: string, signal?: AbortSignal) => Promise<T>

function useReport<T>(
  kind: ReportKind,
  load: Load<T>,
  orgSlug: string,
  branchSlug: string,
  range: DateRange,
  enabled = true,
): ReportRead<T> {
  return useReportQuery(
    ['reports', orgSlug, branchSlug, kind, range.from, range.to],
    (signal) => load(orgSlug, branchSlug, range.from, range.to, signal),
    enabled,
  )
}

/** One report read under `queryKey`, which must name everything the answer depends on: whose it is, which report, which range. */
export function useReportQuery<T>(
  queryKey: readonly string[],
  load: (signal: AbortSignal) => Promise<T>,
  enabled = true,
): ReportRead<T> {
  const query = useQuery({ queryKey, queryFn: ({ signal }) => load(signal), gcTime: 0, enabled })

  let section: ReportSection<T>
  if (query.isFetching && query.data === undefined) {
    // Asking, or asking again after a failure: either way there is nothing to show yet.
    section = { status: 'loading' }
  } else if (query.data !== undefined && !query.isError) {
    section = { status: 'ready', data: query.data }
  } else if (isApiError(query.error) && query.error.status === 404) {
    section = { status: 'unavailable' }
  } else if (isApiError(query.error) && query.error.code === RANGE_BEFORE_HISTORY) {
    section = { status: 'before_history' }
  } else if (query.isError) {
    section = { status: 'error', message: messageFor(query.error) }
  } else {
    section = { status: 'loading' }
  }

  return {
    section,
    loading: query.isFetching,
    retry: () => void query.refetch({ cancelRefetch: false }),
  }
}

export interface SorterReports {
  overview: ReportRead<OverviewReport>
  volume: ReportRead<VolumeReport>
  routing: ReportRead<RoutingReport>
  bins: ReportRead<BinVolumeReport>
  reliability: ReportRead<ReliabilityReport>
  /** Any one of them was answered 404: there are no reports for this sorter. */
  unavailable: boolean
  /** Any one of them was refused because the range starts before the organization's plan lets a report reach. */
  beforeHistory: boolean
}

/**
 * The five reports of one sorter over one range, each read on its own: one
 * that fails says so in its own section and leaves the other four alone.
 */
export function useSorterReports(orgSlug: string, branchSlug: string, range: DateRange, transits: boolean): SorterReports {
  const overview = useReport('overview', getOverviewReport, orgSlug, branchSlug, range)
  const volume = useReport('volume', getVolumeReport, orgSlug, branchSlug, range)
  // Transit routing is a plan feature: without it, the Routing report is not asked for at all.
  const routing = useReport('routing', getRoutingReport, orgSlug, branchSlug, range, transits)
  const bins = useReport('bins', getBinVolumeReport, orgSlug, branchSlug, range)
  const reliability = useReport('reliability', getReliabilityReport, orgSlug, branchSlug, range)
  const reads = [overview, volume, routing, bins, reliability]

  return {
    overview,
    volume,
    routing,
    bins,
    reliability,
    unavailable: reads.some((read) => read.section.status === 'unavailable'),
    beforeHistory: reads.some((read) => read.section.status === 'before_history'),
  }
}

/**
 * A sorter's Holds report over one range. Not one of the five above: it is
 * only for a plan that has it, so it is read only by the section that shows
 * it, and that section is only there when the plan has it. Whatever becomes
 * of it, the five are untouched.
 */
export function useHoldsReport(orgSlug: string, branchSlug: string, range: DateRange): ReportRead<HoldsReport> {
  return useReport('holds', getHoldsReport, orgSlug, branchSlug, range)
}
