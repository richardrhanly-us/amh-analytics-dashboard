/**
 * The one place this app talks to the SortView customer API.
 *
 * Every request is a path under /api on -- by default -- the page's own
 * origin. The session is an HttpOnly cookie the browser attaches by itself:
 * nothing here reads, stores or sends a credential, and there is no token
 * header.
 *
 * Whatever goes wrong, a caller only ever sees an ApiError whose `message` is
 * safe to show a person: either one of the fixed sentences below, or the
 * short fixed message the API itself sends with a `{code, message}` error.
 * Nothing else a server (or a proxy in front of it) returns reaches the UI.
 */

/** A failed API call. `status` is the HTTP status, or null when no response arrived at all. */
export class ApiError extends Error {
  readonly status: number | null
  readonly code: string

  constructor(status: number | null, code: string, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError
}

// Codes this client assigns itself, for failures that carry no usable code of their own.
export const NETWORK_ERROR = 'network_error'
export const UNEXPECTED_RESPONSE = 'unexpected_response'
export const VALIDATION_ERROR = 'validation_error'
export const RATE_LIMITED = 'rate_limited'
export const HTTP_ERROR = 'http_error'

const MESSAGES = {
  network: 'Could not reach the server. Check your connection and try again.',
  unexpected: 'The server sent a response this app could not read. Please try again.',
  validation: 'That request was not valid. Check what you entered and try again.',
  rateLimited: 'Too many attempts. Please wait a moment and try again.',
  generic: 'Something went wrong. Please try again.',
} as const

// The API's own messages are one short sentence. Anything longer is not one of them.
const MAX_SERVER_MESSAGE_LENGTH = 200

export type ApiMethod = 'GET' | 'POST'

export interface ApiRequestOptions {
  method?: ApiMethod
  /** Sent as a JSON body. Omit for a request with no body. */
  body?: unknown
}

/**
 * VITE_API_BASE_URL with surrounding space and trailing slashes removed, or ''
 * (the default) for same-origin requests.
 */
export function apiBaseUrl(): string {
  const configured = import.meta.env.VITE_API_BASE_URL
  if (typeof configured !== 'string') {
    return ''
  }
  return configured.trim().replace(/\/+$/, '')
}

/**
 * The URL for an API path. A path is always absolute-on-the-API and under
 * /api ("/api/auth/session"): a caller names an endpoint, never a host, so a
 * full URL or a protocol-relative "//host" path is refused rather than sent.
 */
export function apiUrl(path: string): string {
  if (!path.startsWith('/api/') || path.startsWith('//') || path.includes('://') || path.includes('\\')) {
    throw new Error('An API path must start with /api/ and name no host.')
  }
  return `${apiBaseUrl()}${path}`
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

async function readJson(response: Response): Promise<unknown> {
  // A body that is empty or is not JSON is reported as `undefined`, never thrown from here.
  try {
    return (await response.json()) as unknown
  } catch {
    return undefined
  }
}

function errorFor(status: number, body: unknown): ApiError {
  if (isRecord(body)) {
    // The customer API's own error shape: a stable code and a fixed, user-safe message.
    const { code, message } = body
    if (
      typeof code === 'string' &&
      code !== '' &&
      typeof message === 'string' &&
      message !== '' &&
      message.length <= MAX_SERVER_MESSAGE_LENGTH
    ) {
      return new ApiError(status, code, message)
    }
    // A validation failure: {code: "validation_error", detail: [...]}. The detail names fields and is not shown.
    if (Array.isArray(body.detail)) {
      return new ApiError(status, VALIDATION_ERROR, MESSAGES.validation)
    }
  }
  if (status === 422) {
    return new ApiError(status, VALIDATION_ERROR, MESSAGES.validation)
  }
  if (status === 429) {
    return new ApiError(status, RATE_LIMITED, MESSAGES.rateLimited)
  }
  return new ApiError(status, HTTP_ERROR, MESSAGES.generic)
}

/**
 * Sends one request and returns the parsed JSON body, or `undefined` for a
 * 204. Rejects with an ApiError for every failure: no response, a non-2xx
 * status, or a 2xx body that is not JSON. The result is typed `unknown` on
 * purpose -- the caller checks the shape it expects.
 */
export async function apiRequest(path: string, options: ApiRequestOptions = {}): Promise<unknown> {
  const { method = 'GET', body } = options
  const headers: Record<string, string> = { Accept: 'application/json' }
  const init: RequestInit = {
    method,
    headers,
    // The page and the API share an origin, so the browser sends the session cookie itself.
    credentials: 'same-origin',
  }
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    init.body = JSON.stringify(body)
  }

  // Outside the try: a bad path is a bug in the caller, not a network failure.
  const url = apiUrl(path)

  let response: Response
  try {
    response = await fetch(url, init)
  } catch {
    throw new ApiError(null, NETWORK_ERROR, MESSAGES.network)
  }

  if (!response.ok) {
    throw errorFor(response.status, await readJson(response))
  }
  if (response.status === 204) {
    return undefined
  }

  const parsed = await readJson(response)
  if (parsed === undefined) {
    throw new ApiError(response.status, UNEXPECTED_RESPONSE, MESSAGES.unexpected)
  }
  return parsed
}

/** For a 2xx response whose JSON is not the shape the caller requires. */
export function unexpectedResponse(status: number | null = null): ApiError {
  return new ApiError(status, UNEXPECTED_RESPONSE, MESSAGES.unexpected)
}
