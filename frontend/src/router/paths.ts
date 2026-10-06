/**
 * The app's addresses. An organization and a sorter appear in one by slug --
 * the only identifier the API gives the browser for either.
 */

export const ORGANIZATIONS_PATH = '/organizations'

export function organizationPath(orgSlug: string): string {
  return `${ORGANIZATIONS_PATH}/${encodeURIComponent(orgSlug)}`
}

export function organizationReportsPath(orgSlug: string): string {
  return `${organizationPath(orgSlug)}/reports`
}

export function sorterPath(orgSlug: string, sorterSlug: string): string {
  return `${organizationPath(orgSlug)}/sorters/${encodeURIComponent(sorterSlug)}`
}

export function sorterReportsPath(orgSlug: string, sorterSlug: string): string {
  return `${sorterPath(orgSlug, sorterSlug)}/reports`
}
