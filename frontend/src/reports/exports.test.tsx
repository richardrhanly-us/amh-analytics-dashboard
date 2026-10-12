import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { FeatureEntitlement } from '../api/organizations.ts'
import styles from '../index.css?raw'
import { efficiencyRoutes, efficiencyStore } from '../test/efficiency.ts'
import {
  ALICE,
  type FetchMock,
  jsonResponse,
  liveRoutes,
  NORTHBRIDGE,
  NORTHBRIDGE_DETAIL,
  type OrganizationSorterFixture,
  organizationReportRoutes,
  REPORT,
  type ReportFixture,
  reportRoutes,
  requestedUrls,
  serveApi,
} from '../test/http.ts'
import { renderApp } from '../test/render.tsx'
import { valueRoutes, valueStore } from '../test/sortviewValue.ts'
import { REVOKE_AFTER_MS } from './csv.ts'

/**
 * Reports R10C: taking a report out of the app, for an organization whose
 * plan includes exports -- a table as a CSV file, and the open report view
 * on paper (or as a PDF) by the browser's Print.
 *
 * A file is made in the browser from the report already loaded: its figures
 * as values, not as the page writes them, and nothing asked of the API.
 */

// 1:50 PM on Monday 5 October 2026 in Chicago: the fixtures' "today".
const NOW = '2026-10-05T18:50:00Z'
const DEFAULT_RANGE = 'from=2026-09-06&to=2026-10-05'
const SORTER = '/organizations/northbridge/sorters/central'
const SORTER_OPERATIONS = `${SORTER}/reports/operations`
const SORTER_EFFICIENCY = `${SORTER}/reports/efficiency`
const ORG_OPERATIONS = '/organizations/northbridge/reports/operations'
const ORG_VALUE = '/organizations/northbridge/reports/value'

/**
 * 1,274 check-ins and 13 rejects every day -- a figure the page writes with a separator. A tenth of a day's
 * check-ins goes to Westside and a twentieth to Library Express, rounded down: 127 and 63.
 *
 *   the 30 days to 5 October    38,220 check-ins, 5,700 in transit (14.91%), 390 rejects (1.02%)
 */
const BUSY: ReportFixture = { ...REPORT, checkins: () => 1274, rejects: () => 13 }
/** A hundred a day, a tenth to Westside and nowhere else, none rejected. */
const EAST: ReportFixture = { ...REPORT, checkins: () => 100, rejects: () => 0, transit: [['westside', 'Westside', 0.1]], home: 'East Side' }
/** Registered, and with nothing to read yet. */
const NORTH = { slug: 'north', name: 'North Sorter', host_branch: { slug: 'north', name: 'North Branch' }, status: 'active', collector_count: 1 }

const on = (limit_value: number | null = null): FeatureEntitlement => ({ enabled: true, limit_value })
const off = (): FeatureEntitlement => ({ enabled: false, limit_value: null })
const WITH_EXPORTS = { transits: on(), history_days: on(), exports: on() }

let fetchMock: FetchMock
/** Every file the app handed to the browser: its name, and what was in it. */
let downloads: Array<{ name: string; blob: Blob }>
let print: ReturnType<typeof vi.fn>

/** A member of Northbridge with `role` on a plan with `entitlements`: two sorters with data, and one with none. */
function serve(entitlements: Record<string, FeatureEntitlement> = WITH_EXPORTS, role = 'admin'): FetchMock {
  const sorters: OrganizationSorterFixture[] = [
    { ...NORTHBRIDGE_DETAIL.sorters[0], report: BUSY },
    { ...NORTHBRIDGE_DETAIL.sorters[1], report: EAST },
    { ...NORTH, report: null },
  ]
  fetchMock = serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    'GET /api/organizations': () => jsonResponse(200, [{ ...NORTHBRIDGE, role }]),
    'GET /api/organizations/northbridge': () =>
      jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role, sorters: [...NORTHBRIDGE_DETAIL.sorters, NORTH], entitlements }),
    ...liveRoutes('northbridge', 'central'),
    ...reportRoutes('northbridge', 'central', BUSY),
    ...organizationReportRoutes('northbridge', sorters),
    ...efficiencyRoutes('northbridge', 'central', efficiencyStore(), () => fetchMock),
    ...valueRoutes('northbridge', valueStore(), () => fetchMock),
  })
  return fetchMock
}

const main = () => screen.getByRole('main')
const reportRequests = () => requestedUrls(fetchMock).filter((url) => url.includes('/reports/'))
const user = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })
/** The names of the "Download CSV" buttons on the page, in order: which tables can be taken away. */
const csvButtons = () =>
  screen
    .queryAllByRole('button', { name: /^Download CSV: / })
    .map((button) => (button.textContent ?? '').replace('Download CSV: ', ''))
