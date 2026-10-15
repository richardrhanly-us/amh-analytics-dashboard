import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import { ALICE, deferred, jsonResponse, requestedUrls, serveApi, type ApiRoutes, type FetchMock } from '../../../src/test/http.ts'
import { AdminApp } from '../AdminApp.tsx'
import { SAVED } from './CollectorConfigPage.tsx'

const SESSION = { 'GET /api/admin/session': () => jsonResponse(200, ALICE) }
const SETTINGS_PATH = '/api/admin/libraries/acme/collector-settings'
const ROUTING_PATH = '/api/admin/libraries/acme/internal-routing'
const LIBRARY = { slug: 'acme', name: 'Acme Library' }

const SETTINGS = {
  library: LIBRARY,
  collector_timezone: 'America/Chicago',
  rules_reviewed_at: '2026-10-11T15:04:05Z',
  home_branch_label: 'Central',
  transit_destination_labels: ['North Branch', 'Bookmobile'],
  internal_routing_counts: {
    branch_services_names: 2,
    collection_services_names: 1,
    branch_services_da_patterns: 0,
    collection_services_da_patterns: 1,
  },
  enrollment_problems: [],
}
const ROUTING = {
  library: LIBRARY,
  branch_services_names: ['Branch Programs', 'Outreach Desk'],
  collection_services_names: ['Cataloging'],
  branch_services_da_patterns: [],
  collection_services_da_patterns: ['DAMEND'],
}

// What the page must never claim: it saves to SortView, and nothing reaches an installed collector.
const FALSE_CLAIMS = /saved to (the )?collector|collector (was )?updated|synced|sync now|pushed|push to/i

function renderAt(path: string, routes: ApiRoutes) {
  const fetchMock = serveApi({ ...SESSION, ...routes })
  render(
    <MemoryRouter initialEntries={[path]}>
      <AdminApp />
    </MemoryRouter>,
  )
  return fetchMock
}

function readyRoutes(extra: ApiRoutes = {}): ApiRoutes {
  return {
    [`GET ${SETTINGS_PATH}`]: () => jsonResponse(200, SETTINGS),
    [`GET ${ROUTING_PATH}`]: () => jsonResponse(200, ROUTING),
    ...extra,
  }
}

function putBodies(mock: FetchMock, path: string): unknown[] {
  return mock.mock.calls
    .filter(([url, init]) => String(url) === path && init?.method === 'PUT')
    .map(([, init]) => JSON.parse(String(init?.body)) as unknown)
}

function facts(): Record<string, string> {
  const section = screen.getByRole('heading', { level: 3, name: 'Collector settings' }).closest('section') as HTMLElement
  return Object.fromEntries(
    within(section)
      .getAllByRole('term')
      .map((term) => [term.textContent ?? '', term.nextElementSibling?.textContent ?? '']),
  )
}

async function loaded() {
  expect(await screen.findByRole('textbox', { name: 'Branch services account names' })).toBeInTheDocument()
  expect(await screen.findByRole('textbox', { name: 'Collector time zone' })).toBeInTheDocument()
}

