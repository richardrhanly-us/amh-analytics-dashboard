import { describe, expect, it } from 'vitest'

import {
  callOf,
  jsonResponse,
  NOT_AUTHENTICATED,
  ORGANIZATION_NOT_FOUND,
  organizationReportBody,
  type OrganizationSorterFixture,
  REPORT,
  stubFetch,
} from '../test/http.ts'
import {
  getOrganizationOverviewReport,
  getOrganizationReliabilityReport,
  getOrganizationRoutingNetworkReport,
  ORGANIZATION_REPORT_KINDS,
} from './organizationReports.ts'

const FROM = '2026-09-28'
const TO = '2026-10-04'
const UNEXPECTED = { code: 'unexpected_response' }

/**
 * Three sorters over Monday to Sunday. Central: 760 check-ins, 38 rejects, a tenth to East Side and a twentieth
 * to Westside. East: 50 a day, 5 rejects a day, a fifth to Westside and a tenth to Library Express. North is
 * registered and has nothing to read.
 */
const SORTERS: OrganizationSorterFixture[] = [
  {
    slug: 'central',
    name: 'Central AMH',
    host_branch: { slug: 'central', name: 'Central Library' },
    status: 'active',
    collector_count: 2,
    report: { ...REPORT, home: 'Central', transit: [['east_side', 'East Side', 0.1], ['westside', 'Westside', 0.05]] },
  },
  {
    slug: 'east',
    name: 'East AMH',
    host_branch: { slug: 'east-side', name: 'East Side' },
    status: 'active',
    collector_count: 1,
    report: {
      ...REPORT,
      checkins: () => 50,
      rejects: () => 5,
      home: 'East Side',
      transit: [['westside', 'Westside', 0.2], ['library_express', 'Library Express', 0.1]],
    },
  },
  { slug: 'north', name: 'North AMH', host_branch: { slug: 'north', name: 'North' }, status: 'provisioning', collector_count: 0, report: null },
]

type Body = Record<string, unknown>
type Reader = (org: string, from: string, to: string, signal?: AbortSignal) => Promise<unknown>

const OVERVIEW = organizationReportBody.overview(SORTERS, FROM, TO)
const NETWORK = organizationReportBody.routingNetwork(SORTERS, FROM, TO)
const RELIABILITY = organizationReportBody.reliability(SORTERS, FROM, TO)

const READS: Array<[kind: (typeof ORGANIZATION_REPORT_KINDS)[number], read: Reader, body: Body]> = [
  ['overview', getOrganizationOverviewReport, OVERVIEW],
  ['routing-network', getOrganizationRoutingNetworkReport, NETWORK],
  ['reliability', getOrganizationReliabilityReport, RELIABILITY],
]

/** Answers with `body` changed by `change`, and reads it. */
function reading(read: Reader, body: Body, change: (body: Body) => unknown) {
  const copy = JSON.parse(JSON.stringify(body)) as Body
  const replacement = change(copy)
  stubFetch().mockResolvedValue(jsonResponse(200, replacement === undefined ? copy : replacement))
  return read('northbridge', FROM, TO)
}
const part = (body: Body, key: string) => body[key] as Body
const rows = (body: Body, key: string) => body[key] as Body[]

