import { describe, expect, it } from 'vitest'

import {
  addDays,
  datesInRange,
  daysInRange,
  DEFAULT_PRESET_DAYS,
  HOLDS_MAX_RANGE_DAYS,
  isCalendarDate,
  lastDays,
  MAX_RANGE_DAYS,
  offersYearToDate,
  presetOf,
  PRESETS,
  rangeProblem,
  rangeProblemText,
  yearToDate,
} from './dateRange.ts'

const TODAY = '2026-10-05'

describe('calendar dates', () => {
  it.each(['2026-10-05', '2028-02-29', '2026-01-01', '2026-12-31'])('%s is a calendar date', (date) => {
    expect(isCalendarDate(date)).toBe(true)
  })

  it.each(['', '2026-10-5', '10/05/2026', '2026-02-30', '2026-13-01', '2025-02-29', '2026-10-05T00:00:00', 'today'])(
    '%j is not',
    (date) => {
      expect(isCalendarDate(date)).toBe(false)
    },
  )

  it('adds and takes away days across months, years, leap days and clock changes', () => {
    expect(addDays('2026-10-05', 0)).toBe('2026-10-05')
    expect(addDays('2026-10-05', -29)).toBe('2026-09-06')
    expect(addDays('2026-12-31', 1)).toBe('2027-01-01')
    expect(addDays('2028-02-28', 1)).toBe('2028-02-29')
    expect(addDays('2028-03-01', -1)).toBe('2028-02-29')
    // The days the clocks change in America/Chicago are one day long like any other: no zone is involved.
    expect(addDays('2026-03-07', 2)).toBe('2026-03-09')
    expect(addDays('2026-10-31', 2)).toBe('2026-11-02')
  })

  it('counts a range with both ends included', () => {
    expect(daysInRange({ from: TODAY, to: TODAY })).toBe(1)
    expect(daysInRange({ from: '2026-09-06', to: TODAY })).toBe(30)
    expect(daysInRange({ from: '2026-03-07', to: '2026-03-09' })).toBe(3)
    expect(daysInRange({ from: '2026-10-06', to: TODAY })).toBe(0)
  })

  it('lists every date of a range in order', () => {
    expect(datesInRange({ from: '2026-02-27', to: '2026-03-02' })).toEqual(['2026-02-27', '2026-02-28', '2026-03-01', '2026-03-02'])
    expect(datesInRange({ from: TODAY, to: '2026-10-01' })).toEqual([])
  })
})

describe('presets', () => {
  it('offers the last 7, 30 and 90 days, and opens on 30', () => {
    expect(PRESETS.map((preset) => [preset.days, preset.label])).toEqual([
      [7, 'Last 7 days'],
      [30, 'Last 30 days'],
      [90, 'Last 90 days'],
    ])
    expect(DEFAULT_PRESET_DAYS).toBe(30)
  })

  it('ends each on today and includes it', () => {
    expect(lastDays(7, TODAY)).toEqual({ from: '2026-09-29', to: TODAY })
    expect(lastDays(30, TODAY)).toEqual({ from: '2026-09-06', to: TODAY })
    expect(lastDays(90, TODAY)).toEqual({ from: '2026-07-08', to: TODAY })
    expect(lastDays(1, TODAY)).toEqual({ from: TODAY, to: TODAY })
    for (const { days } of PRESETS) {
      expect(daysInRange(lastDays(days, TODAY))).toBe(days)
    }
  })

  it('fits every preset inside the longest range available', () => {
    expect(MAX_RANGE_DAYS).toBe(3660)       // the API's engineering guard (R9D2); it was 92
    for (const { days } of PRESETS) {
      expect(days).toBeLessThanOrEqual(MAX_RANGE_DAYS)
      expect(rangeProblem(lastDays(days, TODAY), TODAY)).toBeNull()
    }
  })

  it('knows a range that is a preset, and one that is not', () => {
    expect(presetOf(lastDays(30, TODAY), TODAY)).toBe(30)
    expect(presetOf(lastDays(7, TODAY), TODAY)).toBe(7)
    // The same length, but not ending today: a custom range.
    expect(presetOf({ from: '2026-09-05', to: '2026-10-04' }, TODAY)).toBeNull()
    expect(presetOf({ from: '2026-09-01', to: TODAY }, TODAY)).toBeNull()
  })
})

