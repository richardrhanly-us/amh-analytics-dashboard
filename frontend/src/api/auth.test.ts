import { describe, expect, it } from 'vitest'

import { ALICE, callOf, INVALID_CREDENTIALS, jsonResponse, noContent, NOT_AUTHENTICATED, stubFetch } from '../test/http.ts'
import { getSession, login, logout } from './auth.ts'

describe('login', () => {
  it('POSTs exactly {email, password} to /api/auth/login and returns the user', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, ALICE))

    const user = await login('alice@example.test', ' correct horse ')

    const { url, init } = callOf(fetchMock)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(url).toBe('/api/auth/login')
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toStrictEqual({ email: 'alice@example.test', password: ' correct horse ' })
    expect(user).toStrictEqual(ALICE)
  })

  it('rejects with the API error for bad credentials', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(401, INVALID_CREDENTIALS))

    await expect(login('alice@example.test', 'wrong')).rejects.toMatchObject({
      status: 401,
      code: 'invalid_credentials',
      message: 'Invalid email or password.',
    })
  })
})

describe('getSession', () => {
  it('GETs /api/auth/session with no body and returns the user', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, ALICE))

    const user = await getSession()

    const { url, init } = callOf(fetchMock)
    expect(url).toBe('/api/auth/session')
    expect(init.method).toBe('GET')
    expect(init.body).toBeUndefined()
    expect(user).toStrictEqual(ALICE)
  })

  it('rejects with a 401 when nobody is signed in', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(401, NOT_AUTHENTICATED))

    await expect(getSession()).rejects.toMatchObject({ status: 401, code: 'not_authenticated' })
  })
})

describe('logout', () => {
  it('POSTs to /api/auth/logout with no body and resolves on 204', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(noContent())

    await expect(logout()).resolves.toBeUndefined()

    const { url, init } = callOf(fetchMock)
    expect(url).toBe('/api/auth/logout')
    expect(init.method).toBe('POST')
    expect(init.body).toBeUndefined()
  })
})

describe('the User contract', () => {
  it('accepts an empty full_name', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, { ...ALICE, full_name: '' }))

    await expect(getSession()).resolves.toStrictEqual({ ...ALICE, full_name: '' })
  })

  it('keeps only id, email and full_name', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, { ...ALICE, password_hash: 'x', session_token: 'y' }))

    await expect(getSession()).resolves.toStrictEqual(ALICE)
  })

  it.each([
    ['a missing id', { email: 'a@example.test', full_name: 'A' }],
    ['a string id', { id: '7', email: 'a@example.test', full_name: 'A' }],
    ['a missing email', { id: 7, full_name: 'A' }],
    ['a null full_name', { id: 7, email: 'a@example.test', full_name: null }],
    ['an array', [ALICE]],
    ['null', null],
  ])('rejects a 200 with %s as an unexpected response', async (_label, body) => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, body))

    await expect(getSession()).rejects.toMatchObject({ code: 'unexpected_response' })
  })
})
