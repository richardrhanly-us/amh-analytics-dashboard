/**
 * The app's addresses. An organization and a sorter appear in one by slug --
 * the only identifier the API gives the browser for either.
 */

export const ORGANIZATIONS_PATH = '/organizations'

/** The signed-in person's own account. Under no organization: it is about the person. */
export const ACCOUNT_PATH = '/account'

/**
 * Where an emailed password reset link leads. The one page that is shown signed out as well as signed in; the
 * link's token is in its fragment ("#token=..."), never in this path.
 */
export const RESET_PASSWORD_PATH = '/reset-password'

export function organizationPath(orgSlug: string): string {
  return `${ORGANIZATIONS_PATH}/${encodeURIComponent(orgSlug)}`
}

export function organizationReportsPath(orgSlug: string): string {
  return `${organizationPath(orgSlug)}/reports`
}

/** Who belongs to the organization, and with what role. For its owners and admins. */
export function organizationMembersPath(orgSlug: string): string {
  return `${organizationPath(orgSlug)}/members`
}

export function sorterPath(orgSlug: string, sorterSlug: string): string {
  return `${organizationPath(orgSlug)}/sorters/${encodeURIComponent(sorterSlug)}`
}

export function sorterReportsPath(orgSlug: string, sorterSlug: string): string {
  return `${sorterPath(orgSlug, sorterSlug)}/reports`
}
