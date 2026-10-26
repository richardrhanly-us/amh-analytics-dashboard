import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import {
  ALICE,
  deferred,
  jsonResponse,
  noContent,
  requestedUrls,
  serveApi,
  type ApiRoutes,
  type FetchMock,
} from '../../../src/test/http.ts'
import { axeViolations } from '../../../src/test/axe.ts'
import { AdminApp } from '../AdminApp.tsx'

const SESSION = { 'GET /api/admin/session': () => jsonResponse(200, ALICE) }
const TIMEZONE = 'America/Chicago'

const NBPL = {
  slug: 'nbpl',
  name: 'New Braunfels Public Library',
  status: 'active',
  primary_branch: { slug: 'main', name: 'Main Branch', status: 'active' },
  subscription: { plan_code: 'enterprise', plan_name: 'Enterprise', status: 'trial' },
  sorter_count: 1,
  primary_sorter: { onboarding_status: 'ready', pipeline: { state: 'degraded', last_reported_at: '2026-10-14T15:30:00Z' } },
}
const WAITING = {
  slug: 'waiting',
  name: 'Waiting Library',
  status: 'trial',
  primary_branch: { slug: 'central', name: 'Central', status: 'active' },
  subscription: { plan_code: 'starter', plan_name: 'Starter', status: 'active' },
  sorter_count: 1,
  primary_sorter: { onboarding_status: 'pending', pipeline: { state: 'unknown', last_reported_at: null } },
}
const BARE = {
  slug: 'bare',
  name: 'Bare Library',
  status: 'cancelled',
  primary_branch: null,
  subscription: null,
  sorter_count: 0,
  primary_sorter: null,
}
const LIST = { timezone: TIMEZONE, libraries: [BARE, NBPL, WAITING] }

const NBPL_DETAIL = {
  timezone: TIMEZONE,
  slug: 'nbpl',
  name: 'New Braunfels Public Library',
  status: 'active',
  primary_branch: { slug: 'main', name: 'Main Branch', status: 'active' },
  operational_identity: 'assigned',
  subscription: { plan_code: 'enterprise', plan_name: 'Enterprise', status: 'trial' },
  has_active_owner: true,
  sorters: [
    {
      slug: 'main',
      name: 'Main AMH',
      host_branch: { slug: 'main', name: 'Main Branch' },
      status: 'active',
      collector_count: 1,
      onboarding_status: 'ready',
      pipeline: { state: 'degraded', last_reported_at: '2026-10-14T15:30:00Z' },
    },
    {
      slug: 'annex',
      name: 'Annex AMH',
      host_branch: { slug: 'annex', name: 'Annex' },
      status: 'provisioning',
      collector_count: 0,
      onboarding_status: 'pending',
      pipeline: { state: 'unknown', last_reported_at: null },
    },
  ],
}

// What an overview must never say: a later item's diagnostics or controls.
const DIAGNOSTICS = /error class|last_error|outbox|quarantin|hostname|collector version|heartbeat|schedule|attempt|enrollment|installer/i

function renderAt(path: string, routes: ApiRoutes) {
  const fetchMock = serveApi({ ...SESSION, ...routes })
  render(
    <MemoryRouter initialEntries={[path]}>
      <AdminApp />
    </MemoryRouter>,
  )
  return fetchMock
}

function rowOf(name: string): HTMLElement {
  return screen.getByRole('rowheader', { name }).closest('tr') as HTMLElement
}

function cells(row: HTMLElement): string[] {
  return within(row)
    .getAllByRole('cell')
    .map((cell) => cell.textContent ?? '')
}

function assertReadOnly() {
  expect(document.querySelector('form')).toBeNull()
  expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual(['Log out'])
}

