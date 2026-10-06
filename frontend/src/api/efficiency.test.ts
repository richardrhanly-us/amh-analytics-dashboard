import { describe, expect, it } from 'vitest'

import { efficiencyBody, efficiencyStore, emptyStore, FORBIDDEN, invalidSettings, STORED_INVALID } from '../test/efficiency.ts'
import { callOf, jsonResponse, NOT_AUTHENTICATED, stubFetch, TENANT_NOT_FOUND } from '../test/http.ts'
import { ApiError } from './client.ts'
import {
  getEfficiencyReport,
  getOrganizationEfficiencySettings,
  getSorterEfficiencySettings,
  putOrganizationEfficiencySettings,
  putSorterEfficiencySettings,
  settingProblems,
} from './efficiency.ts'

const FROM = '2026-09-28'
const TO = '2026-10-04'
const SITE = '/api/organizations/northbridge/branches/central'
const UNEXPECTED = { code: 'unexpected_response' }

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- a JSON body a test is about to break on purpose
type Body = Record<string, any>

const STORE = efficiencyStore({ sorter: { labor_rate: '21.50' } })
const REPORT_BODY = efficiencyBody.report(STORE, FROM, TO)
const ORGANIZATION_BODY = efficiencyBody.organization(STORE)
const SORTER_BODY = efficiencyBody.sorter(STORE)

/** Answers with `body` changed by `change`, and reads it with `read`. */
function reading<T>(read: () => Promise<T>, body: Body, change: (body: Body) => unknown = () => undefined): Promise<T> {
  const copy = JSON.parse(JSON.stringify(body)) as Body
  const replacement = change(copy)
  stubFetch().mockResolvedValue(jsonResponse(200, replacement === undefined ? copy : replacement))
  return read()
}
const report = () => getEfficiencyReport('northbridge', 'central', FROM, TO)