const csvButton = (dataset: string) => screen.getByRole('button', { name: `Download CSV: ${dataset}` })
const printButton = () => screen.queryByRole('button', { name: 'Print report' })

async function shown(lastSection: string) {
  await screen.findByRole('region', { name: lastSection })
  await waitFor(() => expect(main()).not.toHaveTextContent(/Loading…|Comparing…/))
}
const sorterShown = () => shown('Reliability')
const organizationShown = () => shown('System reliability')

/** Presses a table's "Download CSV" and returns the file: its name, its bytes, and its records cell by cell. */
async function download(dataset: string) {
  const before = downloads.length
  await user().click(csvButton(dataset))
  expect(downloads).toHaveLength(before + 1)
  const { name, blob } = downloads[before]
  const bytes = new Uint8Array(await blob.arrayBuffer())
  // Decoded with the byte order mark left out, as a spreadsheet reads it.
  const lines = new TextDecoder('utf-8').decode(bytes).split('\r\n')
  return { name, type: blob.type, bytes, lines, header: lines[0], rows: lines.slice(1, -1) }
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'], shouldAdvanceTime: true })
  vi.setSystemTime(new Date(NOW))
  downloads = []
  // jsdom has neither: a file is "handed to the browser" here by being kept, with the name its link carried.
  let made: Blob | null = null
  URL.createObjectURL = vi.fn((blob: Blob) => {
    made = blob
    return 'blob:sortview-test'
  })
  URL.revokeObjectURL = vi.fn()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    downloads.push({ name: this.download, blob: made as Blob })
  })
  print = vi.fn()
  window.print = print as unknown as typeof window.print
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.useRealTimers()
})

// =====================================================================================================================
// Who is offered it
// =====================================================================================================================

describe('what a plan with exports offers', () => {
  it('a sorter’s Operations: a CSV for every table of figures, and Print report', async () => {
    serve()

    renderApp(SORTER_OPERATIONS)
    await sorterShown()

    expect(csvButtons()).toEqual([
      'Daily check-ins',
      'Typical week',
      'Typical day',
      'Where check-ins went',
      'Daily transit',
      'Check-ins by bin',
      'Bin volume by hour',
      'Daily rejects',
      'Reject reasons',
    ])
    expect(printButton()).toBeInTheDocument()
    // Each is an ordinary button, reached and pressed like any other.
    for (const button of [...screen.getAllByRole('button', { name: /^Download CSV: / }), printButton() as HTMLElement]) {
      expect(button).toHaveAttribute('type', 'button')
      expect(button).not.toHaveAttribute('aria-disabled', 'true')
      expect(button).not.toBeDisabled()
    }
  })

  it('an organization’s Operations: a CSV for every table of figures, and Print report', async () => {
    serve()

    renderApp(ORG_OPERATIONS)
    await organizationShown()

    expect(csvButtons()).toEqual([
      'Daily check-ins',
      'Sorter comparison',
      'Routing by sorter',
      'Destination totals',
      'Routing matrix',
      'Daily rejects',
      'Reject reasons',
      'Rejects by sorter',
    ])
    expect(printButton()).toBeInTheDocument()
  })

  it('nothing to download for a card, a note or a sentence: only tables', async () => {
    serve()

    renderApp(`${SORTER_OPERATIONS}?compare=previous`)
    await sorterShown()

    // Nine tables, nine buttons -- comparing adds a line to the cards and a sentence, and no file.
    expect(csvButtons()).toHaveLength(9)
    expect(screen.queryByRole('button', { name: /^Download CSV: (Overview|Check-ins|Rejects|Holds|Comparison)$/ })).not.toBeInTheDocument()
  })

  it('the Efficiency view can be printed, and has no CSV', async () => {
    serve()

    renderApp(SORTER_EFFICIENCY)
    await shown('Efficiency')

    expect(printButton()).toBeInTheDocument()
    expect(csvButtons()).toEqual([])
  })

  it('the Value view can be printed, and has no CSV', async () => {
    // R10G: a report like the others, with nothing counted in it to take away as a file.
    serve()
    const person = user()

    renderApp(ORG_VALUE)
    await shown('SortView Value')

    expect(printButton()).toBeInTheDocument()
    expect(csvButtons()).toEqual([])
    expect(screen.queryByRole('button', { name: /CSV/ })).not.toBeInTheDocument()
    await person.click(printButton() as HTMLElement)
    expect(print).toHaveBeenCalledTimes(1)
  })

  it('Live Today offers neither', async () => {
    serve()

    renderApp(SORTER)
    await screen.findByText('Check-ins today')

    expect(csvButtons()).toEqual([])
    expect(printButton()).not.toBeInTheDocument()
  })
})

