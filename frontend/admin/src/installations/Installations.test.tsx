import type { QueryClient } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ALICE, jsonResponse, requestedUrls, serveApi, type ApiRoutes, type FetchMock } from '../../../src/test/http.ts'
import { axeViolations } from '../../../src/test/axe.ts'
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
  replaces_installation_id: null,
}
const INACTIVE = { ...ACTIVE, id: 124, name: 'Old Sorter', status: 'inactive', required_contract: 1, last_code_redeemed_at: null }
const RETIRED = { ...ACTIVE, id: 125, name: 'Gone Sorter', status: 'retired', last_code_redeemed_at: null }
const LIST = {
  library: { slug: 'acme', name: 'Acme Library', status: 'active' },
  primary_branch: { slug: 'main', name: 'Main Branch', status: 'active' },
  add_problems: [],
  pending_replacement_id: null,
  replacement_enabled: false,
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
const CANARY_TOKEN = 'CANARY-recovery-token-7Q4ZWXYZ2345'
const RECOVERED = { installation: { id: 123, name: 'Main AMH Sorter' }, agent_token: CANARY_TOKEN, revoked_previous_count: 1 }
const UNBOUND = { id: 55, description: 'NBPL canonical agent', branch_name: 'Main Branch', created_at: '2026-01-01T12:00:00Z', last_used_at: null }

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
    expect(document.body.textContent).not.toMatch(/hostname|collector version|installer|customer id|branch id|key id/i)
    expect(requestedUrls(fetchMock)).toEqual(['/api/admin/session', BASE])
  })

  it('says when a library has no installations', async () => {
    renderAt('/libraries/acme/installations', { [`GET ${BASE}`]: () => jsonResponse(200, { ...LIST, installations: [] }) })

    expect(await screen.findByText('No installations are registered for this library.')).toBeInTheDocument()
  })

  it('offers each status only its own actions: Reactivate for inactive, nothing for retired', async () => {
    renderAt('/libraries/acme/installations', { [`GET ${BASE}`]: () => jsonResponse(200, LIST) })
    await loaded()

    expect(buttons(card(123))).toEqual(['Rename', 'Deactivate', 'Retire', 'Issue enrollment code', 'Issue recovery token'])
    expect(buttons(card(124))).toEqual(['Rename', 'Reactivate', 'Retire'])
    expect(buttons(card(125))).toEqual(['Rename'])
    expect(document.body.textContent).not.toMatch(/Return to provisioning|Suspend|Offboard|Provision library/)
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
    expect(within(card(123)).getByText(/agent token stops authenticating until the installation is reactivated/)).toBeInTheDocument()
    expect(bodies(fetchMock, 'PUT', `${BASE}/123/status`)).toEqual([])
    await person.click(within(card(123)).getByRole('button', { name: 'Confirm: Deactivate' }))

    await waitFor(() => expect(bodies(fetchMock, 'PUT', `${BASE}/123/status`)).toEqual([{ status: 'inactive' }]))
  })

  it('reactivates an inactive installation through provisioning, saying it becomes Active only after a successful heartbeat', async () => {
    const fetchMock = renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, LIST),
      [`PUT ${BASE}/124/status`]: () => jsonResponse(200, { ...INACTIVE, status: 'provisioning' }),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(within(card(124)).getByRole('button', { name: 'Reactivate' }))
    expect(
      within(card(124)).getByText(/can authenticate again immediately\. The installation shows as Provisioning until its next successful heartbeat, then becomes Active/),
    ).toBeInTheDocument()
    expect(bodies(fetchMock, 'PUT', `${BASE}/124/status`)).toEqual([])
    await person.click(within(card(124)).getByRole('button', { name: 'Confirm: Reactivate' }))

    await waitFor(() => expect(bodies(fetchMock, 'PUT', `${BASE}/124/status`)).toEqual([{ status: 'provisioning' }]))
  })

  it('asks before retiring, saying it is final here and pointing to replacement instead', async () => {
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
    expect(warning).toHaveTextContent('To replace a Contract 2 Collector computer, use Replace this Collector computer instead.')
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
          operational_identity: 'assigned', subscription: null, has_active_owner: true, sorters: [],
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
    ['branch_already_enrolled', 'This branch already has an enrolled Contract 2 Collector. To replace its computer, use Replace this Collector computer.'],
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

  it('says why retiring a key owner was refused, and leaves it as it was', async () => {
    const message = "This installation owns its branch's active ingest key, so it cannot be retired here. Deactivate it instead."
    renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, LIST),
      [`PUT ${BASE}/123/status`]: () => jsonResponse(409, { code: 'installation_owns_active_key', message }),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(within(card(123)).getByRole('button', { name: 'Retire' }))
    await person.click(within(card(123)).getByRole('button', { name: 'Confirm: Retire' }))

    expect(await within(card(123)).findByRole('alert')).toHaveTextContent(`Nothing was changed. ${message}`)
    expect(within(card(123)).getByText('Active')).toBeInTheDocument()
  })

  it('offers a recovery token only to a Provisioning or Active Contract 2 installation', async () => {
    const provisioning = { ...ACTIVE, id: 126, status: 'provisioning' }
    const legacy = { ...ACTIVE, id: 127, required_contract: 1 }
    renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, { ...LIST, installations: [ACTIVE, provisioning, legacy, { ...INACTIVE, required_contract: 2 }, RETIRED] }),
    })
    await loaded()

    expect(buttons(card(123))).toContain('Issue recovery token')
    expect(buttons(card(126))).toContain('Issue recovery token')
    expect(buttons(card(127))).not.toContain('Issue recovery token')
    expect(buttons(card(124))).not.toContain('Issue recovery token')
    expect(buttons(card(125))).not.toContain('Issue recovery token')
  })

  it('asks first, shows a recovery token once, and keeps it out of the cache, storage and the page after Done or leaving', async () => {
    const fetchMock = renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, LIST),
      [`POST ${BASE}/123/recovery-token`]: () => jsonResponse(201, RECOVERED),
      'GET /api/admin/libraries/acme': () =>
        jsonResponse(200, {
          timezone: 'America/Chicago', slug: 'acme', name: 'Acme Library', status: 'active', primary_branch: null,
          operational_identity: 'assigned', subscription: null, has_active_owner: true, sorters: [],
        }),
    })
    const person = userEvent.setup()
    const writeText = vi.fn(() => Promise.resolve())
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    await loaded()

    await person.click(within(card(123)).getByRole('button', { name: 'Issue recovery token' }))
    expect(within(card(123)).getByText(/A request already in progress may still finish/)).toBeInTheDocument()
    expect(bodies(fetchMock, 'POST', `${BASE}/123/recovery-token`)).toEqual([])
    await person.click(within(card(123)).getByRole('button', { name: 'Confirm: Issue recovery token' }))

    expect(await screen.findByText(CANARY_TOKEN)).toBeInTheDocument()
    expect(screen.getByText(/^Shown only now/)).toHaveTextContent('tools\\set-api-token.ps1')
    expect(screen.getByText(/earlier agent token no longer authenticates new requests/)).toBeInTheDocument()
    await person.click(screen.getByRole('button', { name: 'Copy' }))
    expect(writeText).toHaveBeenCalledWith(CANARY_TOKEN)
    expect(bodies(fetchMock, 'POST', `${BASE}/123/recovery-token`)).toEqual([undefined])

    const cached = clients.flatMap((client) => [
      ...client.getQueryCache().getAll().map((query) => JSON.stringify(query.state.data ?? null)),
      ...client.getMutationCache().getAll().map((mutation) => JSON.stringify(mutation.state.data ?? null)),
    ])
    expect(cached.length).toBeGreaterThan(0)
    expect(cached.join('')).not.toContain(CANARY_TOKEN)
    expect(JSON.stringify({ ...localStorage })).not.toContain(CANARY_TOKEN)
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(CANARY_TOKEN)

    await person.click(screen.getByRole('button', { name: 'Done' }))
    expect(document.body.textContent).not.toContain(CANARY_TOKEN)

    await person.click(within(card(123)).getByRole('button', { name: 'Issue recovery token' }))
    await person.click(within(card(123)).getByRole('button', { name: 'Confirm: Issue recovery token' }))
    expect(await screen.findByText(CANARY_TOKEN)).toBeInTheDocument()
    await person.click(screen.getByRole('link', { name: 'Acme Library' }))
    await screen.findByRole('link', { name: 'Installations' })
    expect(document.body.textContent).not.toContain(CANARY_TOKEN)
    await person.click(screen.getByRole('link', { name: 'Installations' }))
    await loaded()
    expect(document.body.textContent).not.toContain(CANARY_TOKEN)
  })

  it('says why a recovery token was refused, and shows none', async () => {
    const message = "A recovery token needs a Provisioning or Active Contract 2 installation that owns its branch's only active ingest key."
    renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, LIST),
      [`POST ${BASE}/123/recovery-token`]: () => jsonResponse(409, { code: 'installation_not_recoverable', message }),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(within(card(123)).getByRole('button', { name: 'Issue recovery token' }))
    await person.click(within(card(123)).getByRole('button', { name: 'Confirm: Issue recovery token' }))

    expect(await within(card(123)).findByRole('alert')).toHaveTextContent(`Nothing was changed. ${message}`)
    expect(screen.queryByRole('heading', { name: /^Recovery token for/ })).not.toBeInTheDocument()
  })

  it('lists unbound tokens only when asked, and revokes one after a warning that it is not an upload fence', async () => {
    let revoked = false
    const fetchMock = renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, LIST),
      [`GET ${BASE}/unbound-tokens`]: () => jsonResponse(200, { tokens: revoked ? [] : [UNBOUND] }),
      [`POST ${BASE}/unbound-tokens/55/revoke`]: () => {
        revoked = true
        return jsonResponse(200, { id: 55 })
      },
    })
    const person = userEvent.setup()
    await loaded()
    expect(requestedUrls(fetchMock)).toEqual(['/api/admin/session', BASE])

    await person.click(screen.getByRole('button', { name: 'Show legacy unbound tokens' }))
    expect(await screen.findByRole('heading', { level: 4, name: 'Token #55 — NBPL canonical agent' })).toBeInTheDocument()

    await person.click(screen.getByRole('button', { name: 'Revoke' }))
    expect(screen.getByText(/A request already in progress may still finish, so this is not an upload fence/)).toBeInTheDocument()
    expect(bodies(fetchMock, 'POST', `${BASE}/unbound-tokens/55/revoke`)).toEqual([])
    await person.click(screen.getByRole('button', { name: 'Confirm: Revoke' }))

    expect(await screen.findByText('This library has no active unbound tokens.')).toBeInTheDocument()
    expect(bodies(fetchMock, 'POST', `${BASE}/unbound-tokens/55/revoke`)).toEqual([undefined])
  })

  it('says why a revocation was refused, and keeps the token listed', async () => {
    renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, LIST),
      [`GET ${BASE}/unbound-tokens`]: () => jsonResponse(200, { tokens: [UNBOUND] }),
      [`POST ${BASE}/unbound-tokens/55/revoke`]: () => jsonResponse(404, { code: 'token_not_found', message: 'Unbound token not found.' }),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(screen.getByRole('button', { name: 'Show legacy unbound tokens' }))
    await person.click(await screen.findByRole('button', { name: 'Revoke' }))
    await person.click(screen.getByRole('button', { name: 'Confirm: Revoke' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Nothing was changed. Unbound token not found.')
    expect(screen.getByRole('heading', { level: 4, name: /^Token #55/ })).toBeInTheDocument()
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
          operational_identity: 'assigned', subscription: null, has_active_owner: true, sorters: [],
        }),
    })
    const person = userEvent.setup()

    await person.click(await screen.findByRole('link', { name: 'Installations' }))

    expect(await screen.findByRole('heading', { level: 2, name: 'Installations' })).toBeInTheDocument()
  })

  it('shows Libraries / the library / Installations, and its library step leads back with focus on the overview heading', async () => {
    renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, LIST),
      'GET /api/admin/libraries/acme': () =>
        jsonResponse(200, {
          timezone: 'America/Chicago', slug: 'acme', name: 'Acme Library', status: 'active', primary_branch: null,
          operational_identity: 'assigned', subscription: null, has_active_owner: true, sorters: [],
        }),
    })
    await loaded()
    const nav = screen.getByRole('navigation', { name: 'Breadcrumb' })

    expect(within(nav).getAllByRole('listitem').map((step) => step.textContent)).toEqual(['Libraries', 'Acme Library', 'Installations'])
    expect(within(nav).getAllByRole('link').map((link) => link.getAttribute('href'))).toEqual(['/libraries', '/libraries/acme'])
    expect(within(nav).getByText('Installations')).toHaveAttribute('aria-current', 'page')

    await userEvent.setup().click(within(nav).getByRole('link', { name: 'Acme Library' }))
    const overview = await screen.findByRole('heading', { level: 2, name: 'Acme Library' })
    await waitFor(() => expect(overview).toHaveFocus())
  })
})

