import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import {
  ALICE,
  type ApiRoutes,
  callOf,
  type FetchMock,
  jsonResponse,
  NORTHBRIDGE,
  NORTHBRIDGE_DETAIL,
  serveApi,
} from '../test/http.ts'
import { renderApp } from '../test/render.tsx'
import { canManageOrganization } from './settingsText.ts'

const ORG = '/api/organizations/northbridge'
const EFFICIENCY = `${ORG}/settings/efficiency`
const AREA = '/organizations/northbridge/settings'
const GENERAL = `${AREA}/general`
const BRANCHES = `${AREA}/branches`
const EFFICIENCY_PAGE = `${AREA}/efficiency`
const MEMBERS_PAGE = '/organizations/northbridge/members'
// No pause between keystrokes: nothing here depends on one.
const INSTANT = { delay: null }

const NOT_FOR_YOU = "Settings are managed by this organization's owners and admins."
const SECTIONS = ['General', 'Users & Access', 'Branches & Sorters', 'Routing', 'Efficiency']

interface World {
  role?: string
  access_mode?: string
  detail?: Record<string, unknown>
}

/** Alice signed in, and Northbridge with the role and state given. Its Efficiency defaults are one rate, set. */
function serve(world: World = {}, overrides: ApiRoutes = {}): FetchMock {
  const { role = 'admin', access_mode = 'full', detail = {} } = world
  return serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    'GET /api/organizations': () => jsonResponse(200, [{ ...NORTHBRIDGE, role, access_mode }]),
    [`GET ${ORG}`]: () => jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role, access_mode, ...detail }),
    [`GET ${EFFICIENCY}`]: () => jsonResponse(200, { efficiency: { labor_rate: '17.56', manual_items_per_hour: null } }),
    [`GET ${ORG}/members`]: () => jsonResponse(200, { members: [] }),
    [`GET ${ORG}/members/activity`]: () => jsonResponse(200, { activity: [] }),
    ...overrides,
  })
}

const main = () => screen.getByRole('main')
const address = () => screen.getByTestId('address').textContent
const requests = (fetchMock: FetchMock) => fetchMock.mock.calls.map(([url, init]) => `${init?.method ?? 'GET'} ${String(url)}`)
const strip = () => within(screen.getByRole('navigation', { name: 'Settings' }))
const crumbs = () => within(screen.getByRole('navigation', { name: 'Breadcrumb' })).getAllByRole('listitem').map((item) => item.textContent)
const facts = () =>
  Object.fromEntries(
    Array.from(main().querySelectorAll('dl > div')).map((fact) => [fact.querySelector('dt')?.textContent, fact.querySelector('dd')?.textContent]),
  )
/** Everything a person could act on or type into, anywhere on the page below the header. */
const controls = () => Array.from(main().querySelectorAll('button, input, select, textarea, form'))

async function page(path: string, heading: string, world: World = {}, overrides: ApiRoutes = {}) {
  const fetchMock = serve(world, overrides)
  renderApp(path)
  await screen.findByRole('heading', { level: 2, name: heading })
  return { fetchMock, user: userEvent.setup(INSTANT) }
}

// =====================================================================================================================
// Getting there, and who it is for
// =====================================================================================================================

describe('the way in', () => {
  it.each(['owner', 'admin'])('is one link on the organization page for an %s, and opens on General', async (role) => {
    serve({ role })
    const user = userEvent.setup(INSTANT)
    renderApp('/organizations/northbridge')

    const link = await screen.findByRole('link', { name: 'Settings' })
    expect(link).toHaveAttribute('href', AREA)
    // The members page is reached through it now, not from here.
    expect(within(main()).getAllByRole('link').map((item) => item.textContent).slice(0, 3)).toEqual(['Organizations', 'Organization Reports', 'Settings'])
    await user.click(link)

    await screen.findByRole('heading', { level: 2, name: 'General' })
    expect(address()).toBe(GENERAL)
    expect(document.title).toBe('General – SortView')
    expect(crumbs()).toEqual(['Organizations', 'Northbridge Library', 'Settings', 'General'])
  })

  it.each(['manager', 'viewer', 'something-new'])('is not offered to a %s', async (role) => {
    serve({ role })
    renderApp('/organizations/northbridge')
    await screen.findByRole('link', { name: 'Organization Reports' })

    expect(screen.queryByRole('link', { name: 'Settings' })).not.toBeInTheDocument()
    expect(within(screen.getByRole('navigation', { name: 'Account' })).queryByText(/Settings/)).not.toBeInTheDocument()
  })

  it('is offered by the role alone, to an owner and an admin and nobody else', () => {
    expect(['owner', 'admin', 'manager', 'viewer', '', 'Owner', 'superuser'].filter(canManageOrganization)).toEqual(['owner', 'admin'])
  })

  it.each([
    ['manager', AREA],
    ['manager', GENERAL],
    ['viewer', BRANCHES],
    ['viewer', EFFICIENCY_PAGE],
  ])('tells a %s who opens %s that it is not theirs, and shows and asks nothing else', async (role, path) => {
    const fetchMock = serve({ role })
    renderApp(path)

    expect(await screen.findByRole('note')).toHaveTextContent(NOT_FOR_YOU)
    expect(screen.getByRole('heading', { level: 2, name: 'Settings' })).toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: 'Settings' })).not.toBeInTheDocument()
    expect(within(main()).getAllByRole('link').map((item) => item.textContent)).toEqual(['Organizations', 'Northbridge Library'])
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(main().querySelector('dl')).toBeNull()
    expect(controls()).toEqual([])
    expect(main()).not.toHaveTextContent(/Central Branch|Organization defaults|Your role/)
    expect(requests(fetchMock)).toEqual(['GET /api/auth/session', `GET ${ORG}`])
  })
})

