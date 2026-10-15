import { apiRequest, unexpectedResponse } from '../../../src/api/client.ts'
import {
  count,
  oneOf,
  orNull,
  pipeline,
  record,
  text,
  type OnboardingStatus,
  type Pipeline,
} from '../libraries/librariesApi.ts'

/**
 * A sorter site's diagnostics (A2 item 3): read-only, platform administrators only. Every response is checked against
 * the shape below and only these fields are kept, whatever else a response carries. Codes are the API's own values.
 */

export type Contract = 'v2' | 'legacy' | 'unknown'

/** The site's latest Contract v2 heartbeat. */
export interface V2Heartbeat {
  last_heartbeat_at: string | null
  health_status: string | null
  last_error_class: string | null
  collector_schedule_status: string | null
  collector_last_run_at: string | null
  collector_next_run_at: string | null
  collector_run_duration_ms: number | null
  pending_outbox_count: number | null
  quarantined_count: number | null
  oldest_pending_event_at: string | null
  last_success_at: string | null
  watcher_last_active_at: string | null
}

/** The site's legacy report. */
export interface LegacyReport {
  run_status: string | null
  run_reported_at: string | null
  health_status: string | null
  health_reported_at: string | null
  pending_outbox_count: number | null
  quarantined_count: number | null
  checkins_rows: number | null
  rejects_rows: number | null
  uploaded_checkins_rows: number | null
  uploaded_rejects_rows: number | null
  checkins_bad_datetime_rows: number | null
  rejects_bad_datetime_rows: number | null
}

export interface SorterDiagnostics {
  timezone: string
  library: { slug: string; name: string }
  sorter: {
    slug: string
    name: string
    host_branch: { slug: string; name: string }
    status: string
    collector_count: number
    onboarding_status: OnboardingStatus
  }
  pipeline: Pipeline
  contract: Contract
  cutover_at: string | null
  /** Filled only for `v2`; its `status` is null when the site has no active ingest key. */
  v2: { status: V2Heartbeat | null } | null
  /** Filled only for `legacy`; its `status` is null when the site has no legacy report. */
  legacy: { status: LegacyReport | null } | null
}

const CONTRACTS = ['v2', 'legacy', 'unknown'] as const
const ONBOARDING_STATUSES = ['ready', 'pending'] as const

const optionalText = (value: unknown): string | null => orNull(value, text)
const optionalCount = (value: unknown): number | null => orNull(value, count)

function heartbeat(value: unknown): V2Heartbeat {
  const body = record(value)
  return {
    last_heartbeat_at: optionalText(body.last_heartbeat_at),
    health_status: optionalText(body.health_status),
    last_error_class: optionalText(body.last_error_class),
    collector_schedule_status: optionalText(body.collector_schedule_status),
    collector_last_run_at: optionalText(body.collector_last_run_at),
    collector_next_run_at: optionalText(body.collector_next_run_at),
    collector_run_duration_ms: optionalCount(body.collector_run_duration_ms),
    pending_outbox_count: optionalCount(body.pending_outbox_count),
    quarantined_count: optionalCount(body.quarantined_count),
    oldest_pending_event_at: optionalText(body.oldest_pending_event_at),
    last_success_at: optionalText(body.last_success_at),
    watcher_last_active_at: optionalText(body.watcher_last_active_at),
  }
}

function legacyReport(value: unknown): LegacyReport {
  const body = record(value)
  return {
    run_status: optionalText(body.run_status),
    run_reported_at: optionalText(body.run_reported_at),
    health_status: optionalText(body.health_status),
    health_reported_at: optionalText(body.health_reported_at),
    pending_outbox_count: optionalCount(body.pending_outbox_count),
    quarantined_count: optionalCount(body.quarantined_count),
    checkins_rows: optionalCount(body.checkins_rows),
    rejects_rows: optionalCount(body.rejects_rows),
    uploaded_checkins_rows: optionalCount(body.uploaded_checkins_rows),
    uploaded_rejects_rows: optionalCount(body.uploaded_rejects_rows),
    checkins_bad_datetime_rows: optionalCount(body.checkins_bad_datetime_rows),
    rejects_bad_datetime_rows: optionalCount(body.rejects_bad_datetime_rows),
  }
}

/**
 * GET /api/admin/libraries/{org_slug}/sorters/{sorter_slug}/diagnostics. An unknown library or sorter is the API's
 * 404 `sorter_not_found`.
 */
export async function getSorterDiagnostics(
  orgSlug: string,
  sorterSlug: string,
  signal?: AbortSignal,
): Promise<SorterDiagnostics> {
  const path = `/api/admin/libraries/${encodeURIComponent(orgSlug)}/sorters/${encodeURIComponent(sorterSlug)}/diagnostics`
  const body = record(await apiRequest(path, { signal }))
  const library = record(body.library)
  const sorter = record(body.sorter)
  const host = record(sorter.host_branch)
  const contract = oneOf(body.contract, CONTRACTS)
  const v2 = orNull(body.v2, (section) => ({ status: orNull(record(section).status, heartbeat) }))
  const legacy = orNull(body.legacy, (section) => ({ status: orNull(record(section).status, legacyReport) }))
  // Only the current era's section is ever filled: anything else is not the response this client was written for.
  if ((contract === 'v2') !== (v2 !== null) || (contract === 'legacy') !== (legacy !== null)) {
    throw unexpectedResponse(200)
  }
  return {
    timezone: text(body.timezone),
    library: { slug: text(library.slug), name: text(library.name) },
    sorter: {
      slug: text(sorter.slug),
      name: text(sorter.name),
      host_branch: { slug: text(host.slug), name: text(host.name) },
      status: text(sorter.status),
      collector_count: count(sorter.collector_count),
      onboarding_status: oneOf(sorter.onboarding_status, ONBOARDING_STATUSES),
    },
    pipeline: pipeline(body.pipeline),
    contract,
    cutover_at: optionalText(body.cutover_at),
    v2,
    legacy,
  }
}
