import { describe, expect, it } from 'vitest'

import { callOf, jsonResponse, NOT_AUTHENTICATED, ORGANIZATION_NOT_FOUND, stubFetch } from '../test/http.ts'
import {
  emptyValueStore,
  invalidValueSettings,
  VALUE_FORBIDDEN,
  VALUE_READ_ONLY,
  VALUE_STORED_INVALID,
  valueBody,
  valueStore,
} from '../test/sortviewValue.ts'
import { ApiError } from './client.ts'
import {
  getSortViewValueReport,
  getSortViewValueSettings,
  putSortViewValueSettings,
  VALUE_INPUTS,
  valueSettingProblems,
  type SortViewValueInput,
} from './sortviewValue.ts'

const ORG = '/api/organizations/northbridge'
const UNEXPECTED = { code: 'unexpected_response' }

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- a JSON body a test is about to break on purpose
type Body = Record<string, any>

const STORE = valueStore()
const REPORT_BODY = valueBody.report(STORE)
const SETTINGS_BODY = valueBody.settings(STORE)

/** Answers with `body` changed by `change`, and reads it with `read`. */
function reading<T>(read: () => Promise<T>, body: Body, change: (body: Body) => unknown = () => undefined): Promise<T> {
  const copy = JSON.parse(JSON.stringify(body)) as Body
  const replacement = change(copy)
  stubFetch().mockResolvedValue(jsonResponse(200, replacement === undefined ? copy : replacement))
  return read()
}
const report = () => getSortViewValueReport('northbridge')
const settings = () => getSortViewValueSettings('northbridge')

const INPUT: SortViewValueInput = {
  labor_rate: '32.5',
  subscription_annual_cost: '4800',
  reporting: { tasks_per_month: '12', baseline_minutes_per_task: '90', current_minutes_per_task: '15', basis: 'estimate', as_of: '2026-09-01' },
  troubleshooting: { incidents_per_month: '4', staff_per_incident: '2', baseline_minutes: '45', current_minutes: '20', basis: 'measured', as_of: '2026-09-15' },
}