describe('Collector configuration', () => {
  it('shows the collector settings, the transit routing read-only, the counts and the lists', async () => {
    const fetchMock = renderAt('/libraries/acme/collector', readyRoutes())
    await loaded()

    expect(screen.getByRole('heading', { level: 2, name: 'Collector configuration' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Acme Library' })).toHaveAttribute('href', '/libraries/acme')
    expect(screen.getByRole('note')).toHaveTextContent('A Contract v2 collector that is already installed is not updated')
    expect(facts()).toEqual({
      'Home branch label': 'Central',
      'Transit destinations': 'North BranchBookmobile',
      'Branch services account names': '2 entries',
      'Collection services account names': '1 entry',
      'Branch services destination markers': '0 entries',
      'Collection services destination markers': '1 entry',
      'Last reviewed': 'Oct 11, 2026, 10:04 AM CDT',
    })
    expect(screen.getByText('Ready for a Contract v2 enrollment.')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Collector time zone' })).toHaveValue('America/Chicago')
    expect(screen.getByRole('textbox', { name: 'Branch services account names' })).toHaveValue('Branch Programs\nOutreach Desk')
    expect(screen.getByRole('textbox', { name: 'Collection services destination markers' })).toHaveValue('DAMEND')
    expect(requestedUrls(fetchMock)).toEqual(['/api/admin/session', SETTINGS_PATH, ROUTING_PATH])
  })

  it('has no way to change transit routing, installations or enrollment codes', async () => {
    renderAt('/libraries/acme/collector', readyRoutes())
    await loaded()

    // One text field (the time zone), four lists, one confirmation: the transit labels are text, not fields.
    expect(screen.getAllByRole('textbox').map((box) => box.getAttribute('id'))).toEqual([
      'collector-timezone',
      'routing-branch_services_names',
      'routing-collection_services_names',
      'routing-branch_services_da_patterns',
      'routing-collection_services_da_patterns',
    ])
    expect(screen.getAllByRole('checkbox')).toHaveLength(1)
    expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual([
      'Log out',
      'Save collector settings',
      'Save Internal Routing',
      'Discard changes',
    ])
    expect(document.body.textContent).not.toMatch(/enrollment code|installation|hostname|retire|offboard|suspend/i)
    expect(document.body.textContent).not.toMatch(FALSE_CLAIMS)
  })

  it('says what stops a Contract v2 enrollment, in words', async () => {
    renderAt('/libraries/acme/collector', {
      ...readyRoutes(),
      [`GET ${SETTINGS_PATH}`]: () =>
        jsonResponse(200, { ...SETTINGS, collector_timezone: null, rules_reviewed_at: null, enrollment_problems: ['timezone_not_configured', 'rules_not_reviewed'] }),
    })
    await loaded()

    expect(screen.getByText('Not ready for a Contract v2 enrollment:')).toBeInTheDocument()
    expect(screen.getByText('No Collector time zone is recorded.')).toBeInTheDocument()
    expect(screen.getByText('The routing configuration has not been recorded as reviewed.')).toBeInTheDocument()
    expect(facts()['Last reviewed']).toBe('Never')
  })

  it('will not save the collector settings until the review is confirmed', async () => {
    const fetchMock = renderAt('/libraries/acme/collector', readyRoutes())
    const person = userEvent.setup()
    await loaded()

    await person.click(screen.getByRole('button', { name: 'Save collector settings' }))

    expect(screen.getByRole('alert')).toHaveTextContent('Nothing was saved. Check the fields marked below.')
    expect(screen.getByText("Confirm that you have reviewed this library's routing configuration.")).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: "I have reviewed this library's routing configuration" })).toHaveAttribute('aria-invalid', 'true')
    expect(putBodies(fetchMock, SETTINGS_PATH)).toEqual([])
  })

  it('saves the time zone and the review, and says it was saved to SortView', async () => {
    const fetchMock = renderAt(
      '/libraries/acme/collector',
      readyRoutes({ [`PUT ${SETTINGS_PATH}`]: () => jsonResponse(200, { ...SETTINGS, collector_timezone: 'America/New_York' }) }),
    )
    const person = userEvent.setup()
    await loaded()

    const zone = screen.getByRole('textbox', { name: 'Collector time zone' })
    await person.clear(zone)
    await person.type(zone, 'America/New_York')
    expect(screen.getByText('You have unsaved changes.')).toBeInTheDocument()
    await person.click(screen.getByRole('checkbox', { name: "I have reviewed this library's routing configuration" }))
    await person.click(screen.getByRole('button', { name: 'Save collector settings' }))

    expect(await screen.findByRole('status')).toHaveTextContent(SAVED)
    expect(putBodies(fetchMock, SETTINGS_PATH)).toEqual([{ collector_timezone: 'America/New_York', rules_reviewed: true }])
    expect(screen.getByRole('checkbox', { name: "I have reviewed this library's routing configuration" })).not.toBeChecked()
    expect(document.body.textContent).not.toMatch(FALSE_CLAIMS)
  })

  it('shows a refused time zone beside its field', async () => {
    renderAt(
      '/libraries/acme/collector',
      readyRoutes({
        [`PUT ${SETTINGS_PATH}`]: () =>
          jsonResponse(422, {
            code: 'invalid_collector_settings',
            message: 'The collector settings are not valid.',
            problems: [{ field: 'collector_timezone', code: 'timezone_invalid' }],
          }),
      }),
    )
    const person = userEvent.setup()
    await loaded()

    await person.click(screen.getByRole('checkbox', { name: "I have reviewed this library's routing configuration" }))
    await person.click(screen.getByRole('button', { name: 'Save collector settings' }))

    expect(await screen.findByText('Enter a time zone name this server knows, such as America/Chicago.')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Collector time zone' })).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByRole('alert')).toHaveTextContent('Nothing was saved. Check the fields marked below.')
  })

  it('saves the lists line by line and then shows what SortView stored, with the counts brought up to date', async () => {
    let settingsReads = 0
    const stored = { ...ROUTING, branch_services_names: ['Branch Programs', 'Outreach Desk', 'Mobile'] }
    const fetchMock = renderAt('/libraries/acme/collector', {
      [`GET ${SETTINGS_PATH}`]: () =>
        jsonResponse(200, ++settingsReads === 1 ? SETTINGS : { ...SETTINGS, internal_routing_counts: { ...SETTINGS.internal_routing_counts, branch_services_names: 3 } }),
      [`GET ${ROUTING_PATH}`]: () => jsonResponse(200, ROUTING),
      [`PUT ${ROUTING_PATH}`]: () => jsonResponse(200, stored),
    })
    const person = userEvent.setup()
    await loaded()

    const names = screen.getByRole('textbox', { name: 'Branch services account names' })
    await person.type(names, '\n  Mobile \n\nbranch programs')
    expect(screen.getByText('4 entries')).toBeInTheDocument()
    await person.click(screen.getByRole('button', { name: 'Save Internal Routing' }))

    expect(await screen.findByRole('status')).toHaveTextContent(SAVED)
    expect(putBodies(fetchMock, ROUTING_PATH)).toEqual([
      {
        branch_services_names: ['Branch Programs', 'Outreach Desk', '  Mobile ', '', 'branch programs'],
        collection_services_names: ['Cataloging'],
        branch_services_da_patterns: [''],
        collection_services_da_patterns: ['DAMEND'],
      },
    ])
    expect(names).toHaveValue('Branch Programs\nOutreach Desk\nMobile')
    await waitFor(() => expect(facts()['Branch services account names']).toBe('3 entries'))
    expect(document.body.textContent).not.toMatch(FALSE_CLAIMS)
  })

  it('shows each refused line beside its own list, by line number', async () => {
    renderAt(
      '/libraries/acme/collector',
      readyRoutes({
        [`PUT ${ROUTING_PATH}`]: () =>
          jsonResponse(422, {
            code: 'invalid_internal_routing',
            message: 'The Internal Routing lists are not valid.',
            problems: [
              { field: 'collection_services_names.1', code: 'separator_character' },
              { field: 'collection_services_names.2', code: 'too_long' },
              { field: 'branch_services_da_patterns', code: 'too_many' },
            ],
          }),
      }),
    )
    const person = userEvent.setup()
    await loaded()

    await person.type(screen.getByRole('textbox', { name: 'Collection services account names' }), '\nA|B\nlong')
    await person.click(screen.getByRole('button', { name: 'Save Internal Routing' }))

    expect(await screen.findByText('Line 2 contains "|", which no account name or marker can contain.')).toBeInTheDocument()
    expect(screen.getByText('Line 3 is longer than 200 characters.')).toBeInTheDocument()
    expect(screen.getByText('There are more than 5000 entries.')).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Collection services account names' })).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByRole('textbox', { name: 'Branch services account names' })).toHaveAttribute('aria-invalid', 'false')
    expect(screen.getByRole('alert')).toHaveTextContent('Nothing was saved. Check the fields marked below.')
    // What was typed stays, to be corrected.
    expect(screen.getByRole('textbox', { name: 'Collection services account names' })).toHaveValue('Cataloging\nA|B\nlong')
  })

  it('discards unsaved list changes', async () => {
    renderAt('/libraries/acme/collector', readyRoutes())
    const person = userEvent.setup()
    await loaded()

    const markers = screen.getByRole('textbox', { name: 'Collection services destination markers' })
    await person.type(markers, '\nDANEW')
    expect(screen.getByText('You have unsaved changes.')).toBeInTheDocument()
    await person.click(screen.getByRole('button', { name: 'Discard changes' }))

    expect(markers).toHaveValue('DAMEND')
    expect(screen.queryByText('You have unsaved changes.')).not.toBeInTheDocument()
  })

  it('says a cancelled library cannot be changed, and any other failure without losing what was typed', async () => {
    let calls = 0
    renderAt(
      '/libraries/acme/collector',
      readyRoutes({
        [`PUT ${ROUTING_PATH}`]: () =>
          ++calls === 1
            ? jsonResponse(409, { code: 'library_cancelled', message: 'This library is cancelled; its settings cannot be changed.' })
            : jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' }),
      }),
    )
    const person = userEvent.setup()
    await loaded()

    await person.type(screen.getByRole('textbox', { name: 'Branch services account names' }), '\nNew')
    await person.click(screen.getByRole('button', { name: 'Save Internal Routing' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Nothing was saved. This library is cancelled; its settings cannot be changed.')

    await person.click(screen.getByRole('button', { name: 'Save Internal Routing' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Nothing was saved. Internal server error.'))
    expect(screen.getByRole('textbox', { name: 'Branch services account names' })).toHaveValue('Branch Programs\nOutreach Desk\nNew')
  })

  it('says a library that does not exist was not found, with the way back', async () => {
    const notFound = () => jsonResponse(404, { code: 'library_not_found', message: 'Library not found.' })
    renderAt('/libraries/nobody/collector', {
      'GET /api/admin/libraries/nobody/collector-settings': notFound,
      'GET /api/admin/libraries/nobody/internal-routing': notFound,
    })

    expect(await screen.findByRole('heading', { level: 2, name: 'Library not found' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Libraries' })).toHaveAttribute('href', '/libraries')
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })

  it('shows a fixed load error with a retry that works', async () => {
    let calls = 0
    const first = deferred<Response>()
    renderAt('/libraries/acme/collector', {
      ...readyRoutes(),
      // A list that is not the expected shape is a failure (and is not retried by the query client).
      [`GET ${ROUTING_PATH}`]: () => (++calls === 1 ? first.promise : jsonResponse(200, ROUTING)),
    })
    const person = userEvent.setup()

    expect(await screen.findByText('Loading Internal Routing…')).toBeInTheDocument()
    first.resolve(jsonResponse(200, { ...ROUTING, branch_services_names: [7] }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The Internal Routing lists could not be loaded. Please try again.')
    await person.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByRole('textbox', { name: 'Branch services account names' })).toBeInTheDocument()
  })

  it('is opened from the library overview', async () => {
    renderAt('/libraries/acme', {
      ...readyRoutes(),
      'GET /api/admin/libraries/acme': () =>
        jsonResponse(200, {
          timezone: 'America/Chicago',
          slug: 'acme',
          name: 'Acme Library',
          status: 'active',
          primary_branch: null,
          operational_identity: 'missing',
          subscription: null,
          sorters: [],
        }),
    })
    const person = userEvent.setup()

    await person.click(await screen.findByRole('link', { name: 'Collector configuration' }))

    expect(await screen.findByRole('heading', { level: 2, name: 'Collector configuration' })).toBeInTheDocument()
  })
})
