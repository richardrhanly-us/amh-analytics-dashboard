import { describe, expect, it, vi } from 'vitest'

import {
  callOf,
  jsonResponse,
  networkFailure,
  noContent,
  NOT_AUTHENTICATED,
  stubFetch,
  textResponse,
} from '../test/http.ts'
import { ApiError, apiBaseUrl, apiRequest, apiUrl, isApiError } from './client.ts'

async function failure(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise
  } catch (error) {
    if (isApiError(error)) {
      return error
    }
    throw error
  }
  throw new Error('Expected the request to fail.')
}

describe('API base URL', () => {
  it('is same-origin by default', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, {}))

    await apiRequest('/api/auth/session')

    expect(apiBaseUrl()).toBe('')
    expect(callOf(fetchMock).url).toBe('/api/auth/session')
  })

  it('treats an empty or blank VITE_API_BASE_URL as same-origin', () => {
    vi.stubEnv('VITE_API_BASE_URL', '')
    expect(apiUrl('/api/auth/session')).toBe('/api/auth/session')

    vi.stubEnv('VITE_API_BASE_URL', '   ')
    expect(apiUrl('/api/auth/session')).toBe('/api/auth/session')
  })

  it('prefixes requests with a non-empty VITE_API_BASE_URL', async () => {
    vi.stubEnv('VITE_API_BASE_URL', 'http://api.example.test')
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, {}))

    await apiRequest('/api/auth/session')

    expect(callOf(fetchMock).url).toBe('http://api.example.test/api/auth/session')
  })

  it.each(['http://api.example.test/', 'http://api.example.test///', '  http://api.example.test/  '])(
    'never produces a double slash for the base %j',
    (base) => {
      vi.stubEnv('VITE_API_BASE_URL', base)
      expect(apiUrl('/api/auth/session')).toBe('http://api.example.test/api/auth/session')
    },
  )

  it.each([
    'http://elsewhere.example.test/api/auth/session',
    '//elsewhere.example.test/api/auth/session',
    'api/auth/session',
    '/auth/session',
    '/api',
    '',
  ])('refuses the path %j without sending anything', async (path) => {
    const fetchMock = stubFetch()

    await expect(apiRequest(path)).rejects.toThrow('must start with /api/')
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('successful responses', () => {
  it('returns the parsed JSON body', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, { id: 7, email: 'alice@example.test' }))

    await expect(apiRequest('/api/auth/session')).resolves.toEqual({ id: 7, email: 'alice@example.test' })
  })

  it('returns undefined for a 204 without reading a body', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(noContent())

    await expect(apiRequest('/api/auth/logout', { method: 'POST' })).resolves.toBeUndefined()
  })

  it('rejects a 200 whose body is not JSON', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(textResponse(200, '<html>index</html>'))

    const error = await failure(apiRequest('/api/auth/session'))

    expect(error.status).toBe(200)
    expect(error.code).toBe('unexpected_response')
    expect(error.message).not.toContain('html')
  })
})

