import { apiRequest, unexpectedResponse } from '../../../src/api/client.ts'
import { count, list, oneOf, orNull, record, text } from '../libraries/librariesApi.ts'

/**
 * A library's Collector installations and their enrollment codes (A2 item 5): platform administrators only. Every
 * response is checked against the shape below and only these fields are kept, whatever else a response carries.
 *
 * The raw enrollment code is returned by issueEnrollmentCode alone and the raw recovery token by issueRecoveryToken alone --
 * once. Neither is ever put in the query cache.
 *
 * Machine replacement (C6a, docs/c6a-replacement-machine-design.md 14.11): `replaces_installation_id`, the list's
 * `pending_replacement_id` and `replacement_enabled` decide only what the page offers. The server checks every rule again,
 * and refuses replacement entirely while its flag is off, whatever is shown here.
 */

export const STATUSES = ['provisioning', 'active', 'inactive', 'retired'] as const
export type InstallationStatus = (typeof STATUSES)[number]
export type Contract = 1 | 2
export type AddProblem = 'library_cancelled' | 'no_active_primary_branch' | 'collector_settings_not_ready'
export type StatusChange = 'inactive' | 'provisioning' | 'retired'

export interface Installation {
  id: number
  name: string
  branch: { slug: string; name: string; is_primary: boolean; status: string }
  status: InstallationStatus
  required_contract: Contract
  installed_at: string | null
  last_seen_at: string | null
  created_at: string
  pending_code_expires_at: string | null
  last_code_redeemed_at: string | null
  replaces_installation_id: number | null
}

export interface Installations {
  library: { slug: string; name: string; status: string }
  primary_branch: { slug: string; name: string; status: string } | null
  add_problems: AddProblem[]
  pending_replacement_id: number | null
  replacement_enabled: boolean
  installations: Installation[]
}

export interface Replacement {
  installation: Installation
  replaces_installation_id: number
}

export interface IssuedCode {
  installation: { id: number; name: string }
  required_contract: Contract
  enrollment_code: string
  expires_at: string
  ttl_minutes: number
  revoked_previous_count: number
}

export interface IssuedRecoveryToken {
  installation: { id: number; name: string }
  agent_token: string
  revoked_previous_count: number
}

export interface UnboundToken {
  id: number
  description: string | null
  branch_name: string | null
  created_at: string
  last_used_at: string | null
}

const ADD_PROBLEMS = ['library_cancelled', 'no_active_primary_branch', 'collector_settings_not_ready'] as const

function contract(value: unknown): Contract {
  if (value === 1 || value === 2) {
    return value
  }
  throw unexpectedResponse(200)
}

function flag(value: unknown): boolean {
  if (typeof value === 'boolean') {
    return value
  }
  throw unexpectedResponse(200)
}

function installation(value: unknown): Installation {
  const body = record(value)
  const branch = record(body.branch)
  return {
    id: count(body.id),
    name: text(body.name),
    branch: { slug: text(branch.slug), name: text(branch.name), is_primary: flag(branch.is_primary), status: text(branch.status) },
    status: oneOf(body.status, STATUSES),
    required_contract: contract(body.required_contract),
    installed_at: orNull(body.installed_at, text),
    last_seen_at: orNull(body.last_seen_at, text),
    created_at: text(body.created_at),
    pending_code_expires_at: orNull(body.pending_code_expires_at, text),
    last_code_redeemed_at: orNull(body.last_code_redeemed_at, text),
    replaces_installation_id: orNull(body.replaces_installation_id, count),
  }
}

function replacement(value: unknown): Replacement {
  const body = record(value)
  return { installation: installation(body.installation), replaces_installation_id: count(body.replaces_installation_id) }
}

const base = (orgSlug: string) => `/api/admin/libraries/${encodeURIComponent(orgSlug)}/installations`

/** GET /api/admin/libraries/{org_slug}/installations. An unknown library is the API's 404 `library_not_found`. */
export async function getInstallations(orgSlug: string, signal?: AbortSignal): Promise<Installations> {
  const body = record(await apiRequest(base(orgSlug), { signal }))
  const library = record(body.library)
  return {
    library: { slug: text(library.slug), name: text(library.name), status: text(library.status) },
    primary_branch: orNull(body.primary_branch, (value) => {
      const branch = record(value)
      return { slug: text(branch.slug), name: text(branch.name), status: text(branch.status) }
    }),
    add_problems: list(body.add_problems).map((problem) => oneOf(problem, ADD_PROBLEMS)),
    pending_replacement_id: orNull(body.pending_replacement_id, count),
    replacement_enabled: flag(body.replacement_enabled),
    installations: list(body.installations).map(installation),
  }
}