describe('a plan without exports', () => {
  it.each([
    ['has no such feature', { transits: on(), history_days: on() }],
    ['has it switched off', { transits: on(), history_days: on(), exports: off() }],
  ])('that %s offers no CSV and no Print report, on a sorter’s reports or an organization’s', async (_label, entitlements) => {
    serve(entitlements)

    renderApp(SORTER_OPERATIONS)
    await sorterShown()
    expect(csvButtons()).toEqual([])
    expect(printButton()).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/Download CSV|Print report/)
    // The reports themselves are all there, and each chart still has its table.
    expect(screen.getAllByRole('region')).toHaveLength(5)
    expect(screen.getAllByRole('button', { name: /^Show table: / }).length).toBeGreaterThan(0)
  })

  it('offers neither on the organization’s reports or the Efficiency view', async () => {
    serve({ transits: on(), history_days: on() })

    const { unmount } = renderApp(ORG_OPERATIONS)
    await organizationShown()
    expect(csvButtons()).toEqual([])
    expect(printButton()).not.toBeInTheDocument()
    unmount()

    renderApp(SORTER_EFFICIENCY)
    await shown('Efficiency')
    expect(printButton()).not.toBeInTheDocument()
  })

  it('offers no Print report on the Value view either', async () => {
    serve({ transits: on(), history_days: on() })

    renderApp(ORG_VALUE)
    await shown('SortView Value')

    expect(printButton()).not.toBeInTheDocument()
    expect(csvButtons()).toEqual([])
    expect(main()).not.toHaveTextContent(/Download CSV|Print report/)
  })

  it('is about the plan, not the person: every role is offered them when the plan has exports', async () => {
    serve(WITH_EXPORTS, 'viewer')

    renderApp(SORTER_OPERATIONS)
    await sorterShown()

    expect(csvButtons()).toHaveLength(9)
    expect(printButton()).toBeInTheDocument()
  })
})

// =====================================================================================================================
// What a file is
// =====================================================================================================================

