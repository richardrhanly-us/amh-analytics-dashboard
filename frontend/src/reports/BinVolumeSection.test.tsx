import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  ALICE,
  type ApiRoutes,
  deferred,
  type FetchMock,
  jsonResponse,
  livePath,
  NORTHBRIDGE_DETAIL,
  REPORT,
  reportBody,
  type ReportFixture,
  reportRoutes,
  requestedUrls,
  serveApi,
} from '../test/http.ts'
import { renderApp } from '../test/render.tsx'

const CENTRAL_REPORTS = '/organizations/northbridge/sorters/central/reports'
const API = livePath('northbridge', 'central')
const BINS = `GET ${API}/reports/bins?from=*&to=*`

// 1:50 PM on Monday 5 October 2026 in Chicago (CDT, UTC-5): the fixture's "today".
const NOW = '2026-10-05T18:50:00Z'
// The default range: the last 30 days, ending today. 3,140 check-ins in the default fixture.
const FROM = '2026-09-06'
const TO = '2026-10-05'

const SERVER_ERROR = () => jsonResponse(500, { code: 'internal_error', message: 'Internal server error.' })
const NOT_A_FIGURE = /NaN|Infinity|undefined|null|\[object/
// What the old dashboard said about a bin, and what a bin is not. None of it belongs here.
const NOT_BIN_VOLUME = /exception|overflow|estimated|holds?\b|utiliz|capacity|fullness|routing|destination|westside|library express|item type/i

const report = (changes: Partial<ReportFixture>): ReportFixture => ({ ...REPORT, ...changes })
/** `count` bins numbered from `first`, each with the same share of the check-ins. */
const evenBins = (count: number, share: number, first = 1): ReportFixture['bins'] =>
  Array.from({ length: count }, (_, index) => [String(first + index), share])

/** A signed-in member with `role` at a sorter whose reports answer `fixture`, unless a test replaces a route. */
function serve(fixture: ReportFixture = REPORT, overrides: ApiRoutes = {}, role = 'viewer'): FetchMock {
  return serveApi({
    'GET /api/auth/session': () => jsonResponse(200, ALICE),
    'GET /api/organizations/northbridge': () => jsonResponse(200, { ...NORTHBRIDGE_DETAIL, role }),
    ...reportRoutes('northbridge', 'central', fixture),
    ...overrides,
  })
}

const user = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime.bind(vi) })
const pass = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms))
const main = () => screen.getByRole('main')
const section = () => screen.getByRole('region', { name: 'Bin volume' })
const inSection = () => within(section())
/** The value shown beside a headline label, or null when there is no such card. */
function metric(label: string): HTMLElement | null {
  const term = inSection().queryByText(label, { selector: 'dt' })
  return term === null ? null : (term.nextElementSibling as HTMLElement)
}
const note = (label: string) => metric(label)?.nextElementSibling?.textContent ?? null
const metricLabels = () => Array.from(section().querySelectorAll('dt')).map((term) => term.textContent)
/** A table's body rows, cell by cell. */
function rows(name: string): string[][] {
  return within(screen.getByRole('table', { name }))
    .getAllByRole('row')
    .slice(1)
    .map((row) => Array.from(row.children).map((cell) => cell.textContent ?? ''))
}
const columns = (name: string) =>
  within(screen.getByRole('table', { name }))
    .getAllByRole('columnheader')
    .map((cell) => cell.textContent)
/** Opens the chart's companion table and returns its rows. */
function shareRows(): string[][] {
  const show = screen.queryByRole('button', { name: 'Show table: Check-ins by bin' })
  if (show !== null) {
    fireEvent.click(show)
  }
  return rows('Check-ins by bin')
}
const chart = () => inSection().getByRole('img', { name: 'Bar chart of check-ins in each observed bin' })
const bars = () => Array.from(chart().querySelectorAll('[data-bin]'))
const barNames = () => bars().map((bar) => bar.querySelector('.bin-bar-name')?.textContent)
/** How wide each bar is drawn, as a percentage of the longest. */
const barWidths = () =>
  bars().map((bar) => Number(/max\(([\d.]+)%/.exec((bar.querySelector('.bin-bar-track span') as HTMLElement).style.width)?.[1]))

/** Opens the sorter's reports and waits for the Bin volume section to have loaded. */
async function page() {
  renderApp(CENTRAL_REPORTS)
  await screen.findByRole('region', { name: 'Bin volume' })
  await waitFor(() => expect(section()).not.toHaveTextContent('Loading…'))
  await waitFor(() => expect(main()).not.toHaveTextContent('Loading…'))
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'], shouldAdvanceTime: true })
  vi.setSystemTime(new Date(NOW))
})

