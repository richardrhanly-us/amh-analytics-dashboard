import { ApiError, apiRequest, isApiError, unexpectedResponse } from './client.ts'

/**
 * The signed-in person's own account, and password reset.
 *
 * These are about a PERSON, not an organization: no call here takes an
 * organization, a user id or anything else to say whose account it is -- that
 * is always the session's.
 *
 * A password or a reset token goes to the API in a request body and nowhere
 * else: nothing here keeps, logs or returns one. When the API refuses a value
 * it says which field and a stable word for why (`fieldProblems`); it never
 * repeats the value, and neither does this.
 */

/** The account as the API returns it. `full_name` may be an empty string. */
export interface Account {
  /** How the person signs in. Read-only: no call here changes it. */
  email: string
  full_name: string
  /** An instant in UTC ("2026-10-05T18:50:00Z"), or null when it has never happened. */
  last_login_at: string | null
  last_password_changed_at: string | null
}

/** One field the API refused, and its stable word for why. Never the value that was sent. */
export interface FieldProblem {
  field: string
  code: string
}

// The 422 codes of the three writes that answer with a list of problems.
export const INVALID_PROFILE = 'invalid_profile'
export const INVALID_PASSWORD_CHANGE = 'invalid_password_change'
export const INVALID_PASSWORD_RESET = 'invalid_password_reset'
export const INVALID_RESET_TOKEN = 'invalid_reset_token'
export const PASSWORD_RESET_REQUESTED = 'password_reset_requested'

// An instant written in UTC, to the second or finer: what the API promises for both timestamps.
const UTC_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|\+00:00)$/

function instant(value: unknown): string | null {
  if (value === null) {
    return null
  }
  // A string in the promised form that also names a real moment: "2026-02-30T..." has the form and names none.
  if (typeof value !== 'string' || !UTC_INSTANT.test(value) || Number.isNaN(new Date(value).getTime())) {
    throw unexpectedResponse(200)
  }
  return value
}

function parseAccount(value: unknown): Account {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw unexpectedResponse(200)
  }
  const { email, full_name, last_login_at, last_password_changed_at } = value as Record<string, unknown>
  if (typeof email !== 'string' || email === '' || typeof full_name !== 'string') {
    throw unexpectedResponse(200)
  }
  // Only the four known fields are kept, whatever else a response carries.
  return { email, full_name, last_login_at: instant(last_login_at), last_password_changed_at: instant(last_password_changed_at) }
}

/** GET /api/account. Rejects with a 401 ApiError when nobody is signed in. */
export async function getAccount(signal?: AbortSignal): Promise<Account> {
  return parseAccount(await apiRequest('/api/account', { signal }))
}

/**
 * PUT /api/account/profile. The name is the only thing that can be changed; it is sent as typed and the API
 * trims it. Answers with the account as it then is. The session is not affected.
 */
export async function updateAccountProfile(fullName: string): Promise<Account> {
  return parseAccount(await apiRequest('/api/account/profile', { method: 'PUT', body: { full_name: fullName } }))
}

/**
 * POST /api/account/change-password. On success the API has revoked EVERY session of this person -- the one
 * that made the request included -- and cleared the cookie: the caller is signed out and must sign in again.
 */
export async function changePassword(currentPassword: string, newPassword: string, confirmPassword: string): Promise<void> {
  await apiRequest('/api/account/change-password', {
    method: 'POST',
    body: { current_password: currentPassword, new_password: newPassword, confirm_password: confirmPassword },
  })
}

/**
 * POST /api/auth/password-reset/request. Resolves when the API has accepted the request -- which it does in
 * exactly the same way whether or not the address has an account. Nothing about the address comes back.
 */
export async function requestPasswordReset(email: string): Promise<void> {
  const body = await apiRequest('/api/auth/password-reset/request', { method: 'POST', body: { email } })
  if (typeof body !== 'object' || body === null || (body as Record<string, unknown>).code !== PASSWORD_RESET_REQUESTED) {
    throw unexpectedResponse(202)
  }
}

/**
 * POST /api/auth/password-reset/complete, with the token from the emailed link. On success every session of
 * that person is revoked and nobody is signed in: they sign in with the new password.
 */
export async function completePasswordReset(token: string, newPassword: string, confirmPassword: string): Promise<void> {
  await apiRequest('/api/auth/password-reset/complete', {
    method: 'POST',
    body: { token, new_password: newPassword, confirm_password: confirmPassword },
  })
}

/**
 * The fields the API refused, if that is what this failure is: a 422 carrying `code` and a well-formed list.
 * Anything else -- any other failure, another code, or a list that is not one -- is null. Only the field name
 * and the word for why are kept.
 */
export function fieldProblems(error: unknown, code: string): FieldProblem[] | null {
  if (!isApiError(error) || !(error instanceof ApiError) || error.status !== 422 || error.code !== code) {
    return null
  }
  const body = error.body
  if (typeof body !== 'object' || body === null || !Array.isArray((body as { problems?: unknown }).problems)) {
    return null
  }
  const problems: FieldProblem[] = []
  for (const entry of (body as { problems: unknown[] }).problems) {
    if (typeof entry !== 'object' || entry === null) {
      return null
    }
    const { field, code: why } = entry as Record<string, unknown>
    if (typeof field !== 'string' || typeof why !== 'string') {
      return null
    }
    problems.push({ field, code: why })
  }
  return problems
}
