import { describe, expect, it } from 'vitest'

import {
  callOf,
  jsonResponse,
  NORTHBRIDGE,
  NORTHBRIDGE_DETAIL,
  NOT_AUTHENTICATED,
  ORGANIZATION_NOT_FOUND,
  RIVERSIDE,
  RIVERSIDE_DETAIL,
  stubFetch,
} from '../test/http.ts'
import { getOrganization, listOrganizations } from './organizations.ts'

const UNEXPECTED = { code: 'unexpected_response' }

describe('listOrganizations', () => {
  it('GETs exactly /api/organizations and returns the list in the order sent', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, [NORTHBRIDGE, RIVERSIDE]))

    const organizations = await listOrganizations()

    const { url, init } = callOf(fetchMock)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(url).toBe('/api/organizations')
    expect(init.method).toBe('GET')
    expect(init.body).toBeUndefined()
    expect(organizations).toStrictEqual([NORTHBRIDGE, RIVERSIDE])
  })

  it('keeps each role and access mode exactly as sent', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, [NORTHBRIDGE, RIVERSIDE, { ...NORTHBRIDGE, slug: 'x', role: 'curator' }]))

    const organizations = await listOrganizations()

    expect(organizations.map(({ role, access_mode }) => [role, access_mode])).toEqual([
      ['admin', 'full'],
      ['viewer', 'read_only'],
      ['curator', 'full'],
    ])
  })

  it('returns an empty list as an empty list', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, []))

    await expect(listOrganizations()).resolves.toStrictEqual([])
  })

  it('passes the abort signal to fetch', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, []))
    const controller = new AbortController()

    await listOrganizations(controller.signal)

    expect(callOf(fetchMock).init.signal).toBe(controller.signal)
  })

  it('drops fields the contract does not have, such as a database id', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, [{ ...NORTHBRIDGE, organization_id: 41, customer_id: 9 }]))

    await expect(listOrganizations()).resolves.toStrictEqual([NORTHBRIDGE])
  })

  it.each([
    ['an object instead of a list', { organizations: [NORTHBRIDGE] }],
    ['null', null],
    ['a list of strings', ['northbridge']],
    ['a missing slug', [{ name: 'N', role: 'admin', access_mode: 'full' }]],
    ['an empty slug', [{ ...NORTHBRIDGE, slug: '' }]],
    ['a numeric slug', [{ ...NORTHBRIDGE, slug: 41 }]],
    ['a missing name', [{ slug: 'n', role: 'admin', access_mode: 'full' }]],
    ['a missing role', [{ slug: 'n', name: 'N', access_mode: 'full' }]],
    ['a blocked organization', [{ ...NORTHBRIDGE, access_mode: 'blocked' }]],
    ['an unknown access mode', [{ ...NORTHBRIDGE, access_mode: 'admin' }]],
    ['one bad entry among good ones', [NORTHBRIDGE, { ...RIVERSIDE, access_mode: null }]],
  ])('rejects %s as an unexpected response', async (_label, body) => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, body))

    await expect(listOrganizations()).rejects.toMatchObject(UNEXPECTED)
  })

  it('rejects with the 401 when the session has ended', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(401, NOT_AUTHENTICATED))

    await expect(listOrganizations()).rejects.toMatchObject({ status: 401, code: 'not_authenticated' })
  })
})

