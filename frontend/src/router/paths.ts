/**
 * The app's addresses. An organization and a branch appear in one by slug --
 * the only identifier the API gives the browser for either.
 */

export const ORGANIZATIONS_PATH = '/organizations'

export function organizationPath(orgSlug: string): string {
  return `${ORGANIZATIONS_PATH}/${encodeURIComponent(orgSlug)}`
}

export function branchPath(orgSlug: string, branchSlug: string): string {
  return `${organizationPath(orgSlug)}/branches/${encodeURIComponent(branchSlug)}`
}
