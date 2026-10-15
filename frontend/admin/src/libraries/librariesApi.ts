import { apiRequest, unexpectedResponse } from '../../../src/api/client.ts'

/**
 * The administration API's libraries (A2 item 2): read-only. Every response is checked against the shape below and
 * only these fields are kept, whatever else a response carries.
 */

export type PipelineState = 'ok' | 'degraded' | 'failed' | 'unknown'
export type OnboardingStatus = 'ready' | 'pending'
export type OperationalIdentity = 'assigned' | 'partial' | 'missing'

/** What a sorter site last reported, and when (an ISO instant), or `unknown` with no time. */
export interface Pipeline {
  state: PipelineState
  last_reported_at: string | null
}

export interface Subscription {
  plan_code: string
  plan_name: string
  status: string
}

export interface Branch {
  slug: string
  name: string
  status: string
}

/** The sorter site at a library's primary branch: the one state the list shows. */
export interface PrimarySorter {
  onboarding_status: OnboardingStatus
  pipeline: Pipeline
}

export interface LibrarySummary {
  slug: string
  name: string
  status: string
  primary_branch: Branch | null
  subscription: Subscription | null
  sorter_count: number
  primary_sorter: PrimarySorter | null
}

export interface LibraryList {
  timezone: string
  libraries: LibrarySummary[]
}

export interface SorterSite {
  slug: string
  name: string
  host_branch: { slug: string; name: string }
  status: string
  collector_count: number
  onboarding_status: OnboardingStatus
  pipeline: Pipeline
}

export interface LibraryDetail {
  timezone: string
  slug: string
  name: string
  status: string
  primary_branch: Branch | null
  operational_identity: OperationalIdentity
  subscription: Subscription | null
  sorters: SorterSite[]
}

type Json = Record<string, unknown>

// The checks below are shared with the sorter diagnostics client (A2 item 3).

export function record(value: unknown): Json {
  if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
    return value as Json
  }
  throw unexpectedResponse(200)
}

export function text(value: unknown): string {
  if (typeof value === 'string') {
    return value
  }
  throw unexpectedResponse(200)
}

export function count(value: unknown): number {
  if (typeof value === 'number' && Number.isInteger(value) && value >= 0) {
    return value
  }
  throw unexpectedResponse(200)
}

export function oneOf<T extends string>(value: unknown, allowed: readonly T[]): T {
  if (typeof value === 'string' && (allowed as readonly string[]).includes(value)) {
    return value as T
  }
  throw unexpectedResponse(200)
}

export function list(value: unknown): unknown[] {
  if (Array.isArray(value)) {
    return value
  }
  throw unexpectedResponse(200)
}

export function orNull<T>(value: unknown, read: (value: unknown) => T): T | null {
  return value === null ? null : read(value)
}

const PIPELINE_STATES = ['ok', 'degraded', 'failed', 'unknown'] as const
const ONBOARDING_STATUSES = ['ready', 'pending'] as const
const IDENTITIES = ['assigned', 'partial', 'missing'] as const

export function pipeline(value: unknown): Pipeline {
  const body = record(value)
  return { state: oneOf(body.state, PIPELINE_STATES), last_reported_at: orNull(body.last_reported_at, text) }
}

function subscription(value: unknown): Subscription {
  const body = record(value)
  return { plan_code: text(body.plan_code), plan_name: text(body.plan_name), status: text(body.status) }
}

function branch(value: unknown): Branch {
  const body = record(value)
  return { slug: text(body.slug), name: text(body.name), status: text(body.status) }
}

function summary(value: unknown): LibrarySummary {
  const body = record(value)
  return {
    slug: text(body.slug),
    name: text(body.name),
    status: text(body.status),
    primary_branch: orNull(body.primary_branch, branch),
    subscription: orNull(body.subscription, subscription),
    sorter_count: count(body.sorter_count),
    primary_sorter: orNull(body.primary_sorter, (sorter) => {
      const site = record(sorter)
      return { onboarding_status: oneOf(site.onboarding_status, ONBOARDING_STATUSES), pipeline: pipeline(site.pipeline) }
    }),
  }
}

function sorterSite(value: unknown): SorterSite {
  const body = record(value)
  const host = record(body.host_branch)
  return {
    slug: text(body.slug),
    name: text(body.name),
    host_branch: { slug: text(host.slug), name: text(host.name) },
    status: text(body.status),
    collector_count: count(body.collector_count),
    onboarding_status: oneOf(body.onboarding_status, ONBOARDING_STATUSES),
    pipeline: pipeline(body.pipeline),
  }
}

/** GET /api/admin/libraries */
export async function getLibraries(signal?: AbortSignal): Promise<LibraryList> {
  const body = record(await apiRequest('/api/admin/libraries', { signal }))
  return { timezone: text(body.timezone), libraries: list(body.libraries).map(summary) }
}

/** GET /api/admin/libraries/{org_slug}. A library that does not exist is the API's 404 `library_not_found`. */
export async function getLibrary(orgSlug: string, signal?: AbortSignal): Promise<LibraryDetail> {
  const body = record(await apiRequest(`/api/admin/libraries/${encodeURIComponent(orgSlug)}`, { signal }))
  return {
    timezone: text(body.timezone),
    slug: text(body.slug),
    name: text(body.name),
    status: text(body.status),
    primary_branch: orNull(body.primary_branch, branch),
    operational_identity: oneOf(body.operational_identity, IDENTITIES),
    subscription: orNull(body.subscription, subscription),
    sorters: list(body.sorters).map(sorterSite),
  }
}
