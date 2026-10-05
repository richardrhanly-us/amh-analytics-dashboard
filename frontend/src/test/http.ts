import { vi, type Mock } from 'vitest'

/**
 * Test helpers for the one thing every test here fakes: `fetch`. No test in
 * this app talks to a real server.
 */

export type FetchMock = Mock<typeof fetch>

/** Replaces the global fetch with a mock. Undone automatically after each test (see vitest.config.ts). */
export function stubFetch(): FetchMock {
  const mock: FetchMock = vi.fn<typeof fetch>()
  vi.stubGlobal('fetch', mock)
  return mock
}

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

export function textResponse(status: number, body: string, contentType = 'text/html'): Response {
  return new Response(body, { status, headers: { 'Content-Type': contentType } })
}

export function noContent(): Response {
  return new Response(null, { status: 204 })
}

/** What a browser's fetch rejects with when no response arrives. */
export function networkFailure(): TypeError {
  return new TypeError('Failed to fetch')
}

/** A promise a test settles by hand, for holding a request "in flight". */
export function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (reason: unknown) => void } {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

/** The [url, init] of one recorded fetch call, with the init narrowed for assertions. */
export function callOf(mock: FetchMock, index = 0): { url: string; init: RequestInit; headers: Record<string, string> } {
  const call = mock.mock.calls[index]
  if (call === undefined) {
    throw new Error(`fetch was not called ${index + 1} time(s).`)
  }
  const [url, init = {}] = call
  return { url: String(url), init, headers: (init.headers ?? {}) as Record<string, string> }
}

/** The request paths fetch was called with, in order. */
export function requestedUrls(mock: FetchMock): string[] {
  return mock.mock.calls.map(([url]) => String(url))
}

export const ALICE = { id: 7, email: 'alice@example.test', full_name: 'Alice Example' }

export const NOT_AUTHENTICATED = { code: 'not_authenticated', message: 'Authentication is required.' }
export const INVALID_CREDENTIALS = { code: 'invalid_credentials', message: 'Invalid email or password.' }
export const ORIGIN_NOT_ALLOWED = { code: 'origin_not_allowed', message: 'Request origin is not allowed.' }
export const ORGANIZATION_NOT_FOUND = { code: 'organization_not_found', message: 'Organization not found.' }

/**
 * A fetch mock that answers by request, e.g. `{ 'GET /api/organizations': () => jsonResponse(200, []) }`.
 * A handler runs once per matching request, so it can return a fresh Response (a Response body can be read
 * only once) or a promise the test settles later. A request with no handler fails like a dead network.
 */
export function serveApi(routes: Record<string, () => Response | Promise<Response>>): FetchMock {
  const mock = stubFetch()
  mock.mockImplementation((url, init) => {
    const handler = routes[`${init?.method ?? 'GET'} ${String(url)}`]
    return handler === undefined ? Promise.reject(networkFailure()) : Promise.resolve(handler())
  })
  return mock
}

export const NORTHBRIDGE = { slug: 'northbridge', name: 'Northbridge Library', role: 'admin', access_mode: 'full' }
export const RIVERSIDE = { slug: 'riverside', name: 'Riverside Library', role: 'viewer', access_mode: 'read_only' }

export const NORTHBRIDGE_DETAIL = {
  ...NORTHBRIDGE,
  branches: [
    { slug: 'central', name: 'Central Branch', is_primary: true },
    { slug: 'east-side', name: 'East Side Branch', is_primary: false },
  ],
  subscription: { plan_code: 'standard', plan_name: 'Standard', status: 'active' },
  entitlements: {
    transits_tab: { enabled: true, limit_value: null },
    branch_count: { enabled: true, limit_value: 5 },
  },
}

export const RIVERSIDE_DETAIL = {
  ...RIVERSIDE,
  branches: [{ slug: 'main', name: 'Riverside Main', is_primary: true }],
  subscription: null,
  entitlements: {},
}
