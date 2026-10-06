/**
 * The figures a report works out from the counts the API returns. Pure: no
 * clock, no request, no time zone.
 *
 * THE DEFINITIONS, each with its denominator:
 *
 *   average per calendar day   check-ins / every calendar day of the range, including days with none
 *   average per active day     check-ins / days that had at least one check-in. A secondary figure.
 *   weekday average            check-ins on that weekday / how many times that weekday falls in the range,
 *                              including the times nothing happened
 *   hourly average             check-ins in that wall-clock hour across the range / every calendar day of it
 *   busiest weekday            the highest weekday average; a tie goes to the earlier weekday, Monday first
 *   busiest hour               the highest hourly total; a tie goes to the earlier hour
 *   busiest day                the highest daily total; a tie goes to the earlier date
 *   transit rate               transit / check-ins
 *   reject rate                rejects / check-ins. A ratio of two counts, not a share: rejects are not check-ins.
 *
 * A FIGURE WITH NO DENOMINATOR DOES NOT EXIST. Every function that divides
 * returns null when there is nothing to divide by, and "busiest" is null
 * when nothing happened at all. Null is shown as words -- never as 0, 0% or
 * NaN.
 *
 * Rates are always made from two totals. Nothing here averages percentages.
 */

/** `total / divisor`, or null when there is nothing to divide by. */
export function average(total: number, divisor: number): number | null {
  return divisor > 0 ? total / divisor : null
}

/** `part` as a percentage of `whole`, or null when `whole` is zero. */
export function percentOf(part: number, whole: number): number | null {
  return whole > 0 ? (part / whole) * 100 : null
}

export interface DayCount {
  date: string
  checkin_count: number
}

export interface HourCount {
  hour: number
  checkin_count: number
}

/** How many of the days had at least one check-in. */
export function activeDays(days: readonly DayCount[]): number {
  return days.filter((day) => day.checkin_count > 0).length
}

/** The day with the most check-ins, or null if there were none at all. The earliest of equals. */
export function busiestDay(days: readonly DayCount[]): DayCount | null {
  let busiest: DayCount | null = null
  for (const day of days) {
    if (day.checkin_count > 0 && (busiest === null || day.checkin_count > busiest.checkin_count)) {
      busiest = day
    }
  }
  return busiest
}

/** The hour with the highest total, or null if there were no check-ins at all. The earliest of equals. */
export function busiestHour(hours: readonly HourCount[]): HourCount | null {
  let busiest: HourCount | null = null
  for (const hour of hours) {
    if (hour.checkin_count > 0 && (busiest === null || hour.checkin_count > busiest.checkin_count)) {
      busiest = hour
    }
  }
  return busiest
}

/** ISO weekday numbers: Monday is 1, Sunday is 7. */
export const WEEKDAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'] as const

/** The ISO weekday (1 to 7) of a calendar date. Arithmetic on the date as written: no zone is involved. */
export function weekdayOf(date: string): number {
  const [year, month, day] = date.split('-').map(Number)
  const weekday = new Date(Date.UTC(year, month - 1, day)).getUTCDay()
  return weekday === 0 ? 7 : weekday
}

export interface WeekdayAverage {
  /** 1 (Monday) to 7 (Sunday). */
  weekday: number
  name: string
  /** How many times this weekday falls in the range. */
  occurrences: number
  /** Check-ins on all of them together. */
  checkin_count: number
  /** `checkin_count / occurrences`, or null for a weekday the range does not contain. */
  average: number | null
}

/** One entry for each weekday, Monday first. A weekday with no check-ins still counts every time it occurs. */
export function weekdayAverages(days: readonly DayCount[]): WeekdayAverage[] {
  return WEEKDAY_NAMES.map((name, index) => {
    const onThisWeekday = days.filter((day) => weekdayOf(day.date) === index + 1)
    const total = onThisWeekday.reduce((sum, day) => sum + day.checkin_count, 0)
    return {
      weekday: index + 1,
      name,
      occurrences: onThisWeekday.length,
      checkin_count: total,
      average: average(total, onThisWeekday.length),
    }
  })
}

/** The weekday with the highest average, or null if there were no check-ins. The earliest of equals, Monday first. */
export function busiestWeekday(weekdays: readonly WeekdayAverage[]): WeekdayAverage | null {
  let busiest: WeekdayAverage | null = null
  for (const weekday of weekdays) {
    if (weekday.average !== null && weekday.average > 0 && (busiest === null || weekday.average > (busiest.average as number))) {
      busiest = weekday
    }
  }
  return busiest
}

export interface HourAverage {
  hour: number
  checkin_count: number
  /** `checkin_count / rangeDays`, or null for a range of no days. */
  average: number | null
}

/** Each hour's total as an average over EVERY calendar day of the range, whether or not that hour was ever busy. */
export function hourlyAverages(hours: readonly HourCount[], rangeDays: number): HourAverage[] {
  return hours.map((hour) => ({ ...hour, average: average(hour.checkin_count, rangeDays) }))
}

// --- how a figure is written -----------------------------------------------------------------------------------------

export const NOT_AVAILABLE = 'Not available'

export function formatCount(value: number): string {
  return value.toLocaleString('en-US')
}

/** A percentage to one decimal place: "11.4%". */
export function formatPercent(value: number): string {
  return `${value.toFixed(1)}%`
}

/** An average: one decimal place below 100 ("42.5"), a whole number with separators from there up ("1,274"). */
export function formatAverage(value: number): string {
  return value < 100
    ? value.toLocaleString('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 })
    : Math.round(value).toLocaleString('en-US')
}

/** A calendar date, written out: "Jun 10, 2026". No zone is involved: it is the date as the API gave it. */
export function formatDate(date: string): string {
  const [year, month, day] = date.split('-').map(Number)
  return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric', year: 'numeric' }).format(
    new Date(Date.UTC(year, month - 1, day, 12)),
  )
}

/** A calendar date with its weekday, for a table row: "Wed, Jun 10". */
export function formatDayOfWeek(date: string): string {
  const [year, month, day] = date.split('-').map(Number)
  return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', weekday: 'short', month: 'short', day: 'numeric' }).format(
    new Date(Date.UTC(year, month - 1, day, 12)),
  )
}

/** A calendar date as an axis label: "Jun 10". */
export function formatShortDate(date: string): string {
  const [year, month, day] = date.split('-').map(Number)
  return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric' }).format(
    new Date(Date.UTC(year, month - 1, day, 12)),
  )
}
