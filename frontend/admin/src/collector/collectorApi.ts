import { apiRequest, isApiError } from '../../../src/api/client.ts'
import { count, list, oneOf, orNull, record, text } from '../libraries/librariesApi.ts'

/**
 * A library's collector configuration (A2 item 4): read and saved by platform administrators only. Every response is
 * checked against the shape below and only these fields are kept, whatever else a response carries.
 *
 * Saved to SortView. A Contract v2 Collector that is already installed keeps the rules and time zone it received at
 * enrollment: nothing here reaches it.
 */

export type EnrollmentProblem = 'timezone_not_configured' | 'timezone_invalid' | 'rules_not_reviewed'

export const ROUTING_LISTS = [
  'branch_services_names',
  'collection_services_names',
  'branch_services_da_patterns',
  'collection_services_da_patterns',
] as const
export type RoutingList = (typeof ROUTING_LISTS)[number]
export type RoutingLists = Record<RoutingList, string[]>

export interface LibraryRef {
  slug: string
  name: string
}

export interface CollectorSettings {
  library: LibraryRef
  collector_timezone: string | null
  rules_reviewed_at: string | null
  home_branch_label: string
  transit_destination_labels: string[]
  internal_routing_counts: Record<RoutingList, number>
  enrollment_problems: EnrollmentProblem[]
}

export interface InternalRouting extends RoutingLists {
  library: LibraryRef
}

/** One refused field, as the API names it: a field (or "<list>.<n>") and a fixed code. Never the value. */
export interface FieldProblem {
  field: string
  code: string
}

const ENROLLMENT_PROBLEMS = ['timezone_not_configured', 'timezone_invalid', 'rules_not_reviewed'] as const

function library(value: unknown): LibraryRef {
  const body = record(value)
  return { slug: text(body.slug), name: text(body.name) }
}

function strings(value: unknown): string[] {
  return list(value).map(text)
}

function routingLists(body: Record<string, unknown>): RoutingLists {
  return {
    branch_services_names: strings(body.branch_services_names),
    collection_services_names: strings(body.collection_services_names),
    branch_services_da_patterns: strings(body.branch_services_da_patterns),
    collection_services_da_patterns: strings(body.collection_services_da_patterns),
  }
}

function collectorSettings(value: unknown): CollectorSettings {
  const body = record(value)
  const counts = record(body.internal_routing_counts)
  return {
    library: library(body.library),
    collector_timezone: orNull(body.collector_timezone, text),
    rules_reviewed_at: orNull(body.rules_reviewed_at, text),
    home_branch_label: text(body.home_branch_label),
    transit_destination_labels: strings(body.transit_destination_labels),
    internal_routing_counts: {
      branch_services_names: count(counts.branch_services_names),
      collection_services_names: count(counts.collection_services_names),
      branch_services_da_patterns: count(counts.branch_services_da_patterns),
      collection_services_da_patterns: count(counts.collection_services_da_patterns),
    },
    enrollment_problems: list(body.enrollment_problems).map((problem) => oneOf(problem, ENROLLMENT_PROBLEMS)),
  }
}

function internalRouting(value: unknown): InternalRouting {
  const body = record(value)
  return { library: library(body.library), ...routingLists(body) }
}

const libraryPath = (orgSlug: string) => `/api/admin/libraries/${encodeURIComponent(orgSlug)}`

/** GET /api/admin/libraries/{org_slug}/collector-settings. An unknown library is the API's 404 `library_not_found`. */
export async function getCollectorSettings(orgSlug: string, signal?: AbortSignal): Promise<CollectorSettings> {
  return collectorSettings(await apiRequest(`${libraryPath(orgSlug)}/collector-settings`, { signal }))
}

/** PUT /api/admin/libraries/{org_slug}/collector-settings: the time zone, and the review -- which must be confirmed. */
export async function saveCollectorSettings(orgSlug: string, collectorTimezone: string): Promise<CollectorSettings> {
  const body = { collector_timezone: collectorTimezone, rules_reviewed: true }
  return collectorSettings(await apiRequest(`${libraryPath(orgSlug)}/collector-settings`, { method: 'PUT', body }))
}

/** GET /api/admin/libraries/{org_slug}/internal-routing */
export async function getInternalRouting(orgSlug: string, signal?: AbortSignal): Promise<InternalRouting> {
  return internalRouting(await apiRequest(`${libraryPath(orgSlug)}/internal-routing`, { signal }))
}

/** PUT /api/admin/libraries/{org_slug}/internal-routing: all four lists, replacing what is stored. */
export async function saveInternalRouting(orgSlug: string, lists: RoutingLists): Promise<InternalRouting> {
  return internalRouting(await apiRequest(`${libraryPath(orgSlug)}/internal-routing`, { method: 'PUT', body: lists }))
}

/** The fields a 422 with this `code` refused, or null if `error` is not that refusal. */
export function refusedFields(error: unknown, code: string): FieldProblem[] | null {
  if (!isApiError(error) || error.status !== 422 || error.code !== code) {
    return null
  }
  const body = error.body
  if (typeof body !== 'object' || body === null || !Array.isArray((body as { problems?: unknown }).problems)) {
    return null
  }
  return ((body as { problems: unknown[] }).problems).flatMap((problem) => {
    if (typeof problem !== 'object' || problem === null) {
      return []
    }
    const { field, code: why } = problem as { field?: unknown; code?: unknown }
    return typeof field === 'string' && typeof why === 'string' ? [{ field, code: why }] : []
  })
}
