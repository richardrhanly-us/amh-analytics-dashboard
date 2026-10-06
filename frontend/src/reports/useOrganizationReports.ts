import { isApiError } from '../api/client.ts'
import { getPipelineStatus } from '../api/liveToday.ts'
import {
  getOrganizationOverviewReport,
  getOrganizationReliabilityReport,
  getOrganizationRoutingNetworkReport,
  type OrganizationOverviewReport,
  type OrganizationReliabilityReport,
  type OrganizationReportKind,
  type RoutingNetworkReport,
} from '../api/organizationReports.ts'
import type { DateRange } from './dateRange.ts'
import { useProductDayFrom, useReportQuery, type ProductDay, type ReportRead } from './useSorterReports.ts'

/**
 * The product's calendar for an organization: its time zone, and which date
 * is today there.
 *
 * The product has one zone, and the API names it in a sorter's pipeline
 * status -- the read a sorter's own reports take it from. So it is read from
 * the organization's sorters, in order, by host branch: the first one that
 * answers says what day it is for all of them. A sorter with no data to read
 * yet answers 404 and the next is asked; if every one does, the calendar is
 * `unavailable`. The browser's own zone is never used.
 *
 * `branchSlugs` must not be empty: an organization with no sorters has
 * nothing to report on, and nothing is asked for it.
 */
export function useOrganizationProductDay(orgSlug: string, branchSlugs: readonly string[]): { day: ProductDay; retry: () => void } {
  return useProductDayFrom(['organization-reports', orgSlug, 'product-day', ...branchSlugs], async (signal) => {
    let unanswered: unknown
    for (const branchSlug of branchSlugs) {
      try {
        return await getPipelineStatus(orgSlug, branchSlug, signal)
      } catch (error) {
        if (!isApiError(error) || error.status !== 404) {
          throw error
        }
        unanswered = error
      }
    }
    throw unanswered
  })
}

type Load<T> = (orgSlug: string, from: string, to: string, signal?: AbortSignal) => Promise<T>

/** One organization report. Its key names the organization, the report and the range: a change in any is a new question. */
function useReport<T>(kind: OrganizationReportKind, load: Load<T>, orgSlug: string, range: DateRange): ReportRead<T> {
  return useReportQuery(['organization-reports', orgSlug, kind, range.from, range.to], (signal) =>
    load(orgSlug, range.from, range.to, signal),
  )
}

export interface OrganizationReports {
  overview: ReportRead<OrganizationOverviewReport>
  routingNetwork: ReportRead<RoutingNetworkReport>
  reliability: ReportRead<OrganizationReliabilityReport>
  /** Any one of them was answered 404: the organization is not there for this user to see. */
  unavailable: boolean
}

/**
 * The three reports of one organization over one range, each read on its
 * own: one that fails says so in its own section and leaves the others alone.
 */
export function useOrganizationReports(orgSlug: string, range: DateRange): OrganizationReports {
  const overview = useReport('overview', getOrganizationOverviewReport, orgSlug, range)
  const routingNetwork = useReport('routing-network', getOrganizationRoutingNetworkReport, orgSlug, range)
  const reliability = useReport('reliability', getOrganizationReliabilityReport, orgSlug, range)

  return {
    overview,
    routingNetwork,
    reliability,
    unavailable: [overview, routingNetwork, reliability].some((read) => read.section.status === 'unavailable'),
  }
}