describe('the SortView Value report', () => {
  it('GETs exactly its URL, with no range in it, and returns the body unchanged', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, REPORT_BODY))
    const controller = new AbortController()

    const result = await getSortViewValueReport('northbridge', controller.signal)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(callOf(fetchMock).url).toBe(`${ORG}/reports/sortview-value`)
    expect(callOf(fetchMock).url).not.toMatch(/[?&](from|to|compare)=/)
    expect(callOf(fetchMock).init.method).toBe('GET')
    expect(callOf(fetchMock).init.signal).toBe(controller.signal)
    expect(result).toStrictEqual(REPORT_BODY)
    expect(JSON.stringify(result)).toBe(JSON.stringify(REPORT_BODY))
  })

  it('is built on a fixture with the figures the test says', () => {
    expect(REPORT_BODY.currency).toBe('USD')
    expect(REPORT_BODY.assumptions.labor_rate).toEqual({ sortview_value: '32.50', organization_efficiency: null, effective: '32.50', source: 'sortview_value' })
    expect(REPORT_BODY.results).toEqual({
      reporting: { baseline_hours_per_month: '18.00', current_hours_per_month: '3.00', recovered_hours_per_month: '15.00', monthly_value: '487.50', annual_value: '5850.00' },
      troubleshooting: { baseline_hours_per_month: '6.00', current_hours_per_month: '2.67', recovered_hours_per_month: '3.33', monthly_value: '108.33', annual_value: '1300.00' },
      recovered_hours_per_month: '18.33',
      monthly_value: '595.83',
      annual_value: '7150.00',
      net_annual_value: '2350.00',
      roi_percent: '49.0',
      payback_months: '8.1',
    })
    expect(REPORT_BODY.missing).toEqual([])
  })

  it('keeps every figure as the text it arrived as', async () => {
    const result = await reading(report, REPORT_BODY)

    const leaves = (value: unknown): unknown[] => (typeof value === 'object' && value !== null ? Object.values(value).flatMap(leaves) : [value])
    for (const leaf of leaves({ ...result, missing: null })) {
      expect(leaf === null || typeof leaf === 'string').toBe(true)
    }
    expect(result.results.roi_percent).toBe('49.0')
    expect(result.results.payback_months).toBe('8.1')
  })

  it('URL-encodes the organization’s slug', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, REPORT_BODY))

    await getSortViewValueReport('north bridge?x/y')

    expect(callOf(fetchMock).url).toBe('/api/organizations/north%20bridge%3Fx%2Fy/reports/sortview-value')
  })

  it('accepts a report with nothing set: every figure null and all nine inputs missing', async () => {
    const body = valueBody.report(emptyValueStore())

    const result = await reading(report, body)

    expect(result.missing).toEqual([...VALUE_INPUTS])
    expect(Object.values(result.results.reporting)).toEqual([null, null, null, null, null])
    expect(result.results.monthly_value).toBeNull()
    expect(result.assumptions.labor_rate).toEqual({ sortview_value: null, organization_efficiency: null, effective: null, source: null })
  })

  it('accepts a rate that comes from the organization’s Efficiency default, and says whose it is', async () => {
    const result = await reading(report, valueBody.report(valueStore({ labor_rate: null, efficiency_labor_rate: '20.00' })))

    expect(result.assumptions.labor_rate).toEqual({ sortview_value: null, organization_efficiency: '20.00', effective: '20.00', source: 'organization_efficiency' })
    expect(result.results.monthly_value).toBe('366.67')
    expect(result.missing).toEqual([])
  })

  it('accepts hours with no rate, one kind of work without the other, and a figure of zero', async () => {
    const noRate = await reading(report, valueBody.report(valueStore({ labor_rate: null })))
    expect(noRate.results.recovered_hours_per_month).toBe('18.33')
    expect(noRate.results.monthly_value).toBeNull()
    expect(noRate.missing).toEqual(['labor_rate'])

    const oneKind = await reading(report, valueBody.report(valueStore({ troubleshooting: { staff_per_incident: null } })))
    expect(oneKind.results.reporting.monthly_value).toBe('487.50')
    expect(oneKind.results.troubleshooting.recovered_hours_per_month).toBeNull()
    expect(oneKind.results.monthly_value).toBeNull()
    expect(oneKind.missing).toEqual(['troubleshooting.staff_per_incident'])

    const zero = await reading(report, valueBody.report(valueStore({ reporting: { tasks_per_month: '0.0' } })))
    expect(zero.results.reporting.recovered_hours_per_month).toBe('0.00')
    expect(zero.missing).toEqual([])
  })

  it('accepts results below zero, a cost of zero with no return on it, and a value with no payback', async () => {
    const slower = await reading(report, valueBody.report(valueStore({ reporting: { current_minutes_per_task: '120.0' } })))
    expect(slower.results.reporting.recovered_hours_per_month).toBe('-6.00')
    expect(slower.results.monthly_value).toBe('-86.67')
    expect(slower.results.net_annual_value).toBe('-5840.00')
    expect(slower.results.roi_percent).toBe('-121.7')
    expect(slower.results.payback_months).toBeNull()

    const free = await reading(report, valueBody.report(valueStore({ subscription_annual_cost: '0.00' })))
    expect(free.results.net_annual_value).toBe('7150.00')
    expect(free.results.roi_percent).toBeNull()
    expect(free.results.payback_months).toBe('0.0')

    const noCost = await reading(report, valueBody.report(valueStore({ subscription_annual_cost: null })))
    expect([noCost.results.net_annual_value, noCost.results.roi_percent, noCost.results.payback_months]).toEqual([null, null, null])
    expect(noCost.missing).toEqual(['subscription_annual_cost'])
  })

  it('drops fields the contract does not have', async () => {
    const result = await reading(report, REPORT_BODY, (body) => {
      body.organization_id = 7111
      body.results.internal = 'x'
      body.assumptions.reporting.notes = 'y'
      body.range = { from: '2026-09-01', to: '2026-09-30' }
    })

    expect(result).toStrictEqual(REPORT_BODY)
    expect(JSON.stringify(result)).not.toMatch(/organization_id|internal|notes|range/)
  })

  it.each<[string, (body: Body) => unknown]>([
    ['is not an object', () => []],
    ['is null', () => null],
    ['has another currency', (body) => { body.currency = 'EUR' }],
    ['has no assumptions', (body) => { delete body.assumptions }],
    ['has no results', (body) => { delete body.results }],
    ['has results that are not an object', (body) => { body.results = '7150.00' }],
    ['has a missing list that is not a list', (body) => { body.missing = 'labor_rate' }],
    ['names something missing that is not an input', (body) => { body.missing = ['reporting.basis'] }],
    ['has a JSON number for a figure', (body) => { body.results.monthly_value = 595.83 }],
    ['has a JSON number for an assumption', (body) => { body.assumptions.subscription_annual_cost = 4800 }],
    ['has money with one decimal place', (body) => { body.results.annual_value = '7150.0' }],
    ['has money with a separator', (body) => { body.results.annual_value = '7,150.00' }],
    ['has a currency symbol', (body) => { body.results.monthly_value = '$595.83' }],
    ['has an exponent', (body) => { body.results.annual_value = '7.15e3' }],
    ['has a percentage sign', (body) => { body.results.roi_percent = '49.0%' }],
    ['has a return with two places', (body) => { body.results.roi_percent = '49.00' }],
    ['has a negative payback', (body) => { body.results.payback_months = '-8.1' }],
    ['has negative baseline hours', (body) => { body.results.reporting.baseline_hours_per_month = '-18.00' }],
    ['has a count with two places', (body) => { body.assumptions.reporting.tasks_per_month = '12.00' }],
    ['has a negative assumption', (body) => { body.assumptions.reporting.tasks_per_month = '-12.0' }],
    ['has a basis that is not one', (body) => { body.assumptions.reporting.basis = 'guess' }],
    ['has a date that is not one', (body) => { body.assumptions.troubleshooting.as_of = '2026-02-30' }],
    ['has a date with a time', (body) => { body.assumptions.troubleshooting.as_of = '2026-09-15T00:00:00Z' }],
    ['has a labor rate that is not an object', (body) => { body.assumptions.labor_rate = '32.50' }],
    ['has a source that is not one', (body) => { body.assumptions.labor_rate.source = 'sorter' }],
    ['says the Efficiency default applies when the block has its own rate', (body) => { body.assumptions.labor_rate.source = 'organization_efficiency' }],
    ['has an effective rate that is neither of the two', (body) => { body.assumptions.labor_rate.effective = '99.00' }],
    ['has a rate in effect and no source', (body) => { body.assumptions.labor_rate.source = null }],
    ['has a kind of work missing from the results', (body) => { delete body.results.troubleshooting }],
    ['has a figure missing from a kind of work', (body) => { delete body.results.reporting.annual_value }],
    ['has a total missing', (body) => { delete body.results.net_annual_value }],
    // What is listed as missing must be exactly what is missing, in the one order.
    ['lists something missing that is set', (body) => { body.missing = ['labor_rate'] }],
    ['does not list what is missing', (body) => { body.assumptions.subscription_annual_cost = null }],
    ['lists what is missing out of order', (body) => {
      body.assumptions.subscription_annual_cost = null
      body.assumptions.labor_rate = { sortview_value: null, organization_efficiency: null, effective: null, source: null }
      body.missing = ['subscription_annual_cost', 'labor_rate']
    }],
    // A figure is there exactly when what it is made from is.
    ['has a monthly value with no hours', (body) => { body.results.reporting.recovered_hours_per_month = null }],
    ['has hours and no value although there is a rate', (body) => { body.results.reporting.monthly_value = null }],
    ['has an annual value with no monthly one', (body) => { body.results.troubleshooting.monthly_value = null }],
    ['has a total although one kind of work has none', (body) => {
      body.assumptions.troubleshooting.staff_per_incident = null
      body.missing = ['troubleshooting.staff_per_incident']
      body.results.troubleshooting = { baseline_hours_per_month: null, current_hours_per_month: null, recovered_hours_per_month: null, monthly_value: null, annual_value: null }
    }],
    ['has no total although both kinds of work have theirs', (body) => { body.results.monthly_value = null }],
    ['has a net value with no cost', (body) => {
      body.assumptions.subscription_annual_cost = null
      body.missing = ['subscription_annual_cost']
    }],
    ['has a return on a cost of zero', (body) => { body.assumptions.subscription_annual_cost = '0.00' }],
    ['has no return although there is a net value and a cost', (body) => { body.results.roi_percent = null }],
  ])('refuses a report that %s', async (_label, change) => {
    await expect(reading(report, REPORT_BODY, change)).rejects.toMatchObject(UNEXPECTED)
  })

  it('rejects with the API’s own error for 401, 403, 404 and malformed stored settings', async () => {
    for (const [status, body] of [
      [401, NOT_AUTHENTICATED],
      [403, VALUE_FORBIDDEN],
      [404, ORGANIZATION_NOT_FOUND],
      [500, VALUE_STORED_INVALID],
    ] as const) {
      stubFetch().mockResolvedValue(jsonResponse(status, body))
      const failure = await getSortViewValueReport('northbridge').catch((error: unknown) => error)

      expect(failure).toBeInstanceOf(ApiError)
      expect(failure).toMatchObject({ status, code: body.code, message: body.message })
    }
  })
})