describe('rangeProblem', () => {
  it.each([
    [{ from: TODAY, to: TODAY }],
    [{ from: '2026-09-06', to: TODAY }],
    [{ from: addDays(TODAY, -(MAX_RANGE_DAYS - 1)), to: TODAY }], // exactly the longest range
    [{ from: '2025-01-01', to: '2025-03-31' }],
  ])('accepts %j', (range) => {
    expect(rangeProblem(range, TODAY)).toBeNull()
  })

  it.each([
    [{ from: '', to: TODAY }, 'incomplete'],
    [{ from: '2026-09-01', to: '' }, 'incomplete'],
    [{ from: '2026-02-30', to: '2026-03-01' }, 'incomplete'],
    [{ from: TODAY, to: '2026-10-04' }, 'order'],
    [{ from: '2026-10-01', to: '2026-10-06' }, 'future'],
    [{ from: '2026-10-06', to: '2026-10-07' }, 'future'],
    [{ from: addDays(TODAY, -MAX_RANGE_DAYS), to: TODAY }, 'too_long'], // one day more than the longest
    [{ from: '2015-01-01', to: '2025-12-31' }, 'too_long'],         // 4,018 days
  ] as const)('refuses %j as %s', (range, problem) => {
    expect(rangeProblem(range, TODAY)).toBe(problem)
  })

  it('judges order before the future, and the future before length, as the API does', () => {
    expect(rangeProblem({ from: '2030-01-01', to: '2029-01-01' }, TODAY)).toBe('order')
    expect(rangeProblem({ from: '2026-01-01', to: '2027-01-01' }, TODAY)).toBe('future')
  })

  it('takes today from whoever calls it: the same range is fine a day later', () => {
    const range = { from: '2026-10-01', to: '2026-10-06' }

    expect(rangeProblem(range, '2026-10-05')).toBe('future')
    expect(rangeProblem(range, '2026-10-06')).toBeNull()
  })

  it('says what is wrong in a sentence, and calls the length the longest report, not a history limit', () => {
    expect(rangeProblemText('order')).toBe('The start date must be on or before the end date.')
    expect(rangeProblemText('future')).toBe('The end date cannot be after today.')
    expect(rangeProblemText('incomplete')).toBe('Enter both a start date and an end date.')
    expect(rangeProblemText('too_long')).toBe('A single report can cover up to 3,660 days. Choose a shorter range.')
  })
})

describe('year to date (R9D2)', () => {
  it('is 1 January of this year to today', () => {
    expect(yearToDate('2026-10-05')).toEqual({ from: '2026-01-01', to: '2026-10-05' })
    expect(yearToDate('2028-01-01')).toEqual({ from: '2028-01-01', to: '2028-01-01' })
    expect(daysInRange(yearToDate('2028-12-31'))).toBe(366)
  })

  it.each([
    [null, '2026-10-05', true],             // no history limit
    [3650, '2026-10-05', true],
    [730, '2026-10-05', true],
    [278, '2026-10-05', true],              // exactly back to 1 January
    [277, '2026-10-05', false],             // a day short of it: never shortened to fit
    [90, '2026-10-05', false],
    [30, '2026-10-05', false],
    [30, '2027-01-20', true],               // early January: 1 January is inside a 30-day window
    [30, '2027-01-31', false],
  ] as const)('with %s days of history on %s is offered: %s', (historyDays, today, offered) => {
    expect(offersYearToDate(today, historyDays)).toBe(offered)
    if (offered) {
      expect(rangeProblem(yearToDate(today), today)).toBeNull()
    }
  })
})

describe('the Holds report (R9D2)', () => {
  it('covers up to 92 days, whatever a plan allows the other reports', () => {
    expect(HOLDS_MAX_RANGE_DAYS).toBe(92)
    expect(HOLDS_MAX_RANGE_DAYS).toBeLessThan(MAX_RANGE_DAYS)
  })
})
