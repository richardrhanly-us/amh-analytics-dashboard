import { describe, expect, it } from 'vitest'

import { callOf, jsonResponse, NOT_AUTHENTICATED, stubFetch, TENANT_NOT_FOUND } from '../test/http.ts'
import { getHoldsReport } from './reports.ts'

const FROM = '2026-09-28'
const TO = '2026-10-04'
const UNEXPECTED = { code: 'unexpected_response' }

type Body = Record<string, unknown>

const ANSWER: Body = {
  range: { from: FROM, to: TO, days: 7, timezone: 'America/Chicago', includes_today: false },
  public_hold_count: 41,
  ill_hold_count: 3,
  days_with_data: 6,
  public_holds_partial_days: 0,
  ill_uncertain_days: 0,
  unknown_alert_count: 0,
}

const read = () => getHoldsReport('northbridge', 'central', FROM, TO)
/** Answers with a copy of the answer changed by `change`, and reads it. */
function changed(change: (body: Body) => unknown) {
  const copy = JSON.parse(JSON.stringify(ANSWER)) as Body
  const replacement = change(copy)
  stubFetch().mockResolvedValue(jsonResponse(200, replacement === undefined ? copy : replacement))
  return read()
}

describe('the request', () => {
  it('GETs exactly its URL, with the range, and returns the body unchanged', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, ANSWER))

    const result = await read()

    const call = callOf(fetchMock)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(call.url).toBe('/api/organizations/northbridge/branches/central/reports/holds?from=2026-09-28&to=2026-10-04')
    expect(call.init.method).toBe('GET')
    expect(call.init.body).toBeUndefined()
    expect(result).toStrictEqual(ANSWER)
  })

  it('URL-encodes both slugs and passes the abort signal', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, ANSWER))
    const controller = new AbortController()

    await getHoldsReport('north bridge?x', 'a/b', FROM, TO, controller.signal)

    expect(callOf(fetchMock).url).toBe(`/api/organizations/north%20bridge%3Fx/branches/a%2Fb/reports/holds?from=${FROM}&to=${TO}`)
    expect(callOf(fetchMock).init.signal).toBe(controller.signal)
  })

  it.each([
    ['2026-9-28', TO],
    [FROM, 'today'],
    ['2026-02-30', '2026-03-02'],
  ])('refuses the range %j to %j before anything is sent', async (from, to) => {
    const fetchMock = stubFetch()

    await expect(getHoldsReport('northbridge', 'central', from, to)).rejects.toThrow('YYYY-MM-DD')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('rejects with the API error for a 404, a 401, a 403 and a refused range', async () => {
    const fetchMock = stubFetch()
    fetchMock
      .mockResolvedValueOnce(jsonResponse(404, TENANT_NOT_FOUND))
      .mockResolvedValueOnce(jsonResponse(401, NOT_AUTHENTICATED))
      .mockResolvedValueOnce(jsonResponse(403, { code: 'feature_not_available', message: 'This report is not available for this organization.' }))
      .mockResolvedValueOnce(jsonResponse(422, { code: 'validation_error', detail: [] }))

    await expect(read()).rejects.toMatchObject({ status: 404, code: 'tenant_not_found' })
    await expect(read()).rejects.toMatchObject({ status: 401, code: 'not_authenticated' })
    await expect(read()).rejects.toMatchObject({ status: 403, code: 'feature_not_available' })
    await expect(read()).rejects.toMatchObject({ status: 422 })
  })
})

