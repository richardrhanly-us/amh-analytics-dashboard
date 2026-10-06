import type { Figure } from '../liveToday/MetricCard.tsx'
import { formatCount, formatPercent, NOT_AVAILABLE } from './derive.ts'

/** How a report's figures are put on a card. Shared by the sorter's reports and the organization's. */

export const days = (count: number) => `${formatCount(count)} ${count === 1 ? 'day' : 'days'}`

export const count = (value: number, note?: string): Figure => ({ tone: 'value', text: formatCount(value), note })

/** A figure that needs a denominator: shown as words when there is none. */
export const derived = (value: number | null, format: (value: number) => string, note?: string, noneNote?: string): Figure =>
  value === null ? { tone: 'empty', text: NOT_AVAILABLE, note: noneNote } : { tone: 'value', text: format(value), note }

export const transitNote = (rate: number | null) => (rate === null ? 'Transit rate not available' : `${formatPercent(rate)} of check-ins`)
export const rejectNote = (rate: number | null) => (rate === null ? 'Reject rate not available' : `${formatPercent(rate)} reject rate`)
