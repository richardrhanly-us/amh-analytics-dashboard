import type { QueryClient } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ALICE, jsonResponse, requestedUrls, serveApi, type ApiRoutes, type FetchMock } from '../../../src/test/http.ts'
import { AdminApp } from '../AdminApp.tsx'

// Every query client the admin app makes, so a test can look inside its cache for the enrollment code.
const clients = vi.hoisted(() => [] as QueryClient[])
vi.mock('../../../src/query/queryClient.ts', async (importOriginal) => {
  const original = await importOriginal<typeof import('../../../src/query/queryClient.ts')>()
  return {
    ...original,
    createQueryClient: (onSessionExpired: () => void) => {
      const client = original.createQueryClient(onSessionExpired)
      clients.push(client)
      return client
    },
  }
})

const SESSION = { 'GET /api/admin/session': () => jsonResponse(200, ALICE) }
const BASE = '/api/admin/libraries/acme/installations'
const CANARY_CODE = 'SV-CNRY-7Q4Z-WXYZ-2345'

const ACTIVE = {
  id: 123,
  name: 'Main AMH Sorter',
  branch: { slug: 'main', name: 'Main Branch', is_primary: true, status: 'active' },
  status: 'active',
  required_contract: 2,
  installed_at: '2026-10-01T13:00:00Z',
  last_seen_at: '2026-10-04T12:00:00Z',
  created_at: '2026-10-01T12:00:00Z',
  pending_code_expires_at: null,
  last_code_redeemed_at: '2026-10-01T12:50:00Z',
}
const INACTIVE = { ...ACTIVE, id: 124, name: 'Old Sorter', status: 'inactive', required_contract: 1, last_code_redeemed_at: null }
const RETIRED = { ...ACTIVE, id: 125, name: 'Gone Sorter', status: 'retired', last_code_redeemed_at: null }
const LIST = {
  library: { slug: 'acme', name: 'Acme Library', status: 'active' },
  primary_branch: { slug: 'main', name: 'Main Branch', status: 'active' },
  add_problems: [],
  installations: [ACTIVE, INACTIVE, RETIRED],
}
const ISSUED = {
  installation: { id: 123, name: 'Main AMH Sorter' },
  required_contract: 2,
  enrollment_code: CANARY_CODE,
  expires_at: '2026-10-15T18:30:00Z',
  ttl_minutes: 30,
  revoked_previous_count: 0,
}

afterEach(() => {
  clients.length = 0
})

function renderAt(path: string, routes: ApiRoutes) {
  const fetchMock = serveApi({ ...SESSION, ...routes })
  render(
    <MemoryRouter initialEntries={[path]}>
      <AdminApp />
    </MemoryRouter>,
  )
  return fetchMock
}

function bodies(mock: FetchMock, method: string, path: string): unknown[] {
  return mock.mock.calls
    .filter(([url, init]) => String(url) === path && init?.method === method)
    .map(([, init]) => (init?.body === undefined ? undefined : (JSON.parse(String(init.body)) as unknown)))
}

function card(id: number): HTMLElement {
  return screen.getByRole('heading', { level: 3, name: new RegExp(`^Installation #${id} `) }).closest('article') as HTMLElement
}

/** The list has arrived: only then is the time-zone note on the page. */
async function loaded() {
  await screen.findByText(/^Times are shown in /)
}

function buttons(element: HTMLElement): string[] {
  return within(element)
    .queryAllByRole('button')
    .map((button) => button.textContent ?? '')
}