describe('the strip of sections', () => {
  it('lists the five sections that exist, each a link to its own address, and no other', async () => {
    await page(GENERAL, 'General')

    expect(strip().getAllByRole('link').map((item) => [item.textContent, item.getAttribute('href')])).toEqual([
      ['General', GENERAL],
      ['Users & Access', MEMBERS_PAGE],
      ['Branches & Sorters', BRANCHES],
      ['Routing', `${AREA}/routing`],
      ['Efficiency', EFFICIENCY_PAGE],
    ])
    expect(strip().queryByText(/Internal|Workflow|Billing|Plan/i)).not.toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Settings' }).querySelectorAll('button, [role="tab"], [tabindex]')).toHaveLength(0)
  })

  it('goes from section to section, marks the one being shown, and names each in the heading and the tab', async () => {
    const { user } = await page(AREA, 'General')
    const current = () => strip().getAllByRole('link').filter((item) => item.getAttribute('aria-current') === 'page').map((item) => item.textContent)
    expect(address()).toBe(GENERAL)
    expect(current()).toEqual(['General'])

    for (const [section, path] of [
      ['Branches & Sorters', BRANCHES],
      ['Efficiency', EFFICIENCY_PAGE],
      ['Users & Access', MEMBERS_PAGE],
      ['General', GENERAL],
    ]) {
      await user.click(strip().getByRole('link', { name: section }))

      expect(await screen.findByRole('heading', { level: 2, name: section })).toHaveFocus()
      expect(address()).toBe(path)
      expect(current()).toEqual([section])
      expect(document.title).toBe(`${section} – SortView`)
      expect(crumbs()).toEqual(['Organizations', 'Northbridge Library', 'Settings', section])
      expect(strip().getAllByRole('link').map((item) => item.textContent)).toEqual(SECTIONS)
    }
  })

  it('leads back up: the breadcrumb goes to the first section and to the organization', async () => {
    const { user } = await page(BRANCHES, 'Branches & Sorters')
    // Asked for afresh each time: a new page draws a new breadcrumb.
    const breadcrumb = () => within(screen.getByRole('navigation', { name: 'Breadcrumb' }))

    await user.click(breadcrumb().getByRole('link', { name: 'Settings' }))
    await screen.findByRole('heading', { level: 2, name: 'General' })
    expect(address()).toBe(GENERAL)

    await user.click(breadcrumb().getByRole('link', { name: 'Northbridge Library' }))
    await screen.findByRole('heading', { level: 2, name: 'Northbridge Library' })
    expect(address()).toBe('/organizations/northbridge')
  })

  it('has no page for a section that does not exist', async () => {
    serve()
    renderApp(`${AREA}/workflow`)

    expect(await screen.findByRole('heading', { level: 2, name: 'Page not found' })).toBeInTheDocument()
  })
})

// =====================================================================================================================
// General
// =====================================================================================================================

