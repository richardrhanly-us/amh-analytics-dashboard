import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import { ALICE, deferred, jsonResponse, serveApi, type ApiRoutes, type FetchMock } from '../../../src/test/http.ts'
import { axeViolations } from '../../../src/test/axe.ts'
import { AdminApp } from '../AdminApp.tsx'
import { slugify } from './provisionApi.ts'

const SESSION = { 'GET /api/admin/session': () => jsonResponse(200, ALICE) }
const PLANS = { 'GET /api/admin/plans': () => jsonResponse(200, { plans: [{ code: 'starter', name: 'Starter' }, { code: 'pro', name: 'Pro' }] }) }
const NO_OWNER = 'No one can sign in to this library yet. Creating its first owner is not available in SortView administration yet.'

function detail(slug: string, identity: string) {
  return {
    timezone: 'America/Chicago',
    slug,
    name: 'Riverbend Public Library',
    status: 'active',
    primary_branch: { slug: 'main', name: 'Main Branch', status: 'active' },
    operational_identity: identity,
    subscription: { plan_code: 'pro', plan_name: 'Pro', status: 'trial' },
    sorters: [],
  }
}

const CREATED = {
  library: { slug: 'riverbend', name: 'Riverbend Public Library', status: 'active' },
  primary_branch: { slug: 'main', name: 'Main Branch', status: 'active' },
  subscription: { plan_code: 'pro', plan_name: 'Pro', status: 'trial' },
  operational_identity: 'assigned',
}

function renderAt(path: string, routes: ApiRoutes) {
  const fetchMock = serveApi({ ...SESSION, ...routes })
  render(
    <MemoryRouter initialEntries={[path]}>
      <AdminApp />
    </MemoryRouter>,
  )
  return fetchMock
}

function posts(mock: FetchMock): { url: string; body: unknown }[] {
  return mock.mock.calls
    .filter(([, init]) => init?.method === 'POST')
    .map(([url, init]) => ({ url: String(url), body: init?.body === undefined ? undefined : (JSON.parse(String(init.body)) as unknown) }))
}

async function form() {
  await screen.findByRole('option', { name: 'Pro' })
  return screen.getByRole('form', { name: 'Provision a library' })
}

const field = (name: string) => screen.getByLabelText(name)

async function fill(person: ReturnType<typeof userEvent.setup>, name = 'Riverbend Public Library') {
  await person.type(field('Library name'), name)
  await person.selectOptions(field('Plan'), 'pro')
}

// What the provisioning page must never offer.
const NOT_HERE = /owner|invite|e-?mail|contact|notes|password|second branch|add branch|installation name|enrollment code|installer|customer id|branch id|token|key id|hostname|system name/i

describe('slugify', () => {
  it.each([
    ['Riverbend Public Library', 'riverbend-public-library'],
    ['  Main Branch  ', 'main-branch'],
    ["St. Mary's -- Library!", 'st-mary-s-library'],
    ['ÉCOLE 2', 'cole-2'],
    ['!!!', ''],
  ])('makes %j into %j, as Super Admin did', (value, slug) => {
    expect(slugify(value)).toBe(slug)
  })
})