describe('the three organization reports', () => {
  it('are exactly overview, routing-network and reliability', () => {
    expect([...ORGANIZATION_REPORT_KINDS]).toEqual(['overview', 'routing-network', 'reliability'])
  })

  it('are built on a fixture that adds up the way the test says', () => {
    expect(OVERVIEW.totals).toEqual({ checkin_count: 1110, home_count: 891, transit_count: 219, other_count: 0, reject_count: 73 })
    expect(NETWORK.destinations).toEqual([
      { key: 'east_side', label: 'East Side', checkin_count: 76, source_count: 1 },
      { key: 'westside', label: 'Westside', checkin_count: 108, source_count: 2 },
      { key: 'library_express', label: 'Library Express', checkin_count: 35, source_count: 1 },
    ])
    expect(NETWORK.sources.map((source) => source.sorter.slug)).toEqual(['central', 'east'])
    expect(RELIABILITY.sorters.map((entry) => [entry.available, entry.reject_count])).toEqual([[true, 38], [true, 35], [false, 0]])
  })

  describe.each(READS)('%s', (kind, read, body) => {
    it('GETs exactly its URL, by the organization alone, and returns the body unchanged', async () => {
      const fetchMock = stubFetch()
      fetchMock.mockResolvedValue(jsonResponse(200, body))

      const result = await read('northbridge', FROM, TO)

      expect(fetchMock).toHaveBeenCalledTimes(1)
      expect(callOf(fetchMock).url).toBe(`/api/organizations/northbridge/reports/${kind}?from=2026-09-28&to=2026-10-04`)
      expect(callOf(fetchMock).init.method).toBe('GET')
      expect(callOf(fetchMock).init.body).toBeUndefined()
      expect(result).toStrictEqual(body)
      expect(JSON.stringify(result)).toBe(JSON.stringify(body))
    })

    it('URL-encodes the slug, and passes the abort signal', async () => {
      const fetchMock = stubFetch()
      fetchMock.mockResolvedValue(jsonResponse(200, body))
      const controller = new AbortController()

      await read('north bridge?x', FROM, TO, controller.signal)

      expect(callOf(fetchMock).url).toBe(`/api/organizations/north%20bridge%3Fx/reports/${kind}?from=${FROM}&to=${TO}`)
      expect(callOf(fetchMock).init.signal).toBe(controller.signal)
    })

    it.each(['', '.', '..'])('asks nothing for the slug %j', async (slug) => {
      const fetchMock = stubFetch()

      await expect(read(slug, FROM, TO)).rejects.toMatchObject({ status: 404 })
      expect(fetchMock).not.toHaveBeenCalled()
    })

    it.each([
      ['2026-9-28', TO],
      [FROM, '2026-10-04T00:00:00'],
      ['', TO],
      ['2026-02-30', '2026-03-02'],
      [`${FROM}&x=1`, TO],
    ])('refuses the range %j to %j before anything is sent', async (from, to) => {
      const fetchMock = stubFetch()

      await expect(read('northbridge', from, to)).rejects.toThrow('YYYY-MM-DD')
      expect(fetchMock).not.toHaveBeenCalled()
    })

    it('rejects with the API error for a 404, a 401 and a 422', async () => {
      const fetchMock = stubFetch()
      fetchMock
        .mockResolvedValueOnce(jsonResponse(404, ORGANIZATION_NOT_FOUND))
        .mockResolvedValueOnce(jsonResponse(401, NOT_AUTHENTICATED))
        .mockResolvedValueOnce(jsonResponse(422, { code: 'validation_error', detail: [] }))

      await expect(read('northbridge', FROM, TO)).rejects.toMatchObject({ status: 404, code: 'organization_not_found' })
      await expect(read('northbridge', FROM, TO)).rejects.toMatchObject({ status: 401, code: 'not_authenticated' })
      await expect(read('northbridge', FROM, TO)).rejects.toMatchObject({ status: 422 })
    })

    it('drops fields the contract does not have', async () => {
      const result = await reading(read, body, (changed) => {
        changed.customer_id = 41
        part(changed, 'totals').reject_rate = 6.6
        part(changed, 'range').cutover_at = '2026-06-10T17:00:00Z'
      })

      expect(result).toStrictEqual(body)
    })

    it.each<[string, (body: Body) => unknown]>([
      ['a body that is not an object', () => [1, 2]],
      ['no range', (changed) => void delete changed.range],
      ['another start date', (changed) => void (part(changed, 'range').from = '2026-09-27')],
      ['another end date', (changed) => void (part(changed, 'range').to = '2026-10-05')],
      ['a day count that is not the range’s', (changed) => void (part(changed, 'range').days = 8)],
      ['a zone that is not an IANA zone', (changed) => void (part(changed, 'range').timezone = 'Central Time')],
      ['an includes_today that is not true or false', (changed) => void (part(changed, 'range').includes_today = 'no')],
      ['no totals', (changed) => void delete changed.totals],
      ['totals that are a list', (changed) => void (changed.totals = [])],
      ['a total as text', (changed) => void (part(changed, 'totals').checkin_count = '1110')],
      ['a negative total', (changed) => void (part(changed, 'totals').checkin_count = -1)],
      ['a fractional total', (changed) => void (part(changed, 'totals').checkin_count = 1110.5)],
      ['a null total', (changed) => void (part(changed, 'totals').checkin_count = null)],
      ['a total its parts do not add up to', (changed) => void ((part(changed, 'totals').checkin_count as number) += 1)],
    ])('refuses %s', async (_label, change) => {
      await expect(reading(read, body, change)).rejects.toMatchObject(UNEXPECTED)
    })
  })
})