describe('General', () => {
  it.each([
    ['owner', 'Owner'],
    ['admin', 'Admin'],
  ])('shows the name, that it is active, and that the reader is an %s -- and those three things only', async (role, label) => {
    const { fetchMock } = await page(GENERAL, 'General', { role })

    expect(facts()).toEqual({ 'Organization name': 'Northbridge Library', Status: 'Active', 'Your role': label })
    expect(screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent)).toEqual(['Organization'])
    // Not its address name, its plan, what the plan allows, or anything else the API said of it.
    expect(main()).not.toHaveTextContent(/northbridge(?! library)|Standard|standard|transits_tab|branch_count|Central|East Side|Westside/)
    expect(main()).not.toHaveTextContent(/time ?zone|Chicago|contact|notes/i)
    expect(requests(fetchMock)).toEqual(['GET /api/auth/session', `GET ${ORG}`])
  })

  it('is to be read: there is nothing to type in, press or save', async () => {
    await page(GENERAL, 'General', { role: 'owner' })

    expect(controls()).toEqual([])
    expect(within(main()).queryByRole('link', { name: /edit|change|rename/i })).not.toBeInTheDocument()
  })

  it('says a suspended organization is suspended, and is still readable', async () => {
    await page(GENERAL, 'General', { access_mode: 'read_only' })

    expect(facts()).toEqual({ 'Organization name': 'Northbridge Library', Status: 'Suspended', 'Your role': 'Admin' })
    expect(strip().getAllByRole('link').map((item) => item.textContent)).toEqual(SECTIONS)
    expect(main()).toHaveTextContent('This organization’s account is currently suspended.')
    expect(controls()).toEqual([])
  })

  it('shows no word of the API’s own for a role this app does not know', async () => {
    // Offered by role, so only an owner or admin is here; the label is still never the raw value.
    await page(GENERAL, 'General', { role: 'owner' })

    expect(facts()['Your role']).toBe('Owner')
    expect(main()).not.toHaveTextContent(/\bowner\b/)
  })
})

// =====================================================================================================================
// Branches & Sorters
// =====================================================================================================================

const rowsOf = (table: HTMLElement) =>
  within(table).getAllByRole('row').slice(1).map((row) => [within(row).getByRole('rowheader').textContent, ...within(row).getAllByRole('cell').map((cell) => cell.textContent)])