describe('the Efficiency report', () => {
  it('GETs exactly its URL, by the sorter’s host branch, and returns the body unchanged', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, REPORT_BODY))
    const controller = new AbortController()

    const result = await getEfficiencyReport('northbridge', 'central', FROM, TO, controller.signal)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(callOf(fetchMock).url).toBe(`${SITE}/reports/efficiency?from=2026-09-28&to=2026-10-04`)
    expect(callOf(fetchMock).init.method).toBe('GET')
    expect(callOf(fetchMock).init.signal).toBe(controller.signal)
    expect(result).toStrictEqual(REPORT_BODY)
    expect(JSON.stringify(result)).toBe(JSON.stringify(REPORT_BODY))
  })

  it('is built on a fixture with the figures the test says', () => {
    // Monday to Sunday: 760 check-ins. 760 / 40 = 19 h; x 21.50 = 408.50; 7300 / 365 * 7 = 140.00.
    expect(REPORT_BODY.checkin_count).toBe(760)
    expect(REPORT_BODY.assumptions.labor_rate).toEqual({ value: '21.50', source: 'sorter' })
    expect(REPORT_BODY.assumptions.manual_items_per_hour).toEqual({ value: '40.0', source: 'organization' })
    expect(REPORT_BODY.results).toEqual({
      in_service_days: 7,
      in_service_checkin_count: 760,
      staff_time_equivalent_hours: '19.00',
      labor_value_equivalent: '408.50',
      recurring_cost: '140.00',
      net_operational_value: '268.50',
      recurring_cost_per_item: '0.1842',
    })
    expect(REPORT_BODY.missing).toEqual([])
  })

  it('keeps every decimal as the text it arrived as', async () => {
    const result = await reading(report, REPORT_BODY)

    for (const figure of [result.results.staff_time_equivalent_hours, result.results.labor_value_equivalent, result.results.recurring_cost_per_item, result.assumptions.recurring_annual_cost]) {
      expect(typeof figure).toBe('string')
    }
    expect(typeof result.checkin_count).toBe('number')
  })

  it('URL-encodes both slugs and refuses a range that is not two dates before anything is sent', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, REPORT_BODY))

    await getEfficiencyReport('north bridge?x', 'a/b', FROM, TO)
    expect(callOf(fetchMock).url).toBe(`/api/organizations/north%20bridge%3Fx/branches/a%2Fb/reports/efficiency?from=${FROM}&to=${TO}`)

    for (const [from, to] of [['2026-9-28', TO], [FROM, 'today'], ['2026-02-30', '2026-03-02'], [`${FROM}&x=1`, TO]]) {
      await expect(getEfficiencyReport('northbridge', 'central', from, to)).rejects.toThrow('YYYY-MM-DD')
    }
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('accepts a report with nothing configured: counts, nulls and all four assumptions missing', async () => {
    const body = efficiencyBody.report(emptyStore(), FROM, TO)

    const result = await reading(report, body)

    expect(result).toStrictEqual(body)
    expect(result.missing).toEqual(['manual_items_per_hour', 'labor_rate', 'recurring_annual_cost', 'in_service_date'])
    expect(Object.values(result.results).filter((value) => value === null)).toHaveLength(5)
  })

  it('accepts a cost of zero, a net below zero, a range with nothing processed and one that starts before the in-service date', async () => {
    const zeroCost = efficiencyBody.report(efficiencyStore({ sorter: { recurring_annual_cost: '0.00' } }), FROM, TO)
    const negative = efficiencyBody.report(efficiencyStore({ sorter: { recurring_annual_cost: '9000000.00' } }), FROM, TO)
    const nothing = efficiencyBody.report({ ...efficiencyStore(), checkins: () => 0 }, FROM, TO)
    const crossed = efficiencyBody.report(efficiencyStore({ sorter: { in_service_date: '2026-10-01' } }), FROM, TO)

    expect((await reading(report, zeroCost)).results).toMatchObject({ recurring_cost: '0.00', recurring_cost_per_item: '0.0000' })
    expect((await reading(report, negative)).results.net_operational_value).toMatch(/^-\d+\.\d{2}$/)
    expect((await reading(report, nothing)).results).toMatchObject({
      in_service_checkin_count: 0,
      staff_time_equivalent_hours: '0.00',
      labor_value_equivalent: '0.00',
      recurring_cost: '140.00',
      net_operational_value: '-140.00',
      recurring_cost_per_item: null,
    })
    const partial = await reading(report, crossed)
    expect([partial.checkin_count, partial.results.in_service_checkin_count, partial.results.in_service_days]).toEqual([760, 400, 4])
  })

  it('drops fields the contract does not have', async () => {
    const result = await reading(report, REPORT_BODY, (changed) => {
      changed.customer_id = 41
      changed.results.annualized_value = '9999.00'
      changed.assumptions.labor_rate.user_id = 7
    })

    expect(result).toStrictEqual(REPORT_BODY)
  })

  it.each<[string, (body: Body) => unknown]>([
    ['a body that is not an object', () => [1]],
    ['another range', (changed) => void (changed.range.from = '2026-09-27')],
    ['a currency it does not know', (changed) => void (changed.currency = 'EUR')],
    ['no currency', (changed) => void delete changed.currency],
    ['a check-in count as text', (changed) => void (changed.checkin_count = '760')],
    ['a negative count', (changed) => void (changed.results.in_service_days = -1)],
    ['a fractional count', (changed) => void (changed.results.in_service_checkin_count = 759.5)],
    ['no assumptions', (changed) => void delete changed.assumptions],
    ['no results', (changed) => void delete changed.results],
    // A JSON number where text belongs has already been through a binary float.
    ['hours as a number', (changed) => void (changed.results.staff_time_equivalent_hours = 19)],
    ['money as a number', (changed) => void (changed.results.labor_value_equivalent = 408.5)],
    ['cost per item as a number', (changed) => void (changed.results.recurring_cost_per_item = 0.1842)],
    ['a rate as a number', (changed) => void (changed.assumptions.manual_items_per_hour.value = 40)],
    ['a cost assumption as a number', (changed) => void (changed.assumptions.recurring_annual_cost = 7300)],
    ['money with one decimal place', (changed) => void (changed.results.labor_value_equivalent = '408.5')],
    ['money with three decimal places', (changed) => void (changed.results.recurring_cost = '140.000')],
    ['money with a currency symbol', (changed) => void (changed.results.recurring_cost = '$140.00')],
    ['money with a separator', (changed) => void (changed.assumptions.one_time_cost = '125,000.00')],
    ['money in exponent form', (changed) => void (changed.results.recurring_cost = '1.4e2')],
    ['money that is not a number', (changed) => void (changed.results.recurring_cost = 'NaN')],
    ['hours below zero', (changed) => void (changed.results.staff_time_equivalent_hours = '-19.00')],
    ['a cost below zero', (changed) => void (changed.results.recurring_cost = '-140.00')],
    ['cost per item with two places', (changed) => void (changed.results.recurring_cost_per_item = '0.18')],
    ['a manual rate with two places', (changed) => void (changed.assumptions.manual_items_per_hour.value = '40.00')],
    ['a rate with a source it does not know', (changed) => void (changed.assumptions.labor_rate.source = 'default')],
    ['a rate with no source', (changed) => void delete changed.assumptions.labor_rate.source],
    ['a rate that is bare text', (changed) => void (changed.assumptions.labor_rate = '21.50')],
    ['an in-service date that is not a date', (changed) => void (changed.assumptions.in_service_date = '2020-11-31')],
    ['an in-service date with a time', (changed) => void (changed.assumptions.in_service_date = '2020-11-20T00:00:00Z')],
    ['missing that is not a list', (changed) => void (changed.missing = 'none')],
    ['something listed as missing that is set', (changed) => void (changed.missing = ['labor_rate'])],
    ['something missing that is not listed', (changed) => {
      changed.assumptions.one_time_cost = null
      changed.assumptions.labor_rate = null
    }],
    ['a missing assumption it does not know', (changed) => void (changed.missing = ['one_time_cost'])],
    ['more in-service days than the range has', (changed) => void (changed.results.in_service_days = 8)],
    ['more in-service check-ins than check-ins', (changed) => void (changed.results.in_service_checkin_count = 761)],
    ['an estimate with no assumption behind it', (changed) => {
      changed.assumptions.manual_items_per_hour = null
      changed.missing = ['manual_items_per_hour']
    }],
    ['no estimate though the assumption is there', (changed) => void (changed.results.staff_time_equivalent_hours = null)],
    ['a labor value with no labor rate', (changed) => {
      changed.assumptions.labor_rate = null
      changed.missing = ['labor_rate']
    }],
    ['a cost with no cost configured', (changed) => {
      changed.assumptions.recurring_annual_cost = null
      changed.missing = ['recurring_annual_cost']
    }],
    ['a cost per item with nothing processed', (changed) => void (changed.results.in_service_checkin_count = 0)],
    ['no net figure though both parts are there', (changed) => void (changed.results.net_operational_value = null)],
    [
      'fewer days counted than the range though no in-service date is set',
      (changed) => {
        changed.assumptions.in_service_date = null
        changed.missing = ['in_service_date']
        changed.results.in_service_days = 6
      },
    ],
  ])('refuses %s', async (_label, change) => {
    await expect(reading(report, REPORT_BODY, change)).rejects.toMatchObject(UNEXPECTED)
  })

  it('rejects with the API’s own error for 401, 403, 404, 422 and malformed stored assumptions', async () => {
    const fetchMock = stubFetch()
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, NOT_AUTHENTICATED))
      .mockResolvedValueOnce(jsonResponse(403, FORBIDDEN))
      .mockResolvedValueOnce(jsonResponse(404, TENANT_NOT_FOUND))
      .mockResolvedValueOnce(jsonResponse(422, { code: 'validation_error', detail: [] }))
      .mockResolvedValueOnce(jsonResponse(500, STORED_INVALID))

    await expect(report()).rejects.toMatchObject({ status: 401, code: 'not_authenticated' })
    await expect(report()).rejects.toMatchObject({ status: 403, code: 'forbidden' })
    await expect(report()).rejects.toMatchObject({ status: 404 })
    await expect(report()).rejects.toMatchObject({ status: 422 })
    await expect(report()).rejects.toMatchObject({ status: 500, code: 'efficiency_settings_invalid' })
  })
})