afterEach(() => {
  vi.useRealTimers()
})

// =====================================================================================================================
// Where it is, and who sees it
// =====================================================================================================================

describe('where Bin volume is', () => {
  it('is the fourth report: after Routing and before Reliability', async () => {
    serve()

    await page()

    expect(screen.getAllByRole('heading', { level: 4 }).map((heading) => heading.textContent)).toEqual([
      'Overview',
      'Volume & capacity',
      'Routing',
      'Bin volume',
      'Reliability',
    ])
  })

  it('comes before Efficiency for an owner, who has six', async () => {
    serve(REPORT, {}, 'owner')

    await page()

    const headings = screen.getAllByRole('heading', { level: 4 }).map((heading) => heading.textContent)
    expect(headings).toEqual(['Overview', 'Volume & capacity', 'Routing', 'Bin volume', 'Reliability', 'Efficiency'])
  })

  it.each(['viewer', 'member', 'manager', 'admin', 'owner', ''])('is shown to a member with the role %j', async (role) => {
    const fetchMock = serve(REPORT, {}, role)

    await page()

    expect(section()).toBeInTheDocument()
    expect(metric('Known-bin check-ins')).toHaveTextContent(/^2,983$/)
    expect(requestedUrls(fetchMock).filter((url) => url.includes('/reports/bins'))).toEqual([
      `${API}/reports/bins?from=${FROM}&to=${TO}`,
    ])
    expect(section()).not.toHaveTextContent(/owners and administrators|not available to/i)
  })

  it('is called Bin volume and nothing else', async () => {
    serve()

    await page()

    expect(inSection().getByRole('heading', { level: 4 })).toHaveTextContent(/^Bin volume$/)
    expect(main()).not.toHaveTextContent(/bin utili[sz]ation|bin routing/i)
  })

  it('says what it shows, and that only observed bins are listed', async () => {
    serve()

    await page()

    expect(inSection().getByText('Shows which physical sorter bins received check-ins during the selected date range.')).toBeInTheDocument()
    expect(
      inSection().getByText(
        'Only bins observed in this date range are listed. SortView does not currently store the sorter’s configured bin inventory, so a bin with no check-ins during this range may not appear.',
      ),
    ).toBeInTheDocument()
  })

  it('makes one request for the whole section', async () => {
    const fetchMock = serve()

    await page()
    shareRows()
    await pass(1000)

    expect(requestedUrls(fetchMock).filter((url) => url.includes('/reports/bins'))).toHaveLength(1)
  })
})

// =====================================================================================================================
// The headline figures
// =====================================================================================================================

