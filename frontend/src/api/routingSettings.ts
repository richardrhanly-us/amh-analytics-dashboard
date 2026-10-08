import { apiRequest, unexpectedResponse } from './client.ts'
import { segment } from './liveToday.ts'

/**
 * An organization's routing: what its sorting machines call their own
 * branch, and the other places their items are routed to.
 *
 * A destination is its LABEL and whether it is enabled, and nothing else.
 * The API gives a destination no identifier of its own and takes none: what
 * a label matches is decided by the API, and nothing here works it out.
 *
 * These are the organization's own, read and replaced whole. Who may do
 * either is decided by the API on every request.
 */

/** One place items are routed to. `label` is as an administrator wrote it. */
export interface RoutingDestination {
  label: string
  enabled: boolean
}

/** `home_branch_label` may be an empty string: none is set. `destinations` are in the order they are shown in reports. */
export interface RoutingSettings {
  home_branch_label: string
  destinations: RoutingDestination[]
}

/** The 422 code of a replacement the API refused because of a value, with a list of the fields at fault. */
export const INVALID_ROUTING_SETTINGS = 'invalid_routing_settings'

function routingPath(orgSlug: string): string {
  return `/api/organizations/${segment(orgSlug)}/settings/routing`
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw unexpectedResponse(200)
  }
  return value as Record<string, unknown>
}

// Each parser returns only the fields it names and throws on anything that is not the documented shape: a
// response is used whole or not at all.

function parseDestination(value: unknown): RoutingDestination {
  const { label, enabled } = record(value)
  if (typeof label !== 'string' || label === '' || typeof enabled !== 'boolean') {
    throw unexpectedResponse(200)
  }
  return { label, enabled }
}

function parseRouting(value: unknown): RoutingSettings {
  const { home_branch_label, destinations } = record(record(value).routing)
  if (typeof home_branch_label !== 'string' || !Array.isArray(destinations)) {
    throw unexpectedResponse(200)
  }
  return { home_branch_label, destinations: destinations.map(parseDestination) }
}

/** GET the organization's stored routing. For its owners and admins: anyone else is answered 403. */
export async function getRoutingSettings(orgSlug: string, signal?: AbortSignal): Promise<RoutingSettings> {
  return parseRouting(await apiRequest(routingPath(orgSlug), { signal }))
}

/**
 * PUT the whole of the organization's routing, replacing what is stored, and answer with what is then stored --
 * the API trims a label, so that can differ from what was sent. Only a label and whether it is enabled are sent
 * for a destination, whatever else the caller's objects hold.
 */
export async function putRoutingSettings(orgSlug: string, routing: RoutingSettings): Promise<RoutingSettings> {
  const body = {
    routing: {
      home_branch_label: routing.home_branch_label,
      destinations: routing.destinations.map((destination) => ({ label: destination.label, enabled: destination.enabled })),
    },
  }
  return parseRouting(await apiRequest(routingPath(orgSlug), { method: 'PUT', body }))
}
