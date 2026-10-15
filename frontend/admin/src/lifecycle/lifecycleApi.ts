import { apiRequest, unexpectedResponse } from '../../../src/api/client.ts'
import { count, oneOf, record, text } from '../libraries/librariesApi.ts'

/**
 * A library's lifecycle (A2 item 6): platform administrators only. Three explicit actions, no generic status write. Every
 * response is checked against the shape below and only these fields are kept. The offboarding confirmation is sent in
 * the one request that needs it and kept nowhere else.
 */

export const LIBRARY_STATUSES = ['active', 'trial', 'suspended', 'cancelled'] as const
export type LibraryStatus = (typeof LIBRARY_STATUSES)[number]

export interface LifecycleResult {
  library: { slug: string; name: string; status: LibraryStatus }
  previous_status: LibraryStatus
  changed: boolean
}

export interface OffboardCounts {
  agent_tokens_deactivated: number
  installations_retired: number
  enrollment_codes_revoked: number
  ingest_keys_retired: number
  sessions_revoked: number
}

export interface OffboardResult extends LifecycleResult {
  counts: OffboardCounts
}

function result(value: unknown): LifecycleResult {
  const body = record(value)
  const library = record(body.library)
  if (typeof body.changed !== 'boolean') {
    throw unexpectedResponse(200)
  }
  return {
    library: { slug: text(library.slug), name: text(library.name), status: oneOf(library.status, LIBRARY_STATUSES) },
    previous_status: oneOf(body.previous_status, LIBRARY_STATUSES),
    changed: body.changed,
  }
}

const base = (orgSlug: string) => `/api/admin/libraries/${encodeURIComponent(orgSlug)}/lifecycle`

/** POST .../suspend: active or trial -> suspended. Already suspended: `changed` false. */
export async function suspendLibrary(orgSlug: string): Promise<LifecycleResult> {
  return result(await apiRequest(`${base(orgSlug)}/suspend`, { method: 'POST' }))
}

/** POST .../reactivate: suspended -> active. Already active or trial: `changed` false. */
export async function reactivateLibrary(orgSlug: string): Promise<LifecycleResult> {
  return result(await apiRequest(`${base(orgSlug)}/reactivate`, { method: 'POST' }))
}

/** POST .../offboard: permanent. `confirmSlug` must be the library's slug; the server checks it again. */
export async function offboardLibrary(orgSlug: string, confirmSlug: string): Promise<OffboardResult> {
  const body = record(await apiRequest(`${base(orgSlug)}/offboard`, { method: 'POST', body: { confirm_slug: confirmSlug } }))
  const counts = record(body.counts)
  return {
    ...result(body),
    counts: {
      agent_tokens_deactivated: count(counts.agent_tokens_deactivated),
      installations_retired: count(counts.installations_retired),
      enrollment_codes_revoked: count(counts.enrollment_codes_revoked),
      ingest_keys_retired: count(counts.ingest_keys_retired),
      sessions_revoked: count(counts.sessions_revoked),
    },
  }
}
