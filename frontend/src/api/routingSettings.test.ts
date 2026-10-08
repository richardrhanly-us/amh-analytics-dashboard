import { describe, expect, it } from 'vitest'

import { callOf, jsonResponse, stubFetch } from '../test/http.ts'
import { fieldProblems } from './account.ts'
import { isApiError, UNEXPECTED_RESPONSE } from './client.ts'
import { getRoutingSettings, INVALID_ROUTING_SETTINGS, putRoutingSettings } from './routingSettings.ts'
import source from './routingSettings.ts?raw'

const ROUTING = { home_branch_label: 'Central', destinations: [{ label: 'North Annex', enabled: true }, { label: 'Depot', enabled: false }] }

async function rejection(promise: Promise<unknown>) {
  const error = await promise.then(
    () => null,
    (caught: unknown) => caught,
  )
  if (!isApiError(error)) {
    throw new Error('expected an ApiError')
  }
  return error
}

describe('reading the routing', () => {
  it('asks for the organization by slug and returns exactly the named fields', async () => {
    const fetchMock = stubFetch()
    // What the API never sends, sent anyway: none of it is kept.
    fetchMock.mockResolvedValue(
      jsonResponse(200, {
        routing: {
          home_branch_label: 'Central',
          destinations: [{ label: 'North Annex', enabled: true, key: 'north_annex', id: 9 }, { label: 'Depot', enabled: false }],
          branch_overrides: [{ branch: 'east' }],
          internal_routing: {},
        },
        organization_id: 4,
      }),
    )

    const routing = await getRoutingSettings('north bridge')

    expect(callOf(fetchMock).url).toBe('/api/organizations/north%20bridge/settings/routing')
    expect(callOf(fetchMock).init.method).toBe('GET')
    expect(callOf(fetchMock).init.credentials).toBe('same-origin')
    expect(routing).toEqual(ROUTING)
    expect(Object.keys(routing).sort()).toEqual(['destinations', 'home_branch_label'])
    expect(routing.destinations.map((destination) => Object.keys(destination).sort())).toEqual([['enabled', 'label'], ['enabled', 'label']])
  })

  it('accepts a blank home label and no destinations: nothing is set', async () => {
    stubFetch().mockResolvedValue(jsonResponse(200, { routing: { home_branch_label: '', destinations: [] } }))

    expect(await getRoutingSettings('acme')).toEqual({ home_branch_label: '', destinations: [] })
  })

  it.each([
    ['not wrapped', ROUTING],
    ['no routing', {}],
    ['routing that is a list', { routing: [] }],
    ['no home label', { routing: { destinations: [] } }],
    ['a home label that is null', { routing: { home_branch_label: null, destinations: [] } }],
    ['no destinations', { routing: { home_branch_label: 'Central' } }],
    ['destinations that are not a list', { routing: { home_branch_label: 'Central', destinations: { 0: { label: 'A', enabled: true } } } }],
    ['a destination that is text', { routing: { home_branch_label: 'Central', destinations: ['Depot'] } }],
    ['a destination with no label', { routing: { home_branch_label: 'Central', destinations: [{ enabled: true }] } }],
    ['a destination with a blank label', { routing: { home_branch_label: 'Central', destinations: [{ label: '', enabled: true }] } }],
    ['a flag that is not true or false', { routing: { home_branch_label: 'Central', destinations: [{ label: 'Depot', enabled: 'yes' }] } }],
    ['one good destination and one bad', { routing: { home_branch_label: 'Central', destinations: [{ label: 'Depot', enabled: true }, { label: 'Annex' }] } }],
  ])('refuses %s whole', async (_label, body) => {
    stubFetch().mockResolvedValue(jsonResponse(200, body))

    expect((await rejection(getRoutingSettings('acme'))).code).toBe(UNEXPECTED_RESPONSE)
  })

  it('passes a refusal on as the API said it, and makes no request for a slug that is not one', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(403, { code: 'forbidden', message: 'You do not have permission to manage these settings.' }))

    const error = await rejection(getRoutingSettings('acme'))
    expect([error.status, error.code]).toEqual([403, 'forbidden'])

    fetchMock.mockClear()
    expect((await rejection(getRoutingSettings('..'))).status).toBe(404)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('replacing the routing', () => {
  it('sends the whole block as labels and flags, exactly as given, and returns what the API then stored', async () => {
    const fetchMock = stubFetch()
    // The API trims: what comes back is what is stored, not what was sent.
    fetchMock.mockResolvedValue(jsonResponse(200, { routing: ROUTING }))

    const stored = await putRoutingSettings('acme', { home_branch_label: '', destinations: [{ label: '  North Annex ', enabled: true }] })

    const { url, init } = callOf(fetchMock)
    expect([init.method, url]).toEqual(['PUT', '/api/organizations/acme/settings/routing'])
    expect(JSON.parse(String(init.body))).toEqual({ routing: { home_branch_label: '', destinations: [{ label: '  North Annex ', enabled: true }] } })
    expect(stored).toEqual(ROUTING)
  })

  it('sends only a label and a flag for a destination, whatever else it is handed', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(jsonResponse(200, { routing: ROUTING }))
    const handed = { home_branch_label: 'Central', destinations: [{ label: 'Depot', enabled: false, key: 'depot', row: 7 }], extra: true }

    await putRoutingSettings('acme', handed as never)

    const body = JSON.parse(String(callOf(fetchMock).init.body)) as { routing: { destinations: object[] } }
    expect(Object.keys(body)).toEqual(['routing'])
    expect(Object.keys(body.routing).sort()).toEqual(['destinations', 'home_branch_label'])
    expect(body.routing.destinations.map((destination) => Object.keys(destination).sort())).toEqual([['enabled', 'label']])
    expect(JSON.stringify(body)).not.toMatch(/key|row|extra/)
  })

  it('rejects with the fields the API refused, by name and a word for why', async () => {
    const problems = [{ field: 'destinations.1.label', code: 'duplicate' }, { field: 'destinations', code: 'too_many' }]
    stubFetch().mockResolvedValue(jsonResponse(422, { code: INVALID_ROUTING_SETTINGS, message: 'The routing settings are not valid.', problems }))

    const error = await rejection(putRoutingSettings('acme', ROUTING))

    expect([error.status, error.code]).toEqual([422, 'invalid_routing_settings'])
    expect(fieldProblems(error, INVALID_ROUTING_SETTINGS)).toEqual(problems)
    // Another 422 is not a list of this form's problems.
    expect(fieldProblems(error, 'invalid_member')).toBeNull()
  })

  it('refuses an answer to a save that is not the routing', async () => {
    stubFetch().mockResolvedValue(jsonResponse(200, { saved: true }))

    expect((await rejection(putRoutingSettings('acme', ROUTING))).code).toBe(UNEXPECTED_RESPONSE)
  })
})

describe('what this module is made of', () => {
  it('has no identifier for a destination, nothing of a branch, and one address', () => {
    const code = source.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, '')

    expect(code).not.toMatch(/\bkey\b|: number|\bid\b|branch_settings|branchSlug|override|internal/i)
    expect(code.match(/['"`]\/api\/[^'"`]*['"`]/g)).toEqual(['`/api/organizations/${segment(orgSlug)}/settings/routing`'])
  })
})
