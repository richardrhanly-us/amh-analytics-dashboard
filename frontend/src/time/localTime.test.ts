import { describe, expect, it } from 'vitest'

import { formatLocalInstant } from './localTime.ts'

describe('formatLocalInstant', () => {
  it('writes a date, a time and the zone, as words rather than a timestamp', () => {
    const written = formatLocalInstant('2026-10-05T18:50:00Z') as string

    expect(written).toMatch(/^[A-Z][a-z]{2} \d{1,2}, \d{4}, \d{1,2}:\d{2} (AM|PM) \S+$/)
    expect(written).not.toMatch(/T\d|Z$|\+00:00/)
  })

  it('is the reader’s own clock: the day, hour and minute are those of this machine’s zone', () => {
    // Whatever zone the tests run in, the parts must agree with what that zone's clock reads -- not with UTC's.
    for (const instant of ['2026-10-05T18:50:00Z', '2026-01-01T00:05:00Z', '2026-06-30T23:59:59Z', '2026-03-08T07:30:00Z']) {
      const local = new Date(instant)
      const hour = local.getHours() % 12 === 0 ? 12 : local.getHours() % 12
      const minute = String(local.getMinutes()).padStart(2, '0')
      const month = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][local.getMonth()]

      const written = formatLocalInstant(instant) as string

      expect(written.startsWith(`${month} ${local.getDate()}, ${local.getFullYear()}, ${hour}:${minute} ${local.getHours() < 12 ? 'AM' : 'PM'} `)).toBe(true)
    }
  })

  it('reads the two ways the API writes UTC as the same instant', () => {
    expect(formatLocalInstant('2026-10-05T18:50:00+00:00')).toBe(formatLocalInstant('2026-10-05T18:50:00Z'))
    expect(formatLocalInstant('2026-10-05T18:50:00.544504Z')).toBe(formatLocalInstant('2026-10-05T18:50:00Z'))
  })

  it.each([null, undefined, '', 'yesterday', '2026-13-45T00:00:00Z'])('is null for %j: there is nothing truthful to show', (value) => {
    expect(formatLocalInstant(value)).toBeNull()
  })
})
