import { describe, expect, it } from 'vitest'

import { callOf, jsonResponse, noContent, stubFetch } from '../test/http.ts'
import { isApiError, UNEXPECTED_RESPONSE } from './client.ts'
import { addMember, changeMemberRole, getMemberActivity, getMembers, INVALID_MEMBER, removeMember } from './members.ts'
import source from './members.ts?raw'

const VERA = { email: 'vera@example.test', full_name: 'Vera Viewer', role: 'viewer', is_self: false, account_active: true }
const CHANGE = {
  occurred_at: '2026-10-05T18:50:00Z',
  event_type: 'membership_role_updated',
  member_email: 'vera@example.test',
  actor_email: 'olive@example.test',
  previous_role: 'viewer',
  role: 'manager',
}

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

describe('reading the members', () => {
  it('asks for the organization by slug and returns exactly the five fields of each member', async () => {
    const fetchMock = stubFetch()
    // What the API never sends, sent anyway: none of it is kept.
    fetchMock.mockResolvedValue(jsonResponse(200, { members: [{ ...VERA, user_id: 41, password_hash: 'x', last_login_at: null }], total: 1 }))

    const members = await getMembers('north bridge')

    expect(callOf(fetchMock).url).toBe('/api/organizations/north%20bridge/members')
    expect(callOf(fetchMock).init.method).toBe('GET')
    expect(callOf(fetchMock).init.credentials).toBe('same-origin')
    expect(members).toEqual([VERA])
    expect(Object.keys(members[0]).sort()).toEqual(['account_active', 'email', 'full_name', 'is_self', 'role'])
  })

  it('accepts a member with no name and an empty list', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { members: [{ ...VERA, full_name: '' }] }))
    fetchMock.mockResolvedValueOnce(jsonResponse(200, { members: [] }))

    expect((await getMembers('acme'))[0].full_name).toBe('')
    expect(await getMembers('acme')).toEqual([])
  })

  it.each([
    ['a bare list', [VERA]],
    ['no list', {}],
    ['a list that is not one', { members: 'vera' }],
    ['a member that is not an object', { members: ['vera@example.test'] }],
    ['no email', { members: [{ ...VERA, email: '' }] }],
    ['a name that is not text', { members: [{ ...VERA, full_name: null }] }],
    ['no role', { members: [{ ...VERA, role: undefined }] }],
    ['is_self that is not true or false', { members: [{ ...VERA, is_self: 'yes' }] }],
    ['account_active missing', { members: [{ email: VERA.email, full_name: '', role: 'viewer', is_self: false }] }],
    ['one good member and one bad', { members: [VERA, { ...VERA, account_active: 1 }] }],
  ])('refuses %s whole', async (_label, body) => {
    stubFetch().mockResolvedValue(jsonResponse(200, body))

    expect((await rejection(getMembers('acme'))).code).toBe(UNEXPECTED_RESPONSE)
  })

  it('passes a refusal on as the API said it', async () => {
    stubFetch().mockResolvedValue(jsonResponse(403, { code: 'forbidden', message: 'You do not have permission to manage this organization’s members.' }))

    const error = await rejection(getMembers('acme'))

    expect([error.status, error.code]).toEqual([403, 'forbidden'])
  })

  it('makes no request for a slug that is not one', async () => {
    const fetchMock = stubFetch()

    expect((await rejection(getMembers('..'))).status).toBe(404)
    expect(fetchMock).not.toHaveBeenCalled()
  })
})

