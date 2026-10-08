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
  NORTHBRIDGE,
  NORTHBRIDGE_DETAIL,
  NOT_AUTHENTICATED,
  ORGANIZATION_NOT_FOUND,
  serveApi,
} from '../test/http.ts'
import { renderApp } from '../test/render.tsx'

const ORG = '/api/organizations/northbridge'
const ROUTING = `${ORG}/settings/routing`
const PAGE = '/organizations/northbridge/settings/routing'
// No pause between keystrokes: nothing here depends on one.
const INSTANT = { delay: null }

const STORED = { home_branch_label: 'Central', destinations: [{ label: 'North Annex', enabled: true }, { label: 'Depot', enabled: false }] }
const NOTICE =
  'Changes apply to every report as soon as you save, including reports for past dates. Past check-ins are not changed; they are grouped by the labels as they are now. Check-ins whose label no longer matches appear under “Other routing”.'
const refused = (status: number, body: unknown) => () => jsonResponse(status, body)
const invalid = (...problems: Array<[string, string]>) =>
  refused(422, { code: 'invalid_routing_settings', message: 'The routing settings are not valid.', problems: problems.map(([field, code]) => ({ field, code })) })

interface World {
  role?: string
  access_mode?: string
  routing?: unknown
}

/** Alice signed in, Northbridge with the role and state given, and its stored routing. */
function serve(world: World = {}, overrides: ApiRoutes = {}): FetchMock {
  const { role = 'admin', access_mode = 'full', routing = STORED } = world
  return serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    'GET /api/organizations': () => jsonResponse(200, [{ ...NORTHBRIDGE, role, access_mode }]),
    [`GET ${ORG}`]: () => jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role, access_mode }),
    [`GET ${ROUTING}`]: () => jsonResponse(200, { routing }),
    ...overrides,
  })
}

const main = () => screen.getByRole('main')
const requests = (fetchMock: FetchMock) => fetchMock.mock.calls.map(([url, init]) => `${init?.method ?? 'GET'} ${String(url)}`)
const sent = (fetchMock: FetchMock) => JSON.parse(String(callOf(fetchMock, requests(fetchMock).lastIndexOf(`PUT ${ROUTING}`)).init.body)) as unknown
const home = () => screen.getByLabelText('Home branch label')
const rows = () => screen.queryAllByRole('group', { name: /^Destination \d+$/ })
const labelIn = (row: HTMLElement) => within(row).getByRole('textbox')
const enabledIn = (row: HTMLElement) => within(row).getByRole('checkbox', { name: 'Enabled' })
/** Each row as it stands: its name, what is typed, and whether it is ticked. */
const shown = () => rows().map((row) => [row.getAttribute('aria-labelledby') && within(row).getByText(/^Destination \d+$/).textContent, labelIn(row).getAttribute('value') ?? (labelIn(row) as HTMLInputElement).value, (enabledIn(row) as HTMLInputElement).checked])
const button = (name: string) => screen.getByRole('button', { name })
const controls = () => Array.from(main().querySelectorAll('button, input, select, textarea, form'))

async function page(world: World = {}, overrides: ApiRoutes = {}) {
  const fetchMock = serve(world, overrides)
  renderApp(PAGE)
  await screen.findByRole('heading', { level: 3, name: 'Transit destinations' })
  return { fetchMock, user: userEvent.setup(INSTANT) }
}

// =====================================================================================================================
// Getting there, and who it is for
// =====================================================================================================================