describe('getOrganization', () => {
  it('GETs exactly /api/organizations/{slug} and returns the detail', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, NORTHBRIDGE_DETAIL))

    const detail = await getOrganization('northbridge')

    const { url, init } = callOf(fetchMock)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(url).toBe('/api/organizations/northbridge')
    expect(init.method).toBe('GET')
    expect(detail).toStrictEqual(NORTHBRIDGE_DETAIL)
  })

  it('keeps the subscription and every entitlement exactly as sent', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, NORTHBRIDGE_DETAIL))

    const detail = await getOrganization('northbridge')

    expect(detail.subscription).toStrictEqual({ plan_code: 'standard', plan_name: 'Standard', status: 'active' })
    expect(detail.entitlements).toStrictEqual({
      transits_tab: { enabled: true, limit_value: null },
      branch_count: { enabled: true, limit_value: 5 },
      transits: { enabled: true, limit_value: null },
      history_days: { enabled: true, limit_value: null },
    })
    expect(detail.role).toBe('admin')
    expect(detail.access_mode).toBe('full')
  })

  it('accepts no subscription, no entitlements and no branches', async () => {
    const fetchMock = stubFetch()
    const bare = { ...RIVERSIDE_DETAIL, branches: [] }
    fetchMock.mockResolvedValue(jsonResponse(200, bare))

    await expect(getOrganization('riverside')).resolves.toStrictEqual(bare)
  })

  it('drops fields the contract does not have, at every level', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(
      jsonResponse(200, {
        ...NORTHBRIDGE_DETAIL,
        organization_id: 41,
        branches: NORTHBRIDGE_DETAIL.branches.map((branch, index) => ({ ...branch, id: index, branch_id: 100 + index })),
        subscription: { ...NORTHBRIDGE_DETAIL.subscription, plan_id: 3 },
      }),
    )

    await expect(getOrganization('northbridge')).resolves.toStrictEqual(NORTHBRIDGE_DETAIL)
  })

  it.each([
    ['a space', 'north bridge', '/api/organizations/north%20bridge'],
    ['a slash', 'a/b', '/api/organizations/a%2Fb'],
    ['a query', 'a?x=1', '/api/organizations/a%3Fx%3D1'],
    ['a fragment', 'a#b', '/api/organizations/a%23b'],
    ['an attempt to leave the path', '../auth/session', '/api/organizations/..%2Fauth%2Fsession'],
    ['a host', '//elsewhere.example.test', '/api/organizations/%2F%2Felsewhere.example.test'],
  ])('URL-encodes a slug containing %s', async (_label, slug, expected) => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(404, ORGANIZATION_NOT_FOUND))

    await expect(getOrganization(slug)).rejects.toMatchObject({ status: 404 })

    expect(callOf(fetchMock).url).toBe(expected)
  })

  it.each(['', '.', '..'])('treats the slug %j as not found without sending anything', async (slug) => {
    const fetchMock = stubFetch()

    await expect(getOrganization(slug)).rejects.toMatchObject({ status: 404, code: 'organization_not_found' })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('rejects with the 404 for an organization the user cannot see', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(404, ORGANIZATION_NOT_FOUND))

    await expect(getOrganization('someone-elses')).rejects.toMatchObject({
      status: 404,
      code: 'organization_not_found',
      message: 'Organization not found.',
    })
  })

  it('rejects an answer about a different organization', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, RIVERSIDE_DETAIL))

    await expect(getOrganization('northbridge')).rejects.toMatchObject(UNEXPECTED)
  })

  it.each([
    ['a list', [NORTHBRIDGE_DETAIL]],
    ['a summary with no detail', NORTHBRIDGE],
    ['branches that are not a list', { ...NORTHBRIDGE_DETAIL, branches: { central: {} } }],
    ['a branch that is a string', { ...NORTHBRIDGE_DETAIL, branches: ['central'] }],
    ['a branch with no slug', { ...NORTHBRIDGE_DETAIL, branches: [{ name: 'Central', is_primary: true }] }],
    ['a branch with an empty slug', { ...NORTHBRIDGE_DETAIL, branches: [{ slug: '', name: 'C', is_primary: true }] }],
    ['a branch with a numeric name', { ...NORTHBRIDGE_DETAIL, branches: [{ slug: 'c', name: 7, is_primary: true }] }],
    ['a branch with a non-boolean is_primary', { ...NORTHBRIDGE_DETAIL, branches: [{ slug: 'c', name: 'C', is_primary: 1 }] }],
    ['a missing subscription', { ...NORTHBRIDGE_DETAIL, subscription: undefined }],
    ['a subscription that is a string', { ...NORTHBRIDGE_DETAIL, subscription: 'standard' }],
    ['a subscription with no status', { ...NORTHBRIDGE_DETAIL, subscription: { plan_code: 's', plan_name: 'S' } }],
    ['entitlements that are a list', { ...NORTHBRIDGE_DETAIL, entitlements: [] }],
    ['a missing entitlements map', { ...NORTHBRIDGE_DETAIL, entitlements: undefined }],
    ['an entitlement that is a boolean', { ...NORTHBRIDGE_DETAIL, entitlements: { transits_tab: true } }],
    ['an entitlement with a string limit', { ...NORTHBRIDGE_DETAIL, entitlements: { x: { enabled: true, limit_value: '5' } } }],
    ['an entitlement with no limit_value', { ...NORTHBRIDGE_DETAIL, entitlements: { x: { enabled: true } } }],
    ['a blocked access mode', { ...NORTHBRIDGE_DETAIL, access_mode: 'blocked' }],
  ])('rejects %s as an unexpected response', async (_label, body) => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, body))

    await expect(getOrganization('northbridge')).rejects.toMatchObject(UNEXPECTED)
  })
})

