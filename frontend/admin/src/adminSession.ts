import type { User } from '../../src/api/auth.ts'
import { apiRequest, unexpectedResponse } from '../../src/api/client.ts'

/**
 * GET /api/admin/session: the signed-in user, if -- and only if -- the server says they are a platform administrator.
 * Rejects with the API's 401 when nobody is signed in and its 403 when the user is not an administrator. The answer
 * carries the same three fields as the customer session and no flag: that it answered at all is the authorization.
 */
export async function getAdminSession(): Promise<User> {
  const value = await apiRequest('/api/admin/session')
  if (typeof value === 'object' && value !== null) {
    const { id, email, full_name } = value as Record<string, unknown>
    if (typeof id === 'number' && typeof email === 'string' && typeof full_name === 'string') {
      // Only the three known fields are kept, whatever else a response carries.
      return { id, email, full_name }
    }
  }
  throw unexpectedResponse(200)
}
