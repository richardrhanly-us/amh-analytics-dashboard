import { describe, expect, it } from 'vitest'

import { callOf, jsonResponse, NOT_AUTHENTICATED, REPORT, reportBody, stubFetch, TENANT_NOT_FOUND } from '../test/http.ts'
import { getBinVolumeReport } from './reports.ts'

const FROM = '2026-09-28'
const TO = '2026-10-04'
const UNEXPECTED = { code: 'unexpected_response' }

type Body = Record<string, unknown>
type Bin = { key: string; checkin_count: number; hours: number[] }

/** 24 hourly counts, zero but for the hours given. */
const hours = (counts: Record<number, number>) => Array.from({ length: 24 }, (_, hour) => counts[hour] ?? 0)
/** A bin whose check-ins all came in one hour. */
const bin = (key: string, count: number, hour = 10): Bin => ({ key, checkin_count: count, hours: hours({ [hour]: count }) })

/** A whole, self-consistent answer for these bins and this many check-ins with no recognized bin. */
function answer(bins: Bin[], unknown = 0): Body {
  const known = bins.reduce((total, entry) => total + entry.checkin_count, 0)
  return {
    range: { from: FROM, to: TO, days: 7, timezone: 'America/Chicago', includes_today: false },
    checkin_count: known + unknown,
    known_bin_count: known,
    unknown_bin_count: unknown,
    bins,
  }
}

const read = () => getBinVolumeReport('northbridge', 'central', FROM, TO)
function reading(body: unknown) {
  stubFetch().mockResolvedValue(jsonResponse(200, body))
  return read()
}
/** Answers with a copy of `body` changed by `change`, and reads it. */
function changed(body: Body, change: (body: Body) => unknown) {
  const copy = JSON.parse(JSON.stringify(body)) as Body
  const replacement = change(copy)
  return reading(replacement === undefined ? copy : replacement)
}
const binsOf = (body: Body) => body.bins as Array<Record<string, unknown>>

const SEVEN = answer(['0', '1', '2', '3', '4', '5', '6'].map((key, index) => bin(key, 10 + index, 8 + index)), 4)

describe('the request', () => {
  it('GETs exactly its URL, with the range, and returns the body unchanged', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, SEVEN))

    const result = await read()

    const call = callOf(fetchMock)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(call.url).toBe('/api/organizations/northbridge/branches/central/reports/bins?from=2026-09-28&to=2026-10-04')
    expect(call.init.method).toBe('GET')
    expect(call.init.body).toBeUndefined()
    expect(result).toStrictEqual(SEVEN)
  })

  it('URL-encodes both slugs and passes the abort signal', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, SEVEN))
    const controller = new AbortController()

    await getBinVolumeReport('north bridge?x', 'a/b', FROM, TO, controller.signal)

    expect(callOf(fetchMock).url).toBe(`/api/organizations/north%20bridge%3Fx/branches/a%2Fb/reports/bins?from=${FROM}&to=${TO}`)
    expect(callOf(fetchMock).init.signal).toBe(controller.signal)
  })

  it.each([
    ['2026-9-28', TO],
    [FROM, 'today'],
    ['', TO],
    ['2026-02-30', '2026-03-02'],
    [`${FROM}&x=1`, TO],
  ])('refuses the range %j to %j before anything is sent', async (from, to) => {
    const fetchMock = stubFetch()

    await expect(getBinVolumeReport('northbridge', 'central', from, to)).rejects.toThrow('YYYY-MM-DD')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('rejects with the API error for a 404, a 401 and a refused range', async () => {
    const fetchMock = stubFetch()
    fetchMock
      .mockResolvedValueOnce(jsonResponse(404, TENANT_NOT_FOUND))
      .mockResolvedValueOnce(jsonResponse(401, NOT_AUTHENTICATED))
      .mockResolvedValueOnce(jsonResponse(422, { code: 'validation_error', detail: [] }))

    await expect(read()).rejects.toMatchObject({ status: 404, code: 'tenant_not_found' })
    await expect(read()).rejects.toMatchObject({ status: 401, code: 'not_authenticated' })
    await expect(read()).rejects.toMatchObject({ status: 422 })
  })

  it('drops fields the contract does not have, at every level', async () => {
    const result = await changed(SEVEN, (body) => {
      body.customer_id = 41
      body.bin_coverage = 99.1
      body.unknown_bin = { key: 'unknown', checkin_count: 4 }
      binsOf(body)[0].label = 'Holds'
      binsOf(body)[0].destination = 'Westside'
      binsOf(body)[0].capacity = 200
    })

    expect(result).toStrictEqual(SEVEN)
    expect(JSON.stringify(result)).not.toMatch(/customer_id|coverage|Holds|Westside|capacity|"unknown"/)
  })
})