describe('the sorters of an organization', () => {
  const read = () => getOrganization('northbridge')
  /** Answers with Northbridge's detail, its sorters replaced, and reads it. */
  function withSorters(sorters: unknown) {
    stubFetch().mockResolvedValue(jsonResponse(200, { ...NORTHBRIDGE_DETAIL, sorters }))
    return read()
  }
  const central = NORTHBRIDGE_DETAIL.sorters[0]

  it('returns each sorter exactly as sent, in the order sent, apart from the branches', async () => {
    stubFetch().mockResolvedValue(jsonResponse(200, NORTHBRIDGE_DETAIL))

    const detail = await read()

    expect(detail.sorters).toStrictEqual([
      { slug: 'central', name: 'Central Library AMH', host_branch: { slug: 'central', name: 'Central Branch' }, status: 'active', collector_count: 1 },
      { slug: 'east-side', name: 'East Side AMH', host_branch: { slug: 'east-side', name: 'East Side Branch' }, status: 'active', collector_count: 1 },
    ])
    // Three branches, two sorters: a branch is not made a sorter, and a sorter is not made from a branch.
    expect(detail.branches.map((branch) => branch.slug)).toEqual(['central', 'east-side', 'westside'])
  })

  it('accepts an organization with no sorters', async () => {
    expect((await withSorters([])).sorters).toEqual([])
  })

  it.each(['active', 'provisioning', 'inactive'])('accepts the status %s', async (status) => {
    expect((await withSorters([{ ...central, status }])).sorters[0].status).toBe(status)
  })

  it('accepts a site with no reporting collector, and one with several', async () => {
    const detail = await withSorters([
      { ...central, collector_count: 0, status: 'inactive' },
      { ...NORTHBRIDGE_DETAIL.sorters[1], collector_count: 3 },
    ])

    expect(detail.sorters.map((sorter) => sorter.collector_count)).toEqual([0, 3])
  })

  it('accepts a sorter whose slug is not its host branch’s', async () => {
    const detail = await withSorters([{ ...central, slug: 'amh-1' }])

    expect(detail.sorters[0]).toMatchObject({ slug: 'amh-1', host_branch: { slug: 'central' } })
  })

  it('drops fields the contract does not have, such as a hostname or an id', async () => {
    const detail = await withSorters([
      {
        ...central,
        id: 41,
        installation_id: 41,
        hostname: 'NBPL-AMH-PC',
        collector_version: '1.0.13',
        agent_token: 'secret',
        host_branch: { ...central.host_branch, id: 7, operational_branch_id: 7 },
      },
    ])

    expect(detail.sorters).toStrictEqual([central])
    expect(JSON.stringify(detail)).not.toMatch(/hostname|NBPL-AMH-PC|1\.0\.13|secret|installation_id|operational_branch_id|41/)
  })

  it.each<[string, unknown]>([
    ['a missing sorters field', undefined],
    ['sorters that are not a list', { central }],
    ['null', null],
    ['a sorter that is not an object', ['central']],
    ['an empty slug', [{ ...central, slug: '' }]],
    ['a slug that is not text', [{ ...central, slug: 7 }]],
    ['a blank name', [{ ...central, name: '  ' }]],
    ['a name that is not text', [{ ...central, name: null }]],
    ['a missing host branch', [{ ...central, host_branch: undefined }]],
    ['a host branch that is text', [{ ...central, host_branch: 'central' }]],
    ['a host branch with no slug', [{ ...central, host_branch: { slug: '', name: 'Central Branch' } }]],
    ['a host branch with no name', [{ ...central, host_branch: { slug: 'central', name: '' } }]],
    ['the status retired', [{ ...central, status: 'retired' }]],
    ['a status in another case', [{ ...central, status: 'Active' }]],
    ['a missing status', [{ ...central, status: undefined }]],
    ['a negative collector count', [{ ...central, collector_count: -1 }]],
    ['a fractional collector count', [{ ...central, collector_count: 1.5 }]],
    ['a collector count as text', [{ ...central, collector_count: '1' }]],
    ['a missing collector count', [{ ...central, collector_count: undefined }]],
    ['two sorters with one slug', [central, { ...central, host_branch: { slug: 'east-side', name: 'East Side Branch' } }]],
    ['two sorters at one host branch', [central, { ...central, slug: 'amh-2', name: 'AMH 2' }]],
  ])('rejects %s as an unexpected response', async (_label, sorters) => {
    await expect(withSorters(sorters)).rejects.toMatchObject(UNEXPECTED)
  })
})
