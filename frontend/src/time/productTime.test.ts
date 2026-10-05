import { describe, expect, it } from 'vitest'

import { formatCalendarDate, formatHour, formatInstant, isValidTimeZone, productDate, productHour } from './productTime.ts'

const CHICAGO = 'America/Chicago'
const at = (iso: string) => new Date(iso)
// Intl puts a narrow no-break space before AM/PM. A reader sees a space; so does this.
const plain = (text: string | null) => text?.replace(/\s/g, ' ')

describe('productDate', () => {
  it('is the calendar date in America/Chicago', () => {
    expect(productDate(at('2026-10-05T18:50:00Z'), CHICAGO)).toBe('2026-10-05')
    expect(productDate(at('2026-01-15T12:00:00Z'), CHICAGO)).toBe('2026-01-15')
  })

  it('changes at midnight in the product zone, not at midnight UTC', () => {
    // 7:30 PM in Chicago is already tomorrow in UTC.
    expect(productDate(at('2026-10-06T00:30:00Z'), CHICAGO)).toBe('2026-10-05')
    expect(productDate(at('2026-10-06T04:59:59Z'), CHICAGO)).toBe('2026-10-05')
    expect(productDate(at('2026-10-06T05:00:00Z'), CHICAGO)).toBe('2026-10-06')
  })

  it('gives each zone its own date for the same instant', () => {
    const instant = at('2026-10-06T00:30:00Z')

    expect(productDate(instant, 'UTC')).toBe('2026-10-06')
    expect(productDate(instant, 'Asia/Tokyo')).toBe('2026-10-06')
    expect(productDate(instant, CHICAGO)).toBe('2026-10-05')
    expect(productDate(instant, 'Pacific/Honolulu')).toBe('2026-10-05')
  })

  it('does not use the zone this code happens to run in', () => {
    // These two zones are 25 hours apart, so they never share a date -- whatever zone the test machine is in,
    // at least one of them differs from its local date, and both answers are right.
    const instant = at('2026-10-05T18:50:00Z')

    expect(productDate(instant, 'Pacific/Kiritimati')).toBe('2026-10-06')
    expect(productDate(instant, 'Pacific/Pago_Pago')).toBe('2026-10-05')
  })

  it('keeps the date right across the spring DST change', () => {
    // 8 March 2026 in Chicago is 23 hours long: 2 AM CST becomes 3 AM CDT.
    expect(productDate(at('2026-03-08T05:59:59Z'), CHICAGO)).toBe('2026-03-07')
    expect(productDate(at('2026-03-08T06:00:00Z'), CHICAGO)).toBe('2026-03-08')
    expect(productDate(at('2026-03-09T04:59:59Z'), CHICAGO)).toBe('2026-03-08')
    expect(productDate(at('2026-03-09T05:00:00Z'), CHICAGO)).toBe('2026-03-09')
  })

  it('keeps the date right across the fall DST change', () => {
    // 1 November 2026 in Chicago is 25 hours long: 2 AM CDT becomes 1 AM CST.
    expect(productDate(at('2026-11-01T04:59:59Z'), CHICAGO)).toBe('2026-10-31')
    expect(productDate(at('2026-11-01T05:00:00Z'), CHICAGO)).toBe('2026-11-01')
    expect(productDate(at('2026-11-02T05:59:59Z'), CHICAGO)).toBe('2026-11-01')
    expect(productDate(at('2026-11-02T06:00:00Z'), CHICAGO)).toBe('2026-11-02')
  })

  it('pads the year, month and day', () => {
    expect(productDate(at('2026-01-02T12:00:00Z'), 'UTC')).toBe('2026-01-02')
    expect(productDate(at('0900-03-04T12:00:00Z'), 'UTC')).toBe('0900-03-04')
  })

  it.each(['Not/AZone', '', 'Chicago'])('throws for the zone %j rather than falling back to another', (zone) => {
    expect(() => productDate(at('2026-10-05T18:50:00Z'), zone)).toThrow(RangeError)
  })

  it('throws for a value that is not an instant', () => {
    expect(() => productDate(new Date('nonsense'), CHICAGO)).toThrow(RangeError)
  })
})