describe('an organization’s Efficiency defaults', () => {
  const PATH = '/api/organizations/northbridge/settings/efficiency'
  const read = () => getOrganizationEfficiencySettings('northbridge')

  it('GETs exactly its URL and returns the two defaults as text', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, ORGANIZATION_BODY))

    expect(await read()).toStrictEqual({ labor_rate: '18.00', manual_items_per_hour: '40.0' })
    expect(callOf(fetchMock).url).toBe(PATH)
    expect(callOf(fetchMock).init.method).toBe('GET')
  })

  it('keeps null as null, and zero-like text as text', async () => {
    expect(await reading(read, efficiencyBody.organization(emptyStore()))).toStrictEqual({ labor_rate: null, manual_items_per_hour: null })
    expect(await reading(read, { efficiency: { labor_rate: '0.01', manual_items_per_hour: '1.0' } })).toStrictEqual({ labor_rate: '0.01', manual_items_per_hour: '1.0' })
  })

  it.each<[string, (body: Body) => unknown]>([
    ['no efficiency block', (changed) => void delete changed.efficiency],
    ['a block that is a list', (changed) => void (changed.efficiency = [])],
    ['a labor rate as a number', (changed) => void (changed.efficiency.labor_rate = 18)],
    ['a manual rate as a number', (changed) => void (changed.efficiency.manual_items_per_hour = 40)],
    ['a labor rate with one decimal place', (changed) => void (changed.efficiency.labor_rate = '18.0')],
    ['a manual rate with two decimal places', (changed) => void (changed.efficiency.manual_items_per_hour = '40.00')],
    ['a field left out', (changed) => void delete changed.efficiency.labor_rate],
    ['an empty string for nothing', (changed) => void (changed.efficiency.labor_rate = '')],
  ])('refuses %s', async (_label, change) => {
    await expect(reading(read, ORGANIZATION_BODY, change)).rejects.toMatchObject(UNEXPECTED)
  })

  it('PUTs exactly the two fields, as the text given and null for nothing, and returns what is then stored', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, { efficiency: { labor_rate: '19.50', manual_items_per_hour: null } }))

    const stored = await putOrganizationEfficiencySettings('northbridge', { labor_rate: '19.5', manual_items_per_hour: null })

    const call = callOf(fetchMock)
    expect(call.url).toBe(PATH)
    expect(call.init.method).toBe('PUT')
    expect(call.headers['Content-Type']).toBe('application/json')
    // Sent as typed -- "19.5", not a number and not tidied -- and the answer is what the API stored.
    expect(call.init.body).toBe('{"labor_rate":"19.5","manual_items_per_hour":null}')
    expect(stored).toStrictEqual({ labor_rate: '19.50', manual_items_per_hour: null })
  })

  it('sends nothing but the two fields, whatever else it is handed', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, ORGANIZATION_BODY))

    await putOrganizationEfficiencySettings('northbridge', { labor_rate: '18.00', manual_items_per_hour: '40.0', one_time_cost: '5.00' } as never)

    expect(JSON.parse(String(callOf(fetchMock).init.body))).toStrictEqual({ labor_rate: '18.00', manual_items_per_hour: '40.0' })
  })
})

