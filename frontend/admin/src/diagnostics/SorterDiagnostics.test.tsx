import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'

import { ALICE, jsonResponse, requestedUrls, serveApi, type ApiRoutes } from '../../../src/test/http.ts'
import { AdminApp } from '../AdminApp.tsx'

const SESSION = { 'GET /api/admin/session': () => jsonResponse(200, ALICE) }
const PATH = '/api/admin/libraries/nbpl/sorters/main/diagnostics'

const BASE = {
  timezone: 'America/Chicago',
  library: { slug: 'nbpl', name: 'New Braunfels Public Library' },
  sorter: {
    slug: 'main',
    name: 'Main AMH',
    host_branch: { slug: 'main', name: 'Main Branch' },
    status: 'active',
    collector_count: 1,
    onboarding_status: 'ready',
  },
  pipeline: { state: 'degraded', last_reported_at: '2026-10-14T15:30:00Z' },
}

const HEARTBEAT = {
  last_heartbeat_at: '2026-10-14T15:30:00Z',
  health_status: 'degraded',
  last_error_class: 'retryable_infra',
  collector_schedule_status: 'healthy',
  collector_last_run_at: '2026-10-14T15:29:00Z',
  collector_next_run_at: '2026-10-14T15:44:00Z',
  collector_run_duration_ms: 3240,
  pending_outbox_count: 12,
  quarantined_count: 3,
  oldest_pending_event_at: '2026-10-14T15:00:00Z',
  last_success_at: '2026-10-14T15:00:00Z',
  watcher_last_active_at: '2026-10-14T15:29:30Z',
}

const V2 = { ...BASE, contract: 'v2', cutover_at: '2026-10-01T05:00:00Z', v2: { status: HEARTBEAT }, legacy: null }

const LEGACY_REPORT = {
  run_status: 'failed_upload',
  run_reported_at: '2026-10-14T15:30:00Z',
  health_status: 'degraded',
  health_reported_at: '2026-10-14T15:31:00Z',
  pending_outbox_count: 4,
  quarantined_count: 1,
  checkins_rows: 1200,
  rejects_rows: 9,
  uploaded_checkins_rows: 1100,
  uploaded_rejects_rows: 8,
  checkins_bad_datetime_rows: 2,
  rejects_bad_datetime_rows: 0,
}

const LEGACY = { ...BASE, contract: 'legacy', cutover_at: null, v2: null, legacy: { status: LEGACY_REPORT } }

function renderAt(path: string, routes: ApiRoutes) {
  const fetchMock = serveApi({ ...SESSION, ...routes })
  render(
    <MemoryRouter initialEntries={[path]}>
      <AdminApp />
    </MemoryRouter>,
  )
  return fetchMock
}

function facts(heading: string): Record<string, string> {
  const section = screen.getByRole('heading', { level: 3, name: heading }).closest('section') as HTMLElement
  return Object.fromEntries(
    within(section)
      .getAllByRole('term')
      .map((term) => [term.textContent ?? '', term.nextElementSibling?.textContent ?? '']),
  )
}

function assertReadOnly() {
  expect(document.querySelector('form')).toBeNull()
  expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual(['Log out'])
}