describe('the organization overview', () => {
  const read = getOrganizationOverviewReport
  const sorter = (body: Body, index = 0) => rows(body, 'sorters')[index]

  it('accepts an organization with no sorters, and one whose sorters did nothing', async () => {
    const none = organizationReportBody.overview([], FROM, TO)
    const quiet = organizationReportBody.overview([{ ...SORTERS[0], report: { ...REPORT, checkins: () => 0, rejects: () => 0 } }], FROM, TO)

    await expect(reading(read, none, () => undefined)).resolves.toStrictEqual(none)
    await expect(reading(read, quiet, () => undefined)).resolves.toStrictEqual(quiet)
    expect(none.sorters).toEqual([])
    expect(quiet.totals.checkin_count).toBe(0)
  })

  it('keeps a sorter that could not be read, as unavailable with nothing counted', async () => {
    const result = await reading(read, OVERVIEW, () => undefined)

    expect((result as typeof OVERVIEW).sorters[2]).toEqual({
      slug: 'north',
      name: 'North AMH',
      host_branch: { slug: 'north', name: 'North' },
      status: 'provisioning',
      collector_count: 0,
      available: false,
      checkin_count: 0,
      active_days: 0,
      transit_count: 0,
      reject_count: 0,
    })
  })

  it.each<[string, (body: Body) => unknown]>([
    ['home, transit and other that are not the check-ins', (changed) => void ((part(changed, 'totals').home_count as number) -= 1)],
    ['sorters that are not a list', (changed) => void (changed.sorters = { central: {} })],
    ['a sorter that is not an object', (changed) => void (rows(changed, 'sorters')[0] = 'central' as unknown as Body)],
    ['a sorter with no slug', (changed) => void (sorter(changed).slug = '')],
    ['a sorter with a blank name', (changed) => void (sorter(changed).name = '  ')],
    ['a sorter with no host branch', (changed) => void delete sorter(changed).host_branch],
    ['a host branch that is a name alone', (changed) => void (sorter(changed).host_branch = 'Central Library')],
    ['a host branch with no slug', (changed) => void delete part(sorter(changed), 'host_branch').slug],
    ['a status it does not know', (changed) => void (sorter(changed).status = 'retired')],
    ['a collector count as text', (changed) => void (sorter(changed).collector_count = '2')],
    ['a negative collector count', (changed) => void (sorter(changed).collector_count = -1)],
    ['an available that is not true or false', (changed) => void (sorter(changed).available = 'yes')],
    ['no available', (changed) => void delete sorter(changed).available],
    ['a sorter count as text', (changed) => void (sorter(changed).reject_count = '38')],
    ['two sorters with one slug', (changed) => void (sorter(changed, 1).slug = 'central')],
    ['two sorters at one host branch', (changed) => void (part(sorter(changed, 1), 'host_branch').slug = 'central')],
    ['an unavailable sorter with a count', (changed) => void (sorter(changed, 2).reject_count = 1)],
    ['more transit than check-ins at a sorter', (changed) => void (sorter(changed).transit_count = 761)],
    ['more active days than the range has', (changed) => void (sorter(changed).active_days = 8)],
    ['check-ins on no active day', (changed) => void (sorter(changed).active_days = 0)],
    ['sorters whose check-ins are not the total', (changed) => void ((sorter(changed).checkin_count as number) -= 1)],
    ['sorters whose rejects are not the total', (changed) => void ((sorter(changed, 1).reject_count as number) += 1)],
    ['sorters whose transit is not the total', (changed) => void ((sorter(changed, 1).transit_count as number) -= 1)],
    ['days that are not a list', (changed) => void (changed.days = { '2026-09-28': 150 })],
    ['a day missing', (changed) => void rows(changed, 'days').pop()],
    ['a day too many', (changed) => void rows(changed, 'days').push({ ...rows(changed, 'days')[6], date: '2026-10-05' })],
    ['days out of order', (changed) => void rows(changed, 'days').reverse()],
    ['a day repeated', (changed) => void (rows(changed, 'days')[1].date = FROM)],
    ['a day that is not a date', (changed) => void (rows(changed, 'days')[0].date = 'Monday')],
    ['a day count as text', (changed) => void (rows(changed, 'days')[0].checkin_count = '150')],
    ['days whose check-ins are not the total', (changed) => void ((rows(changed, 'days')[0].checkin_count as number) += 1)],
    ['days whose rejects are not the total', (changed) => void ((rows(changed, 'days')[0].reject_count as number) += 1)],
  ])('refuses %s', async (_label, change) => {
    await expect(reading(read, OVERVIEW, change)).rejects.toMatchObject(UNEXPECTED)
  })
})

