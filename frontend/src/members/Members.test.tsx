import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import {
  ALICE,
  type ApiRoutes,
  callOf,
  deferred,
  type FetchMock,
  jsonResponse,
  noContent,
  NORTHBRIDGE,
  NORTHBRIDGE_DETAIL,
  NOT_AUTHENTICATED,
  ORGANIZATION_NOT_FOUND,
  serveApi,
} from '../test/http.ts'
import { renderApp } from '../test/render.tsx'
import { formatLocalInstant } from '../time/localTime.ts'
import { activitySentence, assignableRoles, canManageMembers, roleName } from './memberText.ts'

const ORG = '/api/organizations/northbridge'
const MEMBERS = `${ORG}/members`
const PAGE = '/organizations/northbridge/members'
// No pause between keystrokes: nothing here depends on one.
const INSTANT = { delay: null }

const OLIVE = { email: 'olive@example.test', full_name: 'Olive Owner', role: 'owner', is_self: false, account_active: true }
const ME = { email: ALICE.email, full_name: ALICE.full_name, role: 'admin', is_self: true, account_active: true }
const MAX = { email: 'max@example.test', full_name: '', role: 'manager', is_self: false, account_active: true }
const VERA = { email: 'vera@example.test', full_name: 'Vera Viewer', role: 'viewer', is_self: false, account_active: false }
const EVERYONE = [ME, MAX, OLIVE, VERA]

const CHANGES = [
  { occurred_at: '2026-10-06T15:00:00Z', event_type: 'membership_removed', member_email: 'gone@example.test', actor_email: 'olive@example.test', previous_role: 'manager', role: null },
  { occurred_at: '2026-10-05T18:50:00Z', event_type: 'membership_role_updated', member_email: 'vera@example.test', actor_email: ALICE.email, previous_role: 'manager', role: 'viewer' },
  { occurred_at: '2026-10-04T09:30:00Z', event_type: 'membership_added', member_email: 'max@example.test', actor_email: null, previous_role: null, role: 'manager' },
]

const FORBIDDEN = { code: 'forbidden', message: 'You do not have permission to manage this organization’s members.' }
const LAST_OWNER = { code: 'last_owner', message: 'An organization must keep at least one active owner.' }
const refused = (status: number, body: unknown) => () => jsonResponse(status, body)

interface World {
  role?: string
  access_mode?: string
  members?: unknown[]
  activity?: unknown[]
}

/**
 * Alice signed in, Northbridge with the role and state given, and its members. `world.members` is read on every
 * request, so a test changes what the next read answers by changing it.
 */
function serve(world: World = {}, overrides: ApiRoutes = {}): FetchMock {
  const { role = 'admin', access_mode = 'full' } = world
  return serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    'GET /api/organizations': () => jsonResponse(200, [{ ...NORTHBRIDGE, role, access_mode }]),
    [`GET ${ORG}`]: () => jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role, access_mode }),
    [`GET ${MEMBERS}`]: () => jsonResponse(200, { members: world.members ?? EVERYONE.map((member) => (member.is_self ? { ...member, role } : member)) }),
    [`GET ${MEMBERS}/activity`]: () => jsonResponse(200, { activity: world.activity ?? CHANGES }),
    ...overrides,
  })
}

const main = () => screen.getByRole('main')
const requests = (fetchMock: FetchMock) => fetchMock.mock.calls.map(([url, init]) => `${init?.method ?? 'GET'} ${String(url)}`)
function bodyOf(fetchMock: FetchMock, request: string): unknown {
  return JSON.parse(String(callOf(fetchMock, requests(fetchMock).lastIndexOf(request)).init.body)) as unknown
}
const membersTable = () => screen.getByRole('table', { name: 'Members' })
const rowOf = (email: string) => within(membersTable()).getByRole('cell', { name: email }).closest('tr') as HTMLTableRowElement
const cellsOf = (email: string) => within(rowOf(email)).getAllByRole('cell').map((cell) => cell.textContent)
const choicesIn = (select: HTMLElement) => within(select).getAllByRole('option').map((option) => option.textContent)

/** Opens the page and waits for the list. */
async function page(world: World = {}, overrides: ApiRoutes = {}) {
  const fetchMock = serve(world, overrides)
  renderApp(PAGE)
  await screen.findByRole('table', { name: 'Members' })
  return { fetchMock, user: userEvent.setup(INSTANT) }
}

// =====================================================================================================================
// Getting there, and who it is for
// =====================================================================================================================

