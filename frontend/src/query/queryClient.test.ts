import { describe, expect, it, vi } from 'vitest'

import { ApiError } from '../api/client.ts'
import { createQueryClient, MAX_RETRIES, retryDelay, shouldRetry } from './queryClient.ts'

const failure = (status: number | null) => new ApiError(status, 'test', 'A test failure.')

describe('the retry rule', () => {
  it.each([
    ['no response (network)', null],
    ['500', 500],
    ['502', 502],
    ['503', 503],
    ['504', 504],
  ])('tries again after %s', (_label, status) => {
    expect(shouldRetry(0, failure(status))).toBe(true)
    expect(shouldRetry(MAX_RETRIES - 1, failure(status))).toBe(true)
  })

  it('stops after two retries', () => {
    expect(MAX_RETRIES).toBe(2)
    expect(shouldRetry(MAX_RETRIES, failure(500))).toBe(false)
    expect(shouldRetry(MAX_RETRIES, failure(null))).toBe(false)
  })

  it.each([
    ['401 (session ended)', 401],
    ['403', 403],
    ['404 (not available)', 404],
    ['422 (bad request)', 422],
    ['429 (rate limited)', 429],
    ['400', 400],
    ['an unreadable 200', 200],
  ])('never tries again after %s', (_label, status) => {
    expect(shouldRetry(0, failure(status))).toBe(false)
  })

  it('never tries again after something that is not an API failure', () => {
    expect(shouldRetry(0, new TypeError('a bug'))).toBe(false)
    expect(shouldRetry(0, 'nope')).toBe(false)
  })

  it('waits one second, then two, and never long', () => {
    expect(retryDelay(0)).toBe(1000)
    expect(retryDelay(1)).toBe(2000)
    expect(retryDelay(10)).toBeLessThanOrEqual(4000)
  })
})

describe('the query client', () => {
  it('uses the retry rule for queries and never retries a mutation', () => {
    const { queries, mutations } = createQueryClient(() => {}).getDefaultOptions()

    expect(queries?.retry).toBe(shouldRetry)
    expect(queries?.retryDelay).toBe(retryDelay)
    expect(mutations?.retry).toBe(false)
  })

  it('does not refetch because a window regained focus', () => {
    expect(createQueryClient(() => {}).getDefaultOptions().queries?.refetchOnWindowFocus).toBe(false)
  })

  it('reports an expired session, once, when a read is answered 401', async () => {
    const onSessionExpired = vi.fn()
    const client = createQueryClient(onSessionExpired)
    const queryFn = vi.fn().mockRejectedValue(failure(401))

    await expect(client.fetchQuery({ queryKey: ['x'], queryFn })).rejects.toMatchObject({ status: 401 })

    expect(onSessionExpired).toHaveBeenCalledTimes(1)
    expect(queryFn).toHaveBeenCalledTimes(1)
  })

  it.each([404, 422, 429, 500, null])('does not report an expired session for a %s', async (status) => {
    const onSessionExpired = vi.fn()
    const client = createQueryClient(onSessionExpired)
    client.setDefaultOptions({ queries: { retry: false } })

    await expect(client.fetchQuery({ queryKey: ['x'], queryFn: () => Promise.reject(failure(status)) })).rejects.toBeDefined()

    expect(onSessionExpired).not.toHaveBeenCalled()
  })

  it('gives each session a client of its own', async () => {
    const first = createQueryClient(() => {})
    const second = createQueryClient(() => {})
    await first.fetchQuery({ queryKey: ['x'], queryFn: () => Promise.resolve('for the first user') })

    expect(second.getQueryData(['x'])).toBeUndefined()
    expect(second.getQueryCache()).not.toBe(first.getQueryCache())
  })
})