describe('the routing network', () => {
  const read = getOrganizationRoutingNetworkReport
  const source = (body: Body, index = 0) => rows(body, 'sources')[index]
  const routed = (body: Body, index = 0, slot = 0) => rows(source(body, index), 'transit')[slot]
  const destination = (body: Body, index = 0) => rows(body, 'destinations')[index]

  it('leaves out a sorter that could not be read: only sorters with data are sources', async () => {
    const result = (await reading(read, NETWORK, () => undefined)) as typeof NETWORK

    expect(result.sources.map((entry) => entry.sorter.name)).toEqual(['Central AMH', 'East AMH'])
    expect(result.totals).toEqual({ checkin_count: 1110, transit_count: 219 })
  })

  it('accepts no sources, sources with no destinations, and destinations nothing was routed to', async () => {
    const none = organizationReportBody.routingNetwork([SORTERS[2]], FROM, TO)
    const unconfigured = organizationReportBody.routingNetwork([{ ...SORTERS[0], report: { ...REPORT, transit: [] } }], FROM, TO)
    const unused = organizationReportBody.routingNetwork([{ ...SORTERS[0], report: { ...REPORT, transit: [['westside', 'Westside', 0]] } }], FROM, TO)

    await expect(reading(read, none, () => undefined)).resolves.toStrictEqual(none)
    await expect(reading(read, unconfigured, () => undefined)).resolves.toStrictEqual(unconfigured)
    await expect(reading(read, unused, () => undefined)).resolves.toStrictEqual(unused)
    expect(none.sources).toEqual([])
    expect(unconfigured.destinations).toEqual([])
    expect(unused.destinations).toEqual([{ key: 'westside', label: 'Westside', checkin_count: 0, source_count: 1 }])
  })

  it('keeps a destination that has a sorter’s slug as a destination, and other routing as a count of its source', async () => {
    const body = organizationReportBody.routingNetwork(
      [{ ...SORTERS[0], report: { ...REPORT, transit: [['east', 'East AMH', 0.1]], other: 0.05 } }, SORTERS[1]],
      FROM,
      TO,
    )

    const result = (await reading(read, body, () => undefined)) as typeof body

    expect(result.destinations[0]).toEqual({ key: 'east', label: 'East AMH', checkin_count: 76, source_count: 1 })
    expect(result.sources[0].other_count).toBe(38)
    expect(result.destinations.map((entry) => entry.key)).not.toContain('other')
  })

  it.each<[string, (body: Body) => unknown]>([
    ['sources that are not a list', (changed) => void (changed.sources = {})],
    ['a source with no sorter', (changed) => void delete source(changed).sorter],
    ['a source whose sorter has no name', (changed) => void delete part(source(changed), 'sorter').name],
    ['a source whose sorter has no host branch', (changed) => void delete part(source(changed), 'sorter').host_branch],
    ['two sources for one sorter', (changed) => void (part(source(changed, 1), 'sorter').slug = 'central')],
    ['a source with no home', (changed) => void delete source(changed).home],
    ['a home with a blank label', (changed) => void (part(source(changed), 'home').label = '')],
    ['a home count as text', (changed) => void (part(source(changed), 'home').checkin_count = '646')],
    ['a source’s transit that is not a list', (changed) => void (source(changed).transit = null)],
    ['a source’s other count that is negative', (changed) => void (source(changed).other_count = -1)],
    ['a source whose parts are not its check-ins', (changed) => void ((source(changed).checkin_count as number) += 1)],
    ['a source whose destinations are not its transit', (changed) => void ((routed(changed).checkin_count as number) += 1)],
    ['a source with one key twice', (changed) => void (routed(changed, 0, 1).key = 'east_side')],
    ['a key in capitals', (changed) => void (routed(changed).key = 'East_Side')],
    ['a key with a space', (changed) => void (destination(changed).key = 'east side')],
    ['a destination with no label', (changed) => void delete destination(changed).label],
    ['a destination count as text', (changed) => void (destination(changed).checkin_count = '76')],
    ['destinations that are not a list', (changed) => void (changed.destinations = 'none')],
    ['a destination listed twice', (changed) => void rows(changed, 'destinations').push({ ...destination(changed) })],
    ['a destination no source has', (changed) => void rows(changed, 'destinations').push({ key: 'depot', label: 'Depot', checkin_count: 0, source_count: 0 })],
    ['a source’s destination missing from the totals', (changed) => void rows(changed, 'destinations').pop()],
    ['a destination total that is not its sources’', (changed) => void ((destination(changed, 1).checkin_count as number) -= 1)],
    ['a source count of zero', (changed) => void (destination(changed).source_count = 0)],
    ['a source count that is too high', (changed) => void (destination(changed).source_count = 2)],
    ['a source count that is too low', (changed) => void (destination(changed, 1).source_count = 1)],
    ['a source count as text', (changed) => void (destination(changed).source_count = '1')],
    ['a fractional source count', (changed) => void (destination(changed).source_count = 1.5)],
    ['no source count', (changed) => void delete destination(changed).source_count],
    ['sources whose check-ins are not the total', (changed) => void ((part(changed, 'totals').checkin_count as number) -= 1)],
    ['sources whose transit is not the total', (changed) => void ((part(changed, 'totals').transit_count as number) += 1)],
  ])('refuses %s', async (_label, change) => {
    await expect(reading(read, NETWORK, change)).rejects.toMatchObject(UNEXPECTED)
  })
})

