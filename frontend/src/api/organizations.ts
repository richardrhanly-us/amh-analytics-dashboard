import { ApiError, apiRequest, unexpectedResponse } from './client.ts'

/**
 * The organizations and branches the signed-in user may reach, exactly as the
 * API describes them. Both are identified by slug only: the API returns no
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
  /** The organization's active branches, primary first. */
  branches: BranchSummary[]
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
  const { branches, subscription, entitlements } = record(value)
  return {
    ...parseSummary(value),
    branches: list(branches, parseBranch),
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
