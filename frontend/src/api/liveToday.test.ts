import { describe, expect, it } from 'vitest'

import { callOf, jsonResponse, LIVE, liveBody, NOT_AUTHENTICATED, stubFetch, TENANT_NOT_FOUND } from '../test/http.ts'
import {
  getCheckinCount,
  getCheckinsByHour,
  getPipelineStatus,
  getRejectCount,
  getRejectsByReason,
  PIPELINE_STATES,
  REJECT_REASONS,
} from './liveToday.ts'

const DATE = '2026-10-05'
const BASE = '/api/organizations/northbridge/branches/central'
const UNEXPECTED = { code: 'unexpected_response' }

type Reader = (org: string, branch: string, signal?: AbortSignal) => Promise<unknown>

/** Each of the five reads: how to call it, the URL it must use and a body it must accept. */
const READS: Array<[name: string, read: Reader, url: string, body: unknown]> = [
  ['pipeline-status', (o, b, s) => getPipelineStatus(o, b, s), `${BASE}/pipeline-status`, liveBody.pipeline(LIVE)],
  [
    'checkins/count',
    (o, b, s) => getCheckinCount(o, b, DATE, s),
    `${BASE}/checkins/count?date=2026-10-05`,
    liveBody.checkinCount(LIVE, DATE),
  ],
  [
    'checkins/by-hour',
    (o, b, s) => getCheckinsByHour(o, b, DATE, s),
    `${BASE}/checkins/by-hour?date=2026-10-05`,
    liveBody.checkinsByHour(LIVE, DATE),
  ],
  [
    'rejects/count',
    (o, b, s) => getRejectCount(o, b, DATE, s),
    `${BASE}/rejects/count?date=2026-10-05`,
    liveBody.rejectCount(LIVE, DATE),
  ],
  [
    'rejects/by-reason',
    (o, b, s) => getRejectsByReason(o, b, DATE, s),
    `${BASE}/rejects/by-reason?date=2026-10-05`,
    liveBody.rejectsByReason(LIVE, DATE),
  ],
]

describe.each(READS)('%s', (_name, read, url, body) => {
  it('GETs exactly its URL and returns the body unchanged', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, body))

    const result = await read('northbridge', 'central')

    const call = callOf(fetchMock)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(call.url).toBe(url)
    expect(call.init.method).toBe('GET')
    expect(call.init.body).toBeUndefined()
    expect(result).toStrictEqual(body)
  })

  it('URL-encodes both slugs', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(404, TENANT_NOT_FOUND))

    await expect(read('north bridge/x', 'a?b#c')).rejects.toMatchObject({ status: 404 })

    expect(callOf(fetchMock).url).toBe(url.replace('northbridge', 'north%20bridge%2Fx').replace('central', 'a%3Fb%23c'))
  })

  it.each([
    ['', 'central'],
    ['..', 'central'],
    ['northbridge', '.'],
    ['northbridge', '..'],
    ['northbridge', ''],
  ])('answers not-found for the slugs %j / %j without sending anything', async (org, branch) => {
    const fetchMock = stubFetch()

    await expect(read(org, branch)).rejects.toMatchObject({ status: 404, code: 'tenant_not_found' })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('passes the abort signal to fetch', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, body))
    const controller = new AbortController()

    await read('northbridge', 'central', controller.signal)

    expect(callOf(fetchMock).init.signal).toBe(controller.signal)
  })

  it('drops fields the contract does not have, such as an id', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, { ...(body as object), customer_id: 41, branch_id: 7, key_id: 'k' }))

    await expect(read('northbridge', 'central')).resolves.toStrictEqual(body)
  })

  it.each([
    ['null', null],
    ['a list', [body]],
    ['a string', 'ok'],
    ['an empty object', {}],
    ['a missing timezone', { ...(body as object), timezone: undefined }],
    ['a timezone that is not one', { ...(body as object), timezone: 'Central Time' }],
    ['a numeric timezone', { ...(body as object), timezone: -5 }],
  ])('rejects %s as an unexpected response', async (_label, malformed) => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, malformed))

    await expect(read('northbridge', 'central')).rejects.toMatchObject(UNEXPECTED)
  })

  it('rejects with the API error for a 404 and a 401', async () => {
    const fetchMock = stubFetch()
    fetchMock
      .mockResolvedValueOnce(jsonResponse(404, TENANT_NOT_FOUND))
      .mockResolvedValueOnce(jsonResponse(401, NOT_AUTHENTICATED))

    await expect(read('northbridge', 'central')).rejects.toMatchObject({ status: 404, code: 'tenant_not_found' })
    await expect(read('northbridge', 'central')).rejects.toMatchObject({ status: 401, code: 'not_authenticated' })
  })
})