describe('the headline figures', () => {
  it('shows known-bin check-ins, bins observed, unknown-bin check-ins and bin coverage', async () => {
    serve()

    await page()

    // 3,140 check-ins; a twentieth of each day's has no recognized bin.
    expect(metricLabels()).toEqual(['Known-bin check-ins', 'Bins observed', 'Unknown-bin check-ins', 'Bin coverage'])
    expect(metric('Known-bin check-ins')).toHaveTextContent(/^2,983$/)
    expect(note('Known-bin check-ins')).toBe('Of 3,140 check-ins')
    expect(metric('Bins observed')).toHaveTextContent(/^4$/)
    expect(note('Bins observed')).toBe('With check-ins in this date range')
    expect(metric('Unknown-bin check-ins')).toHaveTextContent(/^157$/)
    expect(note('Unknown-bin check-ins')).toBe('No recognized bin value')
    // 2,983 of 3,140.
    expect(metric('Bin coverage')).toHaveTextContent(/^95\.0%$/)
    expect(note('Bin coverage')).toBe('Check-ins with a recognized bin')
  })

  it('leaves out the unknown card, and the sentence about it, when every check-in has a bin', async () => {
    serve(report({ bins: [['1', 0.5], ['2', 0.5]] }))

    await page()

    expect(metricLabels()).toEqual(['Known-bin check-ins', 'Bins observed', 'Bin coverage'])
    expect(metric('Unknown-bin check-ins')).toBeNull()
    expect(metric('Bin coverage')).toHaveTextContent(/^100\.0%$/)
    expect(note('Known-bin check-ins')).toBe('Of 3,140 check-ins')
    expect(inSection().queryByRole('note')).not.toBeInTheDocument()
    expect(section()).not.toHaveTextContent(/unknown|recognized bin value/i)
  })

  it('works coverage out from the two totals, to one decimal place', async () => {
    // A tenth to each of seven bins: 70% of every day's check-ins have a bin.
    serve(report({ bins: evenBins(7, 0.1, 0) }))

    await page()

    expect(metric('Known-bin check-ins')).toHaveTextContent(/^2,198$/)
    expect(metric('Unknown-bin check-ins')).toHaveTextContent(/^942$/)
    expect(metric('Bin coverage')).toHaveTextContent(/^70\.0%$/)
  })

  it('says how many bins were observed, not how many the sorter has', async () => {
    serve(report({ bins: evenBins(6, 0.1) }))

    await page()

    expect(metric('Bins observed')).toHaveTextContent(/^6$/)
    expect(section()).not.toHaveTextContent(/has 6 bins|6 bins in total|of 6 bins|all bins|every bin/i)
  })
})

// =====================================================================================================================
// Check-ins with no recognized bin
// =====================================================================================================================

describe('check-ins with no recognized bin', () => {
  it('are said in words, neutrally, and are not a bin', async () => {
    serve()

    await page()

    expect(inSection().getByRole('note')).toHaveTextContent(
      '157 check-ins did not have a recognized bin value. They are counted here and are not part of any bin below.',
    )
    expect(section()).not.toHaveTextContent(/error|fail|invalid|missing bin|problem|warning|bad data/i)
    // Not a bar, not a row of the table, not a row of the hours.
    expect(barNames()).toEqual(['Bin 0', 'Bin 1', 'Bin 2', 'Bin 10'])
    expect(shareRows().map((row) => row[0])).toEqual(['Bin 0', 'Bin 1', 'Bin 2', 'Bin 10'])
    expect(rows('Bin volume by hour').map((row) => row[0])).toEqual(['Bin 0', 'Bin 1', 'Bin 2', 'Bin 10'])
    expect(section()).not.toHaveTextContent(/Unknown bin\b|Bin unknown/i)
  })

  it('are one check-in in the singular', async () => {
    const one = reportBody.bins(report({ bins: [['1', 1]] }), FROM, TO)
    one.unknown_bin_count = 1
    one.checkin_count += 1
    serve(REPORT, { [BINS]: () => jsonResponse(200, one) })

    await page()

    expect(inSection().getByRole('note')).toHaveTextContent(
      '1 check-in did not have a recognized bin value. It is counted here and is not part of any bin below.',
    )
    expect(note('Known-bin check-ins')).toBe('Of 3,141 check-ins')
  })
})

// =====================================================================================================================
// The chart and its table
// =====================================================================================================================

