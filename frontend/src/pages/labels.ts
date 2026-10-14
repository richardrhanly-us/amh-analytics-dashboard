import type { SorterStatus, SorterSummary } from '../api/organizations.ts'

// The membership roles the API has today. A role this app does not know gets no label rather than a guess.
const ROLE_LABELS: Record<string, string> = {
  owner: 'Owner',
  admin: 'Admin',
  manager: 'Manager',
  viewer: 'Viewer',
}

export function roleLabel(role: string): string | null {
  return Object.hasOwn(ROLE_LABELS, role) ? ROLE_LABELS[role] : null
}

// What to say about a sorter that is not simply running. An active sorter needs no remark.
const SORTER_STATUS_LABELS: Record<SorterStatus, string | null> = {
  active: null,
  provisioning: 'Being set up',
  inactive: 'Inactive',
}

export function sorterStatusLabel(status: SorterStatus): string | null {
  return SORTER_STATUS_LABELS[status]
}

/**
 * Said of a sorter whose site has not had its first data yet. It is about the site's setup, not the machine: it goes
 * beside the machine's own status, never in its place.
 */
export const WAITING_FOR_FIRST_DATA = 'Waiting for first data'

export function isWaitingForFirstData(sorter: Pick<SorterSummary, 'onboarding_status'>): boolean {
  return sorter.onboarding_status === 'pending'
}
