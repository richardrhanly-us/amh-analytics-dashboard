import { describe, expect, it } from 'vitest'

import { callOf, jsonResponse, noContent, NOT_AUTHENTICATED, ORIGIN_NOT_ALLOWED, stubFetch } from '../test/http.ts'
import {
  changePassword,
  completePasswordReset,
  fieldProblems,
  getAccount,
  INVALID_PASSWORD_CHANGE,
  INVALID_PASSWORD_RESET,
  INVALID_PROFILE,
  requestPasswordReset,
  updateAccountProfile,
} from './account.ts'
import { ApiError } from './client.ts'

const ACCOUNT = {
  email: 'alice@example.test',
  full_name: 'Alice Example',
  last_login_at: '2026-10-05T18:50:00Z',
  last_password_changed_at: '2026-08-01T09:00:00.544504Z',
}
const REQUESTED = {
  code: 'password_reset_requested',
  message: 'If an active account exists for that email address, password reset instructions will be sent.',
}
const UNEXPECTED = { code: 'unexpected_response' }
const CURRENT = 'synthetic-Current-1'
const NEW = 'synthetic-New-2'
const TOKEN = 'synthetic-RESET-token-5f0c'

const problem = (code: string, field: string, why: string) => ({ code, message: 'Not valid.', problems: [{ field, code: why }] })
const sentBody = (fetchMock: ReturnType<typeof stubFetch>) => JSON.parse(String(callOf(fetchMock).init.body)) as unknown

describe('getAccount', () => {
  it('GETs /api/account and returns the four fields', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, ACCOUNT))

    const account = await getAccount()

    const call = callOf(fetchMock)
    expect(call.url).toBe('/api/account')
    expect(call.init.method).toBe('GET')
    expect(call.init.body).toBeUndefined()
    expect(account).toStrictEqual(ACCOUNT)
  })

  it('passes the abort signal to fetch', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, ACCOUNT))
    const controller = new AbortController()

    await getAccount(controller.signal)

    expect(callOf(fetchMock).init.signal).toBe(controller.signal)
  })

  it.each([
    ['both timestamps null', { last_login_at: null, last_password_changed_at: null }],
    ['an empty name', { full_name: '' }],
    ['a +00:00 offset', { last_login_at: '2026-10-05T18:50:00+00:00' }],
    ['whole seconds and fractions of one', { last_login_at: '2026-10-05T18:50:00.1Z', last_password_changed_at: '2026-10-05T18:50:00Z' }],
  ])('accepts %s', async (_label, changes) => {
    stubFetch().mockResolvedValue(jsonResponse(200, { ...ACCOUNT, ...changes }))

    expect(await getAccount()).toStrictEqual({ ...ACCOUNT, ...changes })
  })

  it('keeps only the four fields, whatever else arrives', async () => {
    stubFetch().mockResolvedValue(jsonResponse(200, { ...ACCOUNT, id: 7, password_hash: 'scrypt:CANARY', is_platform_admin: true, role: 'owner' }))

    const account = await getAccount()

    expect(account).toStrictEqual(ACCOUNT)
    expect(JSON.stringify(account)).not.toMatch(/CANARY|password_hash|platform|role|"id"/)
  })

  it.each<[string, unknown]>([
    ['no email', { ...ACCOUNT, email: undefined }],
    ['an empty email', { ...ACCOUNT, email: '' }],
    ['an email that is a number', { ...ACCOUNT, email: 7 }],
    ['a name that is null', { ...ACCOUNT, full_name: null }],
    ['no name', { ...ACCOUNT, full_name: undefined }],
    ['a timestamp left out', { email: ACCOUNT.email, full_name: ACCOUNT.full_name, last_login_at: null }],
    ['a timestamp as a number', { ...ACCOUNT, last_login_at: 1791226200000 }],
    ['a timestamp in a local offset', { ...ACCOUNT, last_login_at: '2026-10-05T13:50:00-05:00' }],
    ['a timestamp with no zone', { ...ACCOUNT, last_login_at: '2026-10-05T18:50:00' }],
    ['a date with no time', { ...ACCOUNT, last_password_changed_at: '2026-10-05' }],
    ['a date that does not exist', { ...ACCOUNT, last_login_at: '2026-13-45T18:50:00Z' }],
    ['words for a timestamp', { ...ACCOUNT, last_login_at: 'yesterday' }],
    ['an empty timestamp', { ...ACCOUNT, last_login_at: '' }],
    ['a list', [ACCOUNT]],
    ['null', null],
    ['text', 'ok'],
  ])('rejects %s as an unexpected response', async (_label, body) => {
    stubFetch().mockResolvedValue(jsonResponse(200, body))

    await expect(getAccount()).rejects.toMatchObject(UNEXPECTED)
  })

  it('rejects with the 401 when nobody is signed in', async () => {
    stubFetch().mockResolvedValue(jsonResponse(401, NOT_AUTHENTICATED))

    await expect(getAccount()).rejects.toMatchObject({ status: 401, code: 'not_authenticated' })
  })
})

