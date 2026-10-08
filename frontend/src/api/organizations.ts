import { ApiError, apiRequest, unexpectedResponse } from './client.ts'

/**
 * The organizations, their sorting machines and their branches that the
 * signed-in user may reach, exactly as the API describes them. Each is
 * identified by slug only: the API returns no
 * database or operational id, and nothing here invents one.
 *
 * The API leaves out anything the user cannot reach, so a response is the
 * whole truth about access: this module adds no organization, branch or
 * permission of its own.
 */

/**
 * "read_only" is a suspended organization: its data can still be read. A
 * blocked organization is never returned, so any other value is not a valid
 * response.
 */
export type AccessMode = 'full' | 'read_only'

export interface OrganizationSummary {
  slug: string
  name: string
  /** The user's membership role, as the API sends it ("owner", "admin", "manager", "viewer"). */
  role: string
  access_mode: AccessMode
}

export interface BranchSummary {
  slug: string
  name: string
  is_primary: boolean
}

/** "retired" is never returned: a retired machine is not listed at all. */
export const SORTER_STATUSES = ['active', 'provisioning', 'inactive'] as const
export type SorterStatus = (typeof SORTER_STATUSES)[number]

/**
 * One sorting machine the organization runs SortView on -- a registered
 * installation, never a branch that merely exists or a place items are routed
 * to. More exactly it is a sorter SITE: the API gives one entry per host
 * branch, because that is the finest scope its figures can be separated by.
 */
export interface SorterSummary {
  /** Identifies the sorter within its organization, and is what its address is built from. */
  slug: string
  /** The machine's registered name. */
  name: string
  /** Where the machine is. Its dashboard's reads are addressed by this branch's slug. */
  host_branch: { slug: string; name: string }
  status: SorterStatus
  /** How many collectors can report for the site. Above one, their figures are combined and inseparable. */
  collector_count: number
}

export interface SubscriptionSummary {
  plan_code: string
  plan_name: string
  status: string
}

export interface FeatureEntitlement {
  enabled: boolean
  limit_value: number | null
}

export interface OrganizationDetail extends OrganizationSummary {
  /** The organization's active branches, primary first. Locations: not every branch has a sorter. */
  branches: BranchSummary[]
  /** The organization's sorting machines. No two share a slug, and no two share a host branch. */
  sorters: SorterSummary[]
  subscription: SubscriptionSummary | null
  /** Keyed by feature key. */
  entitlements: Record<string, FeatureEntitlement>
}

// Each parser returns only the fields it names and throws on anything that is not the documented shape: a
// response is used whole or not at all.

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw unexpectedResponse(200)
  }
  return value as Record<string, unknown>
}

function text(value: unknown): string {
  if (typeof value !== 'string') {
    throw unexpectedResponse(200)
  }
  return value
}

function slug(value: unknown): string {
  // A slug becomes part of a URL. An empty one could not be linked to.
  if (text(value) === '') {
    throw unexpectedResponse(200)
  }
  return value as string
}

function list<T>(value: unknown, parseItem: (item: unknown) => T): T[] {
  if (!Array.isArray(value)) {
    throw unexpectedResponse(200)
  }
  return value.map(parseItem)
}

function parseSummary(value: unknown): OrganizationSummary {
  const { slug: organizationSlug, name, role, access_mode } = record(value)
  if (access_mode !== 'full' && access_mode !== 'read_only') {
    throw unexpectedResponse(200)
  }
  return { slug: slug(organizationSlug), name: text(name), role: text(role), access_mode }
}

function parseBranch(value: unknown): BranchSummary {
  const { slug: branchSlug, name, is_primary } = record(value)
  if (typeof is_primary !== 'boolean') {
    throw unexpectedResponse(200)
  }
  return { slug: slug(branchSlug), name: text(name), is_primary }
}

/** A name a person will read and follow as a link: it has to say something. */
function displayName(value: unknown): string {
  if (text(value).trim() === '') {
    throw unexpectedResponse(200)
  }
  return value as string
}

