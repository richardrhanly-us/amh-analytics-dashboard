import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { isApiError } from '../api/client.ts'
import {
  EFFICIENCY_SETTINGS_INVALID,
  getEfficiencyReport,
  getOrganizationEfficiencySettings,
  getSorterEfficiencySettings,
  putOrganizationEfficiencySettings,
  putSorterEfficiencySettings,
  type EfficiencyReport,
  type OrganizationEfficiencyInput,
  type OrganizationEfficiencySettings,
  type SorterEfficiencyInput,
  type SorterEfficiencySettings,
} from '../api/efficiency.ts'
import { useAuth } from '../auth/useAuth.ts'
import { messageFor } from '../components/errorText.ts'
import { shouldRetry } from '../query/queryClient.ts'
import type { DateRange } from './dateRange.ts'
import type { ReportRead } from './useSorterReports.ts'

/**
 * Efficiency is made of an organization's labor and cost figures, so it is
 * for its owners and admins. The API decides; this only keeps the app from
 * asking on behalf of someone it would refuse.
 */
const EFFICIENCY_ROLES: readonly string[] = ['owner', 'admin']

export function canSeeEfficiency(role: string): boolean {
  return EFFICIENCY_ROLES.includes(role)
}

/**
 * What the Efficiency section has to show once the API has answered -- the
 * report, or one of the answers that is not a report and is not worth
 * asking again:
 *
 *   forbidden          403: this user may not see it after all. Nothing of it is shown.
 *   not_available      404: there is no such report for this sorter
 *   unreadable         the stored assumptions are malformed; saving them again repairs it
 */
export type EfficiencyView =
  | { kind: 'report'; report: EfficiencyReport }
  | { kind: 'forbidden' }
  | { kind: 'not_available' }
  | { kind: 'unreadable' }

const isUnreadable = (error: unknown) => isApiError(error) && error.code === EFFICIENCY_SETTINGS_INVALID
const isForbidden = (error: unknown) => isApiError(error) && error.status === 403

/**
 * The app's own rule for trying a failed read again, with one exception:
 * malformed stored assumptions are the same answer however often it is
 * asked, so that failure is reported at once.
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

const reportKey = (orgSlug: string, branchSlug: string) => ['efficiency-report', orgSlug, branchSlug] as const
const organizationKey = (orgSlug: string) => ['efficiency-settings', 'organization', orgSlug] as const
const sorterKey = (orgSlug: string, branchSlug: string) => ['efficiency-settings', 'sorter', orgSlug, branchSlug] as const

/**
 * One sorter's Efficiency report for one range, read on its own: whatever
 * happens to it changes nothing in the sorter's other reports. Like them it
 * keeps no "last good answer" across ranges or sorters.
 */
export function useEfficiencyReport(orgSlug: string, branchSlug: string, range: DateRange): ReportRead<EfficiencyView> {
  const retry = useRetry()
  const query = useQuery({
    queryKey: [...reportKey(orgSlug, branchSlug), range.from, range.to],
    queryFn: ({ signal }) => getEfficiencyReport(orgSlug, branchSlug, range.from, range.to, signal),
    gcTime: 0,
    retry,
  })

  let section: ReportRead<EfficiencyView>['section']
  if (query.data !== undefined && !query.isError) {
    section = { status: 'ready', data: { kind: 'report', report: query.data } }
  } else if (query.isFetching) {
    section = { status: 'loading' }
  } else if (isForbidden(query.error)) {
    section = { status: 'ready', data: { kind: 'forbidden' } }
  } else if (isApiError(query.error) && query.error.status === 404) {
    section = { status: 'ready', data: { kind: 'not_available' } }
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
 * Stored assumptions, for the form that edits them.
 *
 *   unreadable   what is stored is malformed. The form is still offered, empty: saving replaces it.
 */
export type StoredAssumptions<T> =
  | { status: 'loading' }
  | { status: 'ready'; data: T }
  | { status: 'unreadable' }
  | { status: 'forbidden' }
  | { status: 'error'; message: string }

function stored<T>(query: { data: T | undefined; error: unknown; isError: boolean; isFetching: boolean }): StoredAssumptions<T> {
  if (query.data !== undefined && !query.isError) {
    return { status: 'ready', data: query.data }
  }
  if (query.isFetching || !query.isError) {
    return { status: 'loading' }
  }
  if (isUnreadable(query.error)) {
    return { status: 'unreadable' }
  }
  return isForbidden(query.error) ? { status: 'forbidden' } : { status: 'error', message: messageFor(query.error) }
}

/** The organization's defaults. Asked for only while `wanted`: nothing is read until someone opens the form. */
export function useOrganizationAssumptions(orgSlug: string, wanted: boolean) {
  const retry = useRetry()
  const query = useQuery({
    queryKey: organizationKey(orgSlug),
    queryFn: ({ signal }) => getOrganizationEfficiencySettings(orgSlug, signal),
    enabled: wanted,
    gcTime: 0,
    retry,
  })
  return { stored: stored<OrganizationEfficiencySettings>(query), retry: () => void query.refetch({ cancelRefetch: false }) }
}

/** One sorter's own assumptions, beside the defaults it would inherit. */
export function useSorterAssumptions(orgSlug: string, branchSlug: string, wanted: boolean) {
  const retry = useRetry()
  const query = useQuery({
    queryKey: sorterKey(orgSlug, branchSlug),
    queryFn: ({ signal }) => getSorterEfficiencySettings(orgSlug, branchSlug, signal),
    enabled: wanted,
    gcTime: 0,
    retry,
  })
  return { stored: stored<SorterEfficiencySettings>(query), retry: () => void query.refetch({ cancelRefetch: false }) }
}

/** A 401 to a change means the session is gone, exactly as for a read. */
function useSessionEnd() {
  const { sessionExpired } = useAuth()
  return (error: unknown) => {
    if (isApiError(error) && error.status === 401) {
      sessionExpired()
    }
  }
}

/**
 * Saves the organization's defaults. Nothing on the page changes until the
 * API has stored them; then what it stored is what the form shows, and the
 * figures that depend on it -- every sorter's own view of the defaults, and
 * every Efficiency report of the organization -- are asked for again.
 */
export function useSaveOrganizationAssumptions(orgSlug: string) {
  const queryClient = useQueryClient()
  const sessionEnd = useSessionEnd()
  return useMutation({
    mutationFn: (settings: OrganizationEfficiencyInput) => putOrganizationEfficiencySettings(orgSlug, settings),
    onSuccess: (saved) => {
      queryClient.setQueryData(organizationKey(orgSlug), saved)
      void queryClient.invalidateQueries({ queryKey: ['efficiency-settings', 'sorter', orgSlug] })
      void queryClient.invalidateQueries({ queryKey: ['efficiency-report', orgSlug] })
    },
    onError: sessionEnd,
  })
}

/** Saves one sorter's own assumptions, then asks for that sorter's Efficiency report again. */
export function useSaveSorterAssumptions(orgSlug: string, branchSlug: string) {
  const queryClient = useQueryClient()
  const sessionEnd = useSessionEnd()
  return useMutation({
    mutationFn: (settings: SorterEfficiencyInput) => putSorterEfficiencySettings(orgSlug, branchSlug, settings),
    onSuccess: (saved) => {
      queryClient.setQueryData(sorterKey(orgSlug, branchSlug), saved)
      void queryClient.invalidateQueries({ queryKey: reportKey(orgSlug, branchSlug) })
    },
    onError: sessionEnd,
  })
}