describe('the SortView Value settings', () => {
  it('GETs exactly its URL and returns what is stored and the labor rate that applies', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, SETTINGS_BODY))

    const result = await getSortViewValueSettings('northbridge')

    expect(callOf(fetchMock).url).toBe(`${ORG}/settings/sortview-value`)
    expect(callOf(fetchMock).init.method).toBe('GET')
    expect(result).toStrictEqual(SETTINGS_BODY.sortview_value)
  })

  it('keeps null as null, and zero as the text it arrived as', async () => {
    const empty = await reading(settings, valueBody.settings(emptyValueStore()))
    expect(empty.subscription_annual_cost).toBeNull()
    expect(empty.reporting).toEqual({ tasks_per_month: null, baseline_minutes_per_task: null, current_minutes_per_task: null, basis: null, as_of: null })

    const zero = await reading(settings, valueBody.settings(valueStore({ subscription_annual_cost: '0.00', reporting: { tasks_per_month: '0.0' } })))
    expect(zero.subscription_annual_cost).toBe('0.00')
    expect(zero.reporting.tasks_per_month).toBe('0.0')
  })

  it.each<[string, (body: Body) => unknown]>([
    ['is not an object', () => 'settings'],
    ['is not wrapped in sortview_value', (body) => body.sortview_value],
    ['has a JSON number', (body) => { body.sortview_value.subscription_annual_cost = 4800 }],
    ['has a rate with three places', (body) => { body.sortview_value.labor_rate.sortview_value = '32.500' }],
    ['has minutes with two places', (body) => { body.sortview_value.troubleshooting.baseline_minutes = '45.00' }],
    ['has no reporting', (body) => { delete body.sortview_value.reporting }],
    ['has a basis that is not one', (body) => { body.sortview_value.troubleshooting.basis = 'Measured' }],
    ['has a date that is not one', (body) => { body.sortview_value.reporting.as_of = '09/01/2026' }],
    ['has a source that does not match its rates', (body) => { body.sortview_value.labor_rate.source = null }],
  ])('refuses settings that %s', async (_label, change) => {
    await expect(reading(settings, SETTINGS_BODY, change)).rejects.toMatchObject(UNEXPECTED)
  })

  it('PUTs the whole block, as the text given and null for nothing, and returns what is then stored', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, SETTINGS_BODY))

    const result = await putSortViewValueSettings('northbridge', { ...INPUT, labor_rate: null, reporting: { ...INPUT.reporting, basis: null } })

    expect(callOf(fetchMock).url).toBe(`${ORG}/settings/sortview-value`)
    expect(callOf(fetchMock).init.method).toBe('PUT')
    expect(JSON.parse(String(callOf(fetchMock).init.body))).toStrictEqual({
      labor_rate: null,
      subscription_annual_cost: '4800',
      reporting: { tasks_per_month: '12', baseline_minutes_per_task: '90', current_minutes_per_task: '15', basis: null, as_of: '2026-09-01' },
      troubleshooting: { incidents_per_month: '4', staff_per_incident: '2', baseline_minutes: '45', current_minutes: '20', basis: 'measured', as_of: '2026-09-15' },
    })
    // What was typed is sent as typed: nothing is padded, rounded or turned into a number on the way.
    expect(String(callOf(fetchMock).init.body)).toContain('"subscription_annual_cost":"4800"')
    expect(result).toStrictEqual(SETTINGS_BODY.sortview_value)
  })

  it('sends nothing but the block’s own fields, whatever else it is handed', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, SETTINGS_BODY))
    const handed = {
      ...INPUT,
      organization_id: 7111,
      results: { monthly_value: '1.00' },
      reporting: { ...INPUT.reporting, notes: 'x' },
      troubleshooting: { ...INPUT.troubleshooting, vendor_calls: '2' },
    } as unknown as SortViewValueInput

    await putSortViewValueSettings('northbridge', handed)

    const sent = JSON.parse(String(callOf(fetchMock).init.body)) as Body
    expect(Object.keys(sent)).toEqual(['labor_rate', 'subscription_annual_cost', 'reporting', 'troubleshooting'])
    expect(Object.keys(sent.reporting)).toEqual(['tasks_per_month', 'baseline_minutes_per_task', 'current_minutes_per_task', 'basis', 'as_of'])
    expect(Object.keys(sent.troubleshooting)).toEqual(['incidents_per_month', 'staff_per_incident', 'baseline_minutes', 'current_minutes', 'basis', 'as_of'])
  })

  it('refuses an answer to a PUT that is not the settings', async () => {
    stubFetch().mockResolvedValue(jsonResponse(200, REPORT_BODY))

    await expect(putSortViewValueSettings('northbridge', INPUT)).rejects.toMatchObject(UNEXPECTED)
  })

  it('rejects a PUT with the API’s own error for a suspended organization', async () => {
    stubFetch().mockResolvedValue(jsonResponse(403, VALUE_READ_ONLY))

    await expect(putSortViewValueSettings('northbridge', INPUT)).rejects.toMatchObject({ status: 403, code: 'organization_read_only' })
  })
})

