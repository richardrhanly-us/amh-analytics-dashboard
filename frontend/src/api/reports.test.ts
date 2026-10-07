import { describe, expect, it } from 'vitest'

import { callOf, jsonResponse, NOT_AUTHENTICATED, REPORT, reportBody, stubFetch, TENANT_NOT_FOUND } from '../test/http.ts'
import { getOverviewReport, getReliabilityReport, getRoutingReport, getVolumeReport, REPORT_KINDS } from './reports.ts'

const FROM = '2026-09-28'
const TO = '2026-10-04'
const BASE = '/api/organizations/northbridge/branches/central'
const UNEXPECTED = { code: 'unexpected_response' }

type Body = Record<string, unknown>
type Reader = (org: string, branch: string, from: string, to: string, signal?: AbortSignal) => Promise<unknown>

/** Each of the four reports: how to call it and a body it must accept. */
const READS: Array<[kind: (typeof REPORT_KINDS)[number], read: Reader, body: Body]> = [
  ['overview', getOverviewReport, reportBody.overview(REPORT, FROM, TO)],
  ['volume', getVolumeReport, reportBody.volume(REPORT, FROM, TO)],
  ['routing', getRoutingReport, reportBody.routing(REPORT, FROM, TO)],
  ['reliability', getReliabilityReport, reportBody.reliability(REPORT, FROM, TO)],
]

/** Answers with `body` changed by `change`, and reads it. */
function reading(read: Reader, body: Body, change: (body: Body) => unknown) {
  const copy = JSON.parse(JSON.stringify(body)) as Body
  const replacement = change(copy)
  stubFetch().mockResolvedValue(jsonResponse(200, replacement === undefined ? copy : replacement))
  return read('northbridge', 'central', FROM, TO)
}
const rangeOf = (body: Body) => body.range as Body
const daysOf = (body: Body) => body.days as Body[]