// =====================================================================================================================
// machine replacement (C6a R3, docs/c6a-replacement-machine-design.md 14.11)
// =====================================================================================================================

const ENABLED = { ...LIST, replacement_enabled: true }
const PENDING = {
  ...ACTIVE,
  id: 126,
  name: 'New AMH PC',
  status: 'provisioning',
  installed_at: null,
  last_seen_at: null,
  created_at: '2026-11-12T15:00:00Z',
  last_code_redeemed_at: null,
  replaces_installation_id: 123,
}
const WITH_PENDING = { ...ENABLED, pending_replacement_id: 126, installations: [ACTIVE, PENDING] }
const AUTHORIZED = { installation: PENDING, replaces_installation_id: 123 }
const REDEEMED = {
  ...ENABLED,
  installations: [
    { ...ACTIVE, status: 'retired' },
    { ...PENDING, last_code_redeemed_at: '2026-11-12T16:00:00Z' },
  ],
}

/** A list endpoint that answers each given body in turn, then the last one again. */
function lists(...answers: unknown[]): () => Response {
  let calls = 0
  return () => jsonResponse(200, answers[Math.min(calls++, answers.length - 1)])
}

function listCalls(mock: FetchMock): number {
  return requestedUrls(mock).filter((url) => url === BASE).length
}

