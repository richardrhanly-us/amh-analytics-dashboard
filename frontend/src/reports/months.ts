import { formatDayOfWeek, formatShortDate } from './derive.ts'

/**
 * A daily series shown a calendar month at a time, once a range is too long
 * to read day by day. DISPLAY ONLY: the API's daily figures are what every
 * total, average, "busiest day" and typical week or day is worked out from.
 * Grouping changes what a chart's bars and its table's rows are, and nothing
 * else -- the months of a series add up to exactly what its days do.
 */

/** Up to this many days a series is shown day by day, as it always was; longer, a month at a time. */
export const DAILY_DISPLAY_MAX_DAYS = 92

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** One calendar month of a daily series: only the range's own days in it, and their figures added up. */
export interface MonthTotal {
  /** "2025-10" */
  key: string
  /** "Oct 2025" */
  label: string
  /** The first and last of the range's days in this month. */
  first: string
  last: string
  /** The range starts after the month's first day, or ends before its last. */
  partial: boolean
  /** One sum for each figure of a day, in the order `values` gives them. */
  values: number[]
}

function lastDayOfMonth(year: number, month: number): number {
  return new Date(Date.UTC(year, month, 0)).getUTCDate()
}

/**
 * Daily records (each with a `date`, YYYY-MM-DD, in order) grouped into the
 * calendar months they fall in, in order. `values` picks a day's figures;
 * each month's are their sums. Only the days given are counted: a month the
 * range only touches is partial, and nothing outside the range is assumed.
 */
export function groupByMonth<T extends { date: string }>(days: readonly T[], values: (day: T) => readonly number[]): MonthTotal[] {
  const months: MonthTotal[] = []
  for (const day of days) {
    const key = day.date.slice(0, 7)
    const figures = values(day)
    const current = months.at(-1)
    if (current === undefined || current.key !== key) {
      const [year, month] = key.split('-').map(Number)
      months.push({ key, label: `${MONTH_NAMES[month - 1]} ${year}`, first: day.date, last: day.date, partial: false, values: [...figures] })
    } else {
      if (figures.length !== current.values.length) {
        throw new RangeError('Every day of a series must have the same figures.')
      }
      current.last = day.date
      figures.forEach((figure, index) => {
        current.values[index] += figure
      })
    }
  }
  for (const month of months) {
    const [year, number] = month.key.split('-').map(Number)
    month.partial = !month.first.endsWith('-01') || Number(month.last.slice(8)) !== lastDayOfMonth(year, number)
  }
  return months
}

/** One bar, and one table row, of a series as it is shown. */
export interface SeriesPoint {
  /** The bar's label: "Jun 10", or "Oct 2025". */
  bar: string
  /** The table row's label: "Wed, Jun 10", or "Oct 2025", with the days it covers when it is a partial month. */
  row: string
  values: number[]
}

export interface TimeSeries {
  monthly: boolean
  /** The first column's heading. */
  period: 'Date' | 'Month'
  points: SeriesPoint[]
}

function partialNote(month: MonthTotal): string {
  const [year, number] = month.key.split('-').map(Number)
  const starts = !month.first.endsWith('-01')
  const ends = Number(month.last.slice(8)) !== lastDayOfMonth(year, number)
  if (starts && ends) {
    return `${formatShortDate(month.first)} – ${formatShortDate(month.last)}`
  }
  return starts ? `from ${formatShortDate(month.first)}` : `to ${formatShortDate(month.last)}`
}

/** A daily series as it is shown: day by day up to DAILY_DISPLAY_MAX_DAYS days, a month at a time beyond. */
export function timeSeries<T extends { date: string }>(days: readonly T[], values: (day: T) => readonly number[]): TimeSeries {
  if (days.length <= DAILY_DISPLAY_MAX_DAYS) {
    return {
      monthly: false,
      period: 'Date',
      points: days.map((day) => ({ bar: formatShortDate(day.date), row: formatDayOfWeek(day.date), values: [...values(day)] })),
    }
  }
  return {
    monthly: true,
    period: 'Month',
    points: groupByMonth(days, values).map((month) => ({
      bar: month.label,
      row: month.partial ? `${month.label} (${partialNote(month)})` : month.label,
      values: month.values,
    })),
  }
}