describe('the four reports', () => {
  it('are, with Bin volume, exactly these five kinds', () => {
    // Bin volume (Reports R7B) is the fifth kind. It has no days, so it is tested on its own, in binVolume.test.ts.
    expect([...REPORT_KINDS]).toEqual(['overview', 'volume', 'routing', 'bins', 'reliability'])
  })

  describe.each(READS)('%s', (kind, read, body) => {
    it('GETs exactly its URL, with the range, and returns the body unchanged', async () => {
      const fetchMock = stubFetch()
      fetchMock.mockResolvedValue(jsonResponse(200, body))

      const result = await read('northbridge', 'central', FROM, TO)

      const call = callOf(fetchMock)
      expect(fetchMock).toHaveBeenCalledTimes(1)
      expect(call.url).toBe(`${BASE}/reports/${kind}?from=2026-09-28&to=2026-10-04`)
      expect(call.init.method).toBe('GET')
      expect(call.init.body).toBeUndefined()
      expect(result).toStrictEqual(body)
    })

    it('URL-encodes both slugs', async () => {
      const fetchMock = stubFetch()
      fetchMock.mockResolvedValue(jsonResponse(200, body))

      await read('north bridge?x', 'a/b', FROM, TO)

      expect(callOf(fetchMock).url).toBe(`/api/organizations/north%20bridge%3Fx/branches/a%2Fb/reports/${kind}?from=${FROM}&to=${TO}`)
    })

    it('passes the abort signal to fetch', async () => {
      const fetchMock = stubFetch()
      fetchMock.mockResolvedValue(jsonResponse(200, body))
      const controller = new AbortController()

      await read('northbridge', 'central', FROM, TO, controller.signal)

      expect(callOf(fetchMock).init.signal).toBe(controller.signal)
    })

    it('keeps every count exactly as sent', async () => {
      stubFetch().mockResolvedValue(jsonResponse(200, body))

      const result = (await read('northbridge', 'central', FROM, TO)) as Body

      expect(JSON.stringify(result)).toBe(JSON.stringify(body))
      expect(result.checkin_count).toBe(760) // Monday to Sunday: 100 + 120 + 140 + 160 + 180 + 60 + 0
    })

    it('drops fields the contract does not have, at every level', async () => {
      const result = await reading(read, body, (changed) => {
        changed.customer_id = 41
        changed.reject_rate = 4.9
        rangeOf(changed).cutover_at = '2026-06-10T17:00:00Z'
        daysOf(changed)[0].barcode = '31234000123456'
      })

      expect(result).toStrictEqual(body)
      expect(JSON.stringify(result)).not.toMatch(/customer_id|reject_rate|cutover|barcode|31234/)
    })

    it.each([
      ['2026-9-28', TO],
      ['09/28/2026', TO],
      [FROM, '2026-10-04T00:00:00'],
      ['', TO],
      [FROM, 'today'],
      ['2026-02-30', '2026-03-02'],
      [`${FROM}&x=1`, TO],
    ])('refuses the range %j to %j before anything is sent', async (from, to) => {
      const fetchMock = stubFetch()

      await expect(read('northbridge', 'central', from, to)).rejects.toThrow('YYYY-MM-DD')
      expect(fetchMock).not.toHaveBeenCalled()
    })

    it('rejects with the API error for a 404 and a 401', async () => {
      const fetchMock = stubFetch()
      fetchMock.mockResolvedValueOnce(jsonResponse(404, TENANT_NOT_FOUND)).mockResolvedValueOnce(jsonResponse(401, NOT_AUTHENTICATED))

      await expect(read('northbridge', 'central', FROM, TO)).rejects.toMatchObject({ status: 404, code: 'tenant_not_found' })
      await expect(read('northbridge', 'central', FROM, TO)).rejects.toMatchObject({ status: 401, code: 'not_authenticated' })
    })

    it('rejects with the validation error for a range the API refuses', async () => {
      stubFetch().mockResolvedValue(jsonResponse(422, { code: 'validation_error', detail: [{ loc: ['query', 'to'], type: 'report_range_in_future', msg: 'x' }] }))

      await expect(read('northbridge', 'central', FROM, TO)).rejects.toMatchObject({ status: 422 })
    })

    it.each<[string, (body: Body) => unknown]>([
      ['no range', (changed) => void delete changed.range],
      ['a range that is not an object', (changed) => void (changed.range = `${FROM}/${TO}`)],
      ['another start date', (changed) => void (rangeOf(changed).from = '2026-09-27')],
      ['another end date', (changed) => void (rangeOf(changed).to = '2026-10-05')],
      ['a day count that is not the range’s', (changed) => void (rangeOf(changed).days = 8)],
      ['a day count as text', (changed) => void (rangeOf(changed).days = '7')],
      ['a zone that is not an IANA zone', (changed) => void (rangeOf(changed).timezone = 'Central Time')],
      ['no zone', (changed) => void delete rangeOf(changed).timezone],
      ['an includes_today that is not true or false', (changed) => void (rangeOf(changed).includes_today = 'no')],
      ['no includes_today', (changed) => void delete rangeOf(changed).includes_today],
      ['days that are not a list', (changed) => void (changed.days = { '2026-09-28': 100 })],
      ['a day missing', (changed) => void daysOf(changed).pop()],
      ['a day too many', (changed) => void daysOf(changed).push({ ...daysOf(changed)[6], date: '2026-10-05' })],
      ['no days at all', (changed) => void (changed.days = [])],
      ['days out of order', (changed) => void daysOf(changed).reverse()],
      ['a day repeated', (changed) => void (daysOf(changed)[1].date = daysOf(changed)[0].date)],
      ['a day outside the range', (changed) => void (daysOf(changed)[6].date = '2026-10-09')],
      ['a day that is a timestamp', (changed) => void (daysOf(changed)[0].date = '2026-09-28T00:00:00Z')],
      ['a day that is not an object', (changed) => void (daysOf(changed)[0] = 100 as never)],
      ['a negative count', (changed) => void (daysOf(changed)[0].checkin_count = -1)],
      ['a fractional count', (changed) => void (daysOf(changed)[0].checkin_count = 100.5)],
      ['a count as text', (changed) => void (changed.checkin_count = '760')],
      ['a total that is not its days added up', (changed) => void (changed.checkin_count = 761)],
      ['a list', (changed) => [changed]],
      ['null', () => null],
    ])('rejects %s as an unexpected response', async (_label, change) => {
      await expect(reading(read, body, change)).rejects.toMatchObject(UNEXPECTED)
    })
  })
})