describe('productHour', () => {
  it('is the wall-clock hour in the product zone', () => {
    expect(productHour(at('2026-10-05T18:50:00Z'), CHICAGO)).toBe(13)
    expect(productHour(at('2026-10-05T18:50:00Z'), 'UTC')).toBe(18)
    expect(productHour(at('2026-10-05T18:50:00Z'), 'Asia/Tokyo')).toBe(3)
    expect(productHour(at('2026-01-05T18:50:00Z'), CHICAGO)).toBe(12)
  })

  it('is 0 at midnight and 23 just before it', () => {
    expect(productHour(at('2026-10-05T05:00:00Z'), CHICAGO)).toBe(0)
    expect(productHour(at('2026-10-05T04:59:59Z'), CHICAGO)).toBe(23)
  })

  it('skips 2 AM on the spring DST change', () => {
    expect(productHour(at('2026-03-08T07:59:59Z'), CHICAGO)).toBe(1)
    expect(productHour(at('2026-03-08T08:00:00Z'), CHICAGO)).toBe(3)
  })

  it('reads 1 AM twice on the fall DST change', () => {
    expect(productHour(at('2026-11-01T06:30:00Z'), CHICAGO)).toBe(1)
    expect(productHour(at('2026-11-01T07:30:00Z'), CHICAGO)).toBe(1)
    expect(productHour(at('2026-11-01T08:00:00Z'), CHICAGO)).toBe(2)
  })

  it('throws for a zone that is not valid', () => {
    expect(() => productHour(at('2026-10-05T18:50:00Z'), 'Not/AZone')).toThrow(RangeError)
  })
})

describe('isValidTimeZone', () => {
  it.each([CHICAGO, 'UTC', 'Asia/Tokyo', 'Europe/London'])('accepts %s', (zone) => {
    expect(isValidTimeZone(zone)).toBe(true)
  })

  it.each([['Not/AZone'], [''], ['Central Time'], [null], [undefined], [5], [{}]])('rejects %j', (zone) => {
    expect(isValidTimeZone(zone)).toBe(false)
  })
})

describe('formatInstant', () => {
  it('shows a UTC instant as the product zone reads it, with the zone named', () => {
    expect(plain(formatInstant('2026-10-05T18:45:03Z', CHICAGO))).toBe('Oct 5, 2026, 1:45 PM CDT')
    expect(plain(formatInstant('2026-01-05T18:45:03Z', CHICAGO))).toBe('Jan 5, 2026, 12:45 PM CST')
  })

  it('shows the product zone date when it differs from the UTC date', () => {
    expect(plain(formatInstant('2026-10-06T02:15:00Z', CHICAGO))).toBe('Oct 5, 2026, 9:15 PM CDT')
  })

  it('accepts an instant with an offset or fractional seconds, and epoch milliseconds', () => {
    expect(plain(formatInstant('2026-10-05T13:45:03.123456-05:00', CHICAGO))).toBe('Oct 5, 2026, 1:45 PM CDT')
    expect(plain(formatInstant(Date.parse('2026-10-05T18:45:03Z'), CHICAGO))).toBe('Oct 5, 2026, 1:45 PM CDT')
  })

  it.each([[null], [undefined], ['']])('is null for the missing instant %j', (instant) => {
    expect(formatInstant(instant, CHICAGO)).toBeNull()
  })

  it('is null, not "Invalid Date", for a value that is not an instant', () => {
    expect(formatInstant('yesterday', CHICAGO)).toBeNull()
  })

  it('is null for a zone that is not valid', () => {
    expect(formatInstant('2026-10-05T18:45:03Z', 'Not/AZone')).toBeNull()
  })
})

describe('formatCalendarDate', () => {
  it('writes a date out in full, whatever zone this runs in', () => {
    expect(formatCalendarDate('2026-10-05')).toBe('Monday, October 5, 2026')
    expect(formatCalendarDate('2026-01-01')).toBe('Thursday, January 1, 2026')
    expect(formatCalendarDate('2026-12-31')).toBe('Thursday, December 31, 2026')
  })

  it.each(['2026-02-30', '2026-13-01', '10/05/2026', '2026-10-05T00:00:00Z', ''])('is null for %j', (date) => {
    expect(formatCalendarDate(date)).toBeNull()
  })
})

describe('formatHour', () => {
  it.each([
    [0, '12 AM'],
    [1, '1 AM'],
    [11, '11 AM'],
    [12, '12 PM'],
    [13, '1 PM'],
    [23, '11 PM'],
  ])('labels hour %i as %s', (hour, label) => {
    expect(formatHour(hour)).toBe(label)
  })
})
