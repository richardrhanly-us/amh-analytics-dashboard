import type { QueryClient } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ALICE, jsonResponse, serveApi, type ApiRoutes, type FetchMock } from '../../../src/test/http.ts'
import { axeViolations } from '../../../src/test/axe.ts'
import { AdminApp } from '../AdminApp.tsx'

// Every query client the admin app makes, so a test can see what it invalidates.
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
const DETAIL_URL = '/api/admin/libraries/acme'
const BASE = '/api/admin/libraries/acme/lifecycle'

function detail(status: string) {
  return {
    timezone: 'America/Chicago',
    slug: 'acme',
    name: 'Acme Library',
    status,
    primary_branch: { slug: 'main', name: 'Main Branch', status: 'active' },
    operational_identity: 'assigned',
    subscription: { plan_code: 'starter', plan_name: 'Starter', status: 'trial' },
    has_active_owner: true,
    sorters: [],
  }
}

function answer(status: string, previous: string, changed = true) {
  return { library: { slug: 'acme', name: 'Acme Library', status }, previous_status: previous, changed }
}

const COUNTS = { agent_tokens_deactivated: 2, installations_retired: 1, enrollment_codes_revoked: 1, ingest_keys_retired: 1, sessions_revoked: 3 }

afterEach(() => {
  clients.length = 0
})

function renderAt(routes: ApiRoutes, path = '/libraries/acme/lifecycle') {
  const fetchMock = serveApi({ ...SESSION, ...routes })
  render(
    <MemoryRouter initialEntries={[path]}>
      <AdminApp />
    </MemoryRouter>,
  )
  return fetchMock
}

/** A detail route whose status the test can change, as the server's would after an action. */
function library(initial: string) {
  const state = { status: initial }
  return { state, route: { [`GET ${DETAIL_URL}`]: () => jsonResponse(200, detail(state.status)) } }
}

function posts(mock: FetchMock): { url: string; body: unknown }[] {
  return mock.mock.calls
    .filter(([, init]) => init?.method === 'POST')
    .map(([url, init]) => ({ url: String(url), body: init?.body === undefined ? undefined : (JSON.parse(String(init.body)) as unknown) }))
}

async function loaded() {
  await screen.findByRole('heading', { level: 3, name: 'Status' })
}

function actionButtons(): string[] {
  return screen
    .getAllByRole('button')
    .map((button) => button.textContent ?? '')
    .filter((name) => name !== 'Log out')
}

const FORBIDDEN_CONTROLS = /purge|delete data|un-?offboard|restore|undo/i