function replacementOf(id: number): HTMLElement | null {
  const term = within(card(id)).queryByText('Replacement', { selector: 'dt' })
  return term?.nextElementSibling as HTMLElement | null
}

describe('Machine replacement', () => {
  it('offers Replace only on an enrolled Contract 2 installation at the primary branch, and only while the server allows it', async () => {
    const neverEnrolled = { ...ACTIVE, id: 127, name: 'Unused', status: 'provisioning', last_code_redeemed_at: null }
    const elsewhere = { ...ACTIVE, id: 128, name: 'East', branch: { slug: 'east', name: 'East Branch', is_primary: false, status: 'active' } }
    renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, { ...ENABLED, installations: [ACTIVE, INACTIVE, RETIRED, neverEnrolled, elsewhere] }),
    })
    await loaded()

    expect(buttons(card(123))).toContain('Replace this Collector computer')
    for (const id of [124, 125, 127, 128]) {
      expect(buttons(card(id))).not.toContain('Replace this Collector computer')
    }
    expect(screen.queryByText(/Machine replacement is turned off/)).not.toBeInTheDocument()
  })

  it('offers no replacement while the server has it turned off, and says so', async () => {
    renderAt('/libraries/acme/installations', { [`GET ${BASE}`]: () => jsonResponse(200, LIST) })
    await loaded()

    expect(screen.queryByRole('button', { name: 'Replace this Collector computer' })).not.toBeInTheDocument()
    expect(screen.getByText('Machine replacement is turned off on this server. A pending replacement can still be cancelled.')).toBeInTheDocument()
  })

  it('authorizes after stating the consequences, announces it, refreshes, and moves focus to the new installation', async () => {
    const fetchMock = renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: lists(ENABLED, WITH_PENDING),
      [`POST ${BASE}/123/replacements`]: () => jsonResponse(201, AUTHORIZED),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(within(card(123)).getByRole('button', { name: 'Replace this Collector computer' }))
    const form = within(card(123)).getByRole('form', { name: 'Replace Installation #123' })
    const consequences = within(form).getByText(/^Authorizing adds a new Provisioning installation/)
    expect(form).toHaveAccessibleDescription(consequences.textContent ?? '')
    expect(consequences).toHaveTextContent('It does not disable this computer: Installation #123 keeps running and uploading')
    expect(consequences).toHaveTextContent("until the replacement's enrollment code is used in SortView Setup. Only then are this computer's agent token and ingest key retired, for good.")
    expect(consequences).toHaveTextContent('may have a gap')
    expect(consequences).toHaveTextContent('Verify that the new computer is Active and uploading before cleaning up this one.')
    expect(within(form).queryByRole('checkbox')).not.toBeInTheDocument() // an Active source needs no confirmation

    await person.click(within(form).getByRole('button', { name: 'Confirm: Authorize replacement' }))
    expect(within(form).getByText('Enter a name.')).toBeInTheDocument()
    expect(within(form).getByLabelText('Name for the replacement computer')).toHaveAttribute('aria-invalid', 'true')
    expect(bodies(fetchMock, 'POST', `${BASE}/123/replacements`)).toEqual([])

    await person.type(within(form).getByLabelText('Name for the replacement computer'), 'New AMH PC')
    await person.click(within(form).getByRole('button', { name: 'Confirm: Authorize replacement' }))

    expect(await screen.findByRole('status')).toHaveTextContent(
      'Replacement authorized: Installation #126 — New AMH PC will replace Installation #123. Nothing has changed on Installation #123 yet.',
    )
    expect(bodies(fetchMock, 'POST', `${BASE}/123/replacements`)).toEqual([{ name: 'New AMH PC', confirm_lost_response: false }])
    const heading = await screen.findByRole('heading', { level: 3, name: /^Installation #126 / })
    await waitFor(() => expect(heading).toHaveFocus())
    expect(replacementOf(126)).toHaveTextContent('Pending replacement for Installation #123')
    expect(replacementOf(123)).toHaveTextContent('Being replaced by Installation #126')
    expect(listCalls(fetchMock)).toBe(2)
  })

  it('shows a pending replacement with its own actions: a code, Cancel replacement, and no Retire or recovery token', async () => {
    renderAt('/libraries/acme/installations', { [`GET ${BASE}`]: () => jsonResponse(200, WITH_PENDING) })
    await loaded()

    expect(buttons(card(126))).toEqual(['Rename', 'Deactivate', 'Issue enrollment code', 'Cancel replacement'])
    expect(buttons(card(123))).not.toContain('Replace this Collector computer') // one pending replacement at a time
    expect(within(card(126)).getByText('No unused code. Issue one when the replacement computer is ready.')).toBeInTheDocument()
  })

  it('while replacement is off, a pending replacement gets no code but can still be cancelled', async () => {
    renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, { ...WITH_PENDING, replacement_enabled: false }),
    })
    await loaded()

    expect(buttons(card(126))).toEqual(['Rename', 'Deactivate', 'Cancel replacement'])
    expect(within(card(126)).getByText(/^Machine replacement is turned off on this server, so this replacement cannot be given a code/)).toBeInTheDocument()
  })

  it('requires the lost-response confirmation for a Provisioning source, and sends it', async () => {
    const provisioningSource = { ...ACTIVE, status: 'provisioning', last_seen_at: null, installed_at: null }
    const fetchMock = renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: lists({ ...ENABLED, installations: [provisioningSource] }, WITH_PENDING),
      [`POST ${BASE}/123/replacements`]: () => jsonResponse(201, AUTHORIZED),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(within(card(123)).getByRole('button', { name: 'Replace this Collector computer' }))
    const form = within(card(123)).getByRole('form', { name: 'Replace Installation #123' })
    await person.type(within(form).getByLabelText('Name for the replacement computer'), 'New AMH PC')
    await person.click(within(form).getByRole('button', { name: 'Confirm: Authorize replacement' }))

    const checkbox = within(form).getByRole('checkbox', { name: /SortView Setup reported an unknown result and offered no Resume setup/ })
    expect(checkbox).toHaveAttribute('aria-invalid', 'true')
    expect(checkbox).toHaveAccessibleDescription('Confirm that this installation’s enrollment response was lost.')
    expect(bodies(fetchMock, 'POST', `${BASE}/123/replacements`)).toEqual([])

    await person.click(checkbox)
    await person.click(within(form).getByRole('button', { name: 'Confirm: Authorize replacement' }))

    await screen.findByRole('status')
    expect(bodies(fetchMock, 'POST', `${BASE}/123/replacements`)).toEqual([{ name: 'New AMH PC', confirm_lost_response: true }])
  })

  it('asks for the confirmation when the server says the source became Provisioning meanwhile, keeping the name', async () => {
    const message = 'This installation is still Provisioning. Confirm that its enrollment response was lost and setup cannot resume (try Resume setup and the recovery token first).'
    const fetchMock = renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, ENABLED),
      [`POST ${BASE}/123/replacements`]: () => jsonResponse(409, { code: 'lost_response_confirmation_required', message }),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(within(card(123)).getByRole('button', { name: 'Replace this Collector computer' }))
    const form = within(card(123)).getByRole('form', { name: 'Replace Installation #123' })
    await person.type(within(form).getByLabelText('Name for the replacement computer'), 'New AMH PC')
    await person.click(within(form).getByRole('button', { name: 'Confirm: Authorize replacement' }))

    expect(await within(card(123)).findByRole('alert')).toHaveTextContent(`Nothing was changed. ${message}`)
    expect(within(form).getByRole('checkbox')).not.toBeChecked()
    expect(within(form).getByLabelText('Name for the replacement computer')).toHaveValue('New AMH PC')
    await waitFor(() => expect(listCalls(fetchMock)).toBe(2)) // the current state is read again
  })

  it.each([
    ['replacement_already_pending', 'This branch already has a pending replacement. Cancel it first.'],
    ['unbound_tokens_active', 'This branch still has an active legacy unbound token. Revoke it before authorizing a replacement.'],
    ['replacement_source_not_key_owner', "Only the installation that owns its branch's only active ingest key can be replaced."],
    ['replacement_disabled', 'Machine replacement is turned off on this server. A pending replacement can still be cancelled.'],
  ])('says why authorization was refused (%s), keeps the form, and reads the list again', async (code, message) => {
    const fetchMock = renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, ENABLED),
      [`POST ${BASE}/123/replacements`]: () => jsonResponse(409, { code, message }),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(within(card(123)).getByRole('button', { name: 'Replace this Collector computer' }))
    const form = within(card(123)).getByRole('form', { name: 'Replace Installation #123' })
    await person.type(within(form).getByLabelText('Name for the replacement computer'), 'New AMH PC')
    await person.click(within(form).getByRole('button', { name: 'Confirm: Authorize replacement' }))

    expect(await within(card(123)).findByRole('alert')).toHaveTextContent(`Nothing was changed. ${message}`)
    expect(within(card(123)).getByRole('form', { name: 'Replace Installation #123' })).toBeInTheDocument()
    await waitFor(() => expect(listCalls(fetchMock)).toBe(2))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('shows a refused name against the field', async () => {
    renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, ENABLED),
      [`POST ${BASE}/123/replacements`]: () =>
        jsonResponse(422, { code: 'invalid_installation', message: 'The installation is not valid.', problems: [{ field: 'name', code: 'too_long' }] }),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(within(card(123)).getByRole('button', { name: 'Replace this Collector computer' }))
    const form = within(card(123)).getByRole('form', { name: 'Replace Installation #123' })
    await person.type(within(form).getByLabelText('Name for the replacement computer'), 'x')
    await person.click(within(form).getByRole('button', { name: 'Confirm: Authorize replacement' }))

    expect(await within(form).findByText('Enter a name of at most 120 characters.')).toBeInTheDocument()
    expect(within(form).getByLabelText('Name for the replacement computer')).toHaveAccessibleDescription('Enter a name of at most 120 characters.')
  })

  it('issues the replacement code through the ordinary endpoint, with its own instructions, shown once and kept out of every cache', async () => {
    const fetchMock = renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, WITH_PENDING),
      [`POST ${BASE}/126/enrollment-codes`]: () =>
        jsonResponse(201, { ...ISSUED, installation: { id: 126, name: 'New AMH PC' } }),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(within(card(126)).getByRole('button', { name: 'Issue enrollment code' }))

    expect(await screen.findByText(CANARY_CODE)).toBeInTheDocument()
    expect(bodies(fetchMock, 'POST', `${BASE}/126/enrollment-codes`)).toEqual([undefined])
    const instructions = screen.getByText(/^For the replacement computer:/)
    expect(instructions).toHaveTextContent('Installation #123 keeps running until then.')
    expect(instructions).toHaveTextContent('When Setup accepts the code, Installation #123 stops being accepted: its agent token and ingest key are retired for good.')
    expect(instructions).toHaveTextContent('tools\\uninstall.ps1')
    const cached = clients.flatMap((client) => [
      ...client.getQueryCache().getAll().map((query) => JSON.stringify(query.state.data ?? null)),
      ...client.getMutationCache().getAll().map((mutation) => JSON.stringify(mutation.state.data ?? null)),
    ])
    expect(cached.join('')).not.toContain(CANARY_CODE)
    expect(JSON.stringify({ ...localStorage })).not.toContain(CANARY_CODE)
    expect(JSON.stringify({ ...sessionStorage })).not.toContain(CANARY_CODE)
    expect(requestedUrls(fetchMock).join(' ')).not.toContain(CANARY_CODE)
    expect(window.location.href).not.toContain(CANARY_CODE)

    await person.click(screen.getByRole('button', { name: 'Done' }))
    expect(document.body.textContent).not.toContain(CANARY_CODE)
  })

  it('an ordinary code has no replacement instructions', async () => {
    renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () => jsonResponse(200, ENABLED),
      [`POST ${BASE}/123/enrollment-codes`]: () => jsonResponse(201, ISSUED),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(within(card(123)).getByRole('button', { name: 'Issue enrollment code' }))

    expect(await screen.findByText(CANARY_CODE)).toBeInTheDocument()
    expect(screen.queryByText(/^For the replacement computer:/)).not.toBeInTheDocument()
  })

  it('cancels after a warning that the source is unaffected, announces it, and keeps focus on the cancelled installation', async () => {
    const cancelledPending = { ...PENDING, status: 'retired' }
    const fetchMock = renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: lists(WITH_PENDING, { ...ENABLED, installations: [ACTIVE, cancelledPending] }),
      [`POST ${BASE}/126/cancel-replacement`]: () => jsonResponse(200, { installation: cancelledPending, replaces_installation_id: 123 }),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(within(card(126)).getByRole('button', { name: 'Cancel replacement' }))
    const group = within(card(126)).getByRole('group', { name: 'Cancel the replacement Installation #126' })
    expect(within(group).getByText(/^Cancel this replacement\?/)).toHaveTextContent(
      'Installation #126 is retired and its unused enrollment codes stop working. Installation #123 keeps running unchanged.',
    )
    await person.click(within(group).getByRole('button', { name: 'Keep replacement' }))
    expect(bodies(fetchMock, 'POST', `${BASE}/126/cancel-replacement`)).toEqual([])

    await person.click(within(card(126)).getByRole('button', { name: 'Cancel replacement' }))
    await person.click(within(card(126)).getByRole('button', { name: 'Confirm: Cancel replacement' }))

    expect(await screen.findByRole('status')).toHaveTextContent(
      'Replacement cancelled: Installation #126 is retired and its unused enrollment codes no longer work. Installation #123 is unchanged.',
    )
    expect(bodies(fetchMock, 'POST', `${BASE}/126/cancel-replacement`)).toEqual([undefined])
    await waitFor(() => expect(replacementOf(126)).toHaveTextContent('Cancelled replacement for Installation #123'))
    expect(screen.getByRole('heading', { level: 3, name: /^Installation #126 / })).toHaveFocus()
    expect(buttons(card(126))).toEqual(['Rename'])
    expect(buttons(card(123))).toContain('Replace this Collector computer')
  })

  it('when the replacement was redeemed meanwhile, says cancelling was refused and shows the new state', async () => {
    const message = 'This installation is not a pending replacement.'
    renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: lists(WITH_PENDING, REDEEMED),
      [`POST ${BASE}/126/cancel-replacement`]: () => jsonResponse(409, { code: 'not_a_pending_replacement', message }),
    })
    const person = userEvent.setup()
    await loaded()

    await person.click(within(card(126)).getByRole('button', { name: 'Cancel replacement' }))
    await person.click(within(card(126)).getByRole('button', { name: 'Confirm: Cancel replacement' }))

    expect(await within(card(126)).findByRole('alert')).toHaveTextContent(`Nothing was changed. ${message}`)
    await waitFor(() => expect(replacementOf(126)).toHaveTextContent('Replaces Installation #123'))
    expect(replacementOf(123)).toHaveTextContent('Replaced by Installation #126')
    expect(within(card(123)).getByText('Retired')).toBeInTheDocument()
    expect(buttons(card(126))).not.toContain('Cancel replacement')
  })

  it('shows a chain: an installation that replaced one computer and was itself replaced', async () => {
    const first = { ...ACTIVE, id: 101, name: 'First', status: 'retired' }
    const second = { ...ACTIVE, id: 102, name: 'Second', status: 'retired', replaces_installation_id: 101 }
    const third = { ...ACTIVE, id: 103, name: 'Third', replaces_installation_id: 102 }
    renderAt('/libraries/acme/installations', { [`GET ${BASE}`]: () => jsonResponse(200, { ...ENABLED, installations: [first, second, third] }) })
    await loaded()

    expect(replacementOf(101)).toHaveTextContent('Replaced by Installation #102')
    expect(replacementOf(102)).toHaveTextContent('Replaces Installation #101; Replaced by Installation #103')
    expect(replacementOf(103)).toHaveTextContent('Replaces Installation #102')
    expect(buttons(card(103))).toContain('Replace this Collector computer')
  })

  it('a cancelled library shows its replacements and offers nothing', async () => {
    renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () =>
        jsonResponse(200, { ...WITH_PENDING, library: { ...LIST.library, status: 'cancelled' }, add_problems: ['library_cancelled'] }),
    })
    await loaded()

    expect(replacementOf(126)).toHaveTextContent('Pending replacement for Installation #123')
    expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual(['Log out'])
  })
})