/** POST: a new installation at the library's active primary branch. */
export async function addInstallation(orgSlug: string, name: string, requiredContract: Contract): Promise<Installation> {
  return installation(await apiRequest(base(orgSlug), { method: 'POST', body: { name, required_contract: requiredContract } }))
}

/** PUT: the name alone. */
export async function renameInstallation(orgSlug: string, id: number, name: string): Promise<Installation> {
  return installation(await apiRequest(`${base(orgSlug)}/${id}`, { method: 'PUT', body: { name } }))
}

/** PUT .../status: deactivate, reactivate (back to provisioning), or retire. */
export async function changeInstallationStatus(orgSlug: string, id: number, status: StatusChange): Promise<Installation> {
  return installation(await apiRequest(`${base(orgSlug)}/${id}/status`, { method: 'PUT', body: { status } }))
}

/** POST .../enrollment-codes: a one-time code, returned once. The caller keeps it in component state and nowhere else. */
export async function issueEnrollmentCode(orgSlug: string, id: number): Promise<IssuedCode> {
  const body = record(await apiRequest(`${base(orgSlug)}/${id}/enrollment-codes`, { method: 'POST' }))
  const ref = record(body.installation)
  return {
    installation: { id: count(ref.id), name: text(ref.name) },
    required_contract: contract(body.required_contract),
    enrollment_code: text(body.enrollment_code),
    expires_at: text(body.expires_at),
    ttl_minutes: count(body.ttl_minutes),
    revoked_previous_count: count(body.revoked_previous_count),
  }
}

/** POST .../recovery-token: replaces the installation's agent token, returned once. The caller keeps it in component state and nowhere else. */
export async function issueRecoveryToken(orgSlug: string, id: number): Promise<IssuedRecoveryToken> {
  const body = record(await apiRequest(`${base(orgSlug)}/${id}/recovery-token`, { method: 'POST' }))
  const ref = record(body.installation)
  return {
    installation: { id: count(ref.id), name: text(ref.name) },
    agent_token: text(body.agent_token),
    revoked_previous_count: count(body.revoked_previous_count),
  }
}

/** GET .../unbound-tokens: the library's active legacy unbound tokens. Never a token or its hash. */
export async function getUnboundTokens(orgSlug: string, signal?: AbortSignal): Promise<UnboundToken[]> {
  const body = record(await apiRequest(`${base(orgSlug)}/unbound-tokens`, { signal }))
  return list(body.tokens).map((value) => {
    const token = record(value)
    return {
      id: count(token.id),
      description: orNull(token.description, text),
      branch_name: orNull(token.branch_name, text),
      created_at: text(token.created_at),
      last_used_at: orNull(token.last_used_at, text),
    }
  })
}

/** POST .../unbound-tokens/{id}/revoke: revokes one of them. */
export async function revokeUnboundToken(orgSlug: string, id: number): Promise<void> {
  await apiRequest(`${base(orgSlug)}/unbound-tokens/${id}/revoke`, { method: 'POST' })
}

/**
 * POST .../{source_id}/replacements: authorizes replacing the source's computer (C6a). A new Provisioning installation
 * that names the source; nothing about the source changes until the new installation's code is redeemed.
 * `confirmLostResponse` is required by the server for a Provisioning source (design 14.3a).
 */
export async function authorizeReplacement(
  orgSlug: string,
  sourceId: number,
  name: string,
  confirmLostResponse: boolean,
): Promise<Replacement> {
  return replacement(
    await apiRequest(`${base(orgSlug)}/${sourceId}/replacements`, {
      method: 'POST',
      body: { name, confirm_lost_response: confirmLostResponse },
    }),
  )
}

/** POST .../{id}/cancel-replacement: retires a pending replacement and revokes its unused codes. The source is untouched. */
export async function cancelReplacement(orgSlug: string, id: number): Promise<Replacement> {
  return replacement(await apiRequest(`${base(orgSlug)}/${id}/cancel-replacement`, { method: 'POST' }))
}
