import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { isApiError } from '../api/client.ts'
import {
  getSortViewValueReport,
  getSortViewValueSettings,
  putSortViewValueSettings,
  SORTVIEW_VALUE_SETTINGS_INVALID,
  type SortViewValueInput,
  type SortViewValueReport,
  type SortViewValueSettings,
} from '../api/sortviewValue.ts'
import { useAuth } from '../auth/useAuth.ts'
import { messageFor } from '../components/errorText.ts'
import { shouldRetry } from '../query/queryClient.ts'
import type { ReportRead } from './useSorterReports.ts'

/**
 * SortView Value is made of an organization's labor and cost figures, so it
 * is for its owners and admins. The API decides; the page that shows it only
 * keeps the app from asking on behalf of someone it would refuse
 * (settings/settingsText's canManageOrganization).
 *
 * THE REPORT IS THE API'S. Nothing here works a figure out: a save stores
 * what was entered, and the report is then asked for again.
 */

/**
 * What the page has to show once the API has answered -- the report, or one
 * of the answers that is not a report and is not worth asking again:
 *
 *   forbidden    403: this user may not see it after all. Nothing of it is shown.
 *   unreadable   the stored settings are malformed; saving them again repairs it
 */
export type ValueView = { kind: 'report'; report: SortViewValueReport } | { kind: 'forbidden' } | { kind: 'unreadable' }

const isUnreadable = (error: unknown) => isApiError(error) && error.code === SORTVIEW_VALUE_SETTINGS_INVALID
const isForbidden = (error: unknown) => isApiError(error) && error.status === 403

/**
 * The app's own rule for trying a failed read again, with one exception:
 * malformed stored settings are the same answer however often it is asked,
 * so that failure is reported at once.
 */
function useRetry() {
  const usual = useQueryClient().getDefaultOptions().queries?.retry
  return (failureCount: number, error: Error) => {
    if (isUnreadable(error)) {
      return false
    }
    if (typeof usual === 'function') {
      return usual(failureCount, error)
    }
    return usual === undefined ? shouldRetry(failureCount, error) : usual === true || (typeof usual === 'number' && failureCount < usual)
  }
}

const reportKey = (orgSlug: string) => ['sortview-value-report', orgSlug] as const
const settingsKey = (orgSlug: string) => ['sortview-value-settings', orgSlug] as const

/**
 * An organization's SortView Value report. It covers no range of days, so
 * its key is the organization alone. Nothing is kept once the page is left.
 */
export function useSortViewValueReport(orgSlug: string): ReportRead<ValueView> {
  const retry = useRetry()
  const query = useQuery({
    queryKey: reportKey(orgSlug),
    queryFn: ({ signal }) => getSortViewValueReport(orgSlug, signal),
    gcTime: 0,
    retry,
  })

  let section: ReportRead<ValueView>['section']
  if (query.data !== undefined && !query.isError) {
    // While it is asked for again after a save, what was last answered stays: the figures change when the new ones arrive.
    section = { status: 'ready', data: { kind: 'report', report: query.data } }
  } else if (query.isFetching) {
    section = { status: 'loading' }
  } else if (isForbidden(query.error)) {
    section = { status: 'ready', data: { kind: 'forbidden' } }
  } else if (isUnreadable(query.error)) {
    section = { status: 'ready', data: { kind: 'unreadable' } }
  } else if (query.isError) {
    section = { status: 'error', message: messageFor(query.error) }
  } else {
    section = { status: 'loading' }
  }

  return { section, loading: query.isFetching, retry: () => void query.refetch({ cancelRefetch: false }) }
}

/**
 * The stored settings, for the form that edits them.
 *
 *   unreadable   what is stored is malformed. The form is still offered, empty: saving replaces it.
 */
export type StoredValueSettings =
  | { status: 'loading' }
  | { status: 'ready'; data: SortViewValueSettings }
  | { status: 'unreadable' }
  | { status: 'forbidden' }
  | { status: 'error'; message: string }

/** The organization's stored settings. Asked for only while `wanted`: nothing is read until someone opens the form. */
export function useSortViewValueSettings(orgSlug: string, wanted: boolean): { stored: StoredValueSettings; retry: () => void } {
  const retry = useRetry()
  const query = useQuery({
    queryKey: settingsKey(orgSlug),
    queryFn: ({ signal }) => getSortViewValueSettings(orgSlug, signal),
    enabled: wanted,
    gcTime: 0,
    retry,
  })

  let stored: StoredValueSettings
  if (query.data !== undefined && !query.isError) {
    stored = { status: 'ready', data: query.data }
  } else if (query.isFetching || !query.isError) {
    stored = { status: 'loading' }
  } else if (isUnreadable(query.error)) {
    stored = { status: 'unreadable' }
  } else {
    stored = isForbidden(query.error) ? { status: 'forbidden' } : { status: 'error', message: messageFor(query.error) }
  }
  return { stored, retry: () => void query.refetch({ cancelRefetch: false }) }
}

/**
 * Saves the organization's settings. Nothing on the page changes until the
 * API has stored them; then what it stored is what the form shows, and the
 * report -- every figure of it -- is asked for again. The save is finished
 * when that answer has arrived, or failed: nothing is worked out here in the
 * meantime.
 */
export function useSaveSortViewValue(orgSlug: string) {
  const queryClient = useQueryClient()
  const { sessionExpired } = useAuth()
  return useMutation({
    mutationFn: (settings: SortViewValueInput) => putSortViewValueSettings(orgSlug, settings),
    onSuccess: async (saved) => {
      queryClient.setQueryData(settingsKey(orgSlug), saved)
      await queryClient.invalidateQueries({ queryKey: reportKey(orgSlug) })
    },
    // A 401 to a change means the session is gone, exactly as for a read.
    onError: (error) => {
      if (isApiError(error) && error.status === 401) {
        sessionExpired()
      }
    },
  })
}