describe('the way to the page', () => {
  it('is the fourth section of the strip, between the branches and Efficiency', async () => {
    serve()
    const user = userEvent.setup(INSTANT)
    renderApp('/organizations/northbridge/settings/general')
    const strip = within(await screen.findByRole('navigation', { name: 'Settings' }))

    expect(strip.getAllByRole('link').map((item) => item.textContent)).toEqual(['General', 'Users & Access', 'Branches & Sorters', 'Routing', 'Efficiency'])
    expect(strip.getByRole('link', { name: 'Routing' })).toHaveAttribute('href', PAGE)
    await user.click(strip.getByRole('link', { name: 'Routing' }))

    expect(await screen.findByRole('heading', { level: 2, name: 'Routing' })).toHaveFocus()
    expect(screen.getByTestId('address')).toHaveTextContent(PAGE)
    expect(document.title).toBe('Routing – SortView')
    expect(within(screen.getByRole('navigation', { name: 'Breadcrumb' })).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'Organizations',
      'Northbridge Library',
      'Settings',
      'Routing',
    ])
    expect(within(screen.getByRole('navigation', { name: 'Settings' })).getByRole('link', { name: 'Routing' })).toHaveAttribute('aria-current', 'page')
  })

  it.each(['manager', 'viewer'])('tells a %s it is not theirs, and asks the API for no routing', async (role) => {
    const fetchMock = serve({ role })
    renderApp(PAGE)

    expect(await screen.findByRole('note')).toHaveTextContent("Settings are managed by this organization's owners and admins.")
    expect(screen.queryByRole('navigation', { name: 'Settings' })).not.toBeInTheDocument()
    expect(controls()).toEqual([])
    expect(requests(fetchMock)).toEqual(['GET /api/auth/session', `GET ${ORG}`])
  })

  it('shows the same notice and nothing of the routing when the API says no to someone the page thought was an admin', async () => {
    serve({}, { [`GET ${ROUTING}`]: refused(403, { code: 'forbidden', message: 'You do not have permission to manage these settings.' }) })
    renderApp(PAGE)

    expect(await screen.findByRole('note')).toHaveTextContent("Settings are managed by this organization's owners and admins.")
    expect(controls()).toEqual([])
  })

  it('is the ordinary missing page when the organization is no longer there to see', async () => {
    serve({}, { [`GET ${ROUTING}`]: refused(404, ORGANIZATION_NOT_FOUND) })
    renderApp(PAGE)

    expect(await screen.findByRole('heading', { level: 2, name: 'Page not found' })).toBeInTheDocument()
    expect(screen.getAllByRole('heading', { level: 2 })).toHaveLength(1)
  })

  it('returns to the sign-in form when the session has ended', async () => {
    serve({}, { [`GET ${ROUTING}`]: refused(401, NOT_AUTHENTICATED) })
    renderApp(PAGE)

    expect(await screen.findByLabelText('Password')).toBeInTheDocument()
    expect(screen.getByTestId('address')).toHaveTextContent(PAGE)
  })

  it('says a load failed and tries again when asked', async () => {
    let fail = true
    serve({}, { [`GET ${ROUTING}`]: () => (fail ? jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' }) : jsonResponse(200, { routing: STORED })) })
    const user = userEvent.setup(INSTANT)
    renderApp(PAGE)

    expect(await screen.findByRole('alert')).toHaveTextContent('Internal server error.')
    fail = false
    await user.click(button('Try again'))

    expect(await screen.findByLabelText('Home branch label')).toHaveValue('Central')
  })
})

// =====================================================================================================================
// What is shown
// =====================================================================================================================

describe('the routing as loaded', () => {
  it('shows the home label and each destination in the stored order, with whether it is enabled', async () => {
    await page()

    expect(home()).toHaveValue('Central')
    expect(shown()).toEqual([
      ['Destination 1', 'North Annex', true],
      ['Destination 2', 'Depot', false],
    ])
    expect(screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent)).toEqual(['Home branch', 'Transit destinations'])
    expect(screen.queryByText('You have unsaved changes.')).not.toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('says what routing is, and what saving a change does to reports already made', async () => {
    await page()

    expect(main()).toHaveTextContent(
      'Routing tells SortView how to group check-ins in reports: which destination labels count as kept at a sorting machine’s own branch, and which are transit to another place.',
    )
    expect(screen.getByText(/^Changes apply to every report/).closest('[role="note"]')).toHaveTextContent(NOTICE)
    // Said plainly, and without the one word this app keeps for something else.
    expect(main()).not.toHaveTextContent(/Historical|rewritten|deleted|lost/)
  })

  it('leaves a home label that is not set blank, with no made-up value and no example in its place', async () => {
    await page({ routing: { home_branch_label: '', destinations: [] } })

    expect(home()).toHaveValue('')
    expect(home()).not.toHaveAttribute('placeholder')
    expect(home()).toHaveAccessibleDescription(
      'The label your sorting machines use for items that stay at their own branch. Leave blank to use each sorting machine’s branch name.',
    )
    expect(rows()).toEqual([])
    expect(main()).toHaveTextContent('No transit destinations are configured.')
  })

  it('explains labels and what disabling does, and offers nothing about a branch’s own routing or the internal lists', async () => {
    await page()

    expect(main()).toHaveTextContent(
      'Enter each label as it appears on the sorter. Capital letters, spacing and punctuation do not matter. A disabled destination stays in this list but is not shown in reports; its check-ins count as Other routing.',
    )
    expect(main()).not.toHaveTextContent(/internal|workflow|override|collector|branch services|collection services/i)
    // A label and a tick box to a row, and one field for the home label: nothing to type an identifier into.
    expect(main().querySelectorAll('input[type="text"]')).toHaveLength(3)
    expect(main().querySelectorAll('input[type="checkbox"]')).toHaveLength(2)
    expect(main().querySelectorAll('select, textarea, [draggable="true"]')).toHaveLength(0)
    expect(screen.queryByRole('button', { name: /\bmove\b|\bup\b|\bdown\b|reorder/i })).not.toBeInTheDocument()
  })
})

// =====================================================================================================================
// Editing
// =====================================================================================================================

describe('editing the destinations', () => {
  it('adds an empty, enabled row at the end and puts the cursor in it', async () => {
    const { fetchMock, user } = await page()

    await user.click(button('Add destination'))

    expect(shown()).toEqual([
      ['Destination 1', 'North Annex', true],
      ['Destination 2', 'Depot', false],
      ['Destination 3', '', true],
    ])
    expect(labelIn(rows()[2])).toHaveFocus()
    expect(screen.getByText('You have unsaved changes.')).toBeInTheDocument()
    expect(requests(fetchMock).some((request) => request.startsWith('PUT'))).toBe(false)
  })

  it('edits a label and a tick box in place, and says there are changes to save', async () => {
    const { user } = await page()

    await user.clear(labelIn(rows()[0]))
    await user.type(labelIn(rows()[0]), 'North Wing')
    await user.click(enabledIn(rows()[1]))

    expect(shown()).toEqual([
      ['Destination 1', 'North Wing', true],
      ['Destination 2', 'Depot', true],
    ])
    expect(screen.getByText('You have unsaved changes.')).toBeInTheDocument()
  })

  it('removes a row at once, renumbers the rest, and moves the cursor to the row that took its place', async () => {
    const { fetchMock, user } = await page({ routing: { home_branch_label: 'Central', destinations: ['A', 'B', 'C'].map((label) => ({ label, enabled: true })) } })

    await user.click(button('Remove destination 2'))

    expect(shown()).toEqual([
      ['Destination 1', 'A', true],
      ['Destination 2', 'C', true],
    ])
    expect(labelIn(rows()[1])).toHaveFocus()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(requests(fetchMock).some((request) => request.startsWith('PUT'))).toBe(false)
  })

  it('moves the cursor to the row before when the last row is removed, and to Add destination when none is left', async () => {
    const { user } = await page()

    await user.click(button('Remove destination 2'))
    expect(labelIn(rows()[0])).toHaveFocus()

    await user.click(button('Remove destination 1'))
    expect(rows()).toEqual([])
    expect(button('Add destination')).toHaveFocus()
    expect(main()).toHaveTextContent('No transit destinations are configured.')
  })

  it('stops offering another at twenty, without disabling the button, and offers it again when one is removed', async () => {
    const twenty = Array.from({ length: 20 }, (_, index) => ({ label: `Stop ${index + 1}`, enabled: true }))
    const { user } = await page({ routing: { home_branch_label: 'Central', destinations: twenty } })
    const add = button('Add destination')

    expect(rows()).toHaveLength(20)
    expect(add).toHaveAttribute('aria-disabled', 'true')
    expect(add).not.toBeDisabled()
    expect(main()).toHaveTextContent('An organization can have up to 20 destinations.')
    await user.click(add)
    expect(rows()).toHaveLength(20)

    await user.click(button('Remove destination 20'))
    expect(button('Add destination')).toHaveAttribute('aria-disabled', 'false')
    expect(main()).not.toHaveTextContent('An organization can have up to 20 destinations.')
  })

  it('is worked from the keyboard alone', async () => {
    const { user } = await page()

    home().focus()
    await user.tab()
    expect(labelIn(rows()[0])).toHaveFocus()
    await user.tab()
    expect(enabledIn(rows()[0])).toHaveFocus()
    await user.keyboard(' ')
    expect(enabledIn(rows()[0])).not.toBeChecked()
    await user.tab()
    expect(button('Remove destination 1')).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(shown()).toEqual([['Destination 1', 'Depot', false]])
    expect(main().querySelectorAll('[tabindex]:not(h2)')).toHaveLength(0)
  })
})

// =====================================================================================================================
// Saving and discarding
// =====================================================================================================================

describe('saving', () => {
  it('sends the whole block as labels and flags, then shows what the API stored and says it is saved', async () => {
    const stored = { home_branch_label: 'Main Hall', destinations: [{ label: 'North Annex', enabled: false }, { label: 'South Dock', enabled: true }] }
    const { fetchMock, user } = await page({}, { [`PUT ${ROUTING}`]: () => jsonResponse(200, { routing: stored }) })

    await user.clear(home())
    await user.type(home(), '  Main Hall ')
    await user.click(enabledIn(rows()[0]))
    await user.click(button('Remove destination 2'))
    await user.click(button('Add destination'))
    await user.type(labelIn(rows()[1]), ' South Dock  ')
    await user.click(button('Save routing'))

    expect(await screen.findByRole('status')).toHaveTextContent('Saved. Reports now use these labels.')
    // Exactly what was typed, in order: the API does the trimming.
    expect(sent(fetchMock)).toEqual({
      routing: { home_branch_label: '  Main Hall ', destinations: [{ label: 'North Annex', enabled: false }, { label: ' South Dock  ', enabled: true }] },
    })
    expect(JSON.stringify(sent(fetchMock))).not.toMatch(/key|row|"id"/)
    // What is then on the page is the API's answer, trimmed.
    expect(home()).toHaveValue('Main Hall')
    expect(shown()).toEqual([
      ['Destination 1', 'North Annex', false],
      ['Destination 2', 'South Dock', true],
    ])
    expect(screen.queryByText('You have unsaved changes.')).not.toBeInTheDocument()
    // Asked once, and the routing was not read a second time.
    expect(requests(fetchMock).filter((request) => request.endsWith('/settings/routing'))).toEqual([`GET ${ROUTING}`, `PUT ${ROUTING}`])
  })

  it('sends a blank home label as blank', async () => {
    const { fetchMock, user } = await page({}, { [`PUT ${ROUTING}`]: () => jsonResponse(200, { routing: { ...STORED, home_branch_label: '' } }) })

    await user.clear(home())
    await user.click(button('Save routing'))

    await screen.findByRole('status')
    expect(sent(fetchMock)).toEqual({ routing: { ...STORED, home_branch_label: '' } })
    expect(home()).toHaveValue('')
  })

  it('stays busy, with the cursor, while a save is in flight, and does not ask twice', async () => {
    const pending = deferred<Response>()
    const { fetchMock, user } = await page({}, { [`PUT ${ROUTING}`]: () => pending.promise })

    await user.click(enabledIn(rows()[1]))
    await user.click(button('Save routing'))
    const busy = await screen.findByRole('button', { name: 'Saving…' })
    await user.click(busy)

    expect(busy).toHaveAttribute('aria-disabled', 'true')
    expect(busy).not.toBeDisabled()
    expect(busy).toHaveFocus()
    expect(requests(fetchMock).filter((request) => request === `PUT ${ROUTING}`)).toHaveLength(1)
    pending.resolve(jsonResponse(200, { routing: { ...STORED, destinations: [STORED.destinations[0], { label: 'Depot', enabled: true }] } }))
    expect(await screen.findByRole('status')).toHaveTextContent(/^Saved\./)
  })

  it('stops saying it is saved as soon as something is changed again', async () => {
    const { user } = await page({}, { [`PUT ${ROUTING}`]: () => jsonResponse(200, { routing: STORED }) })
    await user.click(button('Save routing'))
    await screen.findByRole('status')

    await user.type(home(), ' Library')

    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByText('You have unsaved changes.')).toBeInTheDocument()
  })
})

describe('discarding', () => {
  it('goes back to what was loaded, and asks the API for nothing', async () => {
    const { fetchMock, user } = await page()

    await user.type(home(), ' Library')
    await user.click(button('Remove destination 1'))
    await user.click(button('Add destination'))
    await user.click(button('Discard changes'))

    expect(home()).toHaveValue('Central')
    expect(shown()).toEqual([
      ['Destination 1', 'North Annex', true],
      ['Destination 2', 'Depot', false],
    ])
    expect(screen.queryByText('You have unsaved changes.')).not.toBeInTheDocument()
    expect(requests(fetchMock).filter((request) => request.endsWith('/settings/routing'))).toEqual([`GET ${ROUTING}`])
  })

  it('goes back to what was last saved, once something has been', async () => {
    const stored = { home_branch_label: 'Main Hall', destinations: [{ label: 'North Annex', enabled: true }] }
    const { user } = await page({}, { [`PUT ${ROUTING}`]: () => jsonResponse(200, { routing: stored }) })
    await user.click(button('Remove destination 2'))
    await user.click(button('Save routing'))
    await screen.findByRole('status')

    await user.type(labelIn(rows()[0]), ' West')
    await user.click(button('Discard changes'))

    expect(home()).toHaveValue('Main Hall')
    expect(shown()).toEqual([['Destination 1', 'North Annex', true]])
  })

  it('clears what was said about a field along with the field', async () => {
    const { user } = await page({}, { [`PUT ${ROUTING}`]: invalid(['destinations.1.label', 'duplicate']) })
    await user.click(button('Save routing'))
    await screen.findByRole('alert')

    await user.click(button('Discard changes'))

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(labelIn(rows()[1])).toHaveAttribute('aria-invalid', 'false')
  })
})

// =====================================================================================================================
// What the API refuses
// =====================================================================================================================

describe('a refused save', () => {
  it('asks the API for nothing while a destination has no label, and says so under that row', async () => {
    const { fetchMock, user } = await page()

    await user.click(button('Add destination'))
    await user.click(button('Save routing'))

    expect(await screen.findByRole('alert')).toHaveTextContent('Nothing was saved. Check the fields marked below.')
    expect(labelIn(rows()[2])).toHaveAttribute('aria-invalid', 'true')
    expect(labelIn(rows()[2])).toHaveAccessibleDescription('Enter a label, or remove this destination.')
    expect(labelIn(rows()[0])).toHaveAttribute('aria-invalid', 'false')
    expect(requests(fetchMock).some((request) => request.startsWith('PUT'))).toBe(false)
  })

  it.each([
    ['required', 'Enter a label, or remove this destination.'],
    ['duplicate', 'This is the same destination as an earlier one.'],
    ['means_home', 'This label means the home branch, so it cannot also be a destination.'],
    ['something_new', 'This is not valid. Check it and try again.'],
  ])('puts %s under the row the API named, keeps what was typed, and says nothing was saved', async (code, sentence) => {
    const { user } = await page({}, { [`PUT ${ROUTING}`]: invalid(['destinations.1.label', code]) })

    await user.clear(labelIn(rows()[1]))
    await user.type(labelIn(rows()[1]), 'north annex')
    await user.click(button('Save routing'))

    expect(await screen.findByRole('alert')).toHaveTextContent('Nothing was saved. Check the fields marked below.')
    expect(screen.getAllByRole('alert')).toHaveLength(1)
    expect(labelIn(rows()[1])).toHaveAttribute('aria-invalid', 'true')
    expect(labelIn(rows()[1])).toHaveAccessibleDescription(sentence)
    expect(labelIn(rows()[0])).toHaveAttribute('aria-invalid', 'false')
    expect(shown()).toEqual([
      ['Destination 1', 'North Annex', true],
      ['Destination 2', 'north annex', false],
    ])
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(new RegExp(`${code}|destinations\\.1|invalid_routing`))
  })

  it('keeps a problem with its row when another row is removed, and drops it when that field is changed', async () => {
    const { user } = await page({ routing: { home_branch_label: 'Central', destinations: ['A', 'B', 'a'].map((label) => ({ label, enabled: true })) } }, {
      [`PUT ${ROUTING}`]: invalid(['destinations.2.label', 'duplicate']),
    })
    await user.click(button('Save routing'))
    await screen.findByRole('alert')

    await user.click(button('Remove destination 1'))
    expect(labelIn(rows()[1])).toHaveValue('a')
    expect(labelIn(rows()[1])).toHaveAccessibleDescription('This is the same destination as an earlier one.')

    await user.type(labelIn(rows()[1]), 'bc')
    expect(labelIn(rows()[1])).toHaveAttribute('aria-invalid', 'false')
  })

  it('says under the list that there are too many', async () => {
    const { user } = await page({}, { [`PUT ${ROUTING}`]: invalid(['destinations', 'too_many']) })

    await user.click(button('Save routing'))

    expect(await screen.findByRole('alert')).toHaveTextContent('Nothing was saved. Check the fields marked below.')
    expect(within(screen.getByRole('region', { name: 'Transit destinations' })).getByText('An organization can have up to 20 destinations.')).toBeInTheDocument()
    expect(rows().every((row) => labelIn(row).getAttribute('aria-invalid') === 'false')).toBe(true)
  })

  it('says so, without a field, when the API refuses something this form has no field for', async () => {
    const { user } = await page({}, { [`PUT ${ROUTING}`]: invalid(['destinations.0.enabled', 'not_a_boolean']) })

    await user.click(button('Save routing'))

    expect(await screen.findByRole('alert')).toHaveTextContent('Nothing was saved. Check what you entered and try again.')
    expect(main().querySelectorAll('[aria-invalid="true"]')).toHaveLength(0)
  })

  it.each([
    ['a body the API could not read', 422, { code: 'validation_error', detail: [{ loc: ['body', 'routing'], msg: 'CANARY' }] }, 'Nothing was saved. That request was not valid. Check what you entered and try again.'],
    ['a server error', 500, { code: 'internal_error', message: 'Internal server error.' }, 'Nothing was saved. Internal server error.'],
    ['an organization suspended meanwhile', 403, { code: 'organization_read_only', message: 'This organization’s settings cannot be changed.' }, 'Nothing was saved. This organization’s settings cannot be changed.'],
  ])('keeps everything typed after %s, and says nothing was saved', async (_label, status, body, shownMessage) => {
    const { user } = await page({}, { [`PUT ${ROUTING}`]: refused(status, body) })

    await user.type(home(), ' Library')
    await user.click(button('Add destination'))
    await user.type(labelIn(rows()[2]), 'South Dock')
    await user.click(button('Save routing'))

    expect(await screen.findByRole('alert')).toHaveTextContent(shownMessage)
    expect(main()).not.toHaveTextContent('CANARY')
    expect(home()).toHaveValue('Central Library')
    expect(shown()[2]).toEqual(['Destination 3', 'South Dock', true])
    expect(screen.getByText('You have unsaved changes.')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('keeps everything typed when the server cannot be reached', async () => {
    const { user } = await page()  // no handler for the PUT: it fails like a dead network

    await user.type(home(), ' Library')
    await user.click(button('Save routing'))

    expect(await screen.findByRole('alert')).toHaveTextContent('Nothing was saved. Could not reach the server. Check your connection and try again.')
    expect(home()).toHaveValue('Central Library')
  })

  it('returns to the sign-in form when the session ended before the save', async () => {
    const { user } = await page({}, { [`PUT ${ROUTING}`]: refused(401, NOT_AUTHENTICATED) })

    await user.click(button('Save routing'))

    expect(await screen.findByLabelText('Password')).toBeInTheDocument()
  })

  it('shows what the dashboard once stored and lets it be put right', async () => {
    // Two rows that are one destination: readable, and refused only when saved.
    const legacy = { home_branch_label: 'Central', destinations: [{ label: 'Depot', enabled: true }, { label: 'Depot', enabled: false }] }
    let attempts = 0
    const { user } = await page({ routing: legacy }, {
      [`PUT ${ROUTING}`]: () => (attempts++ === 0 ? invalid(['destinations.1.label', 'duplicate'])() : jsonResponse(200, { routing: { home_branch_label: 'Central', destinations: [legacy.destinations[0]] } })),
    })
    expect(shown()).toEqual([
      ['Destination 1', 'Depot', true],
      ['Destination 2', 'Depot', false],
    ])

    await user.click(button('Save routing'))
    await screen.findByRole('alert')
    await user.click(button('Remove destination 2'))
    await user.click(button('Save routing'))

    expect(await screen.findByRole('status')).toHaveTextContent(/^Saved\./)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(shown()).toEqual([['Destination 1', 'Depot', true]])
  })
})

// =====================================================================================================================
// A suspended organization
// =====================================================================================================================

describe('a suspended organization', () => {
  it('shows the routing in words, with nothing to type in, tick, press or save', async () => {
    const { fetchMock } = await page({ role: 'owner', access_mode: 'read_only' })

    expect(main()).toHaveTextContent('This organization is suspended, so its routing cannot be changed.')
    expect(controls()).toEqual([])
    expect(screen.queryByRole('form')).not.toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'Home branch' })).getByText('Central')).toBeInTheDocument()
    expect(within(screen.getByRole('list', { name: 'Transit destinations' })).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'North AnnexEnabled',
      'DepotDisabled',
    ])
    // The warning is about saving, and there is nothing to save.
    expect(main()).not.toHaveTextContent('Changes apply to every report')
    expect(requests(fetchMock).every((request) => request.startsWith('GET'))).toBe(true)
  })

  it('says a home label is not set, and that there are no destinations, in words', async () => {
    await page({ access_mode: 'read_only', routing: { home_branch_label: '', destinations: [] } })

    expect(within(screen.getByRole('region', { name: 'Home branch' })).getByText('Not set — each sorting machine’s branch name is used')).toBeInTheDocument()
    expect(main()).toHaveTextContent('No transit destinations are configured.')
    expect(controls()).toEqual([])
  })
})

// =====================================================================================================================
// Structure
// =====================================================================================================================

describe('the structure of the page', () => {
  it('names the form, its regions, each row and every control', async () => {
    await page()

    expect(screen.getByRole('form', { name: 'Routing' })).toBeInTheDocument()
    expect(screen.getAllByRole('region').map((region) => region.getAttribute('aria-labelledby'))).toEqual(['routing-home-heading', 'routing-destinations-heading'])
    for (const control of within(main()).getAllByRole('button').concat(within(main()).getAllByRole('textbox'), within(main()).getAllByRole('checkbox'))) {
      expect(control).toHaveAccessibleName()
    }
    expect(within(main()).getAllByRole('button').map((control) => control.textContent)).toEqual([
      'Remove destination 1',
      'Remove destination 2',
      'Add destination',
      'Save routing',
      'Discard changes',
    ])
    expect(main().querySelectorAll('[disabled], [aria-live], [role="button"], dialog, table')).toHaveLength(0)
    await waitFor(() => expect(rows()).toHaveLength(2))
  })
})