describe('a sorter’s Efficiency settings', () => {
  const PATH = `${SITE}/settings/efficiency`
  const read = () => getSorterEfficiencySettings('northbridge', 'central')

  it('GETs exactly its URL and returns each rate with its default, its override and which applies', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, SORTER_BODY))

    expect(await read()).toStrictEqual({
      labor_rate: { organization: '18.00', sorter: '21.50', effective: '21.50', source: 'sorter' },
      manual_items_per_hour: { organization: '40.0', sorter: null, effective: '40.0', source: 'organization' },
      one_time_cost: '125000.00',
      recurring_annual_cost: '7300.00',
      in_service_date: '2020-11-20',
    })
    expect(callOf(fetchMock).url).toBe(PATH)
  })

  it('accepts a sorter with nothing anywhere, and an explicit zero cost', async () => {
    const nothing = await reading(read, efficiencyBody.sorter(emptyStore()))
    const zero = await reading(read, efficiencyBody.sorter(efficiencyStore({ sorter: { one_time_cost: '0.00', recurring_annual_cost: '0.00' } })))

    expect(nothing.labor_rate).toStrictEqual({ organization: null, sorter: null, effective: null, source: null })
    expect([nothing.one_time_cost, nothing.recurring_annual_cost, nothing.in_service_date]).toEqual([null, null, null])
    expect([zero.one_time_cost, zero.recurring_annual_cost]).toEqual(['0.00', '0.00'])
  })

  it.each<[string, (body: Body) => unknown]>([
    ['a rate that is bare text', (changed) => void (changed.efficiency.labor_rate = '21.50')],
    ['a rate with a part left out', (changed) => void delete changed.efficiency.labor_rate.organization],
    ['a source it does not know', (changed) => void (changed.efficiency.labor_rate.source = 'inherited')],
    ['a source that is not the one in effect', (changed) => void (changed.efficiency.labor_rate.source = 'organization')],
    ['an effective value that is neither the default nor the override', (changed) => void (changed.efficiency.labor_rate.effective = '99.00')],
    ['an effective value where there is none to have', (changed) => {
      changed.efficiency.manual_items_per_hour = { organization: null, sorter: null, effective: '40.0', source: null }
    }],
    ['no effective value though a default exists', (changed) => void (changed.efficiency.manual_items_per_hour.effective = null)],
    ['a cost as a number', (changed) => void (changed.efficiency.recurring_annual_cost = 7300)],
    ['a cost with no decimal places', (changed) => void (changed.efficiency.one_time_cost = '125000')],
    ['a date that is not one', (changed) => void (changed.efficiency.in_service_date = '20 Nov 2020')],
    ['a field left out', (changed) => void delete changed.efficiency.in_service_date],
  ])('refuses %s', async (_label, change) => {
    await expect(reading(read, SORTER_BODY, change)).rejects.toMatchObject(UNEXPECTED)
  })

  it('PUTs exactly the five fields: text as given, null to clear, zero as zero', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, SORTER_BODY))

    await putSorterEfficiencySettings('northbridge', 'central', {
      labor_rate: null,
      manual_items_per_hour: '52',
      one_time_cost: '0',
      recurring_annual_cost: '7300.00',
      in_service_date: '2020-11-20',
    })

    const call = callOf(fetchMock)
    expect(call.url).toBe(PATH)
    expect(call.init.method).toBe('PUT')
    expect(call.init.body).toBe(
      '{"labor_rate":null,"manual_items_per_hour":"52","one_time_cost":"0","recurring_annual_cost":"7300.00","in_service_date":"2020-11-20"}',
    )
  })
})