describe('Libraries list', () => {
  it('lands on the libraries from / and lists each with its status, plan, sorters and primary sorter', async () => {
    const fetchMock = renderAt('/', { 'GET /api/admin/libraries': () => jsonResponse(200, LIST) })

    expect(await screen.findByRole('rowheader', { name: 'New Braunfels Public Library' })).toBeInTheDocument()
    expect(screen.getAllByRole('columnheader').map((header) => header.textContent)).toEqual([
      'Library',
      'Status',
      'Plan',
      'Sorters',
      'Primary sorter',
      'Last reported',
    ])
    expect(cells(rowOf('New Braunfels Public Library'))).toEqual([
      'Active',
      'Enterprise · Trial',
      '1',
      'Degraded',
      'Oct 14, 2026, 10:30 AM CDT',
    ])
    expect(cells(rowOf('Waiting Library'))).toEqual(['Trial', 'Starter', '1', 'Waiting for first data', 'Not reported'])
    expect(cells(rowOf('Bare Library'))).toEqual(['Cancelled', 'No subscription', '0', 'No sorter', 'Not reported'])
    expect(requestedUrls(fetchMock)).toEqual(['/api/admin/session', '/api/admin/libraries'])
    assertReadOnly()
  })

  it('says when no library has been provisioned', async () => {
    renderAt('/libraries', { 'GET /api/admin/libraries': () => jsonResponse(200, { timezone: TIMEZONE, libraries: [] }) })

    expect(await screen.findByText('No libraries have been provisioned yet.')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('says it is loading, then shows a fixed error with a retry that works', async () => {
    const first = deferred<Response>()
    let calls = 0
    renderAt('/libraries', {
      'GET /api/admin/libraries': () => (++calls === 1 ? first.promise : jsonResponse(200, LIST)),
    })
    const person = userEvent.setup()

    expect(await screen.findByText('Loading libraries…')).toBeInTheDocument()
    first.resolve(jsonResponse(404, { code: 'not_found', message: 'Not found.' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('The libraries could not be loaded. Please try again.')
    await person.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByRole('rowheader', { name: 'Bare Library' })).toBeInTheDocument()
  })

  it('treats a list that is not the expected shape as a failure', async () => {
    renderAt('/libraries', {
      'GET /api/admin/libraries': () => jsonResponse(200, { timezone: TIMEZONE, libraries: [{ ...NBPL, sorter_count: '1' }] }),
    })

    expect(await screen.findByRole('alert')).toHaveTextContent('The libraries could not be loaded.')
  })

  it('opens a library overview from the list', async () => {
    const fetchMock = renderAt('/libraries', {
      'GET /api/admin/libraries': () => jsonResponse(200, LIST),
      'GET /api/admin/libraries/nbpl': () => jsonResponse(200, NBPL_DETAIL),
    })
    const person = userEvent.setup()

    await person.click(await screen.findByRole('link', { name: 'New Braunfels Public Library' }))

    expect(await screen.findByRole('heading', { level: 2, name: 'New Braunfels Public Library' })).toBeInTheDocument()
    expect(requestedUrls(fetchMock)).toContain('/api/admin/libraries/nbpl')
  })
})

describe('Library overview', () => {
  it('shows the library, subscription, primary branch, identity and every sorter with its own state', async () => {
    renderAt('/libraries/nbpl', { 'GET /api/admin/libraries/nbpl': () => jsonResponse(200, NBPL_DETAIL) })

    expect(await screen.findByRole('heading', { level: 2, name: 'New Braunfels Public Library' })).toBeInTheDocument()
    expect(screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent)).toEqual([
      'Library',
      'Subscription',
      'Primary branch',
      'Operational identity',
      'First owner',
      'Collector',
      'Sorters',
    ])
    expect(screen.getByRole('link', { name: 'Libraries' })).toHaveAttribute('href', '/libraries')
    expect(screen.getByRole('link', { name: 'Collector configuration' })).toHaveAttribute('href', '/libraries/nbpl/collector')
    // The lifecycle actions are on their own page; the overview only links to it (and assertReadOnly holds).
    expect(screen.getByRole('link', { name: 'Library lifecycle' })).toHaveAttribute('href', '/libraries/nbpl/lifecycle')
    expect(screen.getByText('Assigned')).toBeInTheDocument()
    expect(screen.getByText('Enterprise')).toBeInTheDocument()
    expect(cells(rowOf('Main AMH'))).toEqual(['Main Branch', 'Active', '1', 'Ready', 'Degraded', 'Oct 14, 2026, 10:30 AM CDT'])
    expect(cells(rowOf('Annex AMH'))).toEqual(['Annex', 'Provisioning', '0', 'Waiting for first data', 'Nothing reported', 'Not reported'])
    expect(document.body.textContent).not.toMatch(DIAGNOSTICS)
    assertReadOnly()
  })

  it('says in words when a library has no subscription, no primary branch, no identity and no sorter', async () => {
    renderAt('/libraries/bare', {
      'GET /api/admin/libraries/bare': () =>
        jsonResponse(200, {
          ...NBPL_DETAIL,
          slug: 'bare',
          name: 'Bare Library',
          status: 'cancelled',
          primary_branch: null,
          operational_identity: 'missing',
          subscription: null,
          sorters: [],
        }),
    })

    expect(await screen.findByRole('heading', { level: 2, name: 'Bare Library' })).toBeInTheDocument()
    expect(screen.getByText('No subscription.')).toBeInTheDocument()
    expect(screen.getByText('No primary branch.')).toBeInTheDocument()
    expect(screen.getByText('Not assigned')).toBeInTheDocument()
    expect(screen.getByText('No sorters are registered for this library.')).toBeInTheDocument()
  })

  it('notes a primary branch that is not active', async () => {
    renderAt('/libraries/nbpl', {
      'GET /api/admin/libraries/nbpl': () =>
        jsonResponse(200, { ...NBPL_DETAIL, primary_branch: { slug: 'main', name: 'Main Branch', status: 'inactive' } }),
    })

    expect(await screen.findByRole('note')).toHaveTextContent('The primary branch is not active')
  })

  it('says a library that does not exist was not found, with the way back', async () => {
    renderAt('/libraries/nobody', {
      'GET /api/admin/libraries/nobody': () => jsonResponse(404, { code: 'library_not_found', message: 'Library not found.' }),
    })

    expect(await screen.findByRole('heading', { level: 2, name: 'Library not found' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Libraries' })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows a fixed error with a retry for any other failure', async () => {
    renderAt('/libraries/nbpl', {
      'GET /api/admin/libraries/nbpl': () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' }),
    })

    expect(await screen.findByRole('alert', {}, { timeout: 8000 })).toHaveTextContent('This library could not be loaded.')
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  it('shows a page that does not exist as not found', async () => {
    renderAt('/nowhere', {})

    expect(await screen.findByRole('heading', { level: 2, name: 'Page not found' })).toBeInTheDocument()
  })
})

/** The breadcrumb's steps as read, and the links among them. */
function breadcrumb() {
  const nav = screen.getByRole('navigation', { name: 'Breadcrumb' })
  return {
    nav,
    steps: within(nav).getAllByRole('listitem').map((step) => step.textContent),
    links: within(nav).queryAllByRole('link').map((link) => [link.textContent, link.getAttribute('href')]),
  }
}

describe('Breadcrumb', () => {
  it('gives the library overview a named navigation landmark: Libraries, then the library as the current page', async () => {
    const fetchMock = renderAt('/libraries/nbpl', { 'GET /api/admin/libraries/nbpl': () => jsonResponse(200, NBPL_DETAIL) })
    const heading = await screen.findByRole('heading', { level: 2, name: 'New Braunfels Public Library' })

    const { nav, steps, links } = breadcrumb()
    expect(screen.getByRole('main')).toContainElement(nav)
    expect(nav.querySelector('ol')).not.toBeNull()
    expect(steps).toEqual(['Libraries', 'New Braunfels Public Library'])
    expect(links).toEqual([['Libraries', '/libraries']])
    // The current page is marked, and is not a link to itself.
    const current = within(nav).getByText('New Braunfels Public Library')
    expect(current).toHaveAttribute('aria-current', 'page')
    expect(current.closest('a')).toBeNull()
    // It comes before the heading, and is built from the overview's own answer: no request of its own.
    expect(nav.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(requestedUrls(fetchMock)).toEqual(['/api/admin/session', '/api/admin/libraries/nbpl'])
  })

  it('names the library generically while it loads, then by name, without moving focus again', async () => {
    const answer = deferred<Response>()
    renderAt('/libraries', {
      'GET /api/admin/libraries': () => jsonResponse(200, LIST),
      'GET /api/admin/libraries/nbpl': () => answer.promise,
    })
    const person = userEvent.setup()

    await person.click(await screen.findByRole('link', { name: 'New Braunfels Public Library' }))
    const loading = await screen.findByRole('heading', { level: 2, name: 'Library' })
    await waitFor(() => expect(loading).toHaveFocus())
    expect(breadcrumb().steps).toEqual(['Libraries', 'Library'])

    answer.resolve(jsonResponse(200, NBPL_DETAIL))
    const named = await screen.findByRole('heading', { level: 2, name: 'New Braunfels Public Library' })
    expect(breadcrumb().steps).toEqual(['Libraries', 'New Braunfels Public Library'])
    expect(named).toHaveFocus()
  })

  it('leads back to Libraries from a library that does not exist', async () => {
    renderAt('/libraries/nobody', {
      'GET /api/admin/libraries/nobody': () => jsonResponse(404, { code: 'library_not_found', message: 'Library not found.' }),
    })
    await screen.findByRole('heading', { level: 2, name: 'Library not found' })
    expect(breadcrumb()).toMatchObject({ steps: ['Libraries', 'Library not found'], links: [['Libraries', '/libraries']] })
  })

  it('leads back to Libraries from a page that does not exist', async () => {
    renderAt('/nowhere', {})
    await screen.findByRole('heading', { level: 2, name: 'Page not found' })
    expect(breadcrumb()).toMatchObject({ steps: ['Libraries', 'Page not found'], links: [['Libraries', '/libraries']] })
  })

  it('has none on the libraries list, which is the top of the trail', async () => {
    renderAt('/libraries', { 'GET /api/admin/libraries': () => jsonResponse(200, LIST) })
    await screen.findByRole('rowheader', { name: 'New Braunfels Public Library' })
    expect(screen.queryByRole('navigation', { name: 'Breadcrumb' })).not.toBeInTheDocument()
  })
})

describe('First owner', () => {
  const UNOWNED = { ...NBPL_DETAIL, slug: 'fresh', name: 'Fresh Library', status: 'trial', has_active_owner: false, sorters: [] }
  const FIRST_OWNER = 'POST /api/admin/libraries/fresh/first-owner'

  function posted(mock: FetchMock): { url: string; body: unknown }[] {
    return mock.mock.calls
      .filter(([, init]) => init?.method === 'POST')
      .map(([url, init]) => ({ url: String(url), body: JSON.parse(String(init?.body)) as unknown }))
  }

  async function ownerSection() {
    await screen.findByRole('heading', { level: 2, name: 'Fresh Library' })
    return screen.getByRole('region', { name: 'First owner' })
  }

  async function fill(person: ReturnType<typeof userEvent.setup>, email: string, again: string) {
    await person.type(screen.getByLabelText("Owner's email address"), email)
    await person.type(screen.getByLabelText('Type the email address again'), again)
    await person.type(screen.getByLabelText("Owner's name (optional)"), 'Olive Owner')
  }

  it('asks for the address twice, confirms the library and the address, then assigns and says how to sign in', async () => {
    let owned = false
    const fetchMock = renderAt('/libraries/fresh', {
      'GET /api/admin/libraries/fresh': () => jsonResponse(200, { ...UNOWNED, has_active_owner: owned }),
      [FIRST_OWNER]: () => {
        owned = true
        return noContent()
      },
    })
    const person = userEvent.setup()
    const section = await ownerSection()
    expect(within(section).getByText('No one can sign in to this library yet. Assign its first owner on this page.')).toBeInTheDocument()
    expect(document.querySelectorAll('input[type="password"]')).toHaveLength(0)

    await fill(person, 'Olive.Owner@Example.test', 'olive.owner@example.test ')
    await person.click(within(section).getByRole('button', { name: 'Review' }))

    const heading = within(section).getByRole('heading', { name: 'Confirm the first owner' })
    expect(heading).toHaveFocus()
    expect(section.textContent).toMatch(/Make olive\.owner@example\.test the first owner of Fresh Library\?/)
    expect(posted(fetchMock)).toEqual([])

    await person.click(within(section).getByRole('button', { name: 'Assign first owner' }))

    expect(await within(section).findByText('This library has an owner. Its owners add everyone else from the Members page in the SortView app.')).toBeInTheDocument()
    expect(within(section).getByRole('status')).toHaveTextContent(
      /^First owner assigned\. Ask olive\.owner@example\.test to open the SortView app, choose .Forgot password\?. on the sign-in page, and set a password with this address\.$/,
    )
    expect(posted(fetchMock)).toEqual([
      {
        url: '/api/admin/libraries/fresh/first-owner',
        body: { email: 'Olive.Owner@Example.test', email_confirmation: 'olive.owner@example.test', full_name: 'Olive Owner' },
      },
    ])
    expect(within(section).queryByRole('form')).toBeNull()
  })

  it('sends nothing while the two addresses differ, or there is no address', async () => {
    const fetchMock = renderAt('/libraries/fresh', { 'GET /api/admin/libraries/fresh': () => jsonResponse(200, UNOWNED) })
    const person = userEvent.setup()
    const section = await ownerSection()

    await person.click(within(section).getByRole('button', { name: 'Review' }))
    expect(screen.getByLabelText("Owner's email address")).toHaveAccessibleDescription("Enter the owner's email address.")

    await fill(person, 'olive@example.test', 'oliver@example.test')
    await person.click(within(section).getByRole('button', { name: 'Review' }))
    expect(screen.getByLabelText('Type the email address again')).toHaveAccessibleDescription('The two email addresses are not the same.')
    expect(within(section).queryByRole('heading', { name: 'Confirm the first owner' })).toBeNull()
    expect(posted(fetchMock)).toEqual([])
  })

  it('goes back to the details, as they were, from the confirmation', async () => {
    const fetchMock = renderAt('/libraries/fresh', { 'GET /api/admin/libraries/fresh': () => jsonResponse(200, UNOWNED) })
    const person = userEvent.setup()
    const section = await ownerSection()
    await fill(person, 'olive@example.test', 'olive@example.test')
    await person.click(within(section).getByRole('button', { name: 'Review' }))

    await person.click(within(section).getByRole('button', { name: 'Change details' }))

    expect(screen.getByLabelText("Owner's email address")).toHaveValue('olive@example.test')
    expect(posted(fetchMock)).toEqual([])
  })

  it('says nothing was changed when the server refuses, in its own words', async () => {
    renderAt('/libraries/fresh', {
      'GET /api/admin/libraries/fresh': () => jsonResponse(200, UNOWNED),
      [FIRST_OWNER]: () => jsonResponse(409, { code: 'owner_exists', message: 'This library already has an owner. Its owners add everyone else.' }),
    })
    const person = userEvent.setup()
    const section = await ownerSection()
    await fill(person, 'olive@example.test', 'olive@example.test')
    await person.click(within(section).getByRole('button', { name: 'Review' }))

    await person.click(within(section).getByRole('button', { name: 'Assign first owner' }))

    expect(await within(section).findByRole('alert')).toHaveTextContent(
      'Nothing was changed. This library already has an owner. Its owners add everyone else.',
    )
    expect(screen.getByLabelText("Owner's email address")).toBeInTheDocument()
    expect(within(section).queryByRole('status')).toBeNull()
  })

  it('offers nothing for a library that already has an owner, or cannot be changed', async () => {
    renderAt('/libraries/nbpl', { 'GET /api/admin/libraries/nbpl': () => jsonResponse(200, NBPL_DETAIL) })
    await screen.findByRole('heading', { level: 2, name: 'New Braunfels Public Library' })
    expect(screen.getByRole('region', { name: 'First owner' })).toHaveTextContent('This library has an owner.')
    assertReadOnly()
  })

  it('explains that a suspended library with no owner cannot be given one', async () => {
    renderAt('/libraries/fresh', { 'GET /api/admin/libraries/fresh': () => jsonResponse(200, { ...UNOWNED, status: 'suspended' }) })
    const section = await ownerSection()
    expect(section).toHaveTextContent('No one can sign in to this library. An owner can be assigned only while the library is active.')
    expect(within(section).queryByRole('textbox')).toBeNull()
  })

  it('treats an overview without the owner flag as a failure', async () => {
    const withoutFlag: Record<string, unknown> = { ...UNOWNED }
    delete withoutFlag.has_active_owner
    renderAt('/libraries/fresh', { 'GET /api/admin/libraries/fresh': () => jsonResponse(200, withoutFlag) })
    expect(await screen.findByRole('alert')).toHaveTextContent('This library could not be loaded. Please try again.')
  })

  it('axe finds no violation on the details or the confirmation', async () => {
    renderAt('/libraries/fresh', { 'GET /api/admin/libraries/fresh': () => jsonResponse(200, UNOWNED) })
    const person = userEvent.setup()
    const section = await ownerSection()
    expect(await axeViolations()).toEqual([])

    await fill(person, 'olive@example.test', 'olive@example.test')
    await person.click(within(section).getByRole('button', { name: 'Review' }))
    expect(await axeViolations()).toEqual([])
  })
})

describe('accessibility', () => {
  it('axe finds no violation on the libraries list', async () => {
    renderAt('/', { 'GET /api/admin/libraries': () => jsonResponse(200, LIST) })
    await screen.findByRole('rowheader', { name: 'New Braunfels Public Library' })
    expect(await axeViolations()).toEqual([])
  })

  it('axe finds no violation on a library overview', async () => {
    renderAt('/libraries/nbpl', { 'GET /api/admin/libraries/nbpl': () => jsonResponse(200, NBPL_DETAIL) })
    await screen.findByRole('heading', { level: 2, name: 'New Braunfels Public Library' })
    expect(await axeViolations()).toEqual([])
  })
  it('names the view in the browser tab, and leaves focus alone on the view the app opened on', async () => {
    renderAt('/', { 'GET /api/admin/libraries': () => jsonResponse(200, LIST) })
    const heading = await screen.findByRole('heading', { level: 2, name: 'Libraries' })

    expect(document.title).toBe('Libraries – SortView Administration')
    expect(heading).not.toHaveFocus()
  })

  it('moves focus to the heading of the view a link leads to, and names it in the browser tab', async () => {
    renderAt('/', {
      'GET /api/admin/libraries': () => jsonResponse(200, LIST),
      'GET /api/admin/libraries/nbpl': () => jsonResponse(200, NBPL_DETAIL),
    })
    const person = userEvent.setup()

    await person.click(await screen.findByRole('link', { name: 'New Braunfels Public Library' }))
    const overview = await screen.findByRole('heading', { level: 2, name: 'New Braunfels Public Library' })
    await waitFor(() => expect(overview).toHaveFocus())
    expect(document.title).toBe('New Braunfels Public Library – SortView Administration')
    expect(await axeViolations()).toEqual([])

    await person.click(screen.getByRole('link', { name: 'Libraries' }))
    const list = await screen.findByRole('heading', { level: 2, name: 'Libraries' })
    await waitFor(() => expect(list).toHaveFocus())
    expect(document.title).toBe('Libraries – SortView Administration')
  })
})
