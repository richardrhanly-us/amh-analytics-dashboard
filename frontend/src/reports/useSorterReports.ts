import { useQuery } from '@tanstack/react-query'

import { isApiError } from '../api/client.ts'
import { getPipelineStatus } from '../api/liveToday.ts'
import {
  getOverviewReport,
  getReliabilityReport,
  getRoutingReport,
  getVolumeReport,
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
  const query = useQuery({
    queryKey: ['reports', orgSlug, branchSlug, 'product-day'],
    queryFn: ({ signal }) => getPipelineStatus(orgSlug, branchSlug, signal),
    gcTime: 0,
  })
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
 *   error        it could not be loaded; `message` is safe to show
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
  | { status: 'error'; message: string }

export interface ReportRead<T> {
  section: ReportSection<T>
  /** A request for it is in flight. */
  loading: boolean
  /** Asks again. Does nothing while a request is already under way. */
  retry: () => void
}

type Load<T> = (orgSlug: string, branchSlug: string, from: string, to: string, signal?: AbortSignal) => Promise<T>

function useReport<T>(kind: ReportKind, load: Load<T>, orgSlug: string, branchSlug: string, range: DateRange): ReportRead<T> {
  const query = useQuery({
    queryKey: ['reports', orgSlug, branchSlug, kind, range.from, range.to],
    queryFn: ({ signal }) => load(orgSlug, branchSlug, range.from, range.to, signal),
    gcTime: 0,
  })

  let section: ReportSection<T>
  if (query.isFetching && query.data === undefined) {
    // Asking, or asking again after a failure: either way there is nothing to show yet.
    section = { status: 'loading' }
  } else if (query.data !== undefined && !query.isError) {
    section = { status: 'ready', data: query.data }
  } else if (isApiError(query.error) && query.error.status === 404) {
    section = { status: 'unavailable' }
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
  reliability: ReportRead<ReliabilityReport>
  /** Any one of them was answered 404: there are no reports for this sorter. */
  unavailable: boolean
}

/**
 * The four reports of one sorter over one range, each read on its own: one
 * that fails says so in its own section and leaves the other three alone.
 */
export function useSorterReports(orgSlug: string, branchSlug: string, range: DateRange): SorterReports {
  const overview = useReport('overview', getOverviewReport, orgSlug, branchSlug, range)
  const volume = useReport('volume', getVolumeReport, orgSlug, branchSlug, range)
  const routing = useReport('routing', getRoutingReport, orgSlug, branchSlug, range)
  const reliability = useReport('reliability', getReliabilityReport, orgSlug, branchSlug, range)

  return {
    overview,
    volume,
    routing,
    reliability,
    unavailable: [overview, volume, routing, reliability].some((read) => read.section.status === 'unavailable'),
  }
}