describe('a sorter’s tables as CSV files', () => {
  beforeEach(async () => {
    serve()
    renderApp(SORTER_OPERATIONS)
    await sorterShown()
    fetchMock.mockClear()
  })

  it('is made from the report already on the page: pressing it asks the API for nothing', async () => {
    await download('Daily check-ins')
    await download('Reject reasons')
    await download('Bin volume by hour')

    expect(requestedUrls(fetchMock)).toEqual([])
  })

  it('is a UTF-8 CSV file, named for the organization, the sorter, the table and the range', async () => {
    const file = await download('Daily check-ins')

    expect(file.name).toBe('northbridge_central_daily-check-ins_2026-09-06_to_2026-10-05.csv')
    expect(file.type).toBe('text/csv;charset=utf-8')
    expect(Array.from(file.bytes.slice(0, 3))).toEqual([0xef, 0xbb, 0xbf])
    expect(file.lines.at(-1)).toBe('')
  })

  it('takes its link off the page at once, and gives up the file’s address only after the browser has begun the download', async () => {
    await download('Daily check-ins')

    // The click has happened and the link is gone -- and the address the browser is downloading from is still good.
    expect(downloads).toHaveLength(1)
    expect(document.querySelector('a[download]')).toBeNull()
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1)
    expect(URL.revokeObjectURL).not.toHaveBeenCalled()

    // (The clock here also moves with real time, so the two halves are well apart.)
    await act(() => vi.advanceTimersByTimeAsync(REVOKE_AFTER_MS / 2))
    expect(URL.revokeObjectURL).not.toHaveBeenCalled()

    await act(() => vi.advanceTimersByTimeAsync(REVOKE_AFTER_MS / 2))
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:sortview-test')
    // Giving it up asks the API for nothing either.
    expect(requestedUrls(fetchMock)).toEqual([])
  })

  it('gives up each file’s address once, however many are downloaded', async () => {
    await download('Daily check-ins')
    await download('Typical week')
    await download('Reject reasons')
    expect(URL.revokeObjectURL).not.toHaveBeenCalled()

    await act(() => vi.advanceTimersByTimeAsync(REVOKE_AFTER_MS))

    expect(URL.createObjectURL).toHaveBeenCalledTimes(3)
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(3)
  })

  it('daily check-ins: a day to a row, the date as a date and each count as a number -- not as the page writes them', async () => {
    const file = await download('Daily check-ins')

    expect(file.header).toBe('Date,Check-ins,Rejects')
    expect(file.rows).toHaveLength(30)
    expect(file.rows[0]).toBe('2026-09-06,1274,13')
    expect(file.rows[29]).toBe('2026-10-05,1274,13')
    // The page says "1,274" and "Sun, Sep 6"; the file does not.
    expect(file.lines.join('\n')).not.toMatch(/1,274|Sep|Sun|"/)
    for (const row of file.rows) {
      expect(row).toMatch(/^\d{4}-\d{2}-\d{2},\d+,\d+$/)
    }
  })

  it('typical week and typical day: the totals, and the averages the page shows, as numbers', async () => {
    const week = await download('Typical week')
    const day = await download('Typical day')

    expect(week.name).toBe('northbridge_central_typical-week_2026-09-06_to_2026-10-05.csv')
    expect(week.header).toBe('Weekday,Days in range,Check-ins,Average per day')
    // Five Mondays and five Sundays in the range, four of every other day.
    expect(week.rows).toEqual([
      'Monday,5,6370,1274',
      'Tuesday,4,5096,1274',
      'Wednesday,4,5096,1274',
      'Thursday,4,5096,1274',
      'Friday,4,5096,1274',
      'Saturday,4,5096,1274',
      'Sunday,5,6370,1274',
    ])

    expect(day.header).toBe('Hour,Check-ins,Average per day')
    expect(day.rows).toHaveLength(24)
    // The hour is the hour, 0 to 23 -- not "10 AM".
    expect(day.rows[0]).toBe('0,0,0')
    expect(day.rows[10]).toBe('10,19110,637')
    expect(day.rows[14]).toBe('14,19110,637')
    expect(day.rows.map((row) => row.split(',')[0])).toEqual(Array.from({ length: 24 }, (_, hour) => String(hour)))
  })

  it('where check-ins went, and daily transit: each destination, with its share as a number', async () => {
    const destinations = await download('Where check-ins went')
    const transit = await download('Daily transit')

    expect(destinations.header).toBe('Destination,Check-ins,Share of check-ins (%)')
    expect(destinations.rows).toEqual(['Main (home),32520,85.09', 'Westside,3810,9.97', 'Library Express,1890,4.95'])
    expect(destinations.lines.join('\n')).not.toMatch(/%\s*$|\d%/m)

    expect(transit.header).toBe('Date,Total transit,Westside,Library Express')
    expect(transit.rows).toHaveLength(30)
    expect(transit.rows[0]).toBe('2026-09-06,190,127,63')
  })

  it('bins: the bin as the sorter logs it, a count for every hour, and the same total both ways', async () => {
    const byBin = await download('Check-ins by bin')
    const byHour = await download('Bin volume by hour')

    expect(byBin.header).toBe('Bin,Check-ins,Share of known-bin check-ins (%)')
    expect(byBin.rows.map((row) => row.split(',')[0])).toEqual(['0', '1', '2', '10'])
    for (const row of byBin.rows) {
      expect(row).toMatch(/^\d+,\d+,\d+(\.\d+)?$/)
    }

    expect(byHour.header).toBe(`Bin,${Array.from({ length: 24 }, (_, hour) => `Hour ${String(hour).padStart(2, '0')}`).join(',')},Total`)
    expect(byHour.rows.map((row) => row.split(',')[0])).toEqual(['0', '1', '2', '10'])
    for (const row of byHour.rows) {
      const cells = row.split(',').map(Number)
      expect(cells).toHaveLength(26)
      expect(cells.every((cell) => Number.isInteger(cell))).toBe(true)
      // A bin's hours add up to its total.
      expect(cells.slice(1, 25).reduce((sum, value) => sum + value, 0)).toBe(cells[25])
    }
    // Each bin has the same total in both files.
    expect(byHour.rows.map((row) => row.split(',')[25])).toEqual(byBin.rows.map((row) => row.split(',')[1]))
  })

  it('rejects: by day, and by reason with the reason’s code beside its name', async () => {
    const daily = await download('Daily rejects')
    const reasons = await download('Reject reasons')

    expect(daily.header).toBe('Date,Rejects,Check-ins')
    expect(daily.rows[0]).toBe('2026-09-06,13,1274')
    expect(daily.rows).toHaveLength(30)

    expect(reasons.header).toBe('Reason code,Reason,Rejects,Share of rejects (%)')
    // Only the reasons that occurred: 13 a day, on alternate days one reason and the other.
    expect([...reasons.rows].sort()).toEqual(['item_not_found,Item not found,195,50', 'rfid_collision,RFID collision,195,50'])
  })
})

