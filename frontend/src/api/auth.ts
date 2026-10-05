import { apiRequest, unexpectedResponse } from './client.ts'

/** The signed-in user, exactly as the API returns it. `full_name` may be an empty string. */
export interface User {
  id: number
  email: string
  full_name: string
}

function parseUser(value: unknown): User {
  if (typeof value === 'object' && value !== null) {
    const { id, email, full_name } = value as Record<string, unknown>
    if (typeof id === 'number' && typeof email === 'string' && typeof full_name === 'string') {
      // Only the three known fields are kept, whatever else a response carries.
      return { id, email, full_name }
    }
  }
  throw unexpectedResponse(200)
}

/** POST /api/auth/login. On success the API sets the session cookie and returns the user. */
export async function login(email: string, password: string): Promise<User> {
  return parseUser(await apiRequest('/api/auth/login', { method: 'POST', body: { email, password } }))
}

/** GET /api/auth/session. Rejects with a 401 ApiError when nobody is signed in. */
export async function getSession(): Promise<User> {
  return parseUser(await apiRequest('/api/auth/session'))
}

/** POST /api/auth/logout. The API answers 204 and clears the session cookie. */
export async function logout(): Promise<void> {
  await apiRequest('/api/auth/logout', { method: 'POST' })
}