describe('Sorter diagnostics', () => {
  it("shows a Contract v2 sorter's heartbeat, read-only", async () => {
    const fetchMock = renderAt('/libraries/nbpl/sorters/main', { [`GET ${PATH}`]: () => jsonResponse(200, V2) })

    expect(await screen.findByRole('heading', { level: 2, name: 'Main AMH' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'New Braunfels Public Library' })).toHaveAttribute('href', '/libraries/nbpl')
    expect(screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent)).toEqual([
      'Sorter',
      'Collector heartbeat',
    ])
    expect(facts('Sorter')).toMatchObject({ Pipeline: 'Degraded', Contract: 'Contract v2', Collectors: '1' })
    expect(facts('Collector heartbeat')).toEqual({
      'Last heartbeat': 'Oct 14, 2026, 10:30 AM CDT',
      Health: 'degraded',
      'Last error class': 'retryable_infra',
      Schedule: 'healthy',
      'Last collector run': 'Oct 14, 2026, 10:29 AM CDT',
      'Next scheduled run': 'Oct 14, 2026, 10:44 AM CDT',
      'Run duration': '3.24 seconds',
      'Pending events': '12',
      'Quarantined events': '3',
      'Oldest pending event': 'Oct 14, 2026, 10:00 AM CDT',
      'Last success': 'Oct 14, 2026, 10:00 AM CDT',
      'Watcher last active': 'Oct 14, 2026, 10:29 AM CDT',
    })
    expect(requestedUrls(fetchMock)).toEqual(['/api/admin/session', PATH])
    assertReadOnly()
  })

  it("shows a legacy sorter's report and no heartbeat", async () => {
    renderAt('/libraries/nbpl/sorters/main', { [`GET ${PATH}`]: () => jsonResponse(200, LEGACY) })

    expect(await screen.findByRole('heading', { level: 3, name: 'Legacy report' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Collector heartbeat' })).not.toBeInTheDocument()
    expect(facts('Sorter')).toMatchObject({ Contract: 'Legacy', Cutover: 'Not reported' })
    expect(facts('Legacy report')).toMatchObject({
      'Run status': 'failed_upload',
      'Agent health': 'degraded',
      'Check-in rows': '1,200',
      'Check-in rows with a bad time': '2',
      'Reject rows with a bad time': '0',
    })
    assertReadOnly()
  })

  it('says a Contract v2 sorter with no active ingest key has none', async () => {
    renderAt('/libraries/nbpl/sorters/main', { [`GET ${PATH}`]: () => jsonResponse(200, { ...V2, v2: { status: null } }) })

    expect(await screen.findByText('This sorter has no active ingest key.')).toBeInTheDocument()
  })

  it('says a sorter with no legacy report has none', async () => {
    renderAt('/libraries/nbpl/sorters/main', { [`GET ${PATH}`]: () => jsonResponse(200, { ...LEGACY, legacy: { status: null } }) })

    expect(await screen.findByText('This sorter has no legacy report.')).toBeInTheDocument()
  })

  it('says a library with no operational identity has nothing to read', async () => {
    renderAt('/libraries/nbpl/sorters/main', {
      [`GET ${PATH}`]: () =>
        jsonResponse(200, { ...BASE, pipeline: { state: 'unknown', last_reported_at: null }, contract: 'unknown', cutover_at: null, v2: null, legacy: null }),
    })

    expect(await screen.findByRole('note')).toHaveTextContent('no operational identity')
    expect(screen.getAllByRole('heading', { level: 3 }).map((heading) => heading.textContent)).toEqual(['Sorter'])
  })

  it('never shows a field the client was not written for, such as a hostname or collector version', async () => {
    renderAt('/libraries/nbpl/sorters/main', {
      [`GET ${PATH}`]: () =>
        jsonResponse(200, { ...V2, hostname: 'SORTER-PC-01', v2: { status: { ...HEARTBEAT, collector_version: '9.9.9', key_id: 'k-1' } } }),
    })

    expect(await screen.findByRole('heading', { level: 3, name: 'Collector heartbeat' })).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/SORTER-PC-01|9\.9\.9|k-1|hostname|version/i)
  })

  it('treats a response with the wrong era filled as a failure', async () => {
    renderAt('/libraries/nbpl/sorters/main', { [`GET ${PATH}`]: () => jsonResponse(200, { ...V2, legacy: { status: LEGACY_REPORT } }) })

    expect(await screen.findByRole('alert')).toHaveTextContent("This sorter's diagnostics could not be loaded.")
  })

  it('says a sorter that does not exist was not found, with the way back', async () => {
    renderAt('/libraries/nbpl/sorters/nobody', {
      'GET /api/admin/libraries/nbpl/sorters/nobody/diagnostics': () =>
        jsonResponse(404, { code: 'sorter_not_found', message: 'Sorter not found.' }),
    })

    expect(await screen.findByRole('heading', { level: 2, name: 'Sorter not found' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Library' })).toHaveAttribute('href', '/libraries/nbpl')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows a fixed error with a retry for a server failure', async () => {
    renderAt('/libraries/nbpl/sorters/main', {
      [`GET ${PATH}`]: () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' }),
    })

    expect(await screen.findByRole('alert', {}, { timeout: 8000 })).toHaveTextContent("This sorter's diagnostics could not be loaded.")
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  it('loads again from the retry button', async () => {
    let calls = 0
    renderAt('/libraries/nbpl/sorters/main', {
      [`GET ${PATH}`]: () => jsonResponse(200, ++calls === 1 ? { ...V2, contract: 'sideways' } : V2),
    })
    const person = userEvent.setup()

    expect(await screen.findByRole('alert')).toHaveTextContent("This sorter's diagnostics could not be loaded.")
    await person.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByRole('heading', { level: 3, name: 'Collector heartbeat' })).toBeInTheDocument()
  })

  it('is opened from a sorter on the library overview', async () => {
    const fetchMock = renderAt('/libraries/nbpl', {
      'GET /api/admin/libraries/nbpl': () =>
        jsonResponse(200, {
          timezone: 'America/Chicago',
          slug: 'nbpl',
          name: 'New Braunfels Public Library',
          status: 'active',
          primary_branch: { slug: 'main', name: 'Main Branch', status: 'active' },
          operational_identity: 'assigned',
          subscription: null,
          sorters: [{ ...BASE.sorter, pipeline: BASE.pipeline }],
        }),
      [`GET ${PATH}`]: () => jsonResponse(200, V2),
    })
    const person = userEvent.setup()

    await person.click(await screen.findByRole('link', { name: 'Main AMH' }))

    expect(await screen.findByRole('heading', { level: 3, name: 'Collector heartbeat' })).toBeInTheDocument()
    expect(requestedUrls(fetchMock)).toContain(PATH)
  })
})