describe('check-ins by bin', () => {
  it('draws a bar for each observed bin, labelled "Bin {key}", in the order the API gave', async () => {
    serve()

    await page()

    expect(barNames()).toEqual(['Bin 0', 'Bin 1', 'Bin 2', 'Bin 10'])
    expect(bars().map((bar) => bar.querySelector('.bin-bar-value')?.textContent)).toEqual(['628', '942', '785', '628'])
    // The longest bar is the bin with the most; the others are drawn against it.
    expect(barWidths()).toEqual([(628 / 942) * 100, 100, (785 / 942) * 100, (628 / 942) * 100].map((width) => Number(String(width))))
    expect(inSection().getByText('2,983 check-ins across 4 observed bins. Most in one bin: 942, in Bin 1.')).toBeInTheDocument()
  })

  it('is one named image, described by its sentence, with nothing to focus on or hover over', async () => {
    serve()

    await page()

    const summary = document.getElementById(chart().getAttribute('aria-describedby') ?? '')
    expect(summary).toHaveTextContent('2,983 check-ins across 4 observed bins.')
    expect(chart().querySelector('[tabindex], a, button, [title], svg title')).toBeNull()
    expect(Array.from(chart().children).every((bar) => bar.getAttribute('aria-hidden') === 'true')).toBe(true)
  })

  it('has a table of the same figures, with each bin’s share of KNOWN-bin check-ins', async () => {
    serve()
    const person = user()

    await page()
    expect(screen.queryByRole('table', { name: 'Check-ins by bin' })).not.toBeInTheDocument()
    const toggle = screen.getByRole('button', { name: 'Show table: Check-ins by bin' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    toggle.focus()
    await person.keyboard('{Enter}')

    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(toggle).toHaveFocus()
    expect(columns('Check-ins by bin')).toEqual(['Bin', 'Check-ins', 'Share of known-bin check-ins'])
    // Of the 2,983 with a bin -- not of all 3,140: 628 of 3,140 would be 20.0%.
    expect(rows('Check-ins by bin')).toEqual([
      ['Bin 0', '628', '21.1%'],
      ['Bin 1', '942', '31.6%'],
      ['Bin 2', '785', '26.3%'],
      ['Bin 10', '628', '21.1%'],
    ])
    expect(within(screen.getByRole('table', { name: 'Check-ins by bin' })).getAllByRole('rowheader')).toHaveLength(4)
  })

  it('shows the same share beside each bar', async () => {
    serve()

    await page()

    expect(bars().map((bar) => bar.querySelector('.bin-bar-share')?.textContent)).toEqual(['21.1%', '31.6%', '26.3%', '21.1%'])
  })

  it.each([
    [1, [4]],
    [3, [1, 2, 3]],
    [7, [0, 1, 2, 3, 4, 5, 6]],
    [12, Array.from({ length: 12 }, (_, index) => index + 1)],
    [20, Array.from({ length: 20 }, (_, index) => index + 1)],
  ])('renders %i observed bins: a bar, a table row and an hourly row for each, and no other', async (count, numbers) => {
    serve(report({ bins: numbers.map((number) => [String(number), 0.05]) }))

    await page()

    const names = numbers.map((number) => `Bin ${number}`)
    expect(barNames()).toEqual(names)
    expect(shareRows().map((row) => row[0])).toEqual(names)
    expect(rows('Bin volume by hour').map((row) => row[0])).toEqual(names)
    expect(metric('Bins observed')).toHaveTextContent(new RegExp(`^${count}$`))
    // Every bin has its own label: none is dropped to make room.
    expect(new Set(barNames()).size).toBe(count)
    expect(section()).not.toHaveTextContent(NOT_A_FIGURE)
  })

  it('does not fill in the bins between the ones that were observed', async () => {
    serve(report({ bins: [['0', 0.1], ['3', 0.1], ['7', 0.1], ['12', 0.1], ['40', 0.1], ['205', 0.1], ['9999', 0.1]] }))

    await page()

    const names = ['Bin 0', 'Bin 3', 'Bin 7', 'Bin 12', 'Bin 40', 'Bin 205', 'Bin 9999']
    expect(barNames()).toEqual(names)
    expect(shareRows().map((row) => row[0])).toEqual(names)
    expect(rows('Bin volume by hour').map((row) => row[0])).toEqual(names)
    for (const invented of ['Bin 1', 'Bin 2', 'Bin 4', 'Bin 5', 'Bin 6', 'Bin 8']) {
      expect(inSection().queryByText(invented, { exact: true })).not.toBeInTheDocument()
    }
  })

  it('keeps the order the API gave: Bin 2 before Bin 10', async () => {
    serve(report({ bins: [['1', 0.1], ['2', 0.1], ['9', 0.1], ['10', 0.1], ['11', 0.1], ['20', 0.1], ['100', 0.1]] }))

    await page()

    expect(barNames()).toEqual(['Bin 1', 'Bin 2', 'Bin 9', 'Bin 10', 'Bin 11', 'Bin 20', 'Bin 100'])
    expect(barNames()).not.toEqual([...barNames()].sort())
  })

  it('leaves out a bin that had no check-ins in the range, as the API does', async () => {
    // Bin 5's share rounds down to nothing on every day: it was not observed, so it is not sent.
    serve(report({ bins: [['1', 0.5], ['5', 0.001], ['8', 0.3]] }))

    await page()

    expect(barNames()).toEqual(['Bin 1', 'Bin 8'])
    expect(metric('Bins observed')).toHaveTextContent(/^2$/)
  })

  it('sets many bins in two columns of bars, and few in one, by how many there are -- never by an assumed seven', async () => {
    serve(report({ bins: evenBins(20, 0.05) }))

    await page()

    expect(chart()).toHaveClass('bin-bars-many')
  })

  it.each([3, 7, 10])('keeps %i bins in one column of bars', async (count) => {
    serve(report({ bins: evenBins(count, 0.05) }))

    await page()

    expect(chart()).not.toHaveClass('bin-bars-many')
  })
})

// =====================================================================================================================
// Bin 0, and what a bin is not
// =====================================================================================================================

describe('what a bin is', () => {
  it('treats Bin 0 exactly as it treats every other bin', async () => {
    serve(report({ bins: [['0', 0.25], ['1', 0.25], ['2', 0.25]] }))

    await page()

    const [zero, one] = bars()
    const table = shareRows()
    const byHour = rows('Bin volume by hour')
    // The same figures as Bin 1, drawn and written the same way, with nothing added to its name.
    expect(zero.querySelector('.bin-bar-name')).toHaveTextContent(/^Bin 0$/)
    expect(zero.className).toBe(one.className)
    expect(zero.querySelector('.bin-bar-value')?.textContent).toBe(one.querySelector('.bin-bar-value')?.textContent)
    expect(barWidths()[0]).toBe(barWidths()[1])
    expect(table[0].slice(1)).toEqual(table[1].slice(1))
    expect(byHour[0].slice(1)).toEqual(byHour[1].slice(1))
    expect(table[0][0]).toBe('Bin 0')
    // No rejects are taken off it, and it is not set apart.
    expect(inSection().queryByText(/Bin 0\s*[(:—-]/)).not.toBeInTheDocument()
    expect(section()).not.toHaveTextContent(NOT_BIN_VOLUME)
  })

  it('gives no bin a name, a destination or a meaning', async () => {
    serve()

    await page()
    shareRows()

    // Bins are their numbers. The sorter's destinations -- Westside, Library Express -- are the Routing report's.
    expect(section()).not.toHaveTextContent(NOT_BIN_VOLUME)
    expect(section()).not.toHaveTextContent(/reject|transit|home|Main\b/i)
    expect(inSection().queryByRole('link')).not.toBeInTheDocument()
    for (const name of [...barNames(), ...rows('Check-ins by bin').map((row) => row[0])]) {
      expect(name).toMatch(/^Bin \d+$/)
    }
  })

  it('says nothing the old dashboard said about bins, anywhere on the page', async () => {
    serve(REPORT, {}, 'owner')

    await page()

    expect(main()).not.toHaveTextContent(/exception bin|overflow|estimated holds|bin utili[sz]ation|bin routing/i)
  })
})

// =====================================================================================================================
// Bin by hour
// =====================================================================================================================

describe('bin volume by hour', () => {
  it('is a table with a row for each bin and a column for each of the 24 hours, and a total', async () => {
    serve()

    await page()

    const headings = columns('Bin volume by hour')
    expect(headings).toHaveLength(26)
    expect(headings[0]).toBe('Bin')
    expect(headings.slice(1, 25)).toEqual([
      '12 AM', '1 AM', '2 AM', '3 AM', '4 AM', '5 AM', '6 AM', '7 AM', '8 AM', '9 AM', '10 AM', '11 AM',
      '12 PM', '1 PM', '2 PM', '3 PM', '4 PM', '5 PM', '6 PM', '7 PM', '8 PM', '9 PM', '10 PM', '11 PM',
    ])
    expect(headings[25]).toBe('Total')
    const table = screen.getByRole('table', { name: 'Bin volume by hour' })
    expect(within(table).getAllByRole('rowheader').map((cell) => cell.textContent)).toEqual(['Bin 0', 'Bin 1', 'Bin 2', 'Bin 10'])
    expect(rows('Bin volume by hour').every((row) => row.length === 26)).toBe(true)
  })

  it('shows each bin’s check-ins in the hour they came, zeros written as 0, and a total that is the bin’s count', async () => {
    serve()

    await page()

    const sent = reportBody.bins(REPORT, FROM, TO).bins
    const byHour = rows('Bin volume by hour')
    for (const [index, bin] of sent.entries()) {
      expect(byHour[index]).toEqual([`Bin ${bin.key}`, ...bin.hours.map((count) => count.toLocaleString('en-US')), bin.checkin_count.toLocaleString('en-US')])
      // The row adds up to its own total.
      const figures = byHour[index].slice(1, 25).map((cell) => Number(cell.replace(/,/g, '')))
      expect(figures.reduce((sum, value) => sum + value, 0)).toBe(bin.checkin_count)
    }
    // Bin 0: half at 10 AM, half at 2 PM, nothing in any other hour.
    expect(byHour[0][1 + 10]).toBe('314')
    expect(byHour[0][1 + 14]).toBe('314')
    expect(byHour[0][1 + 3]).toBe('0')
    expect(byHour[0][25]).toBe('628')
    expect(byHour.map((row) => row[25])).toEqual(shareRows().map((row) => row[1]))
  })

  it('covers the whole day, not opening hours', async () => {
    const all = reportBody.bins(report({ bins: [['3', 1]] }), FROM, TO)
    all.bins[0].hours = Array.from({ length: 24 }, (_, hour) => hour + 1)
    all.bins[0].checkin_count = 300
    all.known_bin_count = 300
    all.checkin_count = 300
    serve(REPORT, { [BINS]: () => jsonResponse(200, all) })

    await page()

    expect(rows('Bin volume by hour')[0].slice(1)).toEqual([...Array.from({ length: 24 }, (_, hour) => String(hour + 1)), '300'])
  })

  it('says whose hours they are', async () => {
    serve()

    await page()

    const table = screen.getByRole('table', { name: 'Bin volume by hour' })
    expect(document.getElementById(table.getAttribute('aria-describedby') ?? '')).toHaveTextContent(
      'Check-ins in each bin by hour of the day, added up across the date range. Hours are in America/Chicago time.',
    )
  })

  it('scrolls sideways in a box of its own that a keyboard can reach', async () => {
    serve()

    await page()

    const table = screen.getByRole('table', { name: 'Bin volume by hour' })
    const box = table.parentElement as HTMLElement
    expect(box).toHaveClass('table-scroll')
    expect([box.getAttribute('role'), box.getAttribute('tabindex'), box.getAttribute('aria-labelledby')]).toEqual(['group', '0', 'bin-hours-heading'])
    expect(document.getElementById('bin-hours-heading')).toHaveTextContent('Bin volume by hour')
  })

  it('tells nothing by colour alone: every cell is its number, and the tint behind it only repeats it', async () => {
    serve()

    await page()

    const cells = Array.from(screen.getByRole('table', { name: 'Bin volume by hour' }).querySelectorAll('tbody td'))
    expect(cells.every((cell) => /^[\d,]+$/.test(cell.textContent ?? ''))).toBe(true)
    for (const cell of cells) {
      const tinted = (cell as HTMLElement).style.getPropertyValue('--heat') !== ''
      // A tint exactly where there is volume in an hour, and never on a zero or a total.
      expect(tinted).toBe(cell.textContent !== '0' && !cell.classList.contains('cell-total'))
    }
    // The busiest hour of the table is the deepest tint.
    const heats = cells.map((cell) => Number((cell as HTMLElement).style.getPropertyValue('--heat') || 0))
    expect(Math.max(...heats)).toBe(1)
  })

  it('has as many rows as there are bins, twenty included', async () => {
    serve(report({ bins: evenBins(20, 0.05) }))

    await page()

    const byHour = rows('Bin volume by hour')
    expect(byHour).toHaveLength(20)
    expect(byHour[19][0]).toBe('Bin 20')
    expect(byHour.every((row) => row.length === 26)).toBe(true)
  })
})

// =====================================================================================================================
// Nothing to show
// =====================================================================================================================

describe('a range with nothing to chart', () => {
  it('says no check-ins were recorded, and draws nothing, when the range has none', async () => {
    serve(report({ checkins: () => 0, rejects: () => 0 }))

    await page()

    expect(inSection().getByText('No check-ins were recorded in this date range.')).toBeInTheDocument()
    expect(inSection().queryByRole('img')).not.toBeInTheDocument()
    expect(inSection().queryByRole('table')).not.toBeInTheDocument()
    expect(inSection().queryByRole('button')).not.toBeInTheDocument()
    expect(inSection().queryByRole('alert')).not.toBeInTheDocument()
    expect(metricLabels()).toEqual([])
    // What the section is, is still said.
    expect(inSection().getByText(/^Shows which physical sorter bins/)).toBeInTheDocument()
    expect(section()).not.toHaveTextContent(/%|NaN|Infinity|Not available/)
    expect(inSection().queryByRole('heading', { level: 5 })).not.toBeInTheDocument()
  })

  it('says none had a recognized bin, with the counts and no empty chart, when every check-in is unknown', async () => {
    serve(report({ bins: [] }))

    await page()

    expect(inSection().getByText('Check-ins were recorded, but none had a recognized bin value.')).toBeInTheDocument()
    expect(metricLabels()).toEqual(['Known-bin check-ins', 'Bins observed', 'Unknown-bin check-ins', 'Bin coverage'])
    expect(metric('Known-bin check-ins')).toHaveTextContent(/^0$/)
    expect(note('Known-bin check-ins')).toBe('Of 3,140 check-ins')
    expect(metric('Bins observed')).toHaveTextContent(/^0$/)
    expect(metric('Unknown-bin check-ins')).toHaveTextContent(/^3,140$/)
    // A real figure: none of 3,140. Not a division by nothing.
    expect(metric('Bin coverage')).toHaveTextContent(/^0\.0%$/)
    expect(inSection().queryByRole('img')).not.toBeInTheDocument()
    expect(inSection().queryByRole('table')).not.toBeInTheDocument()
    expect(inSection().queryByRole('button')).not.toBeInTheDocument()
    expect(inSection().queryByRole('heading', { level: 5 })).not.toBeInTheDocument()
    // Said once: the sentence above, not that and a notice too.
    expect(inSection().queryByRole('note')).not.toBeInTheDocument()
    expect(section()).not.toHaveTextContent(NOT_A_FIGURE)
  })

  it('goes from an empty range to a full one when the range changes', async () => {
    // Nothing on Sundays: a range of one Sunday is empty.
    serve()
    await page()

    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-10-04' } })
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2026-10-04' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply dates' }))

    expect(await inSection().findByText('No check-ins were recorded in this date range.')).toBeInTheDocument()
    expect(inSection().queryByRole('img')).not.toBeInTheDocument()
  })
})

