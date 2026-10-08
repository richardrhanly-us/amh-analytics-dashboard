import type { FieldProblem } from '../api/account.ts'
import type { MemberActivity } from '../api/members.ts'
import { roleLabel } from '../pages/labels.ts'

/** The page's name, wherever it is written: its heading, its link and the browser tab. */
export const MEMBERS_PAGE_NAME = 'Users & Access'

/** Every role a member can hold, most authority first. The API has these four and no other. */
export const ROLES = ['owner', 'admin', 'manager', 'viewer'] as const

/**
 * The roles the signed-in person could give someone, for a list of choices:
 * an owner all four, an admin all but owner, anyone else none. Only what is
 * OFFERED -- the API decides, on every request, what is allowed.
 */
export function assignableRoles(actorRole: string): readonly string[] {
  if (actorRole === 'owner') {
    return ROLES
  }
  return actorRole === 'admin' ? ROLES.filter((role) => role !== 'owner') : []
}

/** Whether to offer the page at all. The API answers 403 to anyone else whatever this says. */
export function canManageMembers(actorRole: string): boolean {
  return assignableRoles(actorRole).length > 0
}

/** A role in words, or one neutral phrase for a role this app does not know. Never the API's own word for it. */
export function roleName(role: string | null): string {
  return (role === null ? null : roleLabel(role)) ?? 'another role'
}

/**
 * One recorded change, as a sentence. A kind of change this app does not
 * know, or one with a part missing, gets a plain sentence that claims
 * nothing -- never the API's name for the event.
 */
export function activitySentence(change: MemberActivity): string {
  const who = change.member_email ?? 'a member'
  switch (change.event_type) {
    case 'membership_added':
      return `Added ${who} as ${roleName(change.role)}`
    case 'membership_role_updated':
      return `Changed ${who} from ${roleName(change.previous_role)} to ${roleName(change.role)}`
    case 'membership_removed':
      return `Removed ${who} (was ${roleName(change.previous_role)})`
    default:
      return `Updated ${who}`
  }
}

// What to say beside a field the API refused. A word this app does not know gets one plain sentence.
const SENTENCES: Record<string, Record<string, string>> = {
  email: { invalid: 'Enter a valid email address.' },
  role: { invalid: 'Choose a role from the list.' },
}
const UNKNOWN = 'This is not valid. Check it and try again.'

/** The sentence for each refused field among `fields`, by field name. Fields the form does not have are left out. */
export function memberProblems<F extends string>(problems: readonly FieldProblem[], fields: readonly F[]): Partial<Record<F, string>> {
  const sentences: Partial<Record<F, string>> = {}
  for (const problem of problems) {
    const field = fields.find((candidate) => candidate === problem.field)
    if (field !== undefined && sentences[field] === undefined) {
      const known = SENTENCES[field]
      sentences[field] = known !== undefined && Object.hasOwn(known, problem.code) ? known[problem.code] : UNKNOWN
    }
  }
  return sentences
}