describe('accessibility', () => {
  it('axe finds no violation on the installations page', async () => {
    renderAt('/libraries/acme/installations', { [`GET ${BASE}`]: () => jsonResponse(200, LIST) })
    await loaded()
    expect(await axeViolations()).toEqual([])
  })

  it('axe finds no violation with a pending replacement, the replacement form with its confirmation, and the cancel warning', async () => {
    const provisioningSource = { ...ACTIVE, id: 129, name: 'Lost Response', status: 'provisioning' }
    renderAt('/libraries/acme/installations', {
      [`GET ${BASE}`]: () =>
        jsonResponse(200, { ...WITH_PENDING, pending_replacement_id: 126, installations: [ACTIVE, PENDING, provisioningSource] }),
    })
    const person = userEvent.setup()
    await loaded()
    await person.click(within(card(126)).getByRole('button', { name: 'Cancel replacement' }))
    expect(await axeViolations()).toEqual([])
  })

  it('axe finds no violation with the replacement form and its confirmation open', async () => {
    const provisioningSource = { ...ACTIVE, status: 'provisioning' }
    renderAt('/libraries/acme/installations', { [`GET ${BASE}`]: () => jsonResponse(200, { ...ENABLED, installations: [provisioningSource] }) })
    const person = userEvent.setup()
    await loaded()
    await person.click(within(card(123)).getByRole('button', { name: 'Replace this Collector computer' }))
    await person.click(within(card(123)).getByRole('button', { name: 'Confirm: Authorize replacement' }))
    expect(within(card(123)).getByRole('checkbox')).toBeInTheDocument()
    expect(await axeViolations()).toEqual([])
  })
})