describe('organization reliability', () => {
  const read = getOrganizationReliabilityReport
  const sorter = (body: Body, index = 0) => rows(body, 'sorters')[index]
  const totalReasons = (body: Body) => rows(part(body, 'totals'), 'reasons')

  it('has the eight reasons, in the one order, for the organization and for every sorter', async () => {
    const result = (await reading(read, RELIABILITY, () => undefined)) as typeof RELIABILITY
    const codes = ['item_not_found', 'ils_acs_failure', 'rfid_collision', 'configuration_error', 'routing_error', 'communication_error', 'other', 'unknown']

    expect(result.totals.reasons.map((reason) => reason.reason)).toEqual(codes)
    for (const entry of result.sorters) {
      expect(entry.reasons.map((reason) => reason.reason)).toEqual(codes)
    }
    expect(result.totals.reject_count).toBe(73)
  })

  it.each<[string, (body: Body) => unknown]>([
    ['no reasons', (changed) => void delete part(changed, 'totals').reasons],
    ['seven reasons', (changed) => void totalReasons(changed).pop()],
    ['nine reasons', (changed) => void totalReasons(changed).push({ reason: 'jam', reject_count: 0 })],
    ['a reason it does not know', (changed) => void (totalReasons(changed)[7].reason = 'jam')],
    ['reasons in another order', (changed) => void totalReasons(changed).reverse()],
    ['a reason twice', (changed) => void (totalReasons(changed)[1].reason = 'item_not_found')],
    ['a reason count as text', (changed) => void (totalReasons(changed)[0].reject_count = '1')],
    ['reasons that are not the rejects', (changed) => void ((totalReasons(changed)[1].reject_count as number) += 1)],
    ['sorters that are not a list', (changed) => void (changed.sorters = null)],
    ['a sorter with no identity', (changed) => void delete sorter(changed).sorter],
    ['a sorter with no available', (changed) => void delete sorter(changed).available],
    ['two rows for one sorter', (changed) => void (part(sorter(changed, 1), 'sorter').slug = 'central')],
    ['a sorter with seven reasons', (changed) => void rows(sorter(changed), 'reasons').pop()],
    ['a sorter with reasons in another order', (changed) => void rows(sorter(changed), 'reasons').reverse()],
    ['a sorter whose reasons are not its rejects', (changed) => void ((sorter(changed).reject_count as number) += 1)],
    ['an unavailable sorter with a count', (changed) => void (sorter(changed, 2).checkin_count = 1)],
    ['sorters whose check-ins are not the total', (changed) => void ((sorter(changed).checkin_count as number) += 1)],
    [
      'sorters whose reasons are not the organization’s, though every sum holds',
      (changed) => {
        // Move one reject from one reason to another in the totals alone: 73 still, but no longer the sorters'.
        const moved = totalReasons(changed)
        const from = moved.findIndex((reason) => (reason.reject_count as number) > 0)
        ;(moved[from].reject_count as number) -= 1
        ;(moved[from === 7 ? 6 : 7].reject_count as number) += 1
      },
    ],
    ['a day missing', (changed) => void rows(changed, 'days').shift()],
    ['days out of order', (changed) => void rows(changed, 'days').reverse()],
    ['days whose rejects are not the total', (changed) => void ((rows(changed, 'days')[2].reject_count as number) += 1)],
    ['days whose check-ins are not the total', (changed) => void ((rows(changed, 'days')[2].checkin_count as number) += 1)],
  ])('refuses %s', async (_label, change) => {
    await expect(reading(read, RELIABILITY, change)).rejects.toMatchObject(UNEXPECTED)
  })
})