describe('what the API says is wrong with a PUT', () => {
  async function failure(status: number, body: unknown): Promise<unknown> {
    stubFetch().mockResolvedValue(jsonResponse(status, body))
    return putOrganizationEfficiencySettings('northbridge', { labor_rate: 'x', manual_items_per_hour: null }).catch((error: unknown) => error)
  }

  it('is each refused field and its code, and nothing else', async () => {
    const error = await failure(422, { ...invalidSettings(['labor_rate', 'too_many_decimal_places'], ['amh_rate', 'unknown_field']), value: 'CANARY' })

    expect(error).toMatchObject({ status: 422, code: 'invalid_efficiency_settings' })
    expect(settingProblems(error)).toStrictEqual([
      { field: 'labor_rate', code: 'too_many_decimal_places' },
      { field: 'amh_rate', code: 'unknown_field' },
    ])
  })

  it('keeps only the field and the code of each problem', async () => {
    const body = invalidSettings(['labor_rate', 'out_of_range'])
    Object.assign(body.problems[0], { input: 'CANARY-17.567', message: 'CANARY' })

    expect(JSON.stringify(settingProblems(await failure(422, body)))).toBe('[{"field":"labor_rate","code":"out_of_range"}]')
  })

  it.each<[string, number, unknown]>([
    ['another 422', 422, { code: 'validation_error', detail: [{ loc: ['body'], type: 'json_invalid' }] }],
    ['the same code with another status', 400, invalidSettings(['labor_rate', 'out_of_range'])],
    ['no problems list', 422, { code: 'invalid_efficiency_settings', message: 'The efficiency settings are not valid.' }],
    ['a problems list that is not a list', 422, { ...invalidSettings(), problems: 'labor_rate' }],
    ['a problem that is not an object', 422, { ...invalidSettings(), problems: ['labor_rate'] }],
    ['a problem with no code', 422, { ...invalidSettings(), problems: [{ field: 'labor_rate' }] }],
    ['a problem whose field is a number', 422, { ...invalidSettings(), problems: [{ field: 1, code: 'out_of_range' }] }],
    ['a refusal for another reason', 403, FORBIDDEN],
  ])('is nothing for %s', async (_label, status, body) => {
    expect(settingProblems(await failure(status, body))).toBeNull()
  })

  it('is nothing for a failure that is not the API’s', () => {
    expect(settingProblems(new Error('boom'))).toBeNull()
    expect(settingProblems(new ApiError(null, 'network_error', 'Could not reach the server.'))).toBeNull()
    expect(settingProblems(undefined)).toBeNull()
  })
})
