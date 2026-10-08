/** The area's name, wherever it is written: its link, its place in the breadcrumb and its notice. */
export const SETTINGS_NAME = 'Settings'

/** The sections of the area, by the name each is shown under. */
export const GENERAL_NAME = 'General'
export const BRANCHES_NAME = 'Branches & Sorters'
export const ROUTING_NAME = 'Routing'
export const ROUTING_NOT_AVAILABLE = 'Routing is not available for this organization.'
export const EFFICIENCY_NAME = 'Efficiency'

/** Said to someone who opens the area and is not one of the people it is for. Nothing else is shown to them. */
export const MANAGED_BY_OWNERS_AND_ADMINS = `${SETTINGS_NAME} are managed by this organization's owners and admins.`

/**
 * Whether to offer the area at all: to an organization's owners and admins,
 * the same people the API lets manage it. Only what is OFFERED -- every
 * request is decided by the API, whatever this says.
 */
export function canManageOrganization(role: string): boolean {
  return role === 'owner' || role === 'admin'
}