describe('the range a file is for', () => {
  it('is the address’s: its name says so, and its rows are that range’s days', async () => {
    serve()
    renderApp(`${SORTER_OPERATIONS}?from=2026-09-01&to=2026-09-14`)
    await sorterShown()

    const file = await download('Daily rejects')

    expect(file.name).toBe('northbridge_central_daily-rejects_2026-09-01_to_2026-09-14.csv')
    expect(file.rows).toHaveLength(14)
    expect(file.rows[0]).toBe('2026-09-01,13,1274')
    expect(file.rows[13]).toBe('2026-09-14,13,1274')
  })

  it('is still its days when the page shows a long range a month at a time', async () => {
    serve()
    renderApp(`${SORTER_OPERATIONS}?from=2026-01-01&to=2026-10-05`)
    await sorterShown()
    // 278 days: the chart and its table are by month.
    expect(within(screen.getByRole('region', { name: 'Overview' })).getByRole('heading', { level: 5, name: 'Check-ins by month' })).toBeInTheDocument()

    const file = await download('Daily check-ins')

    expect(file.name).toBe('northbridge_central_daily-check-ins_2026-01-01_to_2026-10-05.csv')
    expect(file.header).toBe('Date,Check-ins,Rejects')
    expect(file.rows).toHaveLength(278)
    expect(file.rows[0]).toBe('2026-01-01,1274,13')
    expect(file.rows[277]).toBe('2026-10-05,1274,13')
  })

  it('is the same file whether or not the Overview is being compared with the previous period', async () => {
    serve()
    const { unmount } = renderApp(SORTER_OPERATIONS)
    await sorterShown()
    const plain = await download('Daily check-ins')
    unmount()

    renderApp(`${SORTER_OPERATIONS}?compare=previous`)
    await sorterShown()
    expect(main().querySelectorAll('.metric-change').length).toBeGreaterThan(0)
    const compared = await download('Daily check-ins')

    expect(compared.name).toBe(plain.name)
    expect(compared.lines).toEqual(plain.lines)
    // Nothing of the previous period is in it: its days are the range's, and no line is about a change.
    expect(compared.rows.every((row) => row >= '2026-09-06')).toBe(true)
    expect(compared.lines.join('\n')).not.toMatch(/Up|Down|previous|change/i)
  })
})

describe('an organization’s tables as CSV files', () => {
  beforeEach(async () => {
    serve()
    renderApp(ORG_OPERATIONS)
    await organizationShown()
    fetchMock.mockClear()
  })

  it('is named for the organization, the table and the range, and asks the API for nothing', async () => {
    const file = await download('Daily check-ins')

    expect(file.name).toBe('northbridge_daily-check-ins_2026-09-06_to_2026-10-05.csv')
    expect(file.header).toBe('Date,Check-ins,Rejects')
    expect(file.rows).toHaveLength(30)
    // Both machines together: 1,274 and 100.
    expect(file.rows[0]).toBe('2026-09-06,1374,13')
    expect(requestedUrls(fetchMock)).toEqual([])
  })

  it('sorter comparison: a machine to a row, each figure a number, and empty cells -- not zeros -- for a machine with no data', async () => {
    const file = await download('Sorter comparison')

    expect(file.name).toBe('northbridge_sorter-comparison_2026-09-06_to_2026-10-05.csv')
    expect(file.header).toBe(
      'Sorter,Location,Status,Reporting data,Check-ins,Share of check-ins (%),In transit,Transit rate (%),Rejects,Reject rate (%),Active days,Days in range,Collectors',
    )
    expect(file.rows).toEqual([
      'Central Library AMH,Central Branch,Active,Yes,38220,92.72,5700,14.91,390,1.02,30,30,1',
      'East Side AMH,East Side Branch,Active,Yes,3000,7.28,300,10,0,0,30,30,1',
      // No reporting data: the page calls it unavailable, and so does the file -- never "Active".
      'North Sorter,North Branch,Unavailable,No,,,,,,,,30,1',
    ])
    // The page's own words for these -- "6 of 7", "68.5%", the link's hidden text -- are not in the file.
    expect(file.lines.join('\n')).not.toMatch(/\d of \d|%$|sorter reports|—/m)
    // Only the machine with no data is called unavailable; the two with data keep their own status.
    expect(file.rows.filter((row) => row.includes('Unavailable'))).toHaveLength(1)
    expect(file.rows.filter((row) => row.includes(',Active,Yes,'))).toHaveLength(2)
  })

  it('routing: by sorter, by destination, and the matrix with an empty cell where a machine has no such destination', async () => {
    const sources = await download('Routing by sorter')
    const destinations = await download('Destination totals')
    const matrix = await download('Routing matrix')

    expect(sources.header).toBe('Sorter,Check-ins,Home,Kept at home,In transit,Transit rate (%),Other routing')
    expect(sources.rows).toEqual(['Central Library AMH,38220,Main,32520,5700,14.91,0', 'East Side AMH,3000,East Side,2700,300,10,0'])

    expect(destinations.header).toBe('Destination,Check-ins routed,Sorters with this destination,Sorters with routing data')
    expect(destinations.rows).toEqual(['Westside,4110,2,2', 'Library Express,1890,1,2'])

    expect(matrix.header).toBe('From sorter,Westside,Library Express')
    // East Side routes nothing to Library Express because it has no such destination: empty, not 0.
    expect(matrix.rows).toEqual(['Central Library AMH,3810,1890', 'East Side AMH,300,'])
  })

  it('rejects: by day, by reason, and by sorter with the commonest reason’s code and name', async () => {
    const daily = await download('Daily rejects')
    const reasons = await download('Reject reasons')
    const bySorter = await download('Rejects by sorter')

    expect(daily.rows[0]).toBe('2026-09-06,13,1374')
    expect(reasons.header).toBe('Reason code,Reason,Rejects,Share of rejects (%)')
    expect([...reasons.rows].sort()).toEqual(['item_not_found,Item not found,195,50', 'rfid_collision,RFID collision,195,50'])

    expect(bySorter.header).toBe('Sorter,Reporting data,Check-ins,Rejects,Reject rate (%),Most frequent reason code,Most frequent reason')
    expect(bySorter.rows[0]).toMatch(/^Central Library AMH,Yes,38220,390,1\.02,(item_not_found,Item not found|rfid_collision,RFID collision)$/)
    // No rejects at all: a rate of 0, and no commonest reason to name.
    expect(bySorter.rows[1]).toBe('East Side AMH,Yes,3000,0,0,,')
    expect(bySorter.rows[2]).toBe('North Sorter,No,,,,,')
  })
})

