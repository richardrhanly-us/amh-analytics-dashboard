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