describe('the bins that were observed', () => {
  it.each([
    ['one bin', ['4']],
    ['three bins', ['1', '2', '3']],
    ['seven bins', ['0', '1', '2', '3', '4', '5', '6']],
    ['twelve bins', Array.from({ length: 12 }, (_, index) => String(index + 1))],
    ['twenty bins', Array.from({ length: 20 }, (_, index) => String(index + 1))],
    ['bins that are not consecutive', ['0', '3', '7', '12', '40', '205', '9999']],
    ['bin 2 before bin 10', ['2', '10']],
    ['only bin 0', ['0']],
  ])('accepts %s, as many as were sent and in the order sent', async (_label, keys) => {
    const body = answer(keys.map((key, index) => bin(key, index + 1, index % 24)))

    const report = await reading(body)

    expect(report.bins.map((entry) => entry.key)).toEqual(keys)
    expect(report.bins).toHaveLength(keys.length)
    expect(report.known_bin_count).toBe(report.checkin_count)
    expect(report.bins.every((entry) => entry.hours.length === 24)).toBe(true)
  })

  it('accepts what the test fixture answers for a week', async () => {
    const body = reportBody.bins(REPORT, FROM, TO)

    const report = await reading(body)

    expect(report).toStrictEqual(body)
    // Monday to Sunday: 760 check-ins; a twentieth of each day's has no recognized bin.
    expect([report.checkin_count, report.known_bin_count, report.unknown_bin_count]).toEqual([760, 722, 38])
    expect(report.bins.map((entry) => [entry.key, entry.checkin_count])).toEqual([['0', 152], ['1', 228], ['2', 190], ['10', 152]])
  })

  it('keeps the count of check-ins with no recognized bin beside the bins, not among them', async () => {
    const report = await reading(answer([bin('1', 30), bin('2', 12)], 41))

    expect([report.checkin_count, report.known_bin_count, report.unknown_bin_count]).toEqual([83, 42, 41])
    expect(report.bins.map((entry) => entry.key)).toEqual(['1', '2'])
  })

  it('accepts a range with no check-ins: zeros and no bins', async () => {
    const report = await reading(answer([]))

    expect(report.bins).toEqual([])
    expect([report.checkin_count, report.known_bin_count, report.unknown_bin_count]).toEqual([0, 0, 0])
  })

  it('accepts check-ins that all have no recognized bin: no bins, and every one of them unknown', async () => {
    const report = await reading(answer([], 57))

    expect(report.bins).toEqual([])
    expect([report.checkin_count, report.known_bin_count, report.unknown_bin_count]).toEqual([57, 0, 57])
  })

  it('keeps each bin’s 24 hours exactly as sent', async () => {
    const spread = { key: '3', checkin_count: 300, hours: Array.from({ length: 24 }, (_, hour) => hour + 1) }

    const report = await reading(answer([spread]))

    expect(report.bins[0].hours).toEqual(Array.from({ length: 24 }, (_, hour) => hour + 1))
    expect(report.bins[0].hours[0]).toBe(1)
    expect(report.bins[0].hours[23]).toBe(24)
  })
})

