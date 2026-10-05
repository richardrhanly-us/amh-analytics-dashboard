/**
 * Clock and calendar in the PRODUCT's time zone -- the IANA zone the API names
 * -- and never the browser's. Someone looking at a library's dashboard from
 * another zone, or from a laptop set to the wrong one, still sees the
 * library's "today" and the library's hour.
 *
 * Every function takes the instant and the zone as arguments and reads no
 * clock of its own. Built on Intl only.
 */

/** Whether `timeZone` is an IANA zone this browser can compute in. */
export function isValidTimeZone(timeZone: unknown): timeZone is string {
  if (typeof timeZone !== 'string' || timeZone === '') {
    return false
  }
  try {
    new Intl.DateTimeFormat('en-US', { timeZone })
    return true
  } catch {
    return false
  }
}

function parts(instant: Date, timeZone: string): Record<string, string> {
  if (Number.isNaN(instant.getTime())) {
    throw new RangeError('Not a valid instant.')
  }
  // Throws a RangeError for a zone that is not valid: there is no fallback to the browser's zone.
  const formatter = new Intl.DateTimeFormat('en-US', {
    timeZone,
    calendar: 'gregory',
    numberingSystem: 'latn',
    hourCycle: 'h23',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
  })
  return Object.fromEntries(formatter.formatToParts(instant).map((part) => [part.type, part.value]))
}

/** The calendar date at `instant` in `timeZone`, as YYYY-MM-DD. */
export function productDate(instant: Date, timeZone: string): string {
  const { year, month, day } = parts(instant, timeZone)
  return `${year.padStart(4, '0')}-${month}-${day}`
}

/** The wall-clock hour at `instant` in `timeZone`, 0 to 23. */
export function productHour(instant: Date, timeZone: string): number {
  return Number(parts(instant, timeZone).hour) % 24
}

/**
 * An instant as a person reads it in `timeZone`, zone abbreviation included:
 * "Oct 5, 2026, 1:45 PM CDT". Null for a missing instant, and for a value
 * that is not one -- there is nothing truthful to show for either.
 */
export function formatInstant(instant: string | number | Date | null | undefined, timeZone: string): string | null {
  if (instant === null || instant === undefined || instant === '') {
    return null
  }
  const date = new Date(instant)
  if (Number.isNaN(date.getTime()) || !isValidTimeZone(timeZone)) {
    return null
  }
  return new Intl.DateTimeFormat('en-US', {
    timeZone,
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZoneName: 'short',
  }).format(date)
}

/** A calendar date (YYYY-MM-DD) written out: "Monday, October 5, 2026". Null if it is not a date. */
export function formatCalendarDate(date: string): string | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date)
  if (match === null) {
    return null
  }
  // A calendar date has no zone. Noon UTC on that date is the same date everywhere UTC formatting is used.
  const noon = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]), 12))
  // Date rolls an impossible date over into a real one (30 February, month 13). That is not the date asked for.
  if (noon.toISOString().slice(0, 10) !== date) {
    return null
  }
  return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', dateStyle: 'full' }).format(noon)
}

/** A wall-clock hour, 0 to 23, as a 12-hour label: 0 is "12 AM", 13 is "1 PM". */
export function formatHour(hour: number): string {
  return `${hour % 12 === 0 ? 12 : hour % 12} ${hour < 12 ? 'AM' : 'PM'}`
}
