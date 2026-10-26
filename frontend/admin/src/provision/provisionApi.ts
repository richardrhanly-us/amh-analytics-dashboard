import { apiRequest, unexpectedResponse } from '../../../src/api/client.ts'
import { list, oneOf, record, text } from '../libraries/librariesApi.ts'

/**
 * Provisioning a library, and assigning an existing library's operational identity (A2 item 7): platform
 * administrators only. Every response is checked against the shape below and only these fields are kept. No id of any
 * kind is sent or kept.
 */

export interface Plan {
  code: string
  name: string
}

export interface ProvisionRequest {
  name: string
  slug: string
  branch_name: string
  branch_slug: string
  plan_code: string
}

export interface Provisioned {
  library: { slug: string; name: string; status: 'active' }
  primary_branch: { slug: string; name: string; status: 'active' }
  subscription: { plan_code: string; plan_name: string; status: 'trial' }
  operational_identity: 'assigned'
}

const ACTIVE = ['active'] as const
const TRIAL = ['trial'] as const
const ASSIGNED = ['assigned'] as const

export const NAME_LIMIT = 120
export const SLUG_LIMIT = 63
export const SLUG_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/

/** Super Admin's slug rule, as a suggestion only: the server takes a slug exactly as sent, and never rewrites one. */
export function slugify(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/-{2,}/g, '-')
    .replace(/^-+|-+$/g, '')
}

/** GET /api/admin/plans: the active plans. */
export async function getPlans(signal?: AbortSignal): Promise<Plan[]> {
  const body = record(await apiRequest('/api/admin/plans', { signal }))
  return list(body.plans).map((value) => {
    const plan = record(value)
    return { code: text(plan.code), name: text(plan.name) }
  })
}

/** POST /api/admin/libraries. A refusal is the API's 422 `invalid_library` (its problems) or a 409. */
export async function provisionLibrary(request: ProvisionRequest): Promise<Provisioned> {
  const body = record(await apiRequest('/api/admin/libraries', { method: 'POST', body: request }))
  const library = record(body.library)
  const branch = record(body.primary_branch)
  const subscription = record(body.subscription)
  return {
    library: { slug: text(library.slug), name: text(library.name), status: oneOf(library.status, ACTIVE) },
    primary_branch: { slug: text(branch.slug), name: text(branch.name), status: oneOf(branch.status, ACTIVE) },
    subscription: {
      plan_code: text(subscription.plan_code),
      plan_name: text(subscription.plan_name),
      status: oneOf(subscription.status, TRIAL),
    },
    operational_identity: oneOf(body.operational_identity, ASSIGNED),
  }
}

export interface FirstOwnerRequest {
  email: string
  email_confirmation: string
  full_name: string
}

/**
 * POST /api/admin/libraries/{org_slug}/first-owner: makes this person the library's first owner, only while it has
 * none. 204 with no body. No password is sent: the owner sets their own with "Forgot password?" in the SortView app.
 */
export async function assignFirstOwner(orgSlug: string, request: FirstOwnerRequest): Promise<void> {
  await apiRequest(`/api/admin/libraries/${encodeURIComponent(orgSlug)}/first-owner`, { method: 'POST', body: request })
}

/** POST /api/admin/libraries/{org_slug}/operational-identity: the identity is assigned, and whether anything changed. */
export async function assignOperationalIdentity(orgSlug: string): Promise<{ operational_identity: 'assigned'; changed: boolean }> {
  const body = record(await apiRequest(`/api/admin/libraries/${encodeURIComponent(orgSlug)}/operational-identity`, { method: 'POST' }))
  const operationalIdentity = oneOf(body.operational_identity, ASSIGNED)
  if (typeof body.changed !== 'boolean') {
    throw unexpectedResponse(200)
  }
  return { operational_identity: operationalIdentity, changed: body.changed }
}