describe('reading the recent changes', () => {
  it('returns exactly the six fields of each change, with what is unknown as null', async () => {
    const fetchMock = stubFetch()
    const removed = { ...CHANGE, event_type: 'membership_removed', role: null, actor_email: null }
    fetchMock.mockResolvedValue(jsonResponse(200, { activity: [{ ...CHANGE, audit_id: 9, actor_user_id: 3, metadata: {} }, removed] }))

    const activity = await getMemberActivity('acme')

    expect(callOf(fetchMock).url).toBe('/api/organizations/acme/members/activity')
    expect(activity).toEqual([CHANGE, removed])
    expect(Object.keys(activity[0]).sort()).toEqual(['actor_email', 'event_type', 'member_email', 'occurred_at', 'previous_role', 'role'])
  })

  it.each([
    ['no list', { changes: [] }],
    ['a time that is not UTC', { activity: [{ ...CHANGE, occurred_at: '2026-10-05T13:50:00-05:00' }] }],
    ['a time with no zone', { activity: [{ ...CHANGE, occurred_at: '2026-10-05T18:50:00' }] }],
    ['a time that names no moment', { activity: [{ ...CHANGE, occurred_at: '2026-13-45T18:50:00Z' }] }],
    ['no kind of change', { activity: [{ ...CHANGE, event_type: '' }] }],
    ['a role that is a number', { activity: [{ ...CHANGE, role: 3 }] }],
    ['an actor left out', { activity: [{ ...CHANGE, actor_email: undefined }] }],
  ])('refuses %s whole', async (_label, body) => {
    stubFetch().mockResolvedValue(jsonResponse(200, body))

    expect((await rejection(getMemberActivity('acme'))).code).toBe(UNEXPECTED_RESPONSE)
  })
})

describe('changing the members', () => {
  it('adds with an email, a name and a role in the body, and nothing else', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(noContent())

    await expect(addMember('acme', { email: 'new@example.test', full_name: 'New Person', role: 'manager' })).resolves.toBeUndefined()

    const { url, init } = callOf(fetchMock)
    expect([init.method, url]).toEqual(['POST', '/api/organizations/acme/members'])
    expect(JSON.parse(String(init.body))).toEqual({ email: 'new@example.test', full_name: 'New Person', role: 'manager' })
  })

  it('sends only the three fields even if it is handed more', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockResolvedValue(noContent())

    await addMember('acme', { email: 'a@example.test', full_name: '', role: 'viewer', password: 'x' } as never)

    expect(Object.keys(JSON.parse(String(callOf(fetchMock).init.body)) as object).sort()).toEqual(['email', 'full_name', 'role'])
  })

  it('names the member by email in the body, never in the address, when changing a role or removing', async () => {
    const fetchMock = stubFetch()
    fetchMock.mockImplementation(() => Promise.resolve(noContent()))

    await changeMemberRole('acme', 'vera@example.test', 'admin')
    await removeMember('acme', 'vera@example.test')

    const role = callOf(fetchMock, 0)
    const remove = callOf(fetchMock, 1)
    expect([role.init.method, role.url]).toEqual(['PUT', '/api/organizations/acme/members/role'])
    expect(JSON.parse(String(role.init.body))).toEqual({ email: 'vera@example.test', role: 'admin' })
    expect([remove.init.method, remove.url]).toEqual(['POST', '/api/organizations/acme/members/remove'])
    expect(JSON.parse(String(remove.init.body))).toEqual({ email: 'vera@example.test' })
    expect(role.url + remove.url).not.toMatch(/vera|%40|@/)
  })

  it('rejects with the code the API gave, and the fields it refused beside it', async () => {
    stubFetch().mockResolvedValue(
      jsonResponse(422, { code: INVALID_MEMBER, message: 'The member details are not valid.', problems: [{ field: 'email', code: 'invalid' }] }),
    )

    const error = await rejection(addMember('acme', { email: 'nope', full_name: '', role: 'viewer' }))

    expect([error.status, error.code]).toEqual([422, 'invalid_member'])
    expect(error.body).toEqual({ code: 'invalid_member', message: 'The member details are not valid.', problems: [{ field: 'email', code: 'invalid' }] })
  })
})

describe('what this module is made of', () => {
  it('has no number for a person and no secret of any kind in what it sends or keeps', () => {
    const code = source.replace(/\/\*[\s\S]*?\*\/|\/\/.*$/gm, '')

    expect(code).not.toMatch(/: number|user_id|member_id|membership_id|\bid\b/)
    expect(code).not.toMatch(/password|secret|token/i)
    // One address is written out; the others are built from it.
    expect(code.match(/['"`]\/api\/[^'"`]*['"`]/g)).toEqual(['`/api/organizations/${segment(orgSlug)}/members`'])
  })
})