describe('the date', () => {
  const dated = [getCheckinCount, getCheckinsByHour, getRejectCount, getRejectsByReason]

  it.each(['2026-10-5', '10/05/2026', '2026-10-05T00:00:00', '', 'today', '2026-10-05&x=1'])(
    'must be YYYY-MM-DD: %j is refused before anything is sent',
    async (date) => {
      const fetchMock = stubFetch()

      for (const read of dated) {
        await expect(read('northbridge', 'central', date)).rejects.toThrow('YYYY-MM-DD')
      }
      expect(fetchMock).not.toHaveBeenCalled()
    },
  )

  it('must come back as the date that was asked for', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockImplementation(() => Promise.resolve(jsonResponse(200, liveBody.checkinCount(LIVE, '2026-10-04'))))

    await expect(getCheckinCount('northbridge', 'central', DATE)).rejects.toMatchObject(UNEXPECTED)
  })

  it.each([['a missing date', undefined], ['a timestamp', '2026-10-05T00:00:00Z'], ['a number', 20261005]])(
    'rejects %s in the answer',
    async (_label, date) => {
      const fetchMock = stubFetch()
      fetchMock.mockResolvedValue(jsonResponse(200, { ...liveBody.rejectCount(LIVE, DATE), date }))

      await expect(getRejectCount('northbridge', 'central', DATE)).rejects.toMatchObject(UNEXPECTED)
    },
  )
})

describe('pipeline-status', () => {
  const pipeline = liveBody.pipeline(LIVE)

  it.each(PIPELINE_STATES)('accepts the state %s', async (state) => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, { ...pipeline, state }))

    await expect(getPipelineStatus('northbridge', 'central')).resolves.toMatchObject({ state })
  })

  it('accepts a null last_reported_at, and a timestamp with microseconds', async () => {
    const fetchMock = stubFetch()
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, { ...pipeline, state: 'unknown', last_reported_at: null }))
      .mockResolvedValueOnce(jsonResponse(200, { ...pipeline, last_reported_at: '2026-10-05T18:45:03.123456Z' }))

    await expect(getPipelineStatus('northbridge', 'central')).resolves.toStrictEqual({
      timezone: 'America/Chicago',
      state: 'unknown',
      last_reported_at: null,
    })
    await expect(getPipelineStatus('northbridge', 'central')).resolves.toMatchObject({
      last_reported_at: '2026-10-05T18:45:03.123456Z',
    })
  })

  it.each([
    ['a state it does not know', { ...pipeline, state: 'stale' }],
    ['a capitalised state', { ...pipeline, state: 'OK' }],
    ['a missing state', { ...pipeline, state: undefined }],
    ['a missing last_reported_at', { ...pipeline, last_reported_at: undefined }],
    ['a last_reported_at that is not a time', { ...pipeline, last_reported_at: 'recently' }],
    ['a numeric last_reported_at', { ...pipeline, last_reported_at: 1759689903 }],
  ])('rejects %s', async (_label, body) => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, body))

    await expect(getPipelineStatus('northbridge', 'central')).rejects.toMatchObject(UNEXPECTED)
  })
})