describe('Installations', () => {
  it('lists every installation with its facts and nothing that identifies a machine', async () => {
    const fetchMock = renderAt('/libraries/acme/installations', { [`GET ${BASE}`]: () => jsonResponse(200, LIST) })

    await loaded()
    expect(screen.getByRole('heading', { level: 2, name: 'Installations' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Acme Library' })).toHaveAttribute('href', '/libraries/acme')
    const active = card(123)
    expect(within(active).getByText('Main Branch (primary)')).toBeInTheDocument()
    expect(within(active).getByText('Active')).toBeInTheDocument()
    expect(within(active).getByText('Contract 2')).toBeInTheDocument()
    expect(within(active).getByText(/^Last used /)).toBeInTheDocument()
    expect(within(card(124)).getByText('Inactive')).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/hostname|collector version|installer|customer id|branch id|token|key id/i)
    expect(requestedUrls(fetchMock)).toEqual(['/api/admin/session', BASE])
  })

  it('says when a library has no installations', async () => {
    renderAt('/libraries/acme/installations', { [`GET ${BASE}`]: () => jsonResponse(200, { ...LIST, installations: [] }) })

    expect(await screen.findByText('No installations are registered for this library.')).toBeInTheDocument()
  })

  it('offers each status only its own actions: Return to provisioning for inactive, nothing for retired', async () => {
    renderAt('/libraries/acme/installations', { [`GET ${BASE}`]: () => jsonResponse(200, LIST) })
    await loaded()

    expect(buttons(card(123))).toEqual(['Rename', 'Deactivate', 'Retire', 'Issue enrollment code'])
    expect(buttons(card(124))).toEqual(['Rename', 'Return to provisioning', 'Retire'])
    expect(buttons(card(125))).toEqual(['Rename'])
    expect(document.body.textContent).not.toMatch(/Reactivate|Suspend|Offboard|Provision library/)
  })

  it('adds a Contract 2 installation at the primary branch by default', async () => {
    let lists = 0
    const fetchMock = renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, ++lists === 1 ? LIST : { ...LIST, installations: [...LIST.installations, { ...ACTIVE, id: 126, name: 'New Sorter', status: 'provisioning' }] }),
      [`POST ${BASE}`]: () => jsonResponse(201, { ...ACTIVE, id: 126, name: 'New Sorter', status: 'provisioning' }),
    })
    const person = userEvent.setup()
    await loaded()

    expect(screen.getByText(/It will be added at the primary branch,/)).toHaveTextContent('Main Branch')
    expect(screen.getByRole('radio', { name: 'Contract 2 (current)' })).toBeChecked()
    await person.type(screen.getByRole('textbox', { name: 'Installation name' }), 'New Sorter')
    await person.click(screen.getByRole('button', { name: 'Add installation' }))

    expect(await screen.findByText('Added Installation #126 — New Sorter.')).toBeInTheDocument()
    expect(bodies(fetchMock, 'POST', BASE)).toEqual([{ name: 'New Sorter', required_contract: 2 }])
    expect(await screen.findByRole('heading', { level: 3, name: /^Installation #126 / })).toBeInTheDocument()
  })

  it('points Contract 2 at Collector configuration when it is not ready, and still adds Contract 1', async () => {
    const fetchMock = renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, { ...LIST, add_problems: ['collector_settings_not_ready'] }),
      [`POST ${BASE}`]: () => jsonResponse(201, { ...INACTIVE, id: 127, name: 'Legacy Sorter', status: 'provisioning' }),
    })
    const person = userEvent.setup()
    await loaded()

    expect(screen.getByRole('link', { name: 'Collector configuration' })).toHaveAttribute('href', '/libraries/acme/collector')
    await person.type(screen.getByRole('textbox', { name: 'Installation name' }), 'Legacy Sorter')
    await person.click(screen.getByRole('button', { name: 'Add installation' }))
    expect(screen.getByRole('alert')).toHaveTextContent('needs this library’s Collector configuration first')
    expect(bodies(fetchMock, 'POST', BASE)).toEqual([])

    await person.click(screen.getByRole('radio', { name: 'Contract 1 (legacy)' }))
    expect(screen.queryByRole('link', { name: 'Collector configuration' })).not.toBeInTheDocument()
    await person.click(screen.getByRole('button', { name: 'Add installation' }))

    expect(await screen.findByText('Added Installation #127 — Legacy Sorter.')).toBeInTheDocument()
    expect(bodies(fetchMock, 'POST', BASE)).toEqual([{ name: 'Legacy Sorter', required_contract: 1 }])
  })

  it('renames, keeping what was typed when the save is refused', async () => {
    let puts = 0
    const fetchMock = renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, LIST),
      [`PUT ${BASE}/123`]: () =>
        ++puts === 1
          ? jsonResponse(409, { code: 'library_cancelled', message: 'This library is cancelled; its installations cannot be changed.' })
          : jsonResponse(200, { ...ACTIVE, name: 'Front Desk' }),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(within(card(123)).getByRole('button', { name: 'Rename' }))
    const field = within(card(123)).getByRole('textbox', { name: 'Installation name' })
    await person.clear(field)
    await person.type(field, 'Front Desk')
    await person.click(within(card(123)).getByRole('button', { name: 'Save name' }))

    expect(await within(card(123)).findByRole('alert')).toHaveTextContent('Nothing was changed. This library is cancelled')
    expect(field).toHaveValue('Front Desk')
    await person.click(within(card(123)).getByRole('button', { name: 'Save name' }))
    await waitFor(() => expect(within(card(123)).queryByRole('textbox')).not.toBeInTheDocument())
    expect(bodies(fetchMock, 'PUT', `${BASE}/123`)).toEqual([{ name: 'Front Desk' }, { name: 'Front Desk' }])
  })

  it('asks before deactivating, and says the token stops authenticating', async () => {
    const fetchMock = renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, LIST),
      [`PUT ${BASE}/123/status`]: () => jsonResponse(200, { ...ACTIVE, status: 'inactive' }),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(within(card(123)).getByRole('button', { name: 'Deactivate' }))
    expect(within(card(123)).getByText(/agent token stops authenticating until the installation is returned to provisioning/)).toBeInTheDocument()
    expect(bodies(fetchMock, 'PUT', `${BASE}/123/status`)).toEqual([])
    await person.click(within(card(123)).getByRole('button', { name: 'Confirm: Deactivate' }))

    await waitFor(() => expect(bodies(fetchMock, 'PUT', `${BASE}/123/status`)).toEqual([{ status: 'inactive' }]))
  })

  it('returns an inactive installation to provisioning, saying it becomes active only after a heartbeat', async () => {
    const fetchMock = renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, LIST),
      [`PUT ${BASE}/124/status`]: () => jsonResponse(200, { ...INACTIVE, status: 'provisioning' }),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(within(card(124)).getByRole('button', { name: 'Return to provisioning' }))
    expect(within(card(124)).getByText(/can authenticate again\. The installation becomes active only after its next heartbeat/)).toBeInTheDocument()
    await person.click(within(card(124)).getByRole('button', { name: 'Confirm: Return to provisioning' }))

    await waitFor(() => expect(bodies(fetchMock, 'PUT', `${BASE}/124/status`)).toEqual([{ status: 'provisioning' }]))
  })

  it('asks before retiring, saying it is final here and does not replace a v2 machine', async () => {
    const fetchMock = renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, LIST),
      [`PUT ${BASE}/123/status`]: () => jsonResponse(200, { ...ACTIVE, status: 'retired' }),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(within(card(123)).getByRole('button', { name: 'Retire' }))
    const warning = within(card(123)).getByText(/^Retire Installation #123\?/)
    expect(warning).toHaveTextContent('stops authenticating immediately')
    expect(warning).toHaveTextContent('cannot be undone in SortView administration')
    expect(warning).toHaveTextContent('Replacing a Contract 2 collector machine is not handled here')
    await person.click(within(card(123)).getByRole('button', { name: 'Cancel' }))
    expect(bodies(fetchMock, 'PUT', `${BASE}/123/status`)).toEqual([])

    await person.click(within(card(123)).getByRole('button', { name: 'Retire' }))
    await person.click(within(card(123)).getByRole('button', { name: 'Confirm: Retire' }))
    await waitFor(() => expect(bodies(fetchMock, 'PUT', `${BASE}/123/status`)).toEqual([{ status: 'retired' }]))
  })

  it('shows an issued code once, with Copy, and keeps it out of the cache, storage and the page after leaving', async () => {
    const fetchMock = renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, LIST),
      [`POST ${BASE}/123/enrollment-codes`]: () => jsonResponse(201, ISSUED),
      'GET /api/admin/libraries/acme': () =>
        jsonResponse(200, {
          timezone: 'America/Chicago', slug: 'acme', name: 'Acme Library', status: 'active', primary_branch: null,
          operational_identity: 'assigned', subscription: null, sorters: [],
        }),
    })
    const person = userEvent.setup()
    // After setup: user-event installs a clipboard of its own.
    const writeText = vi.fn(() => Promise.resolve())
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    await loaded()

    await person.click(within(card(123)).getByRole('button', { name: 'Issue enrollment code' }))

    expect(await screen.findByText(CANARY_CODE)).toBeInTheDocument()
    expect(screen.getByText(/^Single use\. Shown only now/)).toHaveTextContent(
      /copy it before leaving this page\. It cannot be shown again\. It expires at .+ and stops working when used or when a new code is issued\./,
    )
    await person.click(screen.getByRole('button', { name: 'Copy' }))
    expect(writeText).toHaveBeenCalledWith(CANARY_CODE)
    expect(await screen.findByText('Copied.')).toBeInTheDocument()
    expect(bodies(fetchMock, 'POST', `${BASE}/123/enrollment-codes`)).toEqual([undefined])

    // Never in the query or mutation cache, never in storage.
    const cached = clients.flatMap((client) => [
      ...client.getQueryCache().getAll().map((query) => JSON.stringify(query.state.data ?? null)),
      ...client.getMutationCache().getAll().map((mutation) => JSON.stringify(mutation.state.data ?? null)),
    ])
    expect(cached.length).toBeGreaterThan(0)
    expect(cached.join('')).not.toContain(CANARY_CODE)
    expect(JSON.stringify({ ...localStorage })).not.toContain(CANARY_CODE)
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(CANARY_CODE)

    // Leave the page and come back: the code is gone, and cannot be shown again.
    await person.click(screen.getByRole('link', { name: 'Acme Library' }))
    await screen.findByRole('link', { name: 'Installations' })
    expect(document.body.textContent).not.toContain(CANARY_CODE)
    await person.click(screen.getByRole('link', { name: 'Installations' }))
    await loaded()
    expect(document.body.textContent).not.toContain(CANARY_CODE)
  })

  it('asks before replacing an unused code', async () => {
    const pending = { ...ACTIVE, pending_code_expires_at: '2026-10-15T18:10:00Z' }
    const fetchMock = renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, { ...LIST, installations: [pending] }),
      [`POST ${BASE}/123/enrollment-codes`]: () => jsonResponse(201, { ...ISSUED, revoked_previous_count: 1 }),
    })
    const person = userEvent.setup()
    await loaded()

    expect(within(card(123)).getByText(/^Unused code until /)).toBeInTheDocument()
    await person.click(within(card(123)).getByRole('button', { name: 'Issue enrollment code' }))
    expect(within(card(123)).getByText(/Issuing a new code makes that code stop working/)).toBeInTheDocument()
    expect(bodies(fetchMock, 'POST', `${BASE}/123/enrollment-codes`)).toEqual([])
    await person.click(within(card(123)).getByRole('button', { name: 'Confirm: Issue a new code' }))

    expect(await screen.findByText(CANARY_CODE)).toBeInTheDocument()
    expect(screen.getByText('The earlier unused code for this installation no longer works.')).toBeInTheDocument()
  })

  it.each([
    ['branch_already_enrolled', 'This branch already has an enrolled Contract 2 Collector. Replacing it is not handled here.'],
    ['installation_not_enrollable', 'This installation cannot be given an enrollment code in its current status.'],
    ['library_not_enrollable', 'This library cannot enroll a Collector in its current status.'],
    ['operational_identity_incomplete', "This library's operational identity is not complete."],
  ])('says why a code was refused (%s), and shows no code', async (code, message) => {
    renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, LIST),
      [`POST ${BASE}/123/enrollment-codes`]: () => jsonResponse(409, { code, message }),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(within(card(123)).getByRole('button', { name: 'Issue enrollment code' }))

    expect(await within(card(123)).findByRole('alert')).toHaveTextContent(`Nothing was changed. ${message}`)
    expect(screen.queryByRole('heading', { name: /^Enrollment code for/ })).not.toBeInTheDocument()
  })

  it('shows a cancelled library read-only', async () => {
    renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () =>
        jsonResponse(200, { ...LIST, library: { ...LIST.library, status: 'cancelled' }, add_problems: ['library_cancelled'] }),
    })
    await loaded()

    expect(screen.getByText('This library is cancelled. Its installations can be seen and not changed.')).toBeInTheDocument()
    expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual(['Log out'])
    expect(screen.queryByRole('heading', { name: 'Add an installation' })).not.toBeInTheDocument()
  })

  it('says a library that does not exist was not found', async () => {
    renderAt('/libraries/nobody/installations', {
      'GET /api/admin/libraries/nobody/installations': () => jsonResponse(404, { code: 'library_not_found', message: 'Library not found.' }),
    })

    expect(await screen.findByRole('heading', { level: 2, name: 'Library not found' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Libraries' })).toHaveAttribute('href', '/libraries')
  })

  it('shows a fixed load error with a retry that works', async () => {
    let calls = 0
    renderAt('/libraries/acme/installations', {
      // A list that is not the expected shape is a failure, and is not retried by the query client.
      [`GET ${BASE}`]: () => jsonResponse(200, ++calls === 1 ? { ...LIST, installations: [{ ...ACTIVE, status: 'melted' }] } : LIST),
    })
    const person = userEvent.setup()

    expect(await screen.findByRole('alert')).toHaveTextContent('The installations could not be loaded. Please try again.')
    await person.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByRole('heading', { level: 3, name: /^Installation #123 / })).toBeInTheDocument()
  })

  it('is opened from the library overview', async () => {
    renderAt('/libraries/acme', {
      [`GET ${BASE}`]: () => jsonResponse(200, LIST),
      'GET /api/admin/libraries/acme': () =>
        jsonResponse(200, {
          timezone: 'America/Chicago', slug: 'acme', name: 'Acme Library', status: 'active', primary_branch: null,
          operational_identity: 'assigned', subscription: null, sorters: [],
        }),
    })
    const person = userEvent.setup()

    await person.click(await screen.findByRole('link', { name: 'Installations' }))

    expect(await screen.findByRole('heading', { level: 2, name: 'Installations' })).toBeInTheDocument()
  })
})