describe('overview', () => {
  const body = reportBody.overview(REPORT, FROM, TO)

  it('returns the totals, the split and the days', async () => {
    stubFetch().mockResolvedValue(jsonResponse(200, body))

    expect(await getOverviewReport('northbridge', 'central', FROM, TO)).toMatchObject({
      range: { from: FROM, to: TO, days: 7, timezone: 'America/Chicago', includes_today: false },
      checkin_count: 760,
      active_days: 6,
      home_count: 646,
      transit_count: 114,
      other_count: 0,
      reject_count: 38,
    })
  })

  it.each<[string, (body: Body) => unknown]>([
    ['a split that does not add up to the total', (changed) => void (changed.home_count = 600)],
    ['an active-day count that is not the days with check-ins', (changed) => void (changed.active_days = 7)],
    ['a reject total that is not its days added up', (changed) => void (changed.reject_count = 40)],
    ['no reject count', (changed) => void delete changed.reject_count],
    ['a day with no reject count', (changed) => void delete daysOf(changed)[2].reject_count],
  ])('rejects %s', async (_label, change) => {
    await expect(reading(getOverviewReport, body, change)).rejects.toMatchObject(UNEXPECTED)
  })
})

describe('volume', () => {
  const body = reportBody.volume(REPORT, FROM, TO)
  const hoursOf = (changed: Body) => changed.hours as Body[]

  it('returns all 24 hours in order, as totals across the range', async () => {
    stubFetch().mockResolvedValue(jsonResponse(200, body))

    const report = await getVolumeReport('northbridge', 'central', FROM, TO)

    expect(report.hours.map((hour) => hour.hour)).toEqual(Array.from({ length: 24 }, (_, hour) => hour))
    expect(report.hours.filter((hour) => hour.checkin_count > 0)).toEqual([
      { hour: 10, checkin_count: 380 },
      { hour: 14, checkin_count: 380 },
    ])
  })

  it.each<[string, (body: Body) => unknown]>([
    ['23 hours', (changed) => void hoursOf(changed).pop()],
    ['25 hours', (changed) => void hoursOf(changed).push({ hour: 24, checkin_count: 0 })],
    ['only the hours a library is open', (changed) => void (changed.hours = hoursOf(changed).slice(7, 21))],
    ['hours out of order', (changed) => void hoursOf(changed).reverse()],
    ['an hour numbered wrongly', (changed) => void (hoursOf(changed)[5].hour = 6)],
    ['hours that are not a list', (changed) => void (changed.hours = { 10: 380 })],
    ['no hours', (changed) => void delete changed.hours],
    ['a negative hour count', (changed) => void (hoursOf(changed)[10].checkin_count = -380)],
    ['hours that do not add up to the total', (changed) => void (hoursOf(changed)[3].checkin_count = 5)],
    ['hourly averages in place of totals', (changed) => void (hoursOf(changed)[10].checkin_count = 54.3)],
  ])('rejects %s', async (_label, change) => {
    await expect(reading(getVolumeReport, body, change)).rejects.toMatchObject(UNEXPECTED)
  })
})