describe('Branches & Sorters', () => {
  it('lists each branch once, with the sorting machine the API says is there, or that there is none', async () => {
    await page(BRANCHES, 'Branches & Sorters')
    const table = screen.getByRole('table', { name: 'Branches and sorting machines' })

    expect(within(table).getAllByRole('columnheader').map((header) => header.textContent)).toEqual(['Branch', 'Sorting machine', 'Status', 'Collectors'])
    expect(rowsOf(table)).toEqual([
      ['Central Branch · Primary', 'Central Library AMH', 'Active', '1'],
      ['East Side Branch', 'East Side AMH', 'Active', '1'],
      // A place items are routed to, with no machine of its own: nothing is made up for it.
      ['Westside', 'No sorting machine', 'Not applicable', 'Not applicable'],
    ])
  })

  it('links each machine to its dashboard, and links nothing else in the table', async () => {
    await page(BRANCHES, 'Branches & Sorters')
    const table = screen.getByRole('table', { name: 'Branches and sorting machines' })

    expect(within(table).getAllByRole('link').map((item) => [item.textContent, item.getAttribute('href')])).toEqual([
      ['Central Library AMH', '/organizations/northbridge/sorters/central'],
      ['East Side AMH', '/organizations/northbridge/sorters/east-side'],
    ])
  })

  it('says a machine’s state in the words used everywhere, and that several collectors’ figures are combined', async () => {
    const sorters = [
      { slug: 'central', name: 'Central Library AMH', host_branch: { slug: 'central', name: 'Central Branch' }, status: 'provisioning', collector_count: 0 },
      { slug: 'east-side', name: 'East Side AMH', host_branch: { slug: 'east-side', name: 'East Side Branch' }, status: 'inactive', collector_count: 3 },
    ]
    await page(BRANCHES, 'Branches & Sorters', { detail: { sorters } })

    expect(rowsOf(screen.getByRole('table', { name: 'Branches and sorting machines' })).slice(0, 2)).toEqual([
      ['Central Branch · Primary', 'Central Library AMH', 'Being set up', '0'],
      ['East Side Branch', 'East Side AMH', 'Inactive', '3 — their figures are combined'],
    ])
  })

  it('matches a machine to a branch by the machine’s own host branch, and drops no machine the API listed', async () => {
    const sorters = [
      // Named like one branch, installed at another: it belongs where the API says it is.
      { slug: 'east-side', name: 'Central Annex AMH', host_branch: { slug: 'east-side', name: 'East Side Branch' }, status: 'active', collector_count: 1 },
      // A machine whose branch is not in the branch list at all.
      { slug: 'depot', name: 'Depot AMH', host_branch: { slug: 'depot', name: 'Depot' }, status: 'active', collector_count: 1 },
    ]
    await page(BRANCHES, 'Branches & Sorters', { detail: { sorters } })

    expect(rowsOf(screen.getByRole('table', { name: 'Branches and sorting machines' }))).toEqual([
      ['Central Branch · Primary', 'No sorting machine', 'Not applicable', 'Not applicable'],
      ['East Side Branch', 'Central Annex AMH', 'Active', '1'],
      ['Westside', 'No sorting machine', 'Not applicable', 'Not applicable'],
      ['Depot', 'Depot AMH', 'Active', '1'],
    ])
  })

  it('shows names and never what a branch or a machine is called in an address, and nothing a machine keeps to itself', async () => {
    const detail = {
      branches: [{ slug: 'br-7f3a', name: 'Harbor Branch', is_primary: true, id: 9041, operational_branch_id: 77 }],
      sorters: [
        {
          slug: 'br-7f3a',
          name: 'Harbor AMH',
          host_branch: { slug: 'br-7f3a', name: 'Harbor Branch' },
          status: 'active',
          collector_count: 2,
          // What the API never sends, sent anyway: none of it can reach the page.
          installation_id: 'inst-55c1',
          hostname: 'AMH-PC-01',
          collector_version: '1.0.13',
          api_token: 'tok-secret',
        },
      ],
    }
    await page(BRANCHES, 'Branches & Sorters', { detail })

    expect(rowsOf(screen.getByRole('table', { name: 'Branches and sorting machines' }))).toEqual([
      ['Harbor Branch · Primary', 'Harbor AMH', 'Active', '2 — their figures are combined'],
    ])
    expect(main()).not.toHaveTextContent(/br-7f3a|9041|77|inst-55c1|AMH-PC-01|1\.0\.13|tok-secret|northbridge(?! library)/i)
  })

  it('is an inventory: nothing adds, changes or removes a branch or a machine', async () => {
    await page(BRANCHES, 'Branches & Sorters', { role: 'owner' })

    expect(controls()).toEqual([])
    expect(within(main()).queryByRole('link', { name: /add|edit|delete|remove|enroll|register/i })).not.toBeInTheDocument()
    expect(main()).toHaveTextContent('Branches and sorting machines are set up by SortView support and cannot be changed here.')
  })

  it('says so when the organization has no branches, and stays readable when it is suspended', async () => {
    await page(BRANCHES, 'Branches & Sorters', { access_mode: 'read_only', detail: { branches: [], sorters: [] } })

    expect(main()).toHaveTextContent('No branches are registered for this organization yet.')
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(strip().getAllByRole('link')).toHaveLength(5)
  })
})

// =====================================================================================================================
// Efficiency
// =====================================================================================================================