// =====================================================================================================================
// Print
// =====================================================================================================================

describe('Print report', () => {
  it('prints the open view by the browser’s own Print, and asks the API for nothing', async () => {
    serve()
    const person = user()
    renderApp(SORTER_OPERATIONS)
    await sorterShown()
    fetchMock.mockClear()

    await person.click(printButton() as HTMLElement)

    expect(print).toHaveBeenCalledTimes(1)
    expect(downloads).toEqual([])
    expect(requestedUrls(fetchMock)).toEqual([])
  })

  it('is pressed from the keyboard like any button', async () => {
    serve()
    const person = user()
    renderApp(ORG_OPERATIONS)
    await organizationShown()

    ;(printButton() as HTMLElement).focus()
    await person.keyboard('{Enter}')

    expect(print).toHaveBeenCalledTimes(1)
  })

  it('puts every chart’s table on the page while it is printing, and takes them away again after', async () => {
    serve()
    renderApp(`${SORTER_OPERATIONS}?compare=previous`)
    await sorterShown()
    const chartTables = ['Daily check-ins', 'Typical week', 'Typical day', 'Daily transit', 'Check-ins by bin', 'Daily rejects']
    // On screen a chart's table is there only when asked for.
    for (const name of chartTables) {
      expect(screen.queryByRole('table', { name })).not.toBeInTheDocument()
    }

    act(() => {
      window.dispatchEvent(new Event('beforeprint'))
    })

    for (const name of chartTables) {
      expect(screen.getByRole('table', { name })).toBeInTheDocument()
    }
    expect(within(screen.getByRole('table', { name: 'Daily check-ins' })).getAllByRole('row')).toHaveLength(31)
    // What is printed is the view as it stands: the range, and the comparison that is on.
    expect((document.querySelector('.range-shown') as HTMLElement).textContent).toContain('Showing Sep 6, 2026 to Oct 5, 2026: 30 days')
    expect(main().querySelectorAll('.metric-change').length).toBeGreaterThan(0)
    expect(within(screen.getByRole('region', { name: 'Overview' })).getByRole('note')).toHaveTextContent(/^Compared with the previous period/)
    // Whether a table was asked for on screen is not changed by printing.
    expect(screen.getByRole('button', { name: 'Show table: Daily check-ins' })).toHaveAttribute('aria-expanded', 'false')

    act(() => {
      window.dispatchEvent(new Event('afterprint'))
    })

    for (const name of chartTables) {
      expect(screen.queryByRole('table', { name })).not.toBeInTheDocument()
    }
  })

  it('leaves a table that was asked for on screen where it is, before and after', async () => {
    serve()
    const person = user()
    renderApp(SORTER_OPERATIONS)
    await sorterShown()
    await person.click(screen.getByRole('button', { name: 'Show table: Typical week' }))

    act(() => {
      window.dispatchEvent(new Event('beforeprint'))
    })
    act(() => {
      window.dispatchEvent(new Event('afterprint'))
    })

    expect(screen.getByRole('table', { name: 'Typical week' })).toBeInTheDocument()
    expect(screen.queryByRole('table', { name: 'Typical day' })).not.toBeInTheDocument()
    expect(reportRequests().filter((url) => !url.endsWith(DEFAULT_RANGE))).toEqual([])
  })
})

