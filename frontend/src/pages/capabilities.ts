import type { OrganizationDetail } from '../api/organizations.ts'

/**
 * What an organization's plan lets this app show, read from the features the
 * API lists for it -- by feature, never by the plan's name. The API decides
 * (it refuses what the plan does not include); this keeps the app from
 * showing, or asking for, what it would refuse.
 *
 * Each reading matches the API's own (services.entitlement_service) and,
 * like it, fails closed: a feature that is missing, switched off or holds a
 * value that is not a positive whole number gives the least, never the most.
 */

type WithEntitlements = Pick<OrganizationDetail, 'entitlements'>

/** How far back a report may reach when the plan does not say. */
export const DEFAULT_HISTORY_DAYS = 30

/** Transit routing: where a sorter sent its check-ins, and the routing settings. */
export function hasTransits(organization: WithEntitlements): boolean {
  return organization.entitlements.transits?.enabled === true
}

/** How many calendar days back, today included, a report may start. Null: no earliest date. */
export function historyDays(organization: WithEntitlements): number | null {
  const feature = organization.entitlements.history_days
  if (feature === undefined || !feature.enabled) {
    return DEFAULT_HISTORY_DAYS
  }
  const limit = feature.limit_value
  if (limit === null) {
    return null
  }
  return Number.isInteger(limit) && limit > 0 ? limit : DEFAULT_HISTORY_DAYS
}