describe('counts', () => {
  it.each([
    ['negative', -1],
    ['fractional', 1.5],
    ['a string', '12'],
    ['null', null],
    ['missing', undefined],
  ])('must be whole and not negative: a %s check-in or reject count is rejected', async (_label, value) => {
    const fetchMock = stubFetch()
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, { ...liveBody.checkinCount(LIVE, DATE), checkin_count: value }))
      .mockResolvedValueOnce(jsonResponse(200, { ...liveBody.rejectCount(LIVE, DATE), reject_count: value }))

    await expect(getCheckinCount('northbridge', 'central', DATE)).rejects.toMatchObject(UNEXPECTED)
    await expect(getRejectCount('northbridge', 'central', DATE)).rejects.toMatchObject(UNEXPECTED)
  })

  it('may be zero', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, { ...liveBody.checkinCount(LIVE, DATE), checkin_count: 0 }))

    await expect(getCheckinCount('northbridge', 'central', DATE)).resolves.toMatchObject({ checkin_count: 0 })
  })
})

describe('checkins/by-hour', () => {
  const byHour = liveBody.checkinsByHour(LIVE, DATE)
  const withHours = (hours: unknown) => ({ ...byHour, hours })

  it('returns all 24 hours in order', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, byHour))

    const { hours } = await getCheckinsByHour('northbridge', 'central', DATE)

    expect(hours.map((entry) => entry.hour)).toEqual(Array.from({ length: 24 }, (_, hour) => hour))
    expect(hours[11]).toStrictEqual({ hour: 11, checkin_count: 40 })
  })

  it.each([
    ['23 hours', byHour.hours.slice(0, 23)],
    ['25 hours', [...byHour.hours, { hour: 24, checkin_count: 0 }]],
    ['no hours', []],
    ['hours out of order', [...byHour.hours].reverse()],
    ['an hour repeated', byHour.hours.map((entry) => ({ ...entry, hour: 0 }))],
    ['a negative count', byHour.hours.map((entry) => ({ ...entry, checkin_count: -1 }))],
    ['hours that are numbers, not objects', byHour.hours.map((entry) => entry.checkin_count)],
    ['hours keyed by hour', Object.fromEntries(byHour.hours.map((entry) => [entry.hour, entry.checkin_count]))],
    ['missing hours', undefined],
  ])('rejects %s', async (_label, hours) => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, withHours(hours)))

    await expect(getCheckinsByHour('northbridge', 'central', DATE)).rejects.toMatchObject(UNEXPECTED)
  })
})

describe('rejects/by-reason', () => {
  const byReason = liveBody.rejectsByReason(LIVE, DATE)
  const withReasons = (reasons: unknown) => ({ ...byReason, reasons })

  it('returns the eight reason codes in the order the API lists them', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, byReason))

    const { reasons } = await getRejectsByReason('northbridge', 'central', DATE)

    expect(reasons.map((entry) => entry.reason)).toEqual([...REJECT_REASONS])
    expect(reasons[0]).toStrictEqual({ reason: 'item_not_found', reject_count: 3 })
  })

  it.each([
    ['seven reasons', byReason.reasons.slice(0, 7)],
    ['a ninth reason', [...byReason.reasons, { reason: 'jammed', reject_count: 1 }]],
    ['a reason it does not know', byReason.reasons.map((entry, index) => (index === 6 ? { ...entry, reason: 'jammed' } : entry))],
    ['display text instead of a code', byReason.reasons.map((entry, index) => (index === 0 ? { ...entry, reason: 'Item not found' } : entry))],
    ['reasons out of order', [...byReason.reasons].reverse()],
    ['a negative count', byReason.reasons.map((entry) => ({ ...entry, reject_count: -1 }))],
    ['reasons keyed by code', Object.fromEntries(byReason.reasons.map((entry) => [entry.reason, entry.reject_count]))],
    ['missing reasons', undefined],
  ])('rejects %s', async (_label, reasons) => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, withReasons(reasons)))

    await expect(getRejectsByReason('northbridge', 'central', DATE)).rejects.toMatchObject(UNEXPECTED)
  })
})