/** Which sorter an entry is about: its slug, its name and where it is. */
export function parseSorterIdentity(value: unknown): Pick<SorterSummary, 'slug' | 'name' | 'host_branch'> {
  const { slug: sorterSlug, name, host_branch } = record(value)
  const host = record(host_branch)
  return {
    slug: slug(sorterSlug),
    name: displayName(name),
    host_branch: { slug: slug(host.slug), name: displayName(host.name) },
  }
}

export function parseSorter(value: unknown): SorterSummary {
  const { status, collector_count } = record(value)
  if (
    !SORTER_STATUSES.includes(status as SorterStatus) ||
    typeof collector_count !== 'number' ||
    !Number.isInteger(collector_count) ||
    collector_count < 0
  ) {
    throw unexpectedResponse(200)
  }
  return { ...parseSorterIdentity(value), status: status as SorterStatus, collector_count }
}

/**
 * The sorters of one organization. Two with one slug could not be told apart
 * in an address, and two at one host branch would be two dashboards of the
 * very same figures: neither is a list this app can show truthfully.
 */
function parseSorters(value: unknown): SorterSummary[] {
  const sorters = list(value, parseSorter)
  const distinct = (keys: string[]) => new Set(keys).size === keys.length
  if (!distinct(sorters.map((sorter) => sorter.slug)) || !distinct(sorters.map((sorter) => sorter.host_branch.slug))) {
    throw unexpectedResponse(200)
  }
  return sorters
}

function parseSubscription(value: unknown): SubscriptionSummary | null {
  if (value === null) {
    return null
  }
  const { plan_code, plan_name, status } = record(value)
  return { plan_code: text(plan_code), plan_name: text(plan_name), status: text(status) }
}

function parseEntitlement(value: unknown): FeatureEntitlement {
  const { enabled, limit_value } = record(value)
  if (typeof enabled !== 'boolean' || (limit_value !== null && typeof limit_value !== 'number')) {
    throw unexpectedResponse(200)
  }
  return { enabled, limit_value }
}

function parseDetail(value: unknown): OrganizationDetail {
  const { branches, sorters, subscription, entitlements } = record(value)
  return {
    ...parseSummary(value),
    branches: list(branches, parseBranch),
    sorters: parseSorters(sorters),
    subscription: parseSubscription(subscription),
    entitlements: Object.fromEntries(
      Object.entries(record(entitlements)).map(([featureKey, feature]) => [featureKey, parseEntitlement(feature)]),
    ),
  }
}

/** GET /api/organizations. The organizations the user is a member of and may see, ordered by name. */
export async function listOrganizations(signal?: AbortSignal): Promise<OrganizationSummary[]> {
  return list(await apiRequest('/api/organizations', { signal }), parseSummary)
}

/**
 * What the organization the pages are under is kept as, in the session's query cache. Whoever changes something
 * the organization's answer depends on -- the signed-in person's own role in it -- asks for it again by this.
 */
export function organizationKey(orgSlug: string) {
  return ['organization', orgSlug] as const
}

/**
 * GET /api/organizations/{org_slug}. Rejects with a 404 ApiError for an
 * organization that does not exist or that the user cannot see -- the API
 * answers both identically.
 */
export async function getOrganization(orgSlug: string, signal?: AbortSignal): Promise<OrganizationDetail> {
  // "." and ".." are path navigation to a browser, not a name: they would turn this into a request for a
  // different endpoint. No organization has such a slug.
  if (orgSlug === '' || orgSlug === '.' || orgSlug === '..') {
    throw new ApiError(404, 'organization_not_found', 'Organization not found.')
  }
  const detail = parseDetail(await apiRequest(`/api/organizations/${encodeURIComponent(orgSlug)}`, { signal }))
  if (detail.slug !== orgSlug) {
    // An answer about some other organization is not an answer to this question.
    throw unexpectedResponse(200)
  }
  return detail
}