// =====================================================================================================================
// Loading, failing and trying again, on its own
// =====================================================================================================================

describe('loading and failing on its own', () => {
  it('shows it is loading under its heading while the other reports are already there', async () => {
    const pending = deferred<Response>()
    serve(REPORT, { [BINS]: () => pending.promise })

    renderApp(CENTRAL_REPORTS)
    await screen.findByRole('img', { name: /^Bar chart of rejects/ })

    expect(inSection().getByText('Loading…')).toBeInTheDocument()
    expect(inSection().queryByText(/^Shows which physical sorter bins/)).not.toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'Overview' })).getByText('3,140')).toBeInTheDocument()

    pending.resolve(jsonResponse(200, reportBody.bins(REPORT, FROM, TO)))
    await waitFor(() => expect(metric('Known-bin check-ins')).toHaveTextContent(/^2,983$/))
  })

  it('says it could not load, in its own section, and leaves the other four alone', async () => {
    serve(REPORT, { [BINS]: SERVER_ERROR })

    await page()

    expect(inSection().getByText('Could not load.')).toBeInTheDocument()
    expect(inSection().getByRole('button', { name: 'Try again: Bin volume' })).toBeInTheDocument()
    expect(inSection().queryByRole('img')).not.toBeInTheDocument()
    expect(section()).not.toHaveTextContent('Internal server error.')
    expect(screen.getAllByText('Could not load.')).toHaveLength(1)
    for (const other of ['Overview', 'Volume & capacity', 'Routing', 'Reliability']) {
      expect(within(screen.getByRole('region', { name: other })).getAllByRole('img').length).toBeGreaterThan(0)
    }
  })

  it.each<[string, (body: ReturnType<typeof reportBody.bins>) => unknown]>([
    ['a bin with 23 hours', (body) => void body.bins[0].hours.pop()],
    ['a negative count', (body) => void (body.unknown_bin_count = -1)],
    ['totals that do not add up', (body) => void (body.known_bin_count += 1)],
    ['a bin called "unknown"', (body) => void (body.bins[0].key = 'unknown')],
  ])('treats %s as a failure and shows none of it', async (_label, change) => {
    const body = reportBody.bins(REPORT, FROM, TO)
    change(body)
    serve(REPORT, { [BINS]: () => jsonResponse(200, body) })

    await page()

    expect(inSection().getByText('Could not load.')).toBeInTheDocument()
    expect(section()).not.toHaveTextContent(/2,983|Bin 0|157/)
  })

  it('tries only itself again, keeps the button in place meanwhile, then moves focus to its heading', async () => {
    const again = deferred<Response>()
    let calls = 0
    const fetchMock = serve(REPORT, { [BINS]: () => (calls++ === 0 ? SERVER_ERROR() : again.promise) })
    const person = user()
    await page()
    const retry = inSection().getByRole('button', { name: 'Try again: Bin volume' })
    fetchMock.mockClear()

    await person.click(retry)

    expect(retry).toHaveFocus()
    expect(retry).toHaveAttribute('aria-disabled', 'true')
    expect(retry).not.toBeDisabled()
    expect(inSection().getByText('Trying again…')).toBeInTheDocument()
    await person.click(retry)
    await pass(200)
    expect(requestedUrls(fetchMock).filter((url) => url.includes('/reports/'))).toEqual([`${API}/reports/bins?from=${FROM}&to=${TO}`])

    again.resolve(jsonResponse(200, reportBody.bins(REPORT, FROM, TO)))
    await waitFor(() => expect(metric('Known-bin check-ins')).toHaveTextContent(/^2,983$/))
    expect(screen.getByRole('heading', { level: 4, name: 'Bin volume' })).toHaveFocus()
  })

  it('shows nothing of one range under another', async () => {
    const week = deferred<Response>()
    serve(REPORT, { [BINS]: (url) => (url.includes(`from=${FROM}`) ? jsonResponse(200, reportBody.bins(REPORT, FROM, TO)) : week.promise) })
    const person = user()
    await page()
    expect(metric('Known-bin check-ins')).toHaveTextContent(/^2,983$/)

    await person.click(screen.getByRole('button', { name: 'Last 7 days' }))

    expect(inSection().getByText('Loading…')).toBeInTheDocument()
    expect(section()).not.toHaveTextContent(/2,983|942|Bin 0/)

    week.resolve(jsonResponse(200, reportBody.bins(REPORT, '2026-09-29', '2026-10-05')))
    // Tuesday to Monday: 760 check-ins, 722 with a bin.
    await waitFor(() => expect(metric('Known-bin check-ins')).toHaveTextContent(/^722$/))
    expect(note('Known-bin check-ins')).toBe('Of 760 check-ins')
  })
})