describe('Provision a library', () => {
  it('is linked from the Libraries page', async () => {
    renderAt('/libraries', { 'GET /api/admin/libraries': () => jsonResponse(200, { timezone: 'America/Chicago', libraries: [] }) })

    expect(await screen.findByRole('link', { name: 'Provision a library' })).toHaveAttribute('href', '/provision')
  })

  it('is one form with the five fields, Main Branch as the branch, and no plan chosen', async () => {
    renderAt('/provision', PLANS)
    const provision = await form()

    expect(screen.getByRole('heading', { level: 2, name: 'Provision a library' })).toBeInTheDocument()
    expect(document.querySelectorAll('form')).toHaveLength(1)
    expect(within(provision).getAllByRole('textbox').map((box) => box.id)).toEqual(['name', 'slug', 'branch_name', 'branch_slug'])
    expect(field('Primary branch name')).toHaveValue('Main Branch')
    expect(field('Primary branch slug')).toHaveValue('main-branch')
    expect(field('Plan')).toHaveValue('')
    expect(within(provision).getAllByRole('button').map((button) => button.textContent)).toEqual(['Provision library'])
    expect(provision.textContent).not.toMatch(NOT_HERE)
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('fills a slug from its name until the slug is edited by hand, then never overwrites it', async () => {
    const person = userEvent.setup()
    renderAt('/provision', PLANS)
    await form()

    await person.type(field('Library name'), 'River Bend')
    expect(field('Library slug')).toHaveValue('river-bend')
    await person.clear(field('Library slug'))
    await person.type(field('Library slug'), 'rb')
    await person.type(field('Library name'), ' Public')
    expect(field('Library slug')).toHaveValue('rb')

    await person.clear(field('Primary branch name'))
    await person.type(field('Primary branch name'), 'Central')
    expect(field('Primary branch slug')).toHaveValue('central')
    await person.type(field('Primary branch slug'), '-hq')
    await person.type(field('Primary branch name'), ' Branch')
    expect(field('Primary branch slug')).toHaveValue('central-hq')
  })

  it('checks every field before sending, and sends nothing while a problem remains', async () => {
    const person = userEvent.setup()
    const fetchMock = renderAt('/provision', PLANS)
    await form()
    await person.clear(field('Primary branch name'))
    await person.clear(field('Primary branch slug'))
    await person.type(field('Library slug'), 'Bad--Slug')

    await person.click(screen.getByRole('button', { name: 'Provision library' }))

    expect(field('Library name')).toHaveAccessibleDescription('Required.')
    expect(field('Library slug')).toHaveAccessibleDescription('Use lowercase letters and digits, with single hyphens between them.')
    expect(field('Primary branch name')).toHaveAccessibleDescription('Required.')
    expect(field('Primary branch slug')).toHaveAccessibleDescription('Required.')
    expect(field('Plan')).toHaveAccessibleDescription('Choose a plan.')
    expect(posts(fetchMock)).toEqual([])

    await person.type(field('Library name'), 'N'.repeat(121))
    await person.clear(field('Library slug'))
    await person.type(field('Library slug'), 's'.repeat(64))
    await person.click(screen.getByRole('button', { name: 'Provision library' }))
    expect(field('Library name')).toHaveAccessibleDescription('At most 120 characters.')
    expect(field('Library slug')).toHaveAccessibleDescription('At most 63 characters.')
    expect(posts(fetchMock)).toEqual([])
  })

  it('sends exactly the five fields once, disables itself while sending, and lands on the overview with next steps', async () => {
    const person = userEvent.setup()
    const answer = deferred<Response>()
    const fetchMock = renderAt('/provision', {
      ...PLANS,
      'POST /api/admin/libraries': () => answer.promise,
      'GET /api/admin/libraries/riverbend': () => jsonResponse(200, detail('riverbend', 'assigned')),
    })
    await form()
    await fill(person, '  Riverbend Public Library')

    await person.click(screen.getByRole('button', { name: 'Provision library' }))
    const sending = screen.getByRole('button', { name: 'Provisioning…' })
    expect(sending).toBeDisabled()
    await person.click(sending)
    expect(posts(fetchMock)).toEqual([
      {
        url: '/api/admin/libraries',
        body: { name: '  Riverbend Public Library', slug: 'riverbend-public-library', branch_name: 'Main Branch', branch_slug: 'main-branch', plan_code: 'pro' },
      },
    ])
    answer.resolve(jsonResponse(201, { ...CREATED, library: { ...CREATED.library, slug: 'riverbend' } }))

    expect(await screen.findByRole('heading', { level: 2, name: 'Riverbend Public Library' })).toBeInTheDocument()
    const steps = screen.getByRole('region', { name: 'Library provisioned: next steps' })
    expect(within(steps).getByRole('link', { name: 'Configure its Collector settings' })).toHaveAttribute('href', '/libraries/riverbend/collector')
    expect(within(steps).getByRole('link', { name: 'Add a Collector installation' })).toHaveAttribute('href', '/libraries/riverbend/installations')
    expect(steps.textContent).toMatch(/Issue an enrollment code/)
    expect(within(steps).getByText(NO_OWNER)).toBeInTheDocument()
    expect(posts(fetchMock)).toHaveLength(1)
  })

  it('shows the server’s 422 problems on their own fields', async () => {
    const person = userEvent.setup()
    renderAt('/provision', {
      ...PLANS,
      'POST /api/admin/libraries': () =>
        jsonResponse(422, {
          code: 'invalid_library',
          message: 'Library details are invalid.',
          problems: [{ field: 'slug', code: 'invalid_slug' }, { field: 'branch_name', code: 'too_long' }, { field: 'nope', code: 'x' }],
        }),
    })
    await form()
    await fill(person)

    await person.click(screen.getByRole('button', { name: 'Provision library' }))

    expect(await screen.findByText('Use lowercase letters and digits, with single hyphens between them.')).toBeInTheDocument()
    expect(field('Library slug')).toHaveAccessibleDescription('Use lowercase letters and digits, with single hyphens between them.')
    expect(field('Primary branch name')).toHaveAccessibleDescription('At most 120 characters.')
    expect(screen.getByRole('button', { name: 'Provision library' })).toBeEnabled()
  })

  it.each([
    ['library_slug_taken', 'A library with this slug already exists.'],
    ['plan_not_available', 'This plan is not available for a new library.'],
  ])('shows a 409 %s as its fixed message', async (code, message) => {
    const person = userEvent.setup()
    renderAt('/provision', { ...PLANS, 'POST /api/admin/libraries': () => jsonResponse(409, { code, message }) })
    await form()
    await fill(person)

    await person.click(screen.getByRole('button', { name: 'Provision library' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(`Nothing was created. ${message}`)
  })

  it.each([
    ['a library that is not active', { ...CREATED, library: { ...CREATED.library, status: 'trial' } }],
    ['a branch that is not active', { ...CREATED, primary_branch: { ...CREATED.primary_branch, status: 'inactive' } }],
    ['a subscription that is not a trial', { ...CREATED, subscription: { ...CREATED.subscription, status: 'active' } }],
    ['an identity that is not assigned', { ...CREATED, operational_identity: 'partial' }],
    ['no identity at all', { library: CREATED.library, primary_branch: CREATED.primary_branch, subscription: CREATED.subscription }],
    ['a status that is not text', { ...CREATED, library: { ...CREATED.library, status: 1 } }],
  ])('treats a 201 with %s as an unexpected answer and does not leave the page', async (_case, body) => {
    const person = userEvent.setup()
    renderAt('/provision', {
      ...PLANS,
      'POST /api/admin/libraries': () => jsonResponse(201, body),
      'GET /api/admin/libraries/riverbend': () => jsonResponse(200, detail('riverbend', 'assigned')),
    })
    await form()
    await fill(person)

    await person.click(screen.getByRole('button', { name: 'Provision library' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The library may not have been created. Check the Libraries list before trying again.',
    )
    expect(screen.getByRole('heading', { level: 2, name: 'Provision a library' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Library provisioned: next steps' })).not.toBeInTheDocument()
  })

  it('never shows an unexpected failure’s own text, and says the library may exist', async () => {
    const person = userEvent.setup()
    renderAt('/provision', {
      ...PLANS,
      'POST /api/admin/libraries': () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' }),
    })
    await form()
    await fill(person)

    await person.click(screen.getByRole('button', { name: 'Provision library' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The library may not have been created. Check the Libraries list before trying again.',
    )
  })

  it('cannot be submitted while the plans cannot be loaded', async () => {
    // An answer that is not the expected shape is a failure, and is not retried by the query client.
    renderAt('/provision', { 'GET /api/admin/plans': () => jsonResponse(200, { plans: [{ code: 7 }] }) })

    expect(await screen.findByRole('alert')).toHaveTextContent('The plans could not be loaded')
    expect(screen.getByRole('button', { name: 'Provision library' })).toBeDisabled()
  })
})

describe('Assign operational identity', () => {
  it('is offered only while the identity is not assigned', async () => {
    renderAt('/libraries/riverbend', { 'GET /api/admin/libraries/riverbend': () => jsonResponse(200, detail('riverbend', 'assigned')) })

    await screen.findByRole('heading', { level: 3, name: 'Operational identity' })
    expect(screen.queryByRole('button', { name: 'Assign operational identity' })).not.toBeInTheDocument()
    // Not reached from provisioning: no next steps.
    expect(screen.queryByRole('region', { name: 'Library provisioned: next steps' })).not.toBeInTheDocument()
  })

  it('is not offered for a cancelled library, even when its identity is missing', async () => {
    renderAt('/libraries/legacy', {
      'GET /api/admin/libraries/legacy': () => jsonResponse(200, { ...detail('legacy', 'missing'), status: 'cancelled' }),
    })

    const section = await screen.findByRole('region', { name: 'Operational identity' })
    expect(within(section).getByText('Not assigned')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Assign operational identity' })).not.toBeInTheDocument()
  })

  it.each([
    ['no identity in the answer', { changed: true }],
    ['an identity that is not assigned', { operational_identity: 'partial', changed: true }],
    ['no changed flag', { operational_identity: 'assigned' }],
    ['a changed flag that is not a boolean', { operational_identity: 'assigned', changed: 'yes' }],
  ])('after a 200 with %s, says the result is unconfirmed and shows the identity the server now holds', async (_case, body) => {
    const person = userEvent.setup()
    const state = { identity: 'missing' }
    renderAt('/libraries/legacy', {
      'GET /api/admin/libraries/legacy': () => jsonResponse(200, detail('legacy', state.identity)),
      // The server did assign it; only its answer is malformed.
      'POST /api/admin/libraries/legacy/operational-identity': () => {
        state.identity = 'assigned'
        return jsonResponse(200, body)
      },
    })

    await person.click(await screen.findByRole('button', { name: 'Assign operational identity' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('The assignment result could not be confirmed. The library has been refreshed.')
    expect(alert.textContent).not.toMatch(/Nothing was changed/)
    const section = screen.getByRole('region', { name: 'Operational identity' })
    expect(await within(section).findByText('Assigned')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Assign operational identity' })).not.toBeInTheDocument()
  })

  it('says the result is unconfirmed after a server error, not that nothing changed', async () => {
    const person = userEvent.setup()
    renderAt('/libraries/legacy', {
      'GET /api/admin/libraries/legacy': () => jsonResponse(200, detail('legacy', 'missing')),
      'POST /api/admin/libraries/legacy/operational-identity': () =>
        jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' }),
    })

    await person.click(await screen.findByRole('button', { name: 'Assign operational identity' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'The assignment result could not be confirmed. The library has been refreshed.',
    )
  })

  it.each(['missing', 'partial'])('assigns a %s identity and then shows it as Assigned', async (identity) => {
    const person = userEvent.setup()
    const state = { identity }
    const fetchMock = renderAt('/libraries/legacy', {
      'GET /api/admin/libraries/legacy': () => jsonResponse(200, detail('legacy', state.identity)),
      'POST /api/admin/libraries/legacy/operational-identity': () => {
        state.identity = 'assigned'
        return jsonResponse(200, { operational_identity: 'assigned', changed: true })
      },
    })

    await person.click(await screen.findByRole('button', { name: 'Assign operational identity' }))

    const section = screen.getByRole('region', { name: 'Operational identity' })
    expect(await within(section).findByText('Assigned')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Assign operational identity' })).not.toBeInTheDocument()
    expect(posts(fetchMock)).toEqual([{ url: '/api/admin/libraries/legacy/operational-identity', body: undefined }])
    expect(document.body.textContent).not.toMatch(/customer id|branch id|operational_customer/i)
  })

  it.each([
    ['library_cancelled', 'This library is cancelled; its operational identity cannot be changed.'],
    ['no_primary_branch', 'This library has no single primary branch to assign an operational identity to.'],
    ['operational_identity_inconsistent', "This library's existing operational identity is inconsistent. It was not changed; contact SortView engineering."],
  ])('shows a 409 %s as its fixed message', async (code, message) => {
    const person = userEvent.setup()
    renderAt('/libraries/legacy', {
      'GET /api/admin/libraries/legacy': () => jsonResponse(200, detail('legacy', 'missing')),
      'POST /api/admin/libraries/legacy/operational-identity': () => jsonResponse(409, { code, message }),
    })

    await person.click(await screen.findByRole('button', { name: 'Assign operational identity' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(`Nothing was changed. ${message}`)
  })
})

describe('accessibility', () => {
  it('axe finds no violation on the provisioning form', async () => {
    renderAt('/provision', PLANS)
    await form()
    expect(await axeViolations()).toEqual([])
  })
})