describe('the way to the page', () => {
  it.each(['owner', 'admin'])('is one of the sections an %s reaches from the organization page, at the address it always had', async (role) => {
    serve({ role })
    const user = userEvent.setup(INSTANT)
    renderApp('/organizations/northbridge')

    // R8F: the organization page has one way in, and the members page is a section of what it opens.
    await user.click(await screen.findByRole('link', { name: 'Settings' }))
    const link = within(await screen.findByRole('navigation', { name: 'Settings' })).getByRole('link', { name: 'Users & Access' })
    expect(link).toHaveAttribute('href', PAGE)
    await user.click(link)

    expect(await screen.findByRole('heading', { level: 2, name: 'Users & Access' })).toHaveFocus()
    expect(screen.getByTestId('address')).toHaveTextContent(PAGE)
    expect(document.title).toBe('Users & Access – SortView')
    expect(within(screen.getByRole('navigation', { name: 'Breadcrumb' })).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'Organizations',
      'Northbridge Library',
      'Settings',
      'Users & Access',
    ])
    // The strip of sections is on this page too, and says this is the one being shown.
    const strip = within(screen.getByRole('navigation', { name: 'Settings' }))
    expect(strip.getAllByRole('link').map((item) => item.textContent)).toEqual(['General', 'Users & Access', 'Branches & Sorters', 'Routing', 'Efficiency'])
    expect(strip.getByRole('link', { name: 'Users & Access' })).toHaveAttribute('aria-current', 'page')
    expect(strip.getByRole('link', { name: 'General' })).not.toHaveAttribute('aria-current')
    await screen.findByRole('table', { name: 'Members' })
  })

  it.each(['manager', 'viewer', 'something-new'])('is not offered to a %s, and is not in the header for anyone', async (role) => {
    serve({ role })
    renderApp('/organizations/northbridge')
    await screen.findByRole('link', { name: 'Organization Reports' })

    expect(screen.queryByRole('link', { name: 'Users & Access' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Settings' })).not.toBeInTheDocument()
    expect(within(screen.getByRole('navigation', { name: 'Account' })).queryByText(/Users|Access|Members|Settings/)).not.toBeInTheDocument()
  })

  it.each(['manager', 'viewer'])('says who it is for when a %s goes to its address, and asks the API for nothing', async (role) => {
    const fetchMock = serve({ role })
    renderApp(PAGE)

    expect(await screen.findByRole('note')).toHaveTextContent("Members are managed by this organization's owners and admins.")
    expect(screen.getByRole('heading', { level: 2, name: 'Users & Access' })).toBeInTheDocument()
    // No strip of sections, and no link into them: as before R8F.
    expect(screen.queryByRole('navigation', { name: 'Settings' })).not.toBeInTheDocument()
    expect(within(main()).getAllByRole('link').map((item) => item.textContent)).toEqual(['Organizations', 'Northbridge Library'])
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(within(main()).queryAllByRole('button')).toEqual([])
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(requests(fetchMock).filter((request) => request.includes('/members'))).toEqual([])
  })

  it('shows the same and nothing of a list when the API says no to someone the page thought was an admin', async () => {
    serve({}, { [`GET ${MEMBERS}`]: refused(403, FORBIDDEN), [`GET ${MEMBERS}/activity`]: refused(403, FORBIDDEN) })
    renderApp(PAGE)

    expect(await screen.findByRole('note')).toHaveTextContent("Members are managed by this organization's owners and admins.")
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(within(main()).queryAllByRole('button')).toEqual([])
  })

  it('is the ordinary missing page when the organization is no longer there to see', async () => {
    serve({}, { [`GET ${MEMBERS}`]: refused(404, ORGANIZATION_NOT_FOUND), [`GET ${MEMBERS}/activity`]: refused(404, ORGANIZATION_NOT_FOUND) })
    renderApp(PAGE)

    expect(await screen.findByRole('heading', { level: 2, name: 'Page not found' })).toBeInTheDocument()
    expect(screen.getAllByRole('heading', { level: 2 })).toHaveLength(1)
    expect(main()).not.toHaveTextContent(/northbridge|Users/i)
  })

  it('returns to the sign-in form when the session has ended', async () => {
    serve({}, { [`GET ${MEMBERS}`]: refused(401, NOT_AUTHENTICATED), [`GET ${MEMBERS}/activity`]: refused(401, NOT_AUTHENTICATED) })
    renderApp(PAGE)

    expect(await screen.findByLabelText('Email')).toBeInTheDocument()
    expect(screen.getByLabelText('Password')).toBeInTheDocument()
    expect(screen.getByTestId('address')).toHaveTextContent(PAGE)
  })

  it('says a load failed and tries again when asked', async () => {
    let fail = true
    const fetchMock = serve({}, {
      [`GET ${MEMBERS}`]: () => (fail ? jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' }) : jsonResponse(200, { members: EVERYONE })),
    })
    const user = userEvent.setup(INSTANT)
    renderApp(PAGE)

    expect(await screen.findByRole('alert')).toHaveTextContent('Internal server error.')
    fail = false
    await user.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByRole('table', { name: 'Members' })).toBeInTheDocument()
    expect(requests(fetchMock).filter((request) => request === `GET ${MEMBERS}`)).toHaveLength(2)
  })
})

// =====================================================================================================================
// The members
// =====================================================================================================================

describe('the members table', () => {
  it('lists each member with a name, an email, a role and the state of their account, in the API’s order', async () => {
    await page({ role: 'owner' })

    expect(within(membersTable()).getAllByRole('columnheader').map((header) => header.textContent)).toEqual(['Name', 'Email', 'Role', 'Account', 'Actions'])
    expect(within(membersTable()).getAllByRole('rowheader').map((header) => header.textContent)).toEqual([
      'Alice Example (you)',
      'Not provided',
      'Olive Owner',
      'Vera Viewer',
    ])
    expect(cellsOf('max@example.test').slice(0, 3)).toEqual(['max@example.test', 'Manager', 'Active'])
    expect(cellsOf('vera@example.test').slice(0, 3)).toEqual(['vera@example.test', 'Viewer', 'Deactivated'])
    expect(within(membersTable()).getAllByText('(you)', { exact: false })).toHaveLength(1)
    expect(screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent)).toEqual(['Members', 'Add a member', 'Recent changes'])
  })

  it('explains a deactivated account, and offers nothing that could change it', async () => {
    await page()

    expect(main()).toHaveTextContent(
      'A deactivated SortView account cannot sign in to any organization. This is managed by SortView support and cannot be changed here.',
    )
    expect(within(rowOf('vera@example.test')).getAllByRole('button').map((button) => button.textContent)).toEqual([
      'Change role for vera@example.test',
      'Remove vera@example.test',
    ])
    expect(within(main()).queryByRole('button', { name: /activate|enable|restore/i })).not.toBeInTheDocument()
  })

  it('says nothing about deactivated accounts when there are none', async () => {
    await page({ members: [ME, OLIVE] })

    expect(main()).not.toHaveTextContent(/deactivated/i)
  })

  it('shows a role it does not know in neutral words, never the API’s own', async () => {
    await page({ members: [ME, { ...MAX, role: 'auditor_v2' }] })

    expect(cellsOf('max@example.test')[1]).toBe('another role')
    expect(main()).not.toHaveTextContent('auditor_v2')
  })

  it('has no field for a password and shows no number for anyone', async () => {
    await page({ role: 'owner' })

    expect(document.querySelectorAll('input[type="password"]')).toHaveLength(0)
    expect(screen.queryByLabelText(/password/i)).not.toBeInTheDocument()
    expect(Array.from(document.querySelectorAll('input, select')).map((control) => control.getAttribute('name'))).toEqual(['full_name', 'email', 'role'])
    expect(main()).not.toHaveTextContent(new RegExp(`\\b${ALICE.id}\\b`))
  })
})

// =====================================================================================================================
// Roles
// =====================================================================================================================

describe('which roles are offered', () => {
  it('offers an owner all four, in the add form and in a row', async () => {
    const { user } = await page({ role: 'owner' })

    expect(choicesIn(screen.getByLabelText('Role'))).toEqual(['Owner', 'Admin', 'Manager', 'Viewer'])
    await user.click(screen.getByRole('button', { name: 'Change role for olive@example.test' }))
    expect(choicesIn(screen.getByLabelText('Role for olive@example.test'))).toEqual(['Owner', 'Admin', 'Manager', 'Viewer'])
  })

  it('offers an admin three, and no controls at all on an owner’s row', async () => {
    const { user } = await page({ role: 'admin' })

    expect(choicesIn(screen.getByLabelText('Role'))).toEqual(['Admin', 'Manager', 'Viewer'])
    await user.click(screen.getByRole('button', { name: 'Change role for max@example.test' }))
    expect(choicesIn(screen.getByLabelText('Role for max@example.test'))).toEqual(['Admin', 'Manager', 'Viewer'])

    expect(within(rowOf('olive@example.test')).queryAllByRole('button')).toEqual([])
    expect(rowOf('olive@example.test')).toHaveTextContent('Only an owner can change this member.')
  })

  it('works the choices out from the role alone, and offers none to anyone else', () => {
    expect(assignableRoles('owner')).toEqual(['owner', 'admin', 'manager', 'viewer'])
    expect(assignableRoles('admin')).toEqual(['admin', 'manager', 'viewer'])
    for (const role of ['manager', 'viewer', '', 'Owner', 'superuser']) {
      expect(assignableRoles(role)).toEqual([])
      expect(canManageMembers(role)).toBe(false)
    }
    expect(['owner', 'admin', 'manager', 'viewer', 'x', null].map(roleName)).toEqual(['Owner', 'Admin', 'Manager', 'Viewer', 'another role', 'another role'])
  })
})

describe('changing a role', () => {
  it('opens in the row with focus on the choice, saves, and shows what the API then says', async () => {
    const world: World = { role: 'owner' }
    const { fetchMock, user } = await page(world, {
      [`PUT ${MEMBERS}/role`]: () => {
        world.members = [{ ...ME, role: 'owner' }, { ...MAX, role: 'admin' }, OLIVE, VERA]
        return noContent()
      },
    })

    await user.click(screen.getByRole('button', { name: 'Change role for max@example.test' }))
    const choice = screen.getByLabelText('Role for max@example.test')
    expect(choice).toHaveFocus()
    expect(choice).toHaveValue('manager')
    await user.selectOptions(choice, 'admin')
    await user.click(screen.getByRole('button', { name: 'Save role' }))

    expect(await within(rowOf('max@example.test')).findByRole('status')).toHaveTextContent('Role updated.')
    expect(bodyOf(fetchMock, `PUT ${MEMBERS}/role`)).toEqual({ email: 'max@example.test', role: 'admin' })
    expect(cellsOf('max@example.test')[1]).toBe('Admin')
    // Asked again, not patched by hand: the list and the recent changes.
    expect(requests(fetchMock).filter((request) => request === `GET ${MEMBERS}`)).toHaveLength(2)
    expect(requests(fetchMock).filter((request) => request === `GET ${MEMBERS}/activity`)).toHaveLength(2)
    expect(screen.getByRole('button', { name: 'Change role for max@example.test' })).toHaveFocus()
  })

  it('shows the API’s answer in that row when it would leave no owner, and changes nothing', async () => {
    const { user } = await page({ role: 'owner' }, { [`PUT ${MEMBERS}/role`]: refused(409, LAST_OWNER) })

    await user.click(screen.getByRole('button', { name: `Change role for ${ALICE.email}` }))
    expect(rowOf(ALICE.email)).toHaveTextContent('You will lose access to this page.')
    await user.selectOptions(screen.getByLabelText(`Role for ${ALICE.email}`), 'viewer')
    await user.click(screen.getByRole('button', { name: 'Save role' }))

    expect(await within(rowOf(ALICE.email)).findByRole('alert')).toHaveTextContent('An organization must keep at least one active owner.')
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(screen.getByLabelText(`Role for ${ALICE.email}`)).toHaveValue('viewer')
    expect(screen.getByTestId('address')).toHaveTextContent(PAGE)
  })

  it('says the change was not made when the server fails, and keeps the choice', async () => {
    const { user } = await page({}, { [`PUT ${MEMBERS}/role`]: refused(500, { code: 'internal_error', message: 'Internal server error.' }) })

    await user.click(screen.getByRole('button', { name: 'Change role for max@example.test' }))
    await user.selectOptions(screen.getByLabelText('Role for max@example.test'), 'viewer')
    await user.click(screen.getByRole('button', { name: 'Save role' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('The change was not made. Internal server error.')
    expect(screen.getByLabelText('Role for max@example.test')).toHaveValue('viewer')
  })

  it('asks for the list again when the member turns out to be gone', async () => {
    const world: World = {}
    const { fetchMock, user } = await page(world, {
      [`PUT ${MEMBERS}/role`]: () => {
        world.members = [ME, OLIVE, VERA]
        return jsonResponse(404, { code: 'member_not_found', message: 'That person is not a member of this organization.' })
      },
    })

    await user.click(screen.getByRole('button', { name: 'Change role for max@example.test' }))
    await user.click(screen.getByRole('button', { name: 'Save role' }))

    await waitFor(() => expect(within(membersTable()).queryByRole('cell', { name: 'max@example.test' })).not.toBeInTheDocument())
    expect(requests(fetchMock).filter((request) => request === `GET ${MEMBERS}`)).toHaveLength(2)
  })

  it('cancels back to the button that opened it, having asked for nothing', async () => {
    const { fetchMock, user } = await page()

    await user.click(screen.getByRole('button', { name: 'Change role for max@example.test' }))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(screen.getByRole('button', { name: 'Change role for max@example.test' })).toHaveFocus()
    expect(requests(fetchMock).some((request) => request.startsWith('PUT'))).toBe(false)
  })

  it('goes by the role the API then confirms when an owner makes themself an admin, and stays on the page', async () => {
    // What the API says of Alice: an owner, until the change is made. Every read answers with what it then is.
    let mine = 'owner'
    const detail = () => jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role: mine, access_mode: 'full' })
    const world: World = { role: 'owner' }
    const { fetchMock, user } = await page(world, {
      [`GET ${ORG}`]: detail,
      [`PUT ${MEMBERS}/role`]: () => {
        mine = 'admin'
        world.members = [{ ...ME, role: 'admin' }, MAX, OLIVE, VERA]
        return noContent()
      },
    })
    const organizationReads = () => requests(fetchMock).filter((request) => request === `GET ${ORG}`).length
    expect(organizationReads()).toBe(1)
    expect(within(rowOf('olive@example.test')).getAllByRole('button')).toHaveLength(2)
    expect(choicesIn(screen.getByLabelText('Role'))).toEqual(['Owner', 'Admin', 'Manager', 'Viewer'])

    await user.click(screen.getByRole('button', { name: `Change role for ${ALICE.email}` }))
    await user.selectOptions(screen.getByLabelText(`Role for ${ALICE.email}`), 'admin')
    await user.click(screen.getByRole('button', { name: 'Save role' }))

    // The change was made, and the organization was read again -- once.
    expect(await within(rowOf(ALICE.email)).findByRole('status')).toHaveTextContent('Role updated.')
    expect(bodyOf(fetchMock, `PUT ${MEMBERS}/role`)).toEqual({ email: ALICE.email, role: 'admin' })
    expect(organizationReads()).toBe(2)
    // Still here, on the same page, with the list as the API now has it.
    expect(screen.getByTestId('address')).toHaveTextContent(PAGE)
    expect(screen.getByRole('heading', { level: 2, name: 'Users & Access' })).toBeInTheDocument()
    expect(cellsOf(ALICE.email)[1]).toBe('Admin')
    // An admin now: Owner is not offered, in the add form or in a row ...
    expect(choicesIn(screen.getByLabelText('Role'))).toEqual(['Admin', 'Manager', 'Viewer'])
    await user.click(screen.getByRole('button', { name: 'Change role for max@example.test' }))
    expect(choicesIn(screen.getByLabelText('Role for max@example.test'))).toEqual(['Admin', 'Manager', 'Viewer'])
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    // ... an owner's row has nothing to change it with ...
    expect(within(rowOf('olive@example.test')).queryAllByRole('button')).toEqual([])
    expect(rowOf('olive@example.test')).toHaveTextContent('Only an owner can change this member.')
    // ... and everyone an admin may manage still can be, themself included.
    for (const email of [ALICE.email, 'max@example.test', 'vera@example.test']) {
      expect(within(rowOf(email)).getAllByRole('button')).toHaveLength(2)
    }
  })

  it('does not read the organization again when it is someone else\u2019s role that changed', async () => {
    const world: World = { role: 'owner' }
    const { fetchMock, user } = await page(world, {
      [`PUT ${MEMBERS}/role`]: () => {
        world.members = [{ ...ME, role: 'owner' }, { ...MAX, role: 'viewer' }, OLIVE, VERA]
        return noContent()
      },
    })

    await user.click(screen.getByRole('button', { name: 'Change role for max@example.test' }))
    await user.selectOptions(screen.getByLabelText('Role for max@example.test'), 'viewer')
    await user.click(screen.getByRole('button', { name: 'Save role' }))

    expect(await within(rowOf('max@example.test')).findByRole('status')).toHaveTextContent('Role updated.')
    expect(requests(fetchMock).filter((request) => request === `GET ${ORG}`)).toHaveLength(1)
    expect(choicesIn(screen.getByLabelText('Role'))).toEqual(['Owner', 'Admin', 'Manager', 'Viewer'])
  })

  it('takes the page away if the organization, read again, is no longer this person\u2019s to see', async () => {
    let gone = false
    const { user } = await page({ role: 'owner' }, {
      [`GET ${ORG}`]: () => (gone ? jsonResponse(404, ORGANIZATION_NOT_FOUND) : jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role: 'owner' })),
      [`PUT ${MEMBERS}/role`]: () => {
        gone = true
        return noContent()
      },
    })

    await user.click(screen.getByRole('button', { name: `Change role for ${ALICE.email}` }))
    await user.selectOptions(screen.getByLabelText(`Role for ${ALICE.email}`), 'admin')
    await user.click(screen.getByRole('button', { name: 'Save role' }))

    expect(await screen.findByRole('heading', { level: 2, name: 'Page not found' })).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('leaves for the organization list when an owner makes themself a manager, with the API answering as it then would', async () => {
    // As in production: once the change is made, the organization says "manager", and the members and their
    // recent changes are no longer this person's to read.
    let mine = 'owner'
    const forbiddenNow = (ok: () => Response) => () => (mine === 'owner' ? ok() : jsonResponse(403, FORBIDDEN))
    const fetchMock = serveApi({
      'GET /api/auth/session': () => jsonResponse(200, ALICE),
      'GET /api/organizations': () => jsonResponse(200, [{ ...NORTHBRIDGE, role: mine }]),
      [`GET ${ORG}`]: () => jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role: mine }),
      [`GET ${MEMBERS}`]: forbiddenNow(() => jsonResponse(200, { members: [{ ...ME, role: 'owner' }, MAX, OLIVE, VERA] })),
      [`GET ${MEMBERS}/activity`]: forbiddenNow(() => jsonResponse(200, { activity: CHANGES })),
      [`PUT ${MEMBERS}/role`]: () => {
        mine = 'manager'
        return noContent()
      },
    })
    const user = userEvent.setup(INSTANT)
    renderApp(PAGE)
    await screen.findByRole('table', { name: 'Members' })
    const organizationReads = () => requests(fetchMock).filter((request) => request === `GET ${ORG}`).length
    expect(organizationReads()).toBe(1)

    await user.click(screen.getByRole('button', { name: `Change role for ${ALICE.email}` }))
    await user.selectOptions(screen.getByLabelText(`Role for ${ALICE.email}`), 'manager')
    await user.click(screen.getByRole('button', { name: 'Save role' }))

    // The row that asked is gone by the time the change resolves -- the page it was on is no longer for this
    // person -- and the way out is still taken.
    await waitFor(() => expect(screen.getByTestId('address')).toHaveTextContent(/^\/organizations$/))
    expect(bodyOf(fetchMock, `PUT ${MEMBERS}/role`)).toEqual({ email: ALICE.email, role: 'manager' })
    expect(organizationReads()).toBe(2)
    expect(await screen.findByRole('heading', { level: 2, name: 'Organizations' })).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(within(main()).queryAllByRole('button')).toEqual([])
    expect(within(main()).queryByRole('combobox')).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/Change role|Add a member|Members are managed/)

    // Back in the organization, read afresh: the page is not offered, and its address says who it is for.
    await user.click(await screen.findByRole('link', { name: 'Northbridge Library' }))
    await screen.findByRole('link', { name: 'Organization Reports' })
    expect(screen.queryByRole('link', { name: 'Settings' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Users & Access' })).not.toBeInTheDocument()
  })

  it('leaves for the organization list even when every read goes on answering as before', async () => {
    const { user } = await page({ role: 'owner' }, { [`PUT ${MEMBERS}/role`]: () => noContent() })

    await user.click(screen.getByRole('button', { name: `Change role for ${ALICE.email}` }))
    await user.selectOptions(screen.getByLabelText(`Role for ${ALICE.email}`), 'manager')
    await user.click(screen.getByRole('button', { name: 'Save role' }))

    await waitFor(() => expect(screen.getByTestId('address')).toHaveTextContent(/^\/organizations$/))
    expect(screen.queryByRole('table', { name: 'Members' })).not.toBeInTheDocument()
  })

  it('returns to the sign-in form when the session ended before the change', async () => {
    const { user } = await page({}, { [`PUT ${MEMBERS}/role`]: refused(401, NOT_AUTHENTICATED) })

    await user.click(screen.getByRole('button', { name: 'Change role for max@example.test' }))
    await user.click(screen.getByRole('button', { name: 'Save role' }))

    expect(await screen.findByLabelText('Password')).toBeInTheDocument()
  })
})

// =====================================================================================================================
// Removing
// =====================================================================================================================

describe('removing a member', () => {
  it('asks first, in the row, saying what it does and does not affect', async () => {
    const { fetchMock, user } = await page()

    await user.click(screen.getByRole('button', { name: 'Remove max@example.test' }))

    expect(rowOf('max@example.test')).toHaveTextContent(
      'Remove max@example.test from Northbridge Library? Their SortView account and any other organizations are not affected.',
    )
    expect(within(rowOf('max@example.test')).getAllByRole('button').map((button) => button.textContent)).toEqual(['Remove', 'Cancel'])
    expect(within(rowOf('max@example.test')).getByRole('button', { name: 'Remove' })).toHaveFocus()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(requests(fetchMock).some((request) => request.startsWith('POST'))).toBe(false)
  })

  it('cancels back to the button that opened it, and removes nobody', async () => {
    const { fetchMock, user } = await page()

    await user.click(screen.getByRole('button', { name: 'Remove max@example.test' }))
    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(screen.getByRole('button', { name: 'Remove max@example.test' })).toHaveFocus()
    expect(rowOf('max@example.test')).not.toHaveTextContent(/not affected/)
    expect(requests(fetchMock).some((request) => request.startsWith('POST'))).toBe(false)
  })

  it('removes on the second step, asks for the list again, and moves focus to the Members heading', async () => {
    const world: World = {}
    const { fetchMock, user } = await page(world, {
      [`POST ${MEMBERS}/remove`]: () => {
        world.members = [ME, OLIVE, VERA]
        return noContent()
      },
    })

    await user.click(screen.getByRole('button', { name: 'Remove max@example.test' }))
    await user.click(within(rowOf('max@example.test')).getByRole('button', { name: 'Remove' }))

    await waitFor(() => expect(within(membersTable()).queryByRole('cell', { name: 'max@example.test' })).not.toBeInTheDocument())
    expect(bodyOf(fetchMock, `POST ${MEMBERS}/remove`)).toEqual({ email: 'max@example.test' })
    expect(requests(fetchMock).filter((request) => request === `GET ${MEMBERS}`)).toHaveLength(2)
    expect(screen.getByRole('heading', { level: 3, name: 'Members' })).toHaveFocus()
    expect(within(screen.getByRole('region', { name: 'Members' })).getByRole('status')).toHaveTextContent('Removed from this organization.')
    expect(screen.getByTestId('address')).toHaveTextContent(PAGE)
  })

  it('stays busy, with focus, while the removal is in flight, and does not ask twice', async () => {
    const pending = deferred<Response>()
    const { fetchMock, user } = await page({}, { [`POST ${MEMBERS}/remove`]: () => pending.promise })

    await user.click(screen.getByRole('button', { name: 'Remove max@example.test' }))
    const confirm = within(rowOf('max@example.test')).getByRole('button', { name: 'Remove' })
    await user.click(confirm)
    const busy = await within(rowOf('max@example.test')).findByRole('button', { name: 'Removing…' })
    await user.click(busy)

    expect(busy).toHaveAttribute('aria-disabled', 'true')
    expect(busy).not.toBeDisabled()
    expect(busy).toHaveFocus()
    expect(requests(fetchMock).filter((request) => request === `POST ${MEMBERS}/remove`)).toHaveLength(1)
    pending.resolve(noContent())
    await waitFor(() => expect(screen.getByRole('heading', { level: 3, name: 'Members' })).toHaveFocus())
  })

  it('shows the API’s answer in that row when it would leave no owner', async () => {
    const { user } = await page({ role: 'owner' }, { [`POST ${MEMBERS}/remove`]: refused(409, LAST_OWNER) })

    await user.click(screen.getByRole('button', { name: 'Remove olive@example.test' }))
    await user.click(within(rowOf('olive@example.test')).getByRole('button', { name: 'Remove' }))

    expect(await within(rowOf('olive@example.test')).findByRole('alert')).toHaveTextContent('An organization must keep at least one active owner.')
    expect(within(membersTable()).getByRole('cell', { name: 'olive@example.test' })).toBeInTheDocument()
  })

  it('warns someone removing themself, and takes them to the organization list when it is done', async () => {
    const { user } = await page({}, { [`POST ${MEMBERS}/remove`]: () => noContent() })

    await user.click(screen.getByRole('button', { name: `Remove ${ALICE.email}` }))
    expect(rowOf(ALICE.email)).toHaveTextContent('You will lose access to this page.')
    await user.click(within(rowOf(ALICE.email)).getByRole('button', { name: 'Remove' }))

    await waitFor(() => expect(screen.getByTestId('address')).toHaveTextContent(/^\/organizations$/))
  })

  it.each(['owner', 'admin'])('takes an %s who removes themself to the organization list, with the API answering as it then would', async (role) => {
    // As in production: once removed, the organization is not theirs to see -- not its members, not itself --
    // and it is no longer in their list.
    let member = true
    const goneNow = (ok: () => Response) => () => (member ? ok() : jsonResponse(404, ORGANIZATION_NOT_FOUND))
    const fetchMock = serveApi({
      'GET /api/auth/session': () => jsonResponse(200, ALICE),
      'GET /api/organizations': () => jsonResponse(200, member ? [{ ...NORTHBRIDGE, role }] : []),
      [`GET ${ORG}`]: goneNow(() => jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role })),
      [`GET ${MEMBERS}`]: goneNow(() => jsonResponse(200, { members: [{ ...ME, role }, MAX, OLIVE, VERA] })),
      [`GET ${MEMBERS}/activity`]: goneNow(() => jsonResponse(200, { activity: CHANGES })),
      [`POST ${MEMBERS}/remove`]: () => {
        member = false
        return noContent()
      },
    })
    const user = userEvent.setup(INSTANT)
    renderApp(PAGE)
    await screen.findByRole('table', { name: 'Members' })

    await user.click(screen.getByRole('button', { name: `Remove ${ALICE.email}` }))
    await user.click(within(rowOf(ALICE.email)).getByRole('button', { name: 'Remove' }))

    // The list was asked for again and answered 404, so the row that asked is gone before the removal resolves.
    // The way out is still taken: nobody is left on the page they were on, or on a missing page.
    await waitFor(() => expect(screen.getByTestId('address')).toHaveTextContent(/^\/organizations$/))
    expect(bodyOf(fetchMock, `POST ${MEMBERS}/remove`)).toEqual({ email: ALICE.email })
    expect(requests(fetchMock).filter((request) => request === `GET ${MEMBERS}`)).toHaveLength(2)
    expect(await screen.findByRole('heading', { level: 2, name: 'Organizations' })).toBeInTheDocument()
    expect(await screen.findByText('Your account does not have access to any organizations.')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Page not found' })).not.toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/Users & Access|Northbridge/)
  })
})

// =====================================================================================================================
// Adding
// =====================================================================================================================

describe('adding a member', () => {
  it('sends a name, an email and a role, says the same thing whoever it was, and clears the form', async () => {
    const world: World = {}
    const { fetchMock, user } = await page(world, {
      [`POST ${MEMBERS}`]: () => {
        world.members = [...EVERYONE, { email: 'new@example.test', full_name: 'New Person', role: 'manager', is_self: false, account_active: true }]
        return noContent()
      },
    })
    const form = within(screen.getByRole('form', { name: 'Add a member' }))

    expect(form.getByLabelText('Role')).toHaveValue('viewer')
    expect(form.getByLabelText('Email')).toHaveAccessibleDescription(
      /If this person already has a SortView account, they keep their existing password and sign in as usual\. If they are new, they set a password by choosing .Forgot password\?. on the sign-in page\. No email is sent from here\./,
    )
    await user.type(form.getByLabelText('Full name'), 'New Person')
    await user.type(form.getByLabelText('Email'), 'new@example.test')
    await user.selectOptions(form.getByLabelText('Role'), 'manager')
    await user.click(form.getByRole('button', { name: 'Add member' }))

    expect(await form.findByRole('status')).toHaveTextContent(/^Added\. If they are new to SortView, they will need to use .Forgot password\?. to set a password\.$/)
    expect(bodyOf(fetchMock, `POST ${MEMBERS}`)).toEqual({ email: 'new@example.test', full_name: 'New Person', role: 'manager' })
    expect(form.getByLabelText('Full name')).toHaveValue('')
    expect(form.getByLabelText('Email')).toHaveValue('')
    expect(within(membersTable()).getByRole('cell', { name: 'new@example.test' })).toBeInTheDocument()
    expect(form.getByRole('status')).not.toHaveTextContent(/created|existing account|already had/i)
  })

  it('asks the API for nothing without an email', async () => {
    const { fetchMock, user } = await page()

    await user.click(screen.getByRole('button', { name: 'Add member' }))

    const email = screen.getByLabelText('Email')
    expect(email).toHaveAttribute('aria-invalid', 'true')
    expect(email).toHaveAccessibleDescription(/^Enter an email address\./)
    expect(requests(fetchMock).some((request) => request.startsWith('POST'))).toBe(false)
  })

  it.each([
    ['email', 'Email', 'Enter a valid email address.'],
    ['role', 'Role', 'Choose a role from the list.'],
  ])('puts a refused %s beside its field and keeps what was typed', async (field, label, sentence) => {
    const { user } = await page({}, {
      [`POST ${MEMBERS}`]: refused(422, { code: 'invalid_member', message: 'The member details are not valid.', problems: [{ field, code: 'invalid' }] }),
    })

    await user.type(screen.getByLabelText('Email'), 'not-an-address')
    await user.click(screen.getByRole('button', { name: 'Add member' }))

    await waitFor(() => expect(screen.getByLabelText(label)).toHaveAttribute('aria-invalid', 'true'))
    expect(screen.getByLabelText(label)).toHaveAccessibleDescription(new RegExp(`^${sentence.replace('.', '\\.')}`))
    expect(screen.getByLabelText('Email')).toHaveValue('not-an-address')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent('The member details are not valid.')
  })

  it('says beside the email that the person is already a member', async () => {
    const { user } = await page({}, {
      [`POST ${MEMBERS}`]: refused(409, { code: 'already_member', message: 'That person is already a member of this organization.' }),
    })

    await user.type(screen.getByLabelText('Email'), 'max@example.test')
    await user.click(screen.getByRole('button', { name: 'Add member' }))

    await waitFor(() => expect(screen.getByLabelText('Email')).toHaveAttribute('aria-invalid', 'true'))
    expect(screen.getByLabelText('Email')).toHaveAccessibleDescription(/^That person is already a member of this organization\./)
    expect(screen.getByLabelText('Email')).toHaveValue('max@example.test')
  })

  it.each([
    ['owner_required', 403, 'Only an owner of the organization can do that.', 'Only an owner of the organization can do that.'],
    ['organization_read_only', 403, 'This organization’s members cannot be changed.', 'This organization’s members cannot be changed.'],
    ['internal_error', 500, 'Internal server error.', 'The change was not made. Internal server error.'],
  ])('shows %s in the form and keeps what was typed', async (code, status, message, shown) => {
    const { user } = await page({}, { [`POST ${MEMBERS}`]: refused(status, { code, message }) })

    await user.type(screen.getByLabelText('Full name'), 'New Person')
    await user.type(screen.getByLabelText('Email'), 'new@example.test')
    await user.click(screen.getByRole('button', { name: 'Add member' }))

    expect(await within(screen.getByRole('form', { name: 'Add a member' })).findByRole('alert')).toHaveTextContent(shown)
    expect(screen.getByLabelText('Full name')).toHaveValue('New Person')
    expect(screen.getByLabelText('Email')).toHaveValue('new@example.test')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('says the change was not made when the server cannot be reached', async () => {
    const { user } = await page()  // no handler for the POST: it fails like a dead network

    await user.type(screen.getByLabelText('Email'), 'new@example.test')
    await user.click(screen.getByRole('button', { name: 'Add member' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('The change was not made. Could not reach the server. Check your connection and try again.')
  })
})

// =====================================================================================================================
// A suspended organization
// =====================================================================================================================

describe('a suspended organization', () => {
  it('shows the members and the recent changes, and nothing to change them with', async () => {
    const { fetchMock } = await page({ role: 'owner', access_mode: 'read_only' })

    expect(main()).toHaveTextContent('This organization is suspended, so its members cannot be changed.')
    expect(within(membersTable()).getAllByRole('columnheader').map((header) => header.textContent)).toEqual(['Name', 'Email', 'Role', 'Account'])
    expect(within(membersTable()).getAllByRole('rowheader')).toHaveLength(4)
    expect(await screen.findByRole('table', { name: 'Recent changes' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 3, name: 'Add a member' })).not.toBeInTheDocument()
    expect(within(main()).queryAllByRole('button')).toEqual([])
    expect(within(main()).queryByRole('combobox')).not.toBeInTheDocument()
    expect(within(main()).queryByRole('textbox')).not.toBeInTheDocument()
    expect(requests(fetchMock).every((request) => request.startsWith('GET'))).toBe(true)
  })
})

// =====================================================================================================================
// Recent changes
// =====================================================================================================================

describe('recent changes', () => {
  it('says each change in words, when it happened in the reader’s own time, and who made it', async () => {
    await page()
    const table = await screen.findByRole('table', { name: 'Recent changes' })

    expect(within(table).getAllByRole('columnheader').map((header) => header.textContent)).toEqual(['When', 'Change', 'By'])
    expect(within(table).getAllByRole('row').slice(1).map((row) => within(row).getAllByRole('cell').concat(within(row).getAllByRole('rowheader')).map((cell) => cell.textContent))).toEqual([
      [formatLocalInstant('2026-10-06T15:00:00Z'), 'olive@example.test', 'Removed gone@example.test (was Manager)'],
      [formatLocalInstant('2026-10-05T18:50:00Z'), ALICE.email, 'Changed vera@example.test from Manager to Viewer'],
      [formatLocalInstant('2026-10-04T09:30:00Z'), 'Not recorded', 'Added max@example.test as Manager'],
    ])
    expect(table).not.toHaveTextContent(/membership_|_updated|T15:00:00Z/)
  })

  it('never shows the API’s own name for a change or a role it does not know', () => {
    const change = { occurred_at: '2026-10-06T15:00:00Z', member_email: 'a@example.test', actor_email: null, previous_role: null, role: null }

    expect(activitySentence({ ...change, event_type: 'membership_frozen_v2', role: 'viewer' })).toBe('Updated a@example.test')
    expect(activitySentence({ ...change, event_type: 'membership_added', role: 'auditor_v2' })).toBe('Added a@example.test as another role')
    expect(activitySentence({ ...change, event_type: 'membership_role_updated', previous_role: 'viewer', role: null })).toBe(
      'Changed a@example.test from Viewer to another role',
    )
    expect(activitySentence({ ...change, event_type: 'membership_removed', member_email: null })).toBe('Removed a member (was another role)')
  })

  it('says so when nothing has been recorded', async () => {
    await page({ activity: [] })

    expect(await screen.findByText('No member changes have been recorded yet.')).toBeInTheDocument()
    expect(screen.queryByRole('table', { name: 'Recent changes' })).not.toBeInTheDocument()
  })

  it('fails on its own, with a way to try again, and leaves the members as they are', async () => {
    await page({}, { [`GET ${MEMBERS}/activity`]: refused(500, { code: 'internal_error', message: 'Internal server error.' }) })

    const region = within(screen.getByRole('region', { name: 'Recent changes' }))
    expect(await region.findByRole('alert')).toHaveTextContent('Internal server error.')
    expect(region.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
    expect(membersTable()).toBeInTheDocument()
  })
})

// =====================================================================================================================
// Structure
// =====================================================================================================================

describe('the structure of the page', () => {
  it('names every table, region and control, and uses no tab order of its own', async () => {
    const { user } = await page({ role: 'owner' })
    await screen.findByRole('table', { name: 'Recent changes' })

    expect(screen.getAllByRole('region').map((region) => region.getAttribute('aria-labelledby'))).toEqual([
      'members-heading',
      'add-member-heading',
      'member-activity-heading',
    ])
    for (const control of within(main()).getAllByRole('button').concat(within(main()).getAllByRole('combobox'), within(main()).getAllByRole('textbox'))) {
      expect(control).toHaveAccessibleName()
    }
    expect(Array.from(main().querySelectorAll('[tabindex]')).map((element) => `${element.tagName} ${element.getAttribute('tabindex')}`)).toEqual(['H2 -1', 'H3 -1'])
    expect(main().querySelectorAll('[disabled], [aria-live], [role="button"], dialog')).toHaveLength(0)

    // Every control is reached with Tab alone, in reading order: after the strip of sections, the first row.
    within(screen.getByRole('navigation', { name: 'Settings' })).getByRole('link', { name: 'Efficiency' }).focus()
    await user.tab()
    expect(screen.getByRole('button', { name: `Change role for ${ALICE.email}` })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(screen.getByLabelText(`Role for ${ALICE.email}`)).toHaveFocus()
  })
})