describe('error responses', () => {
  it('carries the status, code and message of a standard {code, message} error', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(401, NOT_AUTHENTICATED))

    const error = await failure(apiRequest('/api/auth/session'))

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toBeInstanceOf(Error)
    expect(error.name).toBe('ApiError')
    expect(error.status).toBe(401)
    expect(error.code).toBe('not_authenticated')
    expect(error.message).toBe('Authentication is required.')
  })

  it('maps a 422 validation body to a fixed message and never shows its detail', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(
      jsonResponse(422, {
        code: 'validation_error',
        detail: [{ loc: ['body', 'email'], type: 'missing', msg: 'Field required' }],
      }),
    )

    const error = await failure(apiRequest('/api/auth/login', { method: 'POST', body: {} }))

    expect(error.status).toBe(422)
    expect(error.code).toBe('validation_error')
    expect(error.message).not.toContain('Field required')
    expect(error.message).not.toContain('email')
  })

  it('handles a bare FastAPI {detail: [...]} body the same way', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(422, { detail: [{ loc: ['body'], type: 'x', msg: 'secret detail' }] }))

    const error = await failure(apiRequest('/api/auth/login', { method: 'POST', body: {} }))

    expect(error.code).toBe('validation_error')
    expect(error.message).not.toContain('secret detail')
  })

  it('maps a 429 with a non-standard body to a fixed rate-limit message', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(429, { error: 'Rate limit exceeded: 10 per 1 minute' }))

    const error = await failure(apiRequest('/api/auth/login', { method: 'POST', body: {} }))

    expect(error.status).toBe(429)
    expect(error.code).toBe('rate_limited')
    expect(error.message).not.toContain('10 per 1 minute')
  })

  it('never shows the text of a non-JSON error body', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(
      textResponse(502, '<html>Traceback (most recent call last): File "main.py", line 1</html>'),
    )

    const error = await failure(apiRequest('/api/auth/session'))

    expect(error.status).toBe(502)
    expect(error.code).toBe('http_error')
    expect(error.message).toBe('Something went wrong. Please try again.')
  })

  it.each([
    ['an empty body', new Response(null, { status: 500 })],
    ['a JSON array', jsonResponse(500, ['Traceback'])],
    ['a JSON string', jsonResponse(500, 'Traceback')],
    ['a non-string message', jsonResponse(500, { code: 'internal_error', message: { trace: 'Traceback' } })],
    ['a missing code', jsonResponse(500, { message: 'Traceback (most recent call last)' })],
    ['an over-long message', jsonResponse(500, { code: 'internal_error', message: 'Traceback '.repeat(100) })],
  ])('falls back to the fixed message for %s', async (_label, response) => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(response)

    const error = await failure(apiRequest('/api/auth/session'))

    expect(error.status).toBe(500)
    expect(error.code).toBe('http_error')
    expect(error.message).toBe('Something went wrong. Please try again.')
  })

  it('reports a network failure with no status and without the underlying error text', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockRejectedValue(networkFailure())

    const error = await failure(apiRequest('/api/auth/session'))

    expect(error.status).toBeNull()
    expect(error.code).toBe('network_error')
    expect(error.message).not.toContain('Failed to fetch')
  })
})

describe('the request that is sent', () => {
  it('sends a GET with no body and no Content-Type by default', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, {}))

    await apiRequest('/api/auth/session')

    const { init, headers } = callOf(fetchMock)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(init.method).toBe('GET')
    expect(init.body).toBeUndefined()
    expect(headers).toEqual({ Accept: 'application/json' })
  })

  it('sends a POST body as JSON', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, {}))

    await apiRequest('/api/auth/login', { method: 'POST', body: { email: 'a@example.test', password: ' p w ' } })

    const { init, headers } = callOf(fetchMock)
    expect(init.method).toBe('POST')
    expect(init.body).toBe('{"email":"a@example.test","password":" p w "}')
    expect(headers).toEqual({ Accept: 'application/json', 'Content-Type': 'application/json' })
  })

  it('uses same-origin credentials and never "include"', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(noContent())

    await apiRequest('/api/auth/logout', { method: 'POST' })

    expect(callOf(fetchMock).init.credentials).toBe('same-origin')
  })

  it('reads no cookie or browser storage and invents no auth header', async () => {
    document.cookie = 'sortview_api_session=cookie-secret-value'
    const cookieRead = vi.spyOn(Document.prototype, 'cookie', 'get')
    const storageRead = vi.spyOn(Storage.prototype, 'getItem')
    const storageWrite = vi.spyOn(Storage.prototype, 'setItem')
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, {}))

    await apiRequest('/api/auth/login', { method: 'POST', body: { email: 'a@example.test', password: 'pw' } })

    expect(cookieRead).not.toHaveBeenCalled()
    expect(storageRead).not.toHaveBeenCalled()
    expect(storageWrite).not.toHaveBeenCalled()
    const { url, init, headers } = callOf(fetchMock)
    expect(Object.keys(headers).map((name) => name.toLowerCase())).toEqual(['accept', 'content-type'])
    expect(JSON.stringify([url, init])).not.toContain('cookie-secret-value')
  })
})
