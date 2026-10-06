import { useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'
import { useCallback, useEffect, useState } from 'react'

import { isApiError } from '../api/client.ts'
import {
  getCheckinCount,
  getCheckinsByDestination,
  getCheckinsByHour,
  getPipelineStatus,
  getRejectCount,
  getRejectsByReason,
  type CheckinCount,
  type CheckinsByDestination,
  type CheckinsByHour,
  type PipelineStatus,
  type RejectCount,
  type RejectsByReason,
} from '../api/liveToday.ts'
import { messageFor } from '../components/errorText.ts'
import { productDate, productHour } from '../time/productTime.ts'

/**
 * How often Live Today refreshes itself: 3 minutes, the interval the existing
 * dashboard uses. The collector uploads every 15 minutes, so asking more often
 * would mostly re-read the same numbers.
 */
export const REFRESH_INTERVAL_MS = 180_000

/**
 * One part of the dashboard.
 *
 *   loading  nothing to show yet
 *   ready    `data` is the last good answer; `stale` is true if a later refresh of it failed
 *   error    it could not be loaded at all; `message` is safe to show
 */
export type Section<T> =
  | { status: 'loading' }
  | { status: 'ready'; data: T; stale: boolean }
  | { status: 'error'; message: string }

function section<T>(query: UseQueryResult<T>): Section<T> {
  if (query.data !== undefined) {
    return { status: 'ready', data: query.data, stale: query.isError }
  }
  return query.isError ? { status: 'error', message: messageFor(query.error) } : { status: 'loading' }
}

export interface LiveToday {
  pipeline: Section<PipelineStatus>
  /** The product's time zone, calendar date (YYYY-MM-DD) and hour (0-23) as of `asOf`. Null until the pipeline status is known. */
  timeZone: string | null
  date: string | null
  currentHour: number | null
  /** When the pipeline status now shown was received (epoch milliseconds), or null. */
  asOf: number | null
  checkinCount: Section<CheckinCount>
  checkinsByHour: Section<CheckinsByHour>
  checkinsByDestination: Section<CheckinsByDestination>
  rejectCount: Section<RejectCount>
  rejectsByReason: Section<RejectsByReason>
  /** The API answered 404 for a branch the user can see: it has no live data to show. */
  unavailable: boolean
  /** A request is in flight. */
  refreshing: boolean
  paused: boolean
  setPaused: (paused: boolean) => void
  /** Refreshes everything. Does nothing while a refresh is already under way. */
  refresh: () => void
}

/**
 * Live Today's data for one branch.
 *
 * ORDER. Pipeline status is asked first, because it names the product's time
 * zone. "Today" is the calendar date in that zone at the moment that answer
 * arrived -- never the browser's date -- and only then are the five dated
 * reads made, all for that one date. If pipeline status cannot be read, no
 * date is guessed and no dated read is made.
 *
 * REFRESH. One timer, on pipeline status. Each time a new pipeline status
 * arrives the five dated reads are refreshed after it, so a refresh -- timed
 * or from the button -- is always the whole set, in the same order as the
 * first load. Pausing stops the timer and nothing else.
 *
 * MIDNIGHT. The date is worked out again from every new pipeline status. On
 * the first refresh after midnight in the product's zone it changes; the
 * dated reads are keyed by date, so they start clean for the new day.
 *
 * Nothing is kept once the page is left (gcTime 0): coming back, or changing
 * branch, starts from loading and never from another visit's numbers.
 */
export function useLiveToday(orgSlug: string, branchSlug: string): LiveToday {
  const queryClient = useQueryClient()
  const [paused, setPaused] = useState(false)

  const pipelineQuery = useQuery({
    queryKey: ['live-today', orgSlug, branchSlug, 'pipeline-status'],
    queryFn: ({ signal }) => getPipelineStatus(orgSlug, branchSlug, signal),
    refetchInterval: paused ? false : REFRESH_INTERVAL_MS,
    gcTime: 0,
  })

  const timeZone = pipelineQuery.data?.timezone ?? null
  const asOf = pipelineQuery.data === undefined ? null : pipelineQuery.dataUpdatedAt
  const date = timeZone === null || asOf === null ? null : productDate(new Date(asOf), timeZone)
  const currentHour = timeZone === null || asOf === null ? null : productHour(new Date(asOf), timeZone)

  // `date ?? ''` only keeps the types simple: with no date the query is disabled and its function never runs.
  const dated = <T>(endpoint: string, load: (org: string, branch: string, date: string, signal: AbortSignal) => Promise<T>) => ({
    queryKey: ['live-today', orgSlug, branchSlug, 'day', date, endpoint],
    queryFn: ({ signal }: { signal: AbortSignal }) => load(orgSlug, branchSlug, date ?? '', signal),
    enabled: date !== null,
    gcTime: 0,
  })
  const checkinCountQuery = useQuery(dated('checkins/count', getCheckinCount))
  const checkinsByHourQuery = useQuery(dated('checkins/by-hour', getCheckinsByHour))
  const checkinsByDestinationQuery = useQuery(dated('checkins/by-destination', getCheckinsByDestination))
  const rejectCountQuery = useQuery(dated('rejects/count', getRejectCount))
  const rejectsByReasonQuery = useQuery(dated('rejects/by-reason', getRejectsByReason))

  // A new pipeline status (`asOf` moved) is followed by the day's five reads. One already in flight -- the first
  // load, or a new date's -- is left alone rather than started again.
  useEffect(() => {
    if (date !== null) {
      void queryClient.refetchQueries(
        { queryKey: ['live-today', orgSlug, branchSlug, 'day', date], type: 'active' },
        { cancelRefetch: false },
      )
    }
  }, [queryClient, orgSlug, branchSlug, date, asOf])

  const queries = [
    pipelineQuery,
    checkinCountQuery,
    checkinsByHourQuery,
    checkinsByDestinationQuery,
    rejectCountQuery,
    rejectsByReasonQuery,
  ]
  const refreshing = queries.some((query) => query.isFetching)
  const refetchPipeline = pipelineQuery.refetch

  const refresh = useCallback(() => {
    // Pipeline status first; the effect above then refreshes the rest. `cancelRefetch: false` joins a request
    // that is already in flight instead of starting a second one.
    void refetchPipeline({ cancelRefetch: false })
  }, [refetchPipeline])

  return {
    pipeline: section(pipelineQuery),
    timeZone,
    date,
    currentHour,
    asOf,
    checkinCount: section(checkinCountQuery),
    checkinsByHour: section(checkinsByHourQuery),
    checkinsByDestination: section(checkinsByDestinationQuery),
    rejectCount: section(rejectCountQuery),
    rejectsByReason: section(rejectsByReasonQuery),
    unavailable: queries.some((query) => isApiError(query.error) && query.error.status === 404),
    refreshing,
    paused,
    setPaused,
    refresh,
  }
}