describe('updateAccountProfile', () => {
  it('PUTs the name alone, exactly as typed, and returns the account the API stored', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, { ...ACCOUNT, full_name: 'Pat Example' }))

    const account = await updateAccountProfile('  Pat Example  ')

    const call = callOf(fetchMock)
    expect(call.url).toBe('/api/account/profile')
    expect(call.init.method).toBe('PUT')
    // Only the name: no email, no id, nothing else. Trimming is the API's.
    expect(sentBody(fetchMock)).toStrictEqual({ full_name: '  Pat Example  ' })
    expect(account.full_name).toBe('Pat Example')
  })

  it('rejects an answer that is not an account', async () => {
    stubFetch().mockResolvedValue(jsonResponse(200, { full_name: 'Pat Example' }))

    await expect(updateAccountProfile('Pat Example')).rejects.toMatchObject(UNEXPECTED)
  })

  it.each(['required', 'too_long', 'invalid_characters'])('reports a refused name with its field and the word %s', async (why) => {
    stubFetch().mockResolvedValue(jsonResponse(422, problem(INVALID_PROFILE, 'full_name', why)))

    const error = await updateAccountProfile('x').catch((caught: unknown) => caught)

    expect(fieldProblems(error, INVALID_PROFILE)).toEqual([{ field: 'full_name', code: why }])
  })
})

describe('changePassword', () => {
  it('POSTs the three passwords and resolves with nothing on 204', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(noContent())

    await expect(changePassword(CURRENT, NEW, NEW)).resolves.toBeUndefined()

    const call = callOf(fetchMock)
    expect(call.url).toBe('/api/account/change-password')
    expect(call.init.method).toBe('POST')
    expect(sentBody(fetchMock)).toStrictEqual({ current_password: CURRENT, new_password: NEW, confirm_password: NEW })
  })

  it('sends a password exactly as typed, spaces and all', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(noContent())

    await changePassword('  spaced "pass\\word é  ', ' new one  ', ' new one  ')

    expect(sentBody(fetchMock)).toStrictEqual({ current_password: '  spaced "pass\\word é  ', new_password: ' new one  ', confirm_password: ' new one  ' })
  })

  it.each([
    ['current_password', 'incorrect'],
    ['new_password', 'too_short'],
    ['confirm_password', 'mismatch'],
    ['new_password', 'same_as_current'],
  ])('reports %s: %s, and never a password', async (field, why) => {
    stubFetch().mockResolvedValue(jsonResponse(422, problem(INVALID_PASSWORD_CHANGE, field, why)))

    const error = await changePassword(CURRENT, NEW, NEW).catch((caught: unknown) => caught)

    expect(error).toMatchObject({ status: 422, code: INVALID_PASSWORD_CHANGE })
    expect(fieldProblems(error, INVALID_PASSWORD_CHANGE)).toEqual([{ field, code: why }])
    expect(JSON.stringify(error) + String((error as Error).message)).not.toMatch(/synthetic-/)
  })

  it.each([
    [401, NOT_AUTHENTICATED, 'not_authenticated'],
    [403, ORIGIN_NOT_ALLOWED, 'origin_not_allowed'],
    [429, { error: 'Rate limit exceeded: 10 per 1 minute' }, 'rate_limited'],
  ])('rejects with the API error for a %i', async (status, body, code) => {
    stubFetch().mockResolvedValue(jsonResponse(status, body))

    await expect(changePassword(CURRENT, NEW, NEW)).rejects.toMatchObject({ status, code })
  })
})

describe('requestPasswordReset', () => {
  it('POSTs the address and resolves with nothing: no token, no account, no address comes back', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(202, REQUESTED))

    await expect(requestPasswordReset('alice@example.test')).resolves.toBeUndefined()

    const call = callOf(fetchMock)
    expect(call.url).toBe('/api/auth/password-reset/request')
    expect(call.init.method).toBe('POST')
    expect(sentBody(fetchMock)).toStrictEqual({ email: 'alice@example.test' })
  })

  it('resolves identically whatever else the answer carries: nothing in it is used', async () => {
    stubFetch().mockResolvedValue(jsonResponse(202, { ...REQUESTED, reset_token: TOKEN, account_exists: true }))

    await expect(requestPasswordReset('alice@example.test')).resolves.toBeUndefined()
  })

  it.each<[string, unknown]>([
    ['another code', { code: 'something_else', message: 'x' }],
    ['no code', { message: 'x' }],
    ['a list', []],
    ['null', null],
  ])('rejects %s as an unexpected response', async (_label, body) => {
    stubFetch().mockResolvedValue(jsonResponse(202, body))

    await expect(requestPasswordReset('alice@example.test')).rejects.toMatchObject(UNEXPECTED)
  })

  it('rejects with the API error when reset email cannot be sent at all', async () => {
    stubFetch().mockResolvedValue(jsonResponse(503, { code: 'password_reset_unavailable', message: 'Password reset is not available right now.' }))

    await expect(requestPasswordReset('alice@example.test')).rejects.toMatchObject({
      status: 503,
      code: 'password_reset_unavailable',
      message: 'Password reset is not available right now.',
    })
  })

  it('rejects with a rate-limit error for a 429', async () => {
    stubFetch().mockResolvedValue(jsonResponse(429, { error: 'Rate limit exceeded: 5 per 1 minute' }))

    await expect(requestPasswordReset('alice@example.test')).rejects.toMatchObject({ status: 429, code: 'rate_limited' })
  })
})