describe('the fields a PUT was refused for', () => {
  const refused = async (status: number, body: unknown) => {
    stubFetch().mockResolvedValue(jsonResponse(status, body))
    return putSortViewValueSettings('northbridge', INPUT).catch((error: unknown) => error)
  }

  it('is each refused field and its code, a kind of work’s field with it in front, and nothing else', async () => {
    const error = await refused(422, invalidValueSettings(['labor_rate', 'out_of_range'], ['reporting.tasks_per_month', 'not_a_decimal'], ['troubleshooting.as_of', 'in_the_future']))

    expect(valueSettingProblems(error)).toEqual([
      { field: 'labor_rate', code: 'out_of_range' },
      { field: 'reporting.tasks_per_month', code: 'not_a_decimal' },
      { field: 'troubleshooting.as_of', code: 'in_the_future' },
    ])
  })

  it('keeps only the field and the code of each problem', async () => {
    const body = { ...invalidValueSettings(['labor_rate', 'out_of_range']), problems: [{ field: 'labor_rate', code: 'out_of_range', value: 'CANARY', hint: 'x' }] }

    expect(valueSettingProblems(await refused(422, body))).toEqual([{ field: 'labor_rate', code: 'out_of_range' }])
    expect(JSON.stringify(valueSettingProblems(await refused(422, body)))).not.toContain('CANARY')
  })

  it.each<[string, number, unknown]>([
    ['another 422', 422, { code: 'validation_error', message: 'x', problems: [{ field: 'labor_rate', code: 'out_of_range' }] }],
    ['the Efficiency settings’ 422', 422, { code: 'invalid_efficiency_settings', message: 'x', problems: [{ field: 'labor_rate', code: 'out_of_range' }] }],
    ['a 403', 403, VALUE_READ_ONLY],
    ['a 500', 500, VALUE_STORED_INVALID],
    ['a list that is not one', 422, { ...invalidValueSettings(), problems: 'labor_rate' }],
    ['a problem that is not an object', 422, { ...invalidValueSettings(), problems: ['labor_rate'] }],
    ['a problem with no code', 422, { ...invalidValueSettings(), problems: [{ field: 'labor_rate' }] }],
    ['a problem whose field is not text', 422, { ...invalidValueSettings(), problems: [{ field: 7, code: 'out_of_range' }] }],
  ])('is nothing for %s', async (_label, status, body) => {
    expect(valueSettingProblems(await refused(status, body))).toBeNull()
  })

  it('is nothing for a failure that is not the API’s', () => {
    expect(valueSettingProblems(new Error('boom'))).toBeNull()
    expect(valueSettingProblems(null)).toBeNull()
    expect(valueSettingProblems({ status: 422, code: 'invalid_sortview_value_settings', body: invalidValueSettings(['labor_rate', 'x']) })).toBeNull()
  })
})