describe('an answer that is not the contract is refused whole', () => {
  it.each<[string, (body: Body) => unknown]>([
    // --- the range ---
    ['no range', (body) => void delete body.range],
    ['another start date', (body) => void ((body.range as Body).from = '2026-09-27')],
    ['a day count that is not the range’s', (body) => void ((body.range as Body).days = 8)],
    ['a zone that is not an IANA zone', (body) => void ((body.range as Body).timezone = 'Central Time')],
    ['an includes_today that is not true or false', (body) => void ((body.range as Body).includes_today = 'no')],
    // --- the counts ---
    ['a count as text', (body) => void (body.checkin_count = '109')],
    ['a count as a number in text, on a bin', (body) => void (binsOf(body)[0].checkin_count = '10')],
    ['a negative total', (body) => void (body.checkin_count = -109)],
    ['a negative unknown count', (body) => void (body.unknown_bin_count = -4)],
    ['a negative bin count', (body) => void (binsOf(body)[0].checkin_count = -10)],
    ['a fractional count', (body) => void (body.known_bin_count = 105.5)],
    ['a count that is null', (body) => void (body.unknown_bin_count = null)],
    ['no known count', (body) => void delete body.known_bin_count],
    ['no unknown count', (body) => void delete body.unknown_bin_count],
    ['a count that is true', (body) => void (body.checkin_count = true)],
    // --- the arithmetic ---
    ['known and unknown that do not add up to the total', (body) => void (body.checkin_count = 110)],
    ['an unknown count that does not fit the total', (body) => void (body.unknown_bin_count = 5)],
    ['bins that do not add up to the known count', (body) => void (body.known_bin_count = 104)],
    ['a bin whose hours do not add up to it', (body) => void ((binsOf(body)[2].hours as number[])[3] = 1)],
    ['a bin whose count is not its hours added up', (body) => void (binsOf(body)[2].checkin_count = 99)],
    // --- the hours ---
    ['a bin with 23 hours', (body) => void (binsOf(body)[0].hours as number[]).pop()],
    ['a bin with 25 hours', (body) => void (binsOf(body)[0].hours as number[]).push(0)],
    ['a bin with only the opening hours', (body) => void (binsOf(body)[0].hours = (binsOf(body)[0].hours as number[]).slice(7, 21))],
    ['a bin with no hours', (body) => void delete binsOf(body)[0].hours],
    ['hours that are an object by hour', (body) => void (binsOf(body)[0].hours = { 8: 10 })],
    ['hours as objects, the volume report’s way', (body) => void (binsOf(body)[0].hours = (binsOf(body)[0].hours as number[]).map((checkin_count, hour) => ({ hour, checkin_count })))],
    ['a negative hour', (body) => void ((binsOf(body)[0].hours as number[])[8] = -10)],
    ['a fractional hour', (body) => void ((binsOf(body)[0].hours as number[])[8] = 9.5)],
    ['an hour as text', (body) => void ((binsOf(body)[0].hours as unknown[])[8] = '10')],
    ['an hour that is null', (body) => void ((binsOf(body)[0].hours as unknown[])[0] = null)],
    // --- the keys ---
    ['a key that is a number', (body) => void (binsOf(body)[1].key = 1)],
    ['a key with a leading zero', (body) => void (binsOf(body)[1].key = '01')],
    ['a key that is a label', (body) => void (binsOf(body)[1].key = 'Bin 1')],
    ['a key that is "unknown"', (body) => void (binsOf(body)[1].key = 'unknown')],
    ['a key that is a destination', (body) => void (binsOf(body)[1].key = 'westside')],
    ['a negative key', (body) => void (binsOf(body)[1].key = '-1')],
    ['a key of five digits', (body) => void (binsOf(body)[6].key = '12345')],
    ['an empty key', (body) => void (binsOf(body)[1].key = '')],
    ['a padded key', (body) => void (binsOf(body)[1].key = ' 1')],
    ['no key', (body) => void delete binsOf(body)[1].key],
    // --- the list ---
    ['bins that are not a list', (body) => void (body.bins = { 0: 10 })],
    ['no bins at all', (body) => void delete body.bins],
    ['a bin that is not an object', (body) => void ((body.bins as unknown[])[0] = 10)],
    ['a bin listed twice', (body) => void (binsOf(body)[1].key = '0')],
    ['bins in text order: 10 before 2', (body) => void (binsOf(body)[1].key = '10')],
    ['bins in descending order', (body) => void binsOf(body).reverse()],
    [
      'a bin with no check-ins: one that was not observed',
      (body) => void binsOf(body).push({ key: '7', checkin_count: 0, hours: Array.from({ length: 24 }, () => 0) }),
    ],
    // --- the body ---
    ['a list', (body) => [body]],
    ['null', () => null],
    ['text', () => 'ok'],
  ])('rejects %s', async (_label, change) => {
    await expect(changed(SEVEN, change)).rejects.toMatchObject(UNEXPECTED)
  })

  it('does not repair an answer that is nearly right', async () => {
    // One too few in the total: no "nearest" reading of this is shown.
    await expect(changed(SEVEN, (body) => void (body.checkin_count = (body.checkin_count as number) - 1))).rejects.toMatchObject(UNEXPECTED)
    // An unknown count the total could have supplied is not worked out for the server.
    await expect(changed(SEVEN, (body) => void delete body.unknown_bin_count)).rejects.toMatchObject(UNEXPECTED)
    // Hours are not padded or trimmed to 24.
    await expect(changed(SEVEN, (body) => void (binsOf(body)[0].hours = [10]))).rejects.toMatchObject(UNEXPECTED)
  })
})
