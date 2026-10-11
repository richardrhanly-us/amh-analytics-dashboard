import type { Figure } from '../liveToday/MetricCard.tsx'
import { formatAverage, formatChange, formatCount, formatPercent, NOT_AVAILABLE, percentChange, pointChange } from './derive.ts'
import type { ReportRead } from './useSorterReports.ts'

/** How a report's figures are put on a card. Shared by the sorter's reports and the organization's. */

export const days = (count: number) => `${formatCount(count)} ${count === 1 ? 'day' : 'days'}`

export const count = (value: number, note?: string): Figure => ({ tone: 'value', text: formatCount(value), note })

/** A figure that needs a denominator: shown as words when there is none. */
export const derived = (value: number | null, format: (value: number) => string, note?: string, noneNote?: string): Figure =>
  value === null ? { tone: 'empty', text: NOT_AVAILABLE, note: noneNote } : { tone: 'value', text: format(value), note }

export const transitNote = (rate: number | null) => (rate === null ? 'Transit rate not available' : `${formatPercent(rate)} of check-ins`)
export const rejectNote = (rate: number | null) => (rate === null ? 'Reject rate not available' : `${formatPercent(rate)} reject rate`)

// --- a report beside the period before it ----------------------------------------------------------------------------

/**
 * The period a report is being compared with, and the read of that period's
 * own report. Absent when nothing is being compared. The read is a second,
 * separate one: whatever becomes of it, the report it is set beside stands.
 */
export interface Comparison<T> {
  /** The range the previous report covers (dateRange's previousRange). */
  range: { from: string; to: string }
  read: ReportRead<T>
}

/**
 * The line a card says about the previous period: nothing when nothing is
 * being compared or the previous report could not be read (the section says
 * that once, in one place), words while it is on its way, and then what
 * `write` makes of it.
 */
export function changeFrom<T>(comparison: Comparison<T> | undefined, write: (previous: T) => string): string | undefined {
  if (comparison === undefined) {
    return undefined
  }
  const { section } = comparison.read
  if (section.status === 'ready') {
    return write(section.data)
  }
  return section.status === 'loading' ? 'Comparing…' : undefined
}

/**
 * How a figure changed from the previous period's: a percentage of the
 * previous one, with the previous one beside it. A figure that does not
 * exist in either period has no change; a previous figure of zero has no
 * percentage, and that is said -- never an invented one.
 */
function figureChange(current: number | null, previous: number | null, write: (value: number) => string): string {
  if (current === null || previous === null) {
    return 'Change not available'
  }
  const change = percentChange(current, previous)
  return change === null ? `Percentage change not available: ${write(previous)} in the previous period` : `${formatChange(change, '%')} from ${write(previous)}`
}

export const countChange = (current: number, previous: number) => figureChange(current, previous, formatCount)
export const averageChange = (current: number | null, previous: number | null) => figureChange(current, previous, formatAverage)

/** A count with a rate under it: the count's change as a percentage, then the rate's in percentage points. */
export function countAndRateChange(current: number, previous: number, currentRate: number | null, previousRate: number | null): string {
  const points = pointChange(currentRate, previousRate)
  const rate = points === null ? 'rate change not available' : points === 0 ? 'rate unchanged' : `rate ${formatChange(points, ' points').toLowerCase()}`
  return `${countChange(current, previous)}; ${rate}`
}
