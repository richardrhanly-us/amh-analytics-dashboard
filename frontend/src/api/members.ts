import { apiRequest, unexpectedResponse } from './client.ts'
import { segment } from './liveToday.ts'

/**
 * An organization's members, and the changes that can be made to them.
 *
 * A member is named by EMAIL, and an email goes in a request body, never in an
 * address. The API gives no number for a person or for their place in an
 * organization, and nothing here has a field for one.
 *
 * There is no password anywhere in this module. Adding someone sends their
 * email, their name and a role; the API decides the rest, and answers the same
 * way whether or not that email already had a SortView account.
 *
 * Who may do any of this is decided by the API on every request. Nothing here
 * decides it.
 */

/** One active member, exactly as the API describes them. `full_name` may be an empty string. */
export interface Member {
  email: string
  full_name: string
  /** As the API sends it: "owner", "admin", "manager" or "viewer". */
  role: string
  /** This row is the signed-in person's own. */
  is_self: boolean
  /** False when the person's whole SortView account has been switched off. Not something an organization changes. */
  account_active: boolean
}

/** One change to the organization's members. */
export interface MemberActivity {
  /** An instant in UTC ("2026-10-05T18:50:00Z"). */
  occurred_at: string
  event_type: string
  member_email: string | null
  actor_email: string | null
  previous_role: string | null
  role: string | null
}

/** What is sent to add someone. There is no secret among it: none exists to send. */
export interface NewMember {
  email: string
  full_name: string
  role: string
}

/** The 422 code of a write the API refused because of a value, with a list of the fields at fault. */
export const INVALID_MEMBER = 'invalid_member'

// An instant written in UTC, to the second or finer: what the API promises.
const UTC_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|\+00:00)$/

function membersPath(orgSlug: string): string {
  return `/api/organizations/${segment(orgSlug)}/members`
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw unexpectedResponse(200)
  }
  return value as Record<string, unknown>
}

/** The list under `key` of an answer that is an object holding exactly that list. */
function listUnder(value: unknown, key: string): unknown[] {
  const list = record(value)[key]
  if (!Array.isArray(list)) {
    throw unexpectedResponse(200)
  }
  return list
}

const text = (value: unknown): value is string => typeof value === 'string' && value !== ''
const textOrNull = (value: unknown): value is string | null => value === null || text(value)

// Each parser returns only the fields it names and throws on anything that is not the documented shape: a
// response is used whole or not at all.

function parseMember(value: unknown): Member {
  const { email, full_name, role, is_self, account_active } = record(value)
  if (!text(email) || typeof full_name !== 'string' || !text(role) || typeof is_self !== 'boolean' || typeof account_active !== 'boolean') {
    throw unexpectedResponse(200)
  }
  return { email, full_name, role, is_self, account_active }
}

function parseActivity(value: unknown): MemberActivity {
  const { occurred_at, event_type, member_email, actor_email, previous_role, role } = record(value)
  if (
    typeof occurred_at !== 'string' ||
    !UTC_INSTANT.test(occurred_at) ||
    Number.isNaN(new Date(occurred_at).getTime()) ||
    !text(event_type) ||
    !textOrNull(member_email) ||
    !textOrNull(actor_email) ||
    !textOrNull(previous_role) ||
    !textOrNull(role)
  ) {
    throw unexpectedResponse(200)
  }
  return { occurred_at, event_type, member_email, actor_email, previous_role, role }
}

/** GET the organization's active members, in the API's order. For its owners and admins: anyone else is answered 403. */
export async function getMembers(orgSlug: string, signal?: AbortSignal): Promise<Member[]> {
  return listUnder(await apiRequest(membersPath(orgSlug), { signal }), 'members').map(parseMember)
}

/** GET the most recent changes to the organization's members, newest first. */
export async function getMemberActivity(orgSlug: string, signal?: AbortSignal): Promise<MemberActivity[]> {
  return listUnder(await apiRequest(`${membersPath(orgSlug)}/activity`, { signal }), 'activity').map(parseActivity)
}

/**
 * POST a new member -- or someone who was removed, added back. Resolves when the API has done it, and learns
 * nothing else: the answer does not say whether the email already had an account.
 */
export async function addMember(orgSlug: string, member: NewMember): Promise<void> {
  await apiRequest(membersPath(orgSlug), {
    method: 'POST',
    body: { email: member.email, full_name: member.full_name, role: member.role },
  })
}

/** PUT a different role for the member with this email, in this organization only. */
export async function changeMemberRole(orgSlug: string, email: string, role: string): Promise<void> {
  await apiRequest(`${membersPath(orgSlug)}/role`, { method: 'PUT', body: { email, role } })
}

/** POST the removal of the member with this email from THIS organization. Their account is not touched. */
export async function removeMember(orgSlug: string, email: string): Promise<void> {
  await apiRequest(`${membersPath(orgSlug)}/remove`, { method: 'POST', body: { email } })
}
