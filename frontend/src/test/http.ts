import { vi, type Mock } from 'vitest'

/**
 * Test helpers for the one thing every test here fakes: `fetch`. No test in
 * this app talks to a real server.
 */

export type FetchMock = Mock<typeof fetch>

/** Replaces the global fetch with a mock. Undone automatically after each test (see vitest.config.ts). */
export function stubFetch(): FetchMock {
  const mock: FetchMock = vi.fn<typeof fetch>()
  vi.stubGlobal('fetch', mock)
  return mock
}

export function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

export function textResponse(status: number, body: string, contentType = 'text/html'): Response {
  return new Response(body, { status, headers: { 'Content-Type': contentType } })
}

export function noContent(): Response {
  return new Response(null, { status: 204 })
}

/** What a browser's fetch rejects with when no response arrives. */
export function networkFailure(): TypeError {
  return new TypeError('Failed to fetch')
}

/** A promise a test settles by hand, for holding a request "in flight". */
export function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void; reject: (reason: unknown) => void } {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

/** The [url, init] of one recorded fetch call, with the init narrowed for assertions. */
export function callOf(mock: FetchMock, index = 0): { url: string; init: RequestInit; headers: Record<string, string> } {
  const call = mock.mock.calls[index]
  if (call === undefined) {
    throw new Error(`fetch was not called ${index + 1} time(s).`)
  }
  const [url, init = {}] = call
  return { url: String(url), init, headers: (init.headers ?? {}) as Record<string, string> }
}

/** The request paths fetch was called with, in order. */
export function requestedUrls(mock: FetchMock): string[] {
  return mock.mock.calls.map(([url]) => String(url))
}

export const ALICE = { id: 7, email: 'alice@example.test', full_name: 'Alice Example' }

export const NOT_AUTHENTICATED = { code: 'not_authenticated', message: 'Authentication is required.' }
export const INVALID_CREDENTIALS = { code: 'invalid_credentials', message: 'Invalid email or password.' }
export const ORIGIN_NOT_ALLOWED = { code: 'origin_not_allowed', message: 'Request origin is not allowed.' }
export const ORGANIZATION_NOT_FOUND = { code: 'organization_not_found', message: 'Organization not found.' }

/**
 * A fetch mock that answers by request, e.g. `{ 'GET /api/organizations': () => jsonResponse(200, []) }`.
 * A handler runs once per matching request, so it can return a fresh Response (a Response body can be read
 * only once) or a promise the test settles later. A request with no handler fails like a dead network.
 */
export function serveApi(routes: ApiRoutes): FetchMock {
  const mock = stubFetch()
  mock.mockImplementation((url, init) => {
    const request = `${init?.method ?? 'GET'} ${String(url)}`
    // `?date=*` in a route answers that request for any date, and `?from=*&to=*` for any range; the handler is
    // given the URL to read them from.
    const handler =
      routes[request] ??
      routes[request.replace(/\?date=\d{4}-\d{2}-\d{2}$/, '?date=*')] ??
      routes[request.replace(/\?from=\d{4}-\d{2}-\d{2}&to=\d{4}-\d{2}-\d{2}$/, '?from=*&to=*')]
    return handler === undefined ? Promise.reject(networkFailure()) : Promise.resolve(handler(String(url)))
  })
  return mock
}

export type ApiRoutes = Record<string, (url: string) => Response | Promise<Response>>

export const NORTHBRIDGE = { slug: 'northbridge', name: 'Northbridge Library', role: 'admin', access_mode: 'full' }
export const RIVERSIDE = { slug: 'riverside', name: 'Riverside Library', role: 'viewer', access_mode: 'read_only' }

export const NORTHBRIDGE_DETAIL = {
  ...NORTHBRIDGE,
  // Three branches, two sorters: Westside is a place items are routed to, with no machine of its own.
  branches: [
    { slug: 'central', name: 'Central Branch', is_primary: true },
    { slug: 'east-side', name: 'East Side Branch', is_primary: false },
    { slug: 'westside', name: 'Westside', is_primary: false },
  ],
  sorters: [
    { slug: 'central', name: 'Central Library AMH', host_branch: { slug: 'central', name: 'Central Branch' }, status: 'active', collector_count: 1 },
    { slug: 'east-side', name: 'East Side AMH', host_branch: { slug: 'east-side', name: 'East Side Branch' }, status: 'active', collector_count: 1 },
  ],
  subscription: { plan_code: 'standard', plan_name: 'Standard', status: 'active' },
  entitlements: {
    transits_tab: { enabled: true, limit_value: null },
    branch_count: { enabled: true, limit_value: 5 },
    // Transit routing, and no limit on how far back a report may start: what a plan without them changes is tested
    // on its own (capabilities.test.tsx).
    transits: { enabled: true, limit_value: null },
    history_days: { enabled: true, limit_value: null },
  },
}

export const RIVERSIDE_DETAIL = {
  ...RIVERSIDE,
  branches: [{ slug: 'main', name: 'Riverside Main', is_primary: true }],
  sorters: [
    { slug: 'main', name: 'Riverside Main AMH', host_branch: { slug: 'main', name: 'Riverside Main' }, status: 'active', collector_count: 1 },
  ],
  subscription: null,
  // As Northbridge: what a plan without these changes is tested on its own (capabilities.test.tsx).
  entitlements: {
    transits: { enabled: true, limit_value: null },
    history_days: { enabled: true, limit_value: null },
  },
}

export const TENANT_NOT_FOUND = { code: 'tenant_not_found', message: 'Organization or branch not found.' }

export const REASON_CODES = [
  'item_not_found',
  'ils_acs_failure',
  'rfid_collision',
  'configuration_error',
  'routing_error',
  'communication_error',
  'other',
  'unknown',
]

/** One configured transit destination and how many of the day's check-ins went to it. */
export type RoutedTo = [key: string, label: string, checkinCount: number]

/**
 * What a branch's six Live Today reads answer. `hours` is 24 check-in counts; `reasons` is 8 reject counts.
 * `routing` says where the check-ins went; left out, a tenth go to Westside and a fiftieth to Library Express
 * (rounded down), so a fixture with any number of check-ins -- none included -- still adds up.
 */
export interface LiveFixture {
  timezone: string
  state: string
  last_reported_at: string | null
  hours: number[]
  reasons: number[]
  routing?: { home?: string; transit: RoutedTo[]; other?: number }
}

const hoursWith = (counts: Record<number, number>) => Array.from({ length: 24 }, (_, hour) => counts[hour] ?? 0)

/** A branch mid-afternoon: 120 check-ins (busiest at 11 AM), 6 rejects. */
export const LIVE: LiveFixture = {
  timezone: 'America/Chicago',
  state: 'ok',
  last_reported_at: '2026-10-05T18:45:03Z',
  hours: hoursWith({ 9: 20, 10: 25, 11: 40, 12: 18, 13: 17 }),
  reasons: [3, 0, 1, 0, 0, 2, 0, 0],
}

const sum = (values: number[]) => values.reduce((total, value) => total + value, 0)
const dateOf = (url: string) => new URL(url, 'http://test.invalid').searchParams.get('date')

/** The exact body each of the five reads returns for `live`, for whichever date is asked. */
export const liveBody = {
  pipeline: (live: LiveFixture) => ({ timezone: live.timezone, state: live.state, last_reported_at: live.last_reported_at }),
  checkinCount: (live: LiveFixture, date: string | null) => ({ date, timezone: live.timezone, checkin_count: sum(live.hours) }),
  checkinsByHour: (live: LiveFixture, date: string | null) => ({
    date,
    timezone: live.timezone,
    hours: live.hours.map((checkin_count, hour) => ({ hour, checkin_count })),
  }),
  checkinsByDestination: (live: LiveFixture, date: string | null) => {
    const total = sum(live.hours)
    const routing = live.routing ?? {
      transit: [
        ['westside', 'Westside', Math.floor(total / 10)],
        ['library_express', 'Library Express', Math.floor(total / 50)],
      ] as RoutedTo[],
    }
    const transitCount = sum(routing.transit.map(([, , checkinCount]) => checkinCount))
    const other = routing.other ?? 0
    return {
      date,
      timezone: live.timezone,
      checkin_count: total,
      home: { label: routing.home ?? 'Main', checkin_count: total - transitCount - other },
      transit: routing.transit.map(([key, label, checkin_count]) => ({ key, label, checkin_count })),
      transit_count: transitCount,
      other_count: other,
    }
  },
  rejectCount: (live: LiveFixture, date: string | null) => ({ date, timezone: live.timezone, reject_count: sum(live.reasons) }),
  rejectsByReason: (live: LiveFixture, date: string | null) => ({
    date,
    timezone: live.timezone,
    reasons: live.reasons.map((reject_count, index) => ({ reason: REASON_CODES[index], reject_count })),
  }),
}

export function livePath(orgSlug: string, branchSlug: string): string {
  return `/api/organizations/${orgSlug}/branches/${branchSlug}`
}

/** Routes for one branch's six Live Today reads. `live` may be a function, to answer differently over time. */
export function liveRoutes(orgSlug: string, branchSlug: string, live: LiveFixture | (() => LiveFixture) = LIVE): ApiRoutes {
  const base = `GET ${livePath(orgSlug, branchSlug)}`
  const now = () => (typeof live === 'function' ? live() : live)
  return {
    [`${base}/pipeline-status`]: () => jsonResponse(200, liveBody.pipeline(now())),
    [`${base}/checkins/count?date=*`]: (url) => jsonResponse(200, liveBody.checkinCount(now(), dateOf(url))),
    [`${base}/checkins/by-hour?date=*`]: (url) => jsonResponse(200, liveBody.checkinsByHour(now(), dateOf(url))),
    [`${base}/checkins/by-destination?date=*`]: (url) => jsonResponse(200, liveBody.checkinsByDestination(now(), dateOf(url))),
    [`${base}/rejects/count?date=*`]: (url) => jsonResponse(200, liveBody.rejectCount(now(), dateOf(url))),
    [`${base}/rejects/by-reason?date=*`]: (url) => jsonResponse(200, liveBody.rejectsByReason(now(), dateOf(url))),
  }
}

// --- range reports ---------------------------------------------------------------------------------------------------

/**
 * What a sorter's four range reports answer, for whatever range is asked. Each day's figures come from the
 * date alone, so any range gives a complete, self-consistent answer.
 *
 *   checkins(date)   that day's check-ins. They are split between two hours: half (rounded down) in `hours[0]`,
 *                    the rest in `hours[1]`.
 *   rejects(date)    that day's rejects. All of a day's rejects have the reason `reasonOn(date)`.
 *   transit          each destination gets `share` of the day's check-ins, rounded down; `other` likewise; the
 *                    rest stay home.
 *   bins             each sort bin gets `share` of the day's check-ins, rounded down; whatever is left has no
 *                    recognized bin. A bin's check-ins are split between the two hours as the day's are.
 */
export interface ReportFixture {
  timezone: string
  /** The product's date today: a range that reaches it is flagged `includes_today`. */
  today: string
  checkins: (date: string) => number
  rejects: (date: string) => number
  hours: [number, number]
  reasonOn: (date: string) => string
  home: string
  transit: Array<[key: string, label: string, share: number]>
  other: number
  /** Sort bins, in numeric order of key. A bin with no check-ins in a range is left out of that range's answer. */
  bins: Array<[key: string, share: number]>
}

const DAY_MS = 86_400_000
const dayNumber = (date: string) => Date.parse(`${date}T00:00:00Z`) / DAY_MS
const dateOfDay = (day: number) => new Date(day * DAY_MS).toISOString().slice(0, 10)
/** Every date from `from` to `to`, both included. */
export function datesBetween(from: string, to: string): string[] {
  return Array.from({ length: dayNumber(to) - dayNumber(from) + 1 }, (_, index) => dateOfDay(dayNumber(from) + index))
}

/**
 * A sorter with a weekly rhythm: 100 check-ins on a Monday, 20 more each day to 180 on a Friday, 60 on a
 * Saturday and none on a Sunday; one reject for every 20 check-ins. A tenth goes to Westside and a twentieth to
 * Library Express. Four sort bins -- 0, 1, 2 and 10 -- take a fifth, three tenths, a quarter and a fifth; the
 * remaining twentieth has no recognized bin. Today is Monday 5 October 2026.
 */
export const REPORT: ReportFixture = {
  timezone: 'America/Chicago',
  today: '2026-10-05',
  checkins: (date) => [0, 100, 120, 140, 160, 180, 60][new Date(`${date}T00:00:00Z`).getUTCDay()],
  rejects: (date) => Math.floor(REPORT.checkins(date) / 20),
  hours: [10, 14],
  reasonOn: (date) => (dayNumber(date) % 2 === 0 ? 'item_not_found' : 'rfid_collision'),
  home: 'Main',
  transit: [
    ['westside', 'Westside', 0.1],
    ['library_express', 'Library Express', 0.05],
  ],
  other: 0,
  bins: [
    ['0', 0.2],
    ['1', 0.3],
    ['2', 0.25],
    ['10', 0.2],
  ],
}

function rangeOf(report: ReportFixture, from: string, to: string) {
  return { from, to, days: datesBetween(from, to).length, timezone: report.timezone, includes_today: to >= report.today }
}

function routedOn(report: ReportFixture, date: string) {
  const checkins = report.checkins(date)
  const transit = report.transit.map(([, , share]) => Math.floor(checkins * share))
  const other = Math.floor(checkins * report.other)
  return { checkins, transit, other, home: checkins - sum(transit) - other }
}

/** The exact body each of the four reports returns for `report` over `from`..`to`. */
export const reportBody = {
  overview: (report: ReportFixture, from: string, to: string) => {
    const days = datesBetween(from, to).map((date) => ({ date, ...routedOn(report, date), rejects: report.rejects(date) }))
    return {
      range: rangeOf(report, from, to),
      checkin_count: sum(days.map((day) => day.checkins)),
      active_days: days.filter((day) => day.checkins > 0).length,
      home_count: sum(days.map((day) => day.home)),
      transit_count: sum(days.map((day) => sum(day.transit))),
      other_count: sum(days.map((day) => day.other)),
      reject_count: sum(days.map((day) => day.rejects)),
      days: days.map((day) => ({ date: day.date, checkin_count: day.checkins, reject_count: day.rejects })),
    }
  },
  volume: (report: ReportFixture, from: string, to: string) => {
    const days = datesBetween(from, to).map((date) => ({ date, checkin_count: report.checkins(date) }))
    const early = sum(days.map((day) => Math.floor(day.checkin_count / 2)))
    const total = sum(days.map((day) => day.checkin_count))
    return {
      range: rangeOf(report, from, to),
      checkin_count: total,
      days,
      hours: Array.from({ length: 24 }, (_, hour) => ({
        hour,
        checkin_count: hour === report.hours[0] ? early : hour === report.hours[1] ? total - early : 0,
      })),
    }
  },
  routing: (report: ReportFixture, from: string, to: string) => {
    const days = datesBetween(from, to).map((date) => ({ date, ...routedOn(report, date) }))
    const transit = report.transit.map(([key, label], slot) => ({ key, label, checkin_count: sum(days.map((day) => day.transit[slot])) }))
    return {
      range: rangeOf(report, from, to),
      checkin_count: sum(days.map((day) => day.checkins)),
      home: { label: report.home, checkin_count: sum(days.map((day) => day.home)) },
      transit,
      transit_count: sum(transit.map((entry) => entry.checkin_count)),
      other_count: sum(days.map((day) => day.other)),
      days: days.map((day) => ({
        date: day.date,
        checkin_count: day.checkins,
        home_count: day.home,
        transit_counts: day.transit,
        other_count: day.other,
      })),
    }
  },
  bins: (report: ReportFixture, from: string, to: string) => {
    const days = datesBetween(from, to).map((date) => report.checkins(date))
    const total = sum(days)
    const bins = report.bins
      .map(([key, share]) => {
        const byDay = days.map((checkins) => Math.floor(checkins * share))
        const early = sum(byDay.map((count) => Math.floor(count / 2)))
        return {
          key,
          checkin_count: sum(byDay),
          hours: Array.from({ length: 24 }, (_, hour) =>
            hour === report.hours[0] ? early : hour === report.hours[1] ? sum(byDay) - early : 0,
          ),
        }
      })
      // Only bins that were observed.
      .filter((bin) => bin.checkin_count > 0)
    const known = sum(bins.map((bin) => bin.checkin_count))
    return { range: rangeOf(report, from, to), checkin_count: total, known_bin_count: known, unknown_bin_count: total - known, bins }
  },
  reliability: (report: ReportFixture, from: string, to: string) => {
    const days = datesBetween(from, to).map((date) => ({
      date,
      checkin_count: report.checkins(date),
      reject_count: report.rejects(date),
    }))
    return {
      range: rangeOf(report, from, to),
      checkin_count: sum(days.map((day) => day.checkin_count)),
      reject_count: sum(days.map((day) => day.reject_count)),
      reasons: REASON_CODES.map((reason) => ({
        reason,
        reject_count: sum(days.filter((day) => report.reasonOn(day.date) === reason).map((day) => day.reject_count)),
      })),
      days,
    }
  },
}

const rangeIn = (url: string): [string, string] => {
  const params = new URL(url, 'http://test.invalid').searchParams
  return [params.get('from') ?? '', params.get('to') ?? '']
}

/**
 * Routes for one sorter's reports page: pipeline status (which names the product's zone) and the four range
 * reports. `report` may be a function, to answer differently over time.
 */
export function reportRoutes(orgSlug: string, branchSlug: string, report: ReportFixture | (() => ReportFixture) = REPORT): ApiRoutes {
  const base = `GET ${livePath(orgSlug, branchSlug)}`
  const now = () => (typeof report === 'function' ? report() : report)
  const answer = (kind: keyof typeof reportBody) => (url: string) => jsonResponse(200, reportBody[kind](now(), ...rangeIn(url)))
  return {
    [`${base}/pipeline-status`]: () => jsonResponse(200, { timezone: now().timezone, state: 'ok', last_reported_at: null }),
    [`${base}/reports/overview?from=*&to=*`]: answer('overview'),
    [`${base}/reports/volume?from=*&to=*`]: answer('volume'),
    [`${base}/reports/routing?from=*&to=*`]: answer('routing'),
    [`${base}/reports/bins?from=*&to=*`]: answer('bins'),
    [`${base}/reports/reliability?from=*&to=*`]: answer('reliability'),
  }
}

// --- organization range reports ----------------------------------------------------------------------------------------

/** One sorter of an organization, for its organization's reports: what the organization lists, and what it did. */
export interface OrganizationSorterFixture {
  slug: string
  name: string
  host_branch: { slug: string; name: string }
  status: string
  collector_count: number
  /** What it reports -- or null for a sorter that is registered but has no data to read. */
  report: ReportFixture | null
}

/**
 * What an organization's three range reports answer for `sorters` over `from`..`to`: each sorter's own figures
 * (reportBody), added up the way the API adds them. `today` flags a range that reaches it.
 */
export const organizationReportBody = {
  overview: (sorters: OrganizationSorterFixture[], from: string, to: string, today = REPORT.today) => {
    const dates = datesBetween(from, to)
    const read = sorters.map((sorter) => (sorter.report === null ? null : reportBody.overview(sorter.report, from, to)))
    const total = (pick: (overview: ReturnType<typeof reportBody.overview>) => number) =>
      sum(read.map((overview) => (overview === null ? 0 : pick(overview))))
    return {
      range: { from, to, days: dates.length, timezone: REPORT.timezone, includes_today: to >= today },
      totals: {
        checkin_count: total((overview) => overview.checkin_count),
        home_count: total((overview) => overview.home_count),
        transit_count: total((overview) => overview.transit_count),
        other_count: total((overview) => overview.other_count),
        reject_count: total((overview) => overview.reject_count),
      },
      sorters: sorters.map((sorter, index) => {
        const overview = read[index]
        return {
          slug: sorter.slug,
          name: sorter.name,
          host_branch: sorter.host_branch,
          status: sorter.status,
          collector_count: sorter.collector_count,
          available: overview !== null,
          checkin_count: overview?.checkin_count ?? 0,
          active_days: overview?.active_days ?? 0,
          transit_count: overview?.transit_count ?? 0,
          reject_count: overview?.reject_count ?? 0,
        }
      }),
      days: dates.map((date, index) => ({
        date,
        checkin_count: total((overview) => overview.days[index].checkin_count),
        reject_count: total((overview) => overview.days[index].reject_count),
      })),
    }
  },
  routingNetwork: (sorters: OrganizationSorterFixture[], from: string, to: string, today = REPORT.today) => {
    const sources = sorters.flatMap((sorter) => {
      if (sorter.report === null) {
        return []
      }
      const routing = reportBody.routing(sorter.report, from, to)
      return [
        {
          sorter: { slug: sorter.slug, name: sorter.name, host_branch: sorter.host_branch },
          checkin_count: routing.checkin_count,
          home: routing.home,
          transit_count: routing.transit_count,
          other_count: routing.other_count,
          transit: routing.transit,
        },
      ]
    })
    // One entry per key, in the order the keys are first met, labelled as the first source labels it.
    const destinations: Array<{ key: string; label: string; checkin_count: number; source_count: number }> = []
    for (const routed of sources.flatMap((source) => source.transit)) {
      const entry = destinations.find((candidate) => candidate.key === routed.key)
      if (entry === undefined) {
        destinations.push({ key: routed.key, label: routed.label, checkin_count: routed.checkin_count, source_count: 1 })
      } else {
        entry.checkin_count += routed.checkin_count
        entry.source_count += 1
      }
    }
    return {
      range: { from, to, days: datesBetween(from, to).length, timezone: REPORT.timezone, includes_today: to >= today },
      totals: {
        checkin_count: sum(sources.map((source) => source.checkin_count)),
        transit_count: sum(sources.map((source) => source.transit_count)),
      },
      sources,
      destinations,
    }
  },
  reliability: (sorters: OrganizationSorterFixture[], from: string, to: string, today = REPORT.today) => {
    const dates = datesBetween(from, to)
    const read = sorters.map((sorter) => (sorter.report === null ? null : reportBody.reliability(sorter.report, from, to)))
    const total = (pick: (reliability: ReturnType<typeof reportBody.reliability>) => number) =>
      sum(read.map((reliability) => (reliability === null ? 0 : pick(reliability))))
    return {
      range: { from, to, days: dates.length, timezone: REPORT.timezone, includes_today: to >= today },
      totals: {
        checkin_count: total((reliability) => reliability.checkin_count),
        reject_count: total((reliability) => reliability.reject_count),
        reasons: REASON_CODES.map((reason, slot) => ({ reason, reject_count: total((reliability) => reliability.reasons[slot].reject_count) })),
      },
      sorters: sorters.map((sorter, index) => ({
        sorter: { slug: sorter.slug, name: sorter.name, host_branch: sorter.host_branch },
        available: read[index] !== null,
        checkin_count: read[index]?.checkin_count ?? 0,
        reject_count: read[index]?.reject_count ?? 0,
        reasons: read[index]?.reasons ?? REASON_CODES.map((reason) => ({ reason, reject_count: 0 })),
      })),
      days: dates.map((date, index) => ({
        date,
        checkin_count: total((reliability) => reliability.days[index].checkin_count),
        reject_count: total((reliability) => reliability.days[index].reject_count),
      })),
    }
  },
}

export function organizationReportsPath(orgSlug: string): string {
  return `/api/organizations/${orgSlug}/reports`
}

/**
 * Routes for one organization's reports page: the three range reports, and each sorter's pipeline status (which
 * names the product's zone) -- a 404 for a sorter with no data to read. `sorters` may be a function, to answer
 * differently over time.
 */
export function organizationReportRoutes(
  orgSlug: string,
  sorters: OrganizationSorterFixture[] | (() => OrganizationSorterFixture[]),
): ApiRoutes {
  const base = `GET ${organizationReportsPath(orgSlug)}`
  const now = () => (typeof sorters === 'function' ? sorters() : sorters)
  const answer = (kind: keyof typeof organizationReportBody) => (url: string) =>
    jsonResponse(200, organizationReportBody[kind](now(), ...rangeIn(url)))
  return {
    ...Object.fromEntries(
      now().map((sorter) => [
        `GET ${livePath(orgSlug, sorter.host_branch.slug)}/pipeline-status`,
        () =>
          sorter.report === null
            ? jsonResponse(404, TENANT_NOT_FOUND)
            : jsonResponse(200, { timezone: sorter.report.timezone, state: 'ok', last_reported_at: null }),
      ]),
    ),
    [`${base}/overview?from=*&to=*`]: answer('overview'),
    [`${base}/routing-network?from=*&to=*`]: answer('routingNetwork'),
    [`${base}/reliability?from=*&to=*`]: answer('reliability'),
  }
}