describe('the answer', () => {
  it('accepts zero holds', async () => {
    const report = await changed((body) => {
      body.public_hold_count = 0
      body.ill_hold_count = 0
    })

    expect([report.public_hold_count, report.ill_hold_count]).toEqual([0, 0])
  })

  it('accepts a range with no days of data, and one where every day had data', async () => {
    expect((await changed((body) => void (body.days_with_data = 0))).days_with_data).toBe(0)
    expect((await changed((body) => void (body.days_with_data = 7))).days_with_data).toBe(7)
  })

  it('keeps what the confirmed counts could not include: partial and ILL-uncertain days, unrecognized alerts', async () => {
    const report = await changed((body) => {
      body.public_holds_partial_days = 4
      body.ill_uncertain_days = 6
      body.unknown_alert_count = 250
    })

    expect([report.public_holds_partial_days, report.ill_uncertain_days, report.unknown_alert_count]).toEqual([4, 6, 250])
    // The confirmed counts are as the server gave them: nothing is added for what is missing.
    expect([report.public_hold_count, report.ill_hold_count]).toEqual([41, 3])
  })

  it('accepts every day of data being partial and ILL-uncertain, and an unknown-alert count above the days', async () => {
    const report = await changed((body) => {
      body.public_holds_partial_days = 6
      body.ill_uncertain_days = 6
      body.unknown_alert_count = 9
    })

    expect([report.public_holds_partial_days, report.ill_uncertain_days, report.unknown_alert_count]).toEqual([6, 6, 9])
  })

  it('reads a server that predates the accuracy fields as having nothing to flag', async () => {
    // The frontend may be deployed before the backend (docs/hold-count-accuracy.md, deployment prerequisites):
    // that backend knows none of these, and its figures carry no partial day.
    const report = await changed((body) => {
      delete body.public_holds_partial_days
      delete body.ill_uncertain_days
      delete body.unknown_alert_count
    })

    expect(report).toStrictEqual(ANSWER)
  })

  it('drops fields the contract does not have', async () => {
    const result = await changed((body) => {
      body.customer_id = 41
      body.programming_hold_count = 2
      body.holds = [{ patron_id: 'p1', barcode: 'b1', destination: 'Westside' }]
    })

    expect(result).toStrictEqual(ANSWER)
    expect(JSON.stringify(result)).not.toMatch(/customer_id|programming|patron|barcode|Westside/)
  })

  it.each([
    ['a count that is missing', (body: Body) => void delete body.public_hold_count],
    ['a count that is negative', (body: Body) => void (body.ill_hold_count = -1)],
    ['a count that is not whole', (body: Body) => void (body.public_hold_count = 1.5)],
    ['a count that is a string', (body: Body) => void (body.ill_hold_count = '3')],
    ['a missing range', (body: Body) => void delete body.range],
    ['a range that was not asked for', (body: Body) => void ((body.range as Body).from = '2026-09-27')],
    ['a range whose days do not add up', (body: Body) => void ((body.range as Body).days = 8)],
    ['a body that is not an object', () => [41, 3]],
    ['days with data that are missing', (body: Body) => void delete body.days_with_data],
    ['more days with data than the range has', (body: Body) => void (body.days_with_data = 8)],
    ['days with data that are not whole', (body: Body) => void (body.days_with_data = 2.5)],
    ['partial days that are negative', (body: Body) => void (body.public_holds_partial_days = -1)],
    ['partial days that are not whole', (body: Body) => void (body.public_holds_partial_days = 0.5)],
    ['partial days that are a string', (body: Body) => void (body.public_holds_partial_days = '1')],
    ['partial days that are null', (body: Body) => void (body.public_holds_partial_days = null)],
    ['more partial days than days with data', (body: Body) => void (body.public_holds_partial_days = 7)],
    ['ILL-uncertain days that are negative', (body: Body) => void (body.ill_uncertain_days = -2)],
    ['ILL-uncertain days that are not a number', (body: Body) => void (body.ill_uncertain_days = true)],
    ['more ILL-uncertain days than days with data', (body: Body) => void (body.ill_uncertain_days = 7)],
    ['an unknown-alert count that is negative', (body: Body) => void (body.unknown_alert_count = -1)],
    ['an unknown-alert count that is not whole', (body: Body) => void (body.unknown_alert_count = 1.25)],
    ['an unknown-alert count that is a string', (body: Body) => void (body.unknown_alert_count = '0')],
  ])('refuses %s', async (_label, change) => {
    await expect(changed(change)).rejects.toMatchObject(UNEXPECTED)
  })
})