describe('Library lifecycle', () => {
  it.each([
    ['active', 'Active', ['Suspend library', 'Offboard library permanently']],
    ['trial', 'Trial', ['Suspend library', 'Offboard library permanently']],
    ['suspended', 'Suspended', ['Reactivate library', 'Offboard library permanently']],
    ['cancelled', 'Cancelled', []],
  ])('offers a %s library only its own actions', async (status, label, expected) => {
    renderAt(library(status).route)
    await loaded()

    expect(screen.getByRole('heading', { level: 2, name: 'Library lifecycle' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Acme Library' })).toHaveAttribute('href', '/libraries/acme')
    expect(within(screen.getByRole('region', { name: 'Status' })).getByText(label)).toBeInTheDocument()
    expect(actionButtons()).toEqual(expected)
    for (const button of screen.getAllByRole('button')) {
      expect(button.textContent).not.toMatch(FORBIDDEN_CONTROLS)
    }
  })

  it('shows a cancelled library as terminal, with no action and no offboarding form', async () => {
    renderAt(library('cancelled').route)
    await loaded()

    expect(screen.getByText('Cancelled — permanently offboarded. This cannot be undone in SortView administration.')).toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Offboard permanently' })).not.toBeInTheDocument()
  })

  it('suspends only after a confirmation that says members keep read-only access', async () => {
    const person = userEvent.setup()
    const { state, route } = library('active')
    const fetchMock = renderAt({
      ...route,
      [`POST ${BASE}/suspend`]: () => {
        state.status = 'suspended'
        return jsonResponse(200, answer('suspended', 'active'))
      },
    })
    await loaded()

    await person.click(screen.getByRole('button', { name: 'Suspend library' }))
    const confirm = screen.getByRole('group', { name: 'Suspend library' })
    expect(confirm.textContent).toMatch(/reversible/)
    expect(confirm.textContent).toMatch(/Collector uploads and enrollment stop/)
    expect(confirm.textContent).toMatch(/customer changes are blocked/)
    expect(confirm.textContent).toMatch(/Agent tokens, installations, ingest keys and settings are kept/)
    expect(confirm.textContent).toMatch(/Members keep read-only access to their dashboard and history/)
    expect(confirm.textContent).not.toMatch(/locked out|cannot sign in/i)
    expect(posts(fetchMock)).toEqual([])

    await person.click(within(confirm).getByRole('button', { name: 'Confirm: Suspend library' }))

    expect(await screen.findByText('Library suspended.')).toBeInTheDocument()
    expect(posts(fetchMock)).toEqual([{ url: `${BASE}/suspend`, body: undefined }])
    expect(within(screen.getByRole('region', { name: 'Status' })).getByText('Suspended')).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Reactivate library' })).toBeInTheDocument()
  })

  it('cancelling a confirmation sends nothing', async () => {
    const person = userEvent.setup()
    const fetchMock = renderAt(library('active').route)
    await loaded()

    await person.click(screen.getByRole('button', { name: 'Suspend library' }))
    await person.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(posts(fetchMock)).toEqual([])
    expect(screen.getByRole('button', { name: 'Suspend library' })).toBeInTheDocument()
  })

  it('reactivates after a confirmation that says the status becomes Active and Trial is not restored', async () => {
    const person = userEvent.setup()
    const { state, route } = library('suspended')
    const fetchMock = renderAt({
      ...route,
      [`POST ${BASE}/reactivate`]: () => {
        state.status = 'active'
        return jsonResponse(200, answer('active', 'suspended'))
      },
    })
    await loaded()
    expect(screen.getByText(/Members can still read their dashboard and history/)).toBeInTheDocument()

    await person.click(screen.getByRole('button', { name: 'Reactivate library' }))
    const confirm = screen.getByRole('group', { name: 'Reactivate library' })
    expect(confirm.textContent).toMatch(/Its status becomes Active; a previous Trial label is not restored/)
    expect(confirm.textContent).toMatch(/Normal customer access and Collector authentication return/)
    await person.click(within(confirm).getByRole('button', { name: 'Confirm: Reactivate library' }))

    expect(await screen.findByText('Library reactivated.')).toBeInTheDocument()
    expect(posts(fetchMock)).toEqual([{ url: `${BASE}/reactivate`, body: undefined }])
    expect(within(screen.getByRole('region', { name: 'Status' })).getByText('Active')).toBeInTheDocument()
  })

  it('warns that offboarding is permanent and not a purge, and needs the exact slug typed', async () => {
    const person = userEvent.setup()
    const fetchMock = renderAt(library('active').route)
    await loaded()

    const section = screen.getByRole('region', { name: 'Offboard permanently' })
    const warning = section.textContent ?? ''
    expect(warning).toMatch(/Permanent in SortView administration/)
    for (const effect of [/deactivates its agent tokens/, /retires its Collector installations/, /revokes its unused enrollment codes/,
      /retires its active ingest keys/, /signs out customers left with no other library/]) {
      expect(warning).toMatch(effect)
    }
    expect(warning).toMatch(/does not delete customer data, memberships, subscriptions or settings/)
    expect(warning).toMatch(/does not remove v2 cutovers/)
    expect(warning).toMatch(/This is not a data purge\./)

    const button = within(section).getByRole('button', { name: 'Offboard library permanently' })
    const input = within(section).getByRole('textbox', { name: "Type the library's slug (acme) to confirm" })
    expect(button).toBeDisabled()
    for (const typed of ['acm', 'ACME', ' acme', 'acme ', 'Acme Library']) {
      await person.clear(input)
      await person.type(input, typed)
      expect(button).toBeDisabled()
    }
    await person.clear(input)
    await person.type(input, 'acme')
    expect(button).toBeEnabled()
    expect(posts(fetchMock)).toEqual([])
  })

  it('offboards with the typed slug, shows counts only and then the terminal state', async () => {
    const person = userEvent.setup()
    const { state, route } = library('suspended')
    const fetchMock = renderAt({
      ...route,
      [`POST ${BASE}/offboard`]: () => {
        state.status = 'cancelled'
        return jsonResponse(200, { ...answer('cancelled', 'suspended'), counts: COUNTS })
      },
    })
    await loaded()

    await person.type(screen.getByRole('textbox'), 'acme')
    await person.click(screen.getByRole('button', { name: 'Offboard library permanently' }))

    expect(
      await screen.findByText(
        'Library offboarded: 2 agent tokens deactivated, 1 installation retired, 1 enrollment code revoked, 1 ingest key retired, 3 sessions revoked.',
      ),
    ).toBeInTheDocument()
    expect(posts(fetchMock)).toEqual([{ url: `${BASE}/offboard`, body: { confirm_slug: 'acme' } }])
    expect(screen.getByText('Cancelled — permanently offboarded. This cannot be undone in SortView administration.')).toBeInTheDocument()
    expect(actionButtons()).toEqual([])
  })

  it('invalidates the library, the list and the installations after an action', async () => {
    const person = userEvent.setup()
    const { state, route } = library('active')
    renderAt({
      ...route,
      [`POST ${BASE}/suspend`]: () => {
        state.status = 'suspended'
        return jsonResponse(200, answer('suspended', 'active'))
      },
    })
    await loaded()
    const client = clients[clients.length - 1]
    const invalidate = vi.spyOn(client, 'invalidateQueries')

    await person.click(screen.getByRole('button', { name: 'Suspend library' }))
    await person.click(screen.getByRole('button', { name: 'Confirm: Suspend library' }))
    await screen.findByText('Library suspended.')

    const keys = invalidate.mock.calls.map(([filters]) => JSON.stringify(filters?.queryKey))
    expect(keys).toEqual(
      expect.arrayContaining([
        JSON.stringify(['admin', 'library', 'acme']),
        JSON.stringify(['admin', 'libraries']),
        JSON.stringify(['admin', 'installations', 'acme']),
      ]),
    )
  })

  it.each([
    [409, 'library_cancelled', 'This library is cancelled; its lifecycle cannot be changed.'],
    [409, 'invalid_transition', 'This library cannot be changed from its current status.'],
    [404, 'library_not_found', 'Library not found.'],
  ])('shows a %s %s refusal as its fixed message and changes nothing', async (status, code, message) => {
    const person = userEvent.setup()
    renderAt({ ...library('active').route, [`POST ${BASE}/suspend`]: () => jsonResponse(status, { code, message }) })
    await loaded()

    await person.click(screen.getByRole('button', { name: 'Suspend library' }))
    await person.click(screen.getByRole('button', { name: 'Confirm: Suspend library' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(`Nothing was changed. ${message}`)
    expect(screen.queryByText('Library suspended.')).not.toBeInTheDocument()
  })

  it('shows a 422 confirmation refusal from the server without echoing what was typed', async () => {
    const person = userEvent.setup()
    renderAt({
      ...library('active').route,
      [`POST ${BASE}/offboard`]: () =>
        jsonResponse(422, { code: 'confirmation_mismatch', message: "The confirmation does not match this library's slug." }),
    })
    await loaded()

    await person.type(screen.getByRole('textbox'), 'acme')
    await person.click(screen.getByRole('button', { name: 'Offboard library permanently' }))

    expect(await screen.findByRole('alert')).toHaveTextContent("Nothing was changed. The confirmation does not match this library's slug.")
  })

  it('says when there is no such library', async () => {
    renderAt({ [`GET ${DETAIL_URL}`]: () => jsonResponse(404, { code: 'library_not_found', message: 'Library not found.' }) })

    expect(await screen.findByRole('heading', { level: 2, name: 'Library not found' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Libraries' })).toHaveAttribute('href', '/libraries')
    expect(screen.queryByRole('button', { name: /Suspend|Reactivate|Offboard/ })).not.toBeInTheDocument()
  })
})

describe('accessibility', () => {
  it('axe finds no violation on the lifecycle page of an active library', async () => {
    renderAt(library('active').route)
    await loaded()
    expect(await axeViolations()).toEqual([])
  })
})