describe('routing', () => {
  const body = reportBody.routing(REPORT, FROM, TO)
  const transitOf = (changed: Body) => changed.transit as Body[]

  it('returns home, each destination in the order given, and each day lined up with them', async () => {
    stubFetch().mockResolvedValue(jsonResponse(200, body))

    const report = await getRoutingReport('northbridge', 'central', FROM, TO)

    expect(report.transit).toEqual([
      { key: 'westside', label: 'Westside', checkin_count: 76 },
      { key: 'library_express', label: 'Library Express', checkin_count: 38 },
    ])
    expect(report.home).toEqual({ label: 'Main', checkin_count: 646 })
    expect(report.days[0]).toEqual({ date: FROM, checkin_count: 100, home_count: 85, transit_counts: [10, 5], other_count: 0 })
    expect(report.days.every((day) => day.transit_counts.length === report.transit.length)).toBe(true)
  })

  it('accepts a sorter with no destinations, each day then having none', async () => {
    const none = reportBody.routing({ ...REPORT, transit: [], other: 0.1 }, FROM, TO)
    stubFetch().mockResolvedValue(jsonResponse(200, none))

    const report = await getRoutingReport('northbridge', 'central', FROM, TO)

    expect(report.transit).toEqual([])
    expect(report.days.every((day) => day.transit_counts.length === 0)).toBe(true)
    expect(report.other_count).toBe(76)
  })

  it('accepts many destinations', async () => {
    const many = reportBody.routing(
      { ...REPORT, transit: Array.from({ length: 30 }, (_, index) => [`stop_${index}`, `Stop ${index}`, 0.01] as [string, string, number]) },
      FROM,
      TO,
    )
    stubFetch().mockResolvedValue(jsonResponse(200, many))

    expect((await getRoutingReport('northbridge', 'central', FROM, TO)).transit).toHaveLength(30)
  })

  it.each<[string, (body: Body) => unknown]>([
    ['a day with one count too few', (changed) => void (daysOf(changed)[0].transit_counts as number[]).pop()],
    ['a day with one count too many', (changed) => void (daysOf(changed)[0].transit_counts as number[]).push(0)],
    ['a day with no transit counts', (changed) => void delete daysOf(changed)[3].transit_counts],
    ['a day whose transit counts are an object by key', (changed) => void (daysOf(changed)[0].transit_counts = { westside: 10, library_express: 5 })],
    ['a day whose counts are in another order', (changed) => void (daysOf(changed)[0].transit_counts as number[]).reverse()],
    ['a day whose parts do not add up to its total', (changed) => void (daysOf(changed)[0].other_count = 3)],
    ['a negative count in a day', (changed) => void ((daysOf(changed)[0].transit_counts as number[])[0] = -10)],
    ['a destination whose days do not add up to it', (changed) => void (transitOf(changed)[0].checkin_count = 77)],
    ['a transit total that is not the sum of its destinations', (changed) => void (changed.transit_count = 115)],
    ['parts that do not add up to the total', (changed) => void (changed.other_count = 4)],
    ['two destinations with one key', (changed) => void (transitOf(changed)[1].key = 'westside')],
    ['a key that is not a slug', (changed) => void (transitOf(changed)[0].key = 'West Side')],
    ['a destination with no label', (changed) => void (transitOf(changed)[0].label = '  ')],
    ['transit that is not a list', (changed) => void (changed.transit = { westside: 76 })],
    ['no home', (changed) => void delete changed.home],
    ['a home with no label', (changed) => void ((changed.home as Body).label = '')],
  ])('rejects %s', async (_label, change) => {
    await expect(reading(getRoutingReport, body, change)).rejects.toMatchObject(UNEXPECTED)
  })
})

describe('reliability', () => {
  const body = reportBody.reliability(REPORT, FROM, TO)
  const reasonsOf = (changed: Body) => changed.reasons as Body[]

  it('returns the eight reason codes in the order the API lists them', async () => {
    stubFetch().mockResolvedValue(jsonResponse(200, body))

    const report = await getReliabilityReport('northbridge', 'central', FROM, TO)

    expect(report.reasons.map((reason) => reason.reason)).toEqual([
      'item_not_found',
      'ils_acs_failure',
      'rfid_collision',
      'configuration_error',
      'routing_error',
      'communication_error',
      'other',
      'unknown',
    ])
    expect(report.reasons.reduce((sum, reason) => sum + reason.reject_count, 0)).toBe(report.reject_count)
    expect(report.reject_count).toBe(38)
  })

  it.each<[string, (body: Body) => unknown]>([
    ['seven reasons', (changed) => void reasonsOf(changed).pop()],
    ['nine reasons', (changed) => void reasonsOf(changed).push({ reason: 'jam', reject_count: 0 })],
    ['reasons out of order', (changed) => void reasonsOf(changed).reverse()],
    ['a reason code this app does not know', (changed) => void (reasonsOf(changed)[6].reason = 'belt_jam')],
    ['a stored message in place of a code', (changed) => void (reasonsOf(changed)[0].reason = 'Item not found in database')],
    ['a reason repeated', (changed) => void (reasonsOf(changed)[1].reason = 'item_not_found')],
    ['reasons that are not a list', (changed) => void (changed.reasons = { item_not_found: 38 })],
    ['no reasons', (changed) => void delete changed.reasons],
    ['a negative reason count', (changed) => void (reasonsOf(changed)[0].reject_count = -1)],
    ['reasons that do not add up to the reject total', (changed) => void (reasonsOf(changed)[7].reject_count = 2)],
    ['days whose rejects do not add up to the reject total', (changed) => void (daysOf(changed)[0].reject_count = 9)],
  ])('rejects %s', async (_label, change) => {
    await expect(reading(getReliabilityReport, body, change)).rejects.toMatchObject(UNEXPECTED)
  })
})