describe('Efficiency', () => {
  it('is the organization-defaults form, with what is stored, under headings in order', async () => {
    const { fetchMock } = await page(EFFICIENCY_PAGE, 'Efficiency')
    const form = within(await screen.findByRole('form', { name: 'Organization defaults' }))

    expect(form.getByLabelText('Hourly labor rate (USD per hour)')).toHaveValue('17.56')
    expect(form.getByLabelText('Manual processing rate assumption (items per staff labor-hour)')).toHaveValue('')
    expect(form.getByRole('button', { name: 'Save organization defaults' })).toBeInTheDocument()
    // No date in this form, and none of a sorting machine's own fields.
    expect(main().querySelectorAll('input[type="date"]')).toHaveLength(0)
    expect(main().querySelectorAll('input')).toHaveLength(2)
    expect(Array.from(main().querySelectorAll('h2, h3, h4, h5, h6')).map((heading) => `${heading.tagName} ${heading.textContent}`)).toEqual([
      'H2 Efficiency',
      'H3 Organization defaults',
      'H3 Sorting machine overrides',
    ])
    expect(requests(fetchMock).filter((request) => request.includes('/settings/efficiency'))).toEqual([`GET ${EFFICIENCY}`])
  })

  it('saves through the same request as beside a report, and says it is saved', async () => {
    const { fetchMock, user } = await page(EFFICIENCY_PAGE, 'Efficiency', {}, {
      [`PUT ${EFFICIENCY}`]: () => jsonResponse(200, { efficiency: { labor_rate: '17.56', manual_items_per_hour: '45.5' } }),
    })
    const form = within(await screen.findByRole('form', { name: 'Organization defaults' }))

    await user.type(form.getByLabelText('Manual processing rate assumption (items per staff labor-hour)'), '45.5')
    await user.click(form.getByRole('button', { name: 'Save organization defaults' }))

    expect(await form.findByRole('status')).toHaveTextContent(/^Saved\./)
    const put = requests(fetchMock).lastIndexOf(`PUT ${EFFICIENCY}`)
    expect(JSON.parse(String(callOf(fetchMock, put).init.body))).toEqual({ labor_rate: '17.56', manual_items_per_hour: '45.5' })
  })

  it('keeps the form’s own rules: a value it refuses is not sent', async () => {
    const { fetchMock, user } = await page(EFFICIENCY_PAGE, 'Efficiency')
    const form = within(await screen.findByRole('form', { name: 'Organization defaults' }))

    await user.type(form.getByLabelText('Manual processing rate assumption (items per staff labor-hour)'), 'lots')
    await user.click(form.getByRole('button', { name: 'Save organization defaults' }))

    expect(await form.findByRole('alert')).toHaveTextContent('Nothing was saved. Check the fields marked below.')
    expect(requests(fetchMock).some((request) => request.startsWith('PUT'))).toBe(false)
  })

  it('leads to each sorting machine’s reports for its own figures, and has no form for them here', async () => {
    await page(EFFICIENCY_PAGE, 'Efficiency')
    const list = within(screen.getByRole('list', { name: 'Sorting machine overrides' }))

    expect(list.getAllByRole('link').map((item) => [item.textContent, item.getAttribute('href')])).toEqual([
      ['Central Library AMH reports', '/organizations/northbridge/sorters/central/reports'],
      ['East Side AMH reports', '/organizations/northbridge/sorters/east-side/reports'],
    ])
    expect(list.getAllByRole('listitem').map((item) => item.textContent)).toEqual(['Central Library AMH reportsCentral Branch', 'East Side AMH reportsEast Side Branch'])
    await screen.findByRole('form', { name: 'Organization defaults' })
    expect(screen.getAllByRole('form')).toHaveLength(1)
    expect(main()).not.toHaveTextContent(/This sorter|One-time cost|In-service date/)
  })

  it('says so when there is no sorting machine to have figures of its own', async () => {
    await page(EFFICIENCY_PAGE, 'Efficiency', { detail: { sorters: [] } })

    expect(main()).toHaveTextContent('No sorting machines are registered for this organization yet.')
    expect(screen.queryByRole('list', { name: 'Sorting machine overrides' })).not.toBeInTheDocument()
  })

  it('shows a suspended organization’s defaults as they are stored, with nothing to change or save them with', async () => {
    const { fetchMock } = await page(EFFICIENCY_PAGE, 'Efficiency', { role: 'owner', access_mode: 'read_only' })

    await waitFor(() => expect(facts()).toEqual({ 'Hourly labor rate': '$17.56 per hour', 'Manual processing rate assumption': 'Not set' }))
    expect(main()).toHaveTextContent('This organization is suspended, so its Efficiency assumptions cannot be changed.')
    expect(controls()).toEqual([])
    expect(screen.queryByRole('form')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'Organization defaults' })).toBeInTheDocument()
    // Still leads to the machines' reports, which can still be read.
    expect(within(screen.getByRole('list', { name: 'Sorting machine overrides' })).getAllByRole('link')).toHaveLength(2)
    expect(requests(fetchMock).every((request) => request.startsWith('GET'))).toBe(true)
  })
})

// =====================================================================================================================
// The members page, as a section
// =====================================================================================================================

describe('Users & Access', () => {
  it('keeps its own address and is drawn with the same strip, so the other sections are one click away', async () => {
    const { user } = await page(MEMBERS_PAGE, 'Users & Access')
    await screen.findByRole('table', { name: 'Members' })

    expect(address()).toBe(MEMBERS_PAGE)
    expect(strip().getAllByRole('link').map((item) => item.textContent)).toEqual(SECTIONS)
    expect(strip().getByRole('link', { name: 'Users & Access' })).toHaveAttribute('aria-current', 'page')

    await user.click(strip().getByRole('link', { name: 'Branches & Sorters' }))
    expect(await screen.findByRole('heading', { level: 2, name: 'Branches & Sorters' })).toHaveFocus()
    expect(address()).toBe(BRANCHES)
  })

  it('has no address under the area: it was not moved or copied there', async () => {
    serve()
    renderApp(`${AREA}/members`)

    expect(await screen.findByRole('heading', { level: 2, name: 'Page not found' })).toBeInTheDocument()
  })
})
