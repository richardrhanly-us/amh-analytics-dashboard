/**
 * Calendar-date arithmetic for a report's range. A calendar date
 * (YYYY-MM-DD) has no time zone: everything here is arithmetic on the date
 * as written, and none of it reads a clock or the browser's zone. Which date
 * is "today" is the product's to say, and is always passed in.
 */

/**
 * The most days one report may cover, both ends included: the API's
 * engineering guard (MAX_REPORT_RANGE_DAYS), mirrored. It is not how far back
 * an organization may look -- that is its plan's (earliestDate below), and a
 * plan with no history limit has none, however long this is.
 */
export const MAX_RANGE_DAYS = 3660

/** The longest range the Holds report covers, whatever the plan allows (the API's HOLDS_MAX_RANGE_DAYS). */
export const HOLDS_MAX_RANGE_DAYS = 92

export interface DateRange {
  from: string
  to: string
}

const MS_PER_DAY = 86_400_000
const CALENDAR_DATE = /^(\d{4})-(\d{2})-(\d{2})$/

/** The date's day number, or null if it is not a real calendar date. */
function dayNumber(date: string): number | null {
  const match = CALENDAR_DATE.exec(date)
  if (match === null) {
    return null
  }
  const utc = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
  // Date rolls an impossible date over into a real one (30 February). That is not the date that was written.
  return new Date(utc).toISOString().slice(0, 10) === date ? utc / MS_PER_DAY : null
}

export function isCalendarDate(date: string): boolean {
  return dayNumber(date) !== null
}

/** The date `days` after `date` (before it, if negative). */
export function addDays(date: string, days: number): string {
  const start = dayNumber(date)
  if (start === null) {
    throw new RangeError('Not a calendar date.')
  }
  return new Date((start + days) * MS_PER_DAY).toISOString().slice(0, 10)
}

/** How many calendar dates a range covers, both ends included. */
export function daysInRange(range: DateRange): number {
  const first = dayNumber(range.from)
  const last = dayNumber(range.to)
  if (first === null || last === null) {
    throw new RangeError('Not a calendar date.')
  }
  return last - first + 1
}

/** Every date of a range, in order. */
export function datesInRange(range: DateRange): string[] {
  return Array.from({ length: Math.max(daysInRange(range), 0) }, (_, index) => addDays(range.from, index))
}

/** The ranges offered as one-click choices. Each ends on the product's current date. */
export const PRESETS = [
  { days: 7, label: 'Last 7 days' },
  { days: 30, label: 'Last 30 days' },
  { days: 90, label: 'Last 90 days' },
] as const

/** The preset a report page opens on. */
export const DEFAULT_PRESET_DAYS = 30

/**
 * The presets an organization can use: those that reach no further back than
 * its plan allows. `historyDays` is pages/capabilities' historyDays -- null
 * for no limit.
 */
export function presetsFor(historyDays: number | null): ReadonlyArray<(typeof PRESETS)[number]> {
  return PRESETS.filter(({ days }) => historyDays === null || days <= historyDays)
}

/** This year so far: 1 January of today's year, to today. */
export function yearToDate(today: string): DateRange {
  return { from: `${today.slice(0, 4)}-01-01`, to: today }
}

/**
 * Whether "Year to date" can be offered: only when 1 January is inside the
 * plan's window. It is never shortened to fit -- a shorter range is not this
 * year so far.
 */
export function offersYearToDate(today: string, historyDays: number | null): boolean {
  const earliest = earliestDate(today, historyDays)
  return earliest === null || yearToDate(today).from >= earliest
}

/** The first date a report may start on, or null for no earliest date. */
export function earliestDate(today: string, historyDays: number | null): string | null {
  return historyDays === null ? null : addDays(today, -(historyDays - 1))
}

/** The range a report page opens on: the default preset, or the whole window if the plan allows less. */
export function defaultRange(today: string, historyDays: number | null): DateRange {
  return lastDays(historyDays === null ? DEFAULT_PRESET_DAYS : Math.min(DEFAULT_PRESET_DAYS, historyDays), today)
}

/** The last `days` days, ending on `today` and including it. */
export function lastDays(days: number, today: string): DateRange {
  return { from: addDays(today, -(days - 1)), to: today }
}

/** The preset a range is, if it is one: same length, ending today. */
export function presetOf(range: DateRange, today: string): number | null {
  const preset = PRESETS.find(({ days }) => range.to === today && range.from === addDays(today, -(days - 1)))
  return preset?.days ?? null
}

export type RangeProblem = 'incomplete' | 'order' | 'future' | 'too_long' | 'before_history'

/**
 * What is wrong with a range someone typed, or null if it can be asked for.
 * The same rules, in the same order, as the API applies -- so a range that
 * passes here is not then refused there.
 */
export function rangeProblem(range: DateRange, today: string, earliest: string | null = null): RangeProblem | null {
  if (!isCalendarDate(range.from) || !isCalendarDate(range.to)) {
    return 'incomplete'
  }
  if (range.from > range.to) {
    return 'order'
  }
  if (range.to > today) {
    return 'future'
  }
  if (daysInRange(range) > MAX_RANGE_DAYS) {
    return 'too_long'
  }
  // How far back the organization's plan lets a range start (`earliest`, from earliestDate): the API's last rule.
  return earliest !== null && range.from < earliest ? 'before_history' : null
}

const PROBLEM_TEXT: Record<RangeProblem, string> = {
  incomplete: 'Enter both a start date and an end date.',
  order: 'The start date must be on or before the end date.',
  future: 'The end date cannot be after today.',
  too_long: `A single report can cover up to ${MAX_RANGE_DAYS.toLocaleString('en-US')} days. Choose a shorter range.`,
  before_history: "The start date is before this organization's available reporting window.",
}

export function rangeProblemText(problem: RangeProblem): string {
  return PROBLEM_TEXT[problem]
}
