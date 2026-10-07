/**
 * An instant as the person looking at the screen reads it: in THEIR time
 * zone, the browser's.
 *
 * This is for things that happened to a person -- when they last signed in --
 * and is the opposite of time/productTime.ts, which is for a library's
 * operating day and never uses the browser's zone. The two are kept apart so
 * that neither is used for the other.
 */

/**
 * "Oct 5, 2026, 1:45 PM CDT", in the browser's zone with its abbreviation, so
 * the reader can tell whose clock it is. Null for a missing instant and for a
 * value that is not one: there is nothing truthful to show for either.
 */
export function formatLocalInstant(instant: string | null | undefined): string | null {
  if (instant === null || instant === undefined || instant === '') {
    return null
  }
  const date = new Date(instant)
  if (Number.isNaN(date.getTime())) {
    return null
  }
  return new Intl.DateTimeFormat('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZoneName: 'short',
  }).format(date)
}