describe('the page a report is printed from', () => {
  const boundary = () => document.querySelectorAll('.report-view')

  it.each([
    ['a sorter’s Operations', SORTER_OPERATIONS, 'Reliability'],
    ['a sorter’s Efficiency', SORTER_EFFICIENCY, 'Efficiency'],
    ['an organization’s Operations', ORG_OPERATIONS, 'System reliability'],
  ])('%s is marked as a report, once, on its own heading, inside the app', async (_label, path, lastSection) => {
    serve()

    renderApp(path)
    await shown(lastSection)

    expect(boundary()).toHaveLength(1)
    expect(boundary()[0]).toHaveRole('heading')
    expect(boundary()[0].closest('.app')).not.toBeNull()
    // What the print styles take off a report is all inside the same `.app`: the header above it included.
    expect(boundary()[0].closest('.app')?.querySelector('.app-header')).not.toBeNull()
  })

  it('is marked whether or not the plan has exports: the browser’s own Print is the same treatment', async () => {
    serve({ transits: on(), history_days: on() })

    renderApp(SORTER_OPERATIONS)
    await sorterShown()

    expect(printButton()).not.toBeInTheDocument()
    expect(boundary()).toHaveLength(1)
  })

  it('the Value view is a report view too, and is marked', async () => {
    serve()

    renderApp(ORG_VALUE)
    await shown('SortView Value')

    expect(boundary()).toHaveLength(1)
  })

  it('the Value view’s edit panel is one marked boundary, with everything for editing inside it and the report outside', async () => {
    serve()
    renderApp(ORG_VALUE)
    await shown('SortView Value')
    const edit = () => document.querySelectorAll('.value-edit-panel')
    const report = screen.getByRole('region', { name: 'SortView Value' })

    // There before it is opened, once, inside the report page the print styles are scoped to.
    expect(edit()).toHaveLength(1)
    expect(edit()[0].closest('.app')?.querySelector('.report-view')).not.toBeNull()
    expect(edit()[0]).toContainElement(screen.getByRole('button', { name: 'Edit assumptions' }))

    fireEvent.click(screen.getByRole('button', { name: 'Edit assumptions' }))
    const form = await screen.findByRole('form', { name: 'Assumptions' })
    fireEvent.change(within(form).getByLabelText('Reporting tasks per month'), { target: { value: 'abc' } })
    fireEvent.click(within(form).getByRole('button', { name: 'Save assumptions' }))
    const failure = await within(form).findByRole('alert')

    // Open, and with a save refused: the form, every field and its help, and what went wrong are all inside it...
    expect(edit()).toHaveLength(1)
    expect(edit()[0]).toContainElement(form)
    expect(edit()[0]).toContainElement(failure)
    for (const inside of report.querySelectorAll('input, select, .field-help, .field-problem, [role="alert"], [role="status"]')) {
      expect(edit()[0]).toContainElement(inside as HTMLElement)
    }
    // ...and on screen it is all still there to be used.
    expect(form).toBeVisible()
    expect(failure).toBeVisible()
    // What is read, not edited, is outside it: the disclosure, the cards, both tables.
    expect(edit()[0]).not.toContainElement(within(report).getByRole('table', { name: 'Assumptions used' }))
    expect(edit()[0]).not.toContainElement(within(report).getByRole('table', { name: 'By kind of work' }))
    expect(edit()[0]).not.toContainElement(within(report).getByText('Estimated ROI'))
    expect(edit()[0]).not.toContainElement(within(report).getByText(/^SortView Value figures are estimates/))
    expect(edit()[0]).not.toContainElement(within(report).getByRole('heading', { name: 'Estimated value' }))
  })

  it('a sorter’s Efficiency has no such boundary: its settings panel prints as it did', async () => {
    serve()
    renderApp(SORTER_EFFICIENCY)
    await shown('Efficiency')

    fireEvent.click(screen.getByRole('button', { name: 'Efficiency settings' }))
    await screen.findAllByRole('form')

    expect(document.querySelectorAll('.assumptions-panel')).toHaveLength(1)
    expect(document.querySelectorAll('.value-edit-panel')).toHaveLength(0)
  })

  it.each([
    ['Live Today', SORTER, 'Check-ins today'],
    ['an organization’s own page', '/organizations/northbridge', 'Sorting machines'],
    ['the list of organizations', '/organizations', 'Northbridge Library'],
    ['Settings', '/organizations/northbridge/settings/general', 'General'],
    ['Efficiency settings', '/organizations/northbridge/settings/efficiency', 'Sorting machine overrides'],
    ['a page that does not exist', '/organizations/northbridge/nowhere', 'Page not found'],
  ])('%s is not a report: nothing marks it, so nothing about how it prints has changed', async (_label, path, landmark) => {
    serve()

    renderApp(path)
    await screen.findAllByText(landmark)

    expect(boundary()).toHaveLength(0)
    // It has things the report styles would take off a report -- and they are not under any report boundary.
    expect(document.querySelector('.app-header')).not.toBeNull()
  })

  it('the sign-in form is not a report', async () => {
    serveApi({ 'GET /api/auth/session': () => jsonResponse(401, { code: 'not_authenticated', message: 'Authentication is required.' }) })

    renderApp(SORTER_OPERATIONS)
    await screen.findByRole('button', { name: 'Sign in' })

    expect(boundary()).toHaveLength(0)
  })

  it('stops being marked on leaving the reports', async () => {
    serve()
    const person = user()
    renderApp(SORTER_OPERATIONS)
    await sorterShown()
    expect(boundary()).toHaveLength(1)

    await person.click(within(screen.getByRole('navigation', { name: 'Sorter views' })).getByRole('link', { name: 'Live Today' }))
    await screen.findByText('Check-ins today')

    expect(boundary()).toHaveLength(0)
  })
})