describe('completePasswordReset', () => {
  it('POSTs the token in the body -- never in the address -- with the two passwords', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(noContent())

    await expect(completePasswordReset(TOKEN, NEW, NEW)).resolves.toBeUndefined()

    const call = callOf(fetchMock)
    expect(call.url).toBe('/api/auth/password-reset/complete')
    expect(call.url).not.toContain(TOKEN)
    expect(call.init.method).toBe('POST')
    expect(sentBody(fetchMock)).toStrictEqual({ token: TOKEN, new_password: NEW, confirm_password: NEW })
    expect(JSON.stringify(call.headers)).not.toContain(TOKEN)
  })

  it('rejects with one generic error for a token that is invalid, expired or used', async () => {
    stubFetch().mockResolvedValue(jsonResponse(400, { code: 'invalid_reset_token', message: 'This password reset link is invalid or has expired.' }))

    const error = await completePasswordReset(TOKEN, NEW, NEW).catch((caught: unknown) => caught)

    expect(error).toMatchObject({ status: 400, code: 'invalid_reset_token', message: 'This password reset link is invalid or has expired.' })
    expect(String((error as Error).message)).not.toContain(TOKEN)
  })

  it.each([
    ['new_password', 'too_short'],
    ['confirm_password', 'mismatch'],
    ['new_password', 'same_as_current'],
  ])('reports %s: %s', async (field, why) => {
    stubFetch().mockResolvedValue(jsonResponse(422, problem(INVALID_PASSWORD_RESET, field, why)))

    const error = await completePasswordReset(TOKEN, NEW, NEW).catch((caught: unknown) => caught)

    expect(fieldProblems(error, INVALID_PASSWORD_RESET)).toEqual([{ field, code: why }])
  })
})

describe('fieldProblems', () => {
  const refused = (body: unknown, status = 422, code = INVALID_PROFILE) => new ApiError(status, code, 'Not valid.', body)
  const list = [{ field: 'full_name', code: 'required' }]

  it('returns the field and the word of each problem, and nothing else of it', () => {
    const error = refused({ problems: [{ field: 'full_name', code: 'too_long', value: 'CANARY', limit: 120 }] })

    expect(fieldProblems(error, INVALID_PROFILE)).toStrictEqual([{ field: 'full_name', code: 'too_long' }])
  })

  it('is null for any failure that is not this one', () => {
    expect(fieldProblems(refused({ problems: list }, 400), INVALID_PROFILE)).toBeNull()
    expect(fieldProblems(refused({ problems: list }, 422, INVALID_PASSWORD_CHANGE), INVALID_PROFILE)).toBeNull()
    expect(fieldProblems(new Error('boom'), INVALID_PROFILE)).toBeNull()
    expect(fieldProblems({ status: 422, code: INVALID_PROFILE, body: { problems: list } }, INVALID_PROFILE)).toBeNull()
    expect(fieldProblems(null, INVALID_PROFILE)).toBeNull()
  })

  it.each<[string, unknown]>([
    ['no body', undefined],
    ['no list', {}],
    ['a list that is an object', { problems: { full_name: 'required' } }],
    ['an entry that is text', { problems: ['full_name'] }],
    ['an entry that is null', { problems: [null] }],
    ['a field that is not text', { problems: [{ field: 1, code: 'required' }] }],
    ['a word that is not text', { problems: [{ field: 'full_name', code: null }] }],
    ['one good entry and one bad', { problems: [...list, { field: 'full_name' }] }],
  ])('is null for %s: a list that is not one is not half-used', (_label, body) => {
    expect(fieldProblems(refused(body), INVALID_PROFILE)).toBeNull()
  })

  it('is an empty list for an empty list', () => {
    expect(fieldProblems(refused({ problems: [] }), INVALID_PROFILE)).toEqual([])
  })
})