describe('the print styles', () => {
  // What a browser does with these on paper cannot be seen from here: this is only how they are written.
  const SCOPE = '.app:has(.report-view)'
  const printBlock = styles.slice(styles.indexOf('@media print {') + '@media print {'.length)
  /** Each rule of the print block: its selector, and what it declares. */
  const rules = Array.from(printBlock.matchAll(/([^{}]+)\{([^{}]*)\}/g)).map(([, selector, declarations]) => ({
    selector: selector.replace(/\/\*[\s\S]*?\*\//g, '').trim(),
    declarations,
  }))
  const hiding = rules.filter((rule) => /display:\s*none/.test(rule.declarations))

  it('are one block, for print alone, at the end of the stylesheet', () => {
    expect(styles.match(/@media print/g)).toHaveLength(1)
    expect(rules.length).toBeGreaterThan(5)
    // Nothing follows the block: every rule read above is inside it.
    expect(styles.trimEnd().endsWith('}')).toBe(true)
    expect(styles.slice(styles.indexOf('@media print')).match(/@media/g)).toHaveLength(1)
  })

  it('are every one of them under the report boundary: no rule reaches a page that is not a report', () => {
    for (const rule of rules) {
      expect(rule.selector.startsWith(`${SCOPE} `)).toBe(true)
      // One selector to a rule, so that no second selector after a comma slips out from under the boundary.
      expect(rule.selector.replace(/:is\([^)]*\)/g, '')).not.toContain(',')
    }
  })

  it('take off a report what is there to be pressed, not read -- and only off a report', () => {
    // R10G: and one thing more, off the Value view alone -- its panel for editing assumptions (below).
    expect(hiding).toHaveLength(2)
    expect(hiding[0].selector).toBe(`${SCOPE} :is(.app-header, .view-nav, .range-control, .table-actions, .report-actions, button)`)
  })

  it('take SortView Value’s edit panel off the paper, and nothing of Efficiency’s', () => {
    expect(hiding[1].selector).toBe(`${SCOPE} .value-edit-panel`)
    expect(hiding[1].declarations).toMatch(/display:\s*none\s*!important/)
    // The class the two panels share, and everything of Efficiency's, is in no print rule: it prints as it did.
    expect(printBlock).not.toMatch(/assumptions-(panel|form|forms|actions|saved)|efficiency/i)
    // And on screen nothing hides the panel: the class is in the print block and nowhere else in the stylesheet.
    expect(styles.slice(0, styles.indexOf('@media print')).replace(/\/\*[\s\S]*?\*\//g, '')).not.toContain('value-edit-panel')
  })

  it('leave on a report what says which report it is and what range it is for', () => {
    for (const kept of ['.range-shown', '.breadcrumb', '.page-context', '.metric-change', '.chart-table', '.report-view', 'h2', 'h3']) {
      expect(hiding[0].selector.slice(SCOPE.length).split(/[\s,()]+/)).not.toContain(kept)
    }
  })

  it('keep a wide table whole, repeat a long table’s headings, and do not cut a row, a card or a chart in two', () => {
    const declared = (selector: string) => rules.find((rule) => rule.selector === `${SCOPE} ${selector}`)?.declarations ?? ''

    expect(declared('.table-scroll')).toMatch(/overflow:\s*visible/)
    expect(declared('.data-table thead')).toMatch(/display:\s*table-header-group/)
    expect(declared(':is(.data-table tr, .metric, .notice, .chart, .bin-bar)')).toMatch(/break-inside:\s*avoid/)
    expect(declared('.report-sections')).toMatch(/print-color-adjust:\s*exact/)
  })
})
