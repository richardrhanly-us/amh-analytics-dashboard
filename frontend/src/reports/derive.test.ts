import { describe, expect, it } from 'vitest'

import {
  activeDays,
  average,
  busiestDay,
  busiestHour,
  busiestWeekday,
  formatAverage,
  formatCount,
  formatDate,
  formatDayOfWeek,
  formatPercent,
  formatShortDate,
  hourlyAverages,
  NOT_AVAILABLE,
  percentOf,
  weekdayAverages,
  weekdayOf,
  WEEKDAY_NAMES,
} from './derive.ts'

const day = (date: string, checkin_count: number) => ({ date, checkin_count })
const hours = (counts: Record<number, number>) => Array.from({ length: 24 }, (_, hour) => ({ hour, checkin_count: counts[hour] ?? 0 }))

// Monday 1 June 2026 to Sunday 14 June 2026: two of every weekday.
const TWO_WEEKS = Array.from({ length: 14 }, (_, index) => `2026-06-${String(index + 1).padStart(2, '0')}`)

describe('average', () => {
  it('divides a total by its denominator', () => {
    expect(average(300, 30)).toBe(10)
    expect(average(1, 3)).toBeCloseTo(0.3333, 4)
    expect(average(0, 30)).toBe(0)
  })

  it('does not exist without a denominator, and is never zero, NaN or Infinity instead', () => {
    expect(average(0, 0)).toBeNull()
    expect(average(120, 0)).toBeNull()
    expect(average(120, -1)).toBeNull()
  })
})

describe('percentOf', () => {
  it('is the part as a percentage of the whole', () => {
    expect(percentOf(145, 1274)).toBeCloseTo(11.3815, 3)
    expect(percentOf(0, 100)).toBe(0)
    expect(percentOf(100, 100)).toBe(100)
  })

  it('may exceed 100: rejects are a ratio against check-ins, not a share of them', () => {
    expect(percentOf(3, 2)).toBe(150)
  })

  it('does not exist when the whole is zero', () => {
    expect(percentOf(0, 0)).toBeNull()
    expect(percentOf(4, 0)).toBeNull()
  })

  it('is made from two totals, which is not the average of the parts’ percentages', () => {
    // 1 of 10 (10%) and 50 of 100 (50%): together 51 of 110, not the 30% their percentages average to.
    expect(percentOf(1 + 50, 10 + 100)).toBeCloseTo(46.36, 2)
    expect(((percentOf(1, 10) as number) + (percentOf(50, 100) as number)) / 2).toBe(30)
  })
})

describe('the average per calendar day and per active day', () => {
  const days = [day('2026-06-01', 100), day('2026-06-02', 0), day('2026-06-03', 50), day('2026-06-04', 0)]
  const total = 150

  it('divides by every calendar day for the first, and only by days with check-ins for the second', () => {
    expect(average(total, days.length)).toBe(37.5)
    expect(activeDays(days)).toBe(2)
    expect(average(total, activeDays(days))).toBe(75)
  })

  it('has no per-active-day average when no day was active', () => {
    const quiet = [day('2026-06-01', 0), day('2026-06-02', 0)]

    expect(activeDays(quiet)).toBe(0)
    expect(average(0, activeDays(quiet))).toBeNull()
    expect(average(0, quiet.length)).toBe(0)
  })
})

describe('busiestDay', () => {
  it('is the day with the most check-ins', () => {
    expect(busiestDay([day('2026-06-01', 5), day('2026-06-02', 9), day('2026-06-03', 7)])).toEqual(day('2026-06-02', 9))
  })

  it('is the earlier date when days tie', () => {
    expect(busiestDay([day('2026-06-01', 3), day('2026-06-02', 9), day('2026-06-03', 9), day('2026-06-04', 9)])?.date).toBe('2026-06-02')
  })

  it('is no day at all when nothing happened', () => {
    expect(busiestDay([day('2026-06-01', 0), day('2026-06-02', 0)])).toBeNull()
    expect(busiestDay([])).toBeNull()
  })
})

describe('busiestHour', () => {
  it('is the hour with the highest total', () => {
    expect(busiestHour(hours({ 9: 20, 11: 40, 14: 30 }))).toEqual({ hour: 11, checkin_count: 40 })
  })

  it('is the earlier hour when hours tie', () => {
    expect(busiestHour(hours({ 14: 40, 9: 40, 11: 40 }))?.hour).toBe(9)
  })

  it('is no hour when there were no check-ins', () => {
    expect(busiestHour(hours({}))).toBeNull()
  })

  it('considers every hour of the day, not only opening hours', () => {
    expect(busiestHour(hours({ 2: 9, 10: 5 }))?.hour).toBe(2)
    expect(busiestHour(hours({ 23: 9, 10: 5 }))?.hour).toBe(23)
  })
})

describe('weekdayOf', () => {
  it.each([
    ['2026-06-01', 1],
    ['2026-06-02', 2],
    ['2026-06-06', 6],
    ['2026-06-07', 7],
    ['2026-10-05', 1],
    ['2028-02-29', 2],
    ['2026-03-08', 7], // the day the clocks go forward: still a Sunday
    ['2026-11-01', 7],
  ])('%s is weekday %i, Monday being 1', (date, weekday) => {
    expect(weekdayOf(date)).toBe(weekday)
  })
})

describe('weekdayAverages', () => {
  it('has one entry for each weekday, Monday first', () => {
    const weekdays = weekdayAverages(TWO_WEEKS.map((date) => day(date, 0)))

    expect(weekdays.map((weekday) => weekday.name)).toEqual([...WEEKDAY_NAMES])
    expect(weekdays.map((weekday) => weekday.weekday)).toEqual([1, 2, 3, 4, 5, 6, 7])
    expect(weekdays.map((weekday) => weekday.occurrences)).toEqual([2, 2, 2, 2, 2, 2, 2])
  })

  it('divides by every time the weekday falls in the range, including the times nothing happened', () => {
    // Two Mondays: 100 on the first, nothing on the second. The average is 50, not 100.
    const days = TWO_WEEKS.map((date) => day(date, date === '2026-06-01' ? 100 : 0))

    const monday = weekdayAverages(days)[0]

    expect(monday).toEqual({ weekday: 1, name: 'Monday', occurrences: 2, checkin_count: 100, average: 50 })
  })

  it('counts each weekday as often as it actually occurs in an uneven range', () => {
    // Monday 1 June to Wednesday 10 June: two each of Monday to Wednesday, one of each other day.
    const days = TWO_WEEKS.slice(0, 10).map((date) => day(date, 10))

    const weekdays = weekdayAverages(days)

    expect(weekdays.map((weekday) => weekday.occurrences)).toEqual([2, 2, 2, 1, 1, 1, 1])
    expect(weekdays.map((weekday) => weekday.checkin_count)).toEqual([20, 20, 20, 10, 10, 10, 10])
    expect(weekdays.every((weekday) => weekday.average === 10)).toBe(true)
  })

  it('has no average for a weekday the range does not contain', () => {
    const weekdays = weekdayAverages([day('2026-06-01', 8), day('2026-06-02', 4)])

    expect(weekdays.map((weekday) => weekday.average)).toEqual([8, 4, null, null, null, null, null])
  })

  it('adds up to the range’s total', () => {
    const days = TWO_WEEKS.map((date, index) => day(date, index * 3))

    expect(weekdayAverages(days).reduce((sum, weekday) => sum + weekday.checkin_count, 0)).toBe(
      days.reduce((sum, entry) => sum + entry.checkin_count, 0),
    )
  })
})

describe('busiestWeekday', () => {
  it('is the weekday with the highest AVERAGE, not the highest total', () => {
    // Mondays: 90 over two occurrences (45 a day). A single Thursday: 60. Thursday is busier on average.
    const days = [...TWO_WEEKS.slice(0, 10).map((date) => day(date, 0))]
    days[0] = day('2026-06-01', 50)
    days[7] = day('2026-06-08', 40)
    days[3] = day('2026-06-04', 60)

    const busiest = busiestWeekday(weekdayAverages(days))

    expect(busiest?.name).toBe('Thursday')
    expect(busiest?.average).toBe(60)
  })

  it('is the earlier weekday when weekdays tie, Monday first', () => {
    const everyDayTheSame = TWO_WEEKS.map((date) => day(date, 20))
    expect(busiestWeekday(weekdayAverages(everyDayTheSame))?.name).toBe('Monday')

    const midweekTie = TWO_WEEKS.map((date) => day(date, [3, 5].includes(weekdayOf(date)) ? 30 : 1))
    expect(busiestWeekday(weekdayAverages(midweekTie))?.name).toBe('Wednesday')

    const weekendTie = TWO_WEEKS.map((date) => day(date, weekdayOf(date) >= 6 ? 30 : 0))
    expect(busiestWeekday(weekdayAverages(weekendTie))?.name).toBe('Saturday')
  })

  it('is no weekday when there were no check-ins', () => {
    expect(busiestWeekday(weekdayAverages(TWO_WEEKS.map((date) => day(date, 0))))).toBeNull()
    expect(busiestWeekday(weekdayAverages([]))).toBeNull()
  })
})

describe('hourlyAverages', () => {
  it('divides each hour’s total by every calendar day of the range', () => {
    // 60 check-ins at 9 AM over a 30-day range: 2 a day -- even if they all came on three days.
    const averages = hourlyAverages(hours({ 9: 60, 14: 15 }), 30)

    expect(averages).toHaveLength(24)
    expect(averages[9]).toEqual({ hour: 9, checkin_count: 60, average: 2 })
    expect(averages[14].average).toBe(0.5)
    expect(averages[3].average).toBe(0)
  })

  it('keeps all 24 hours, in order', () => {
    expect(hourlyAverages(hours({ 0: 1, 23: 1 }), 1).map((hour) => hour.hour)).toEqual(Array.from({ length: 24 }, (_, hour) => hour))
  })

  it('adds up to the average per calendar day', () => {
    const counts = hours({ 8: 10, 9: 35, 13: 55 })

    const total = hourlyAverages(counts, 20).reduce((sum, hour) => sum + (hour.average as number), 0)

    expect(total).toBeCloseTo(average(100, 20) as number, 10)
  })

  it('has no averages for a range of no days', () => {
    expect(hourlyAverages(hours({ 9: 5 }), 0).every((hour) => hour.average === null)).toBe(true)
  })
})

describe('how figures are written', () => {
  it('writes a count with separators', () => {
    expect(formatCount(0)).toBe('0')
    expect(formatCount(1274)).toBe('1,274')
    expect(formatCount(1250067)).toBe('1,250,067')
  })

  it('writes a percentage to one decimal place', () => {
    expect(formatPercent(11.3815)).toBe('11.4%')
    expect(formatPercent(0)).toBe('0.0%')
    expect(formatPercent(100)).toBe('100.0%')
    expect(formatPercent(0.04)).toBe('0.0%')
  })

  it('writes an average to one decimal place below 100 and as a whole number from there up', () => {
    expect(formatAverage(0)).toBe('0.0')
    expect(formatAverage(0.5)).toBe('0.5')
    expect(formatAverage(42.46)).toBe('42.5')
    expect(formatAverage(99.94)).toBe('99.9')
    expect(formatAverage(104.67)).toBe('105')
    expect(formatAverage(1274.4)).toBe('1,274')
  })

  it('writes a calendar date as the date it is, whatever zone this machine is in', () => {
    expect(formatDate('2026-06-10')).toBe('Jun 10, 2026')
    expect(formatDate('2026-01-01')).toBe('Jan 1, 2026')
    expect(formatDate('2026-12-31')).toBe('Dec 31, 2026')
    expect(formatShortDate('2026-06-10')).toBe('Jun 10')
    expect(formatDayOfWeek('2026-06-10')).toBe('Wed, Jun 10')
    expect(formatDayOfWeek('2026-11-01')).toBe('Sun, Nov 1')
  })

  it('has one phrase for a figure that does not exist', () => {
    expect(NOT_AVAILABLE).toBe('Not available')
  })
})

describe('an organization’s figures', () => {
  // Two sorters: a busy one that rejects little, and a quiet one that rejects a lot.
  const SORTERS = [
    { checkin_count: 760, transit_count: 114, reject_count: 38 },
    { checkin_count: 350, transit_count: 105, reject_count: 35 },
  ]
  const total = (pick: (sorter: (typeof SORTERS)[number]) => number) => SORTERS.reduce((sum, sorter) => sum + pick(sorter), 0)
  const checkins = total((sorter) => sorter.checkin_count)

  it('makes the organization’s reject rate from its summed counts', () => {
    const rate = percentOf(total((sorter) => sorter.reject_count), checkins) as number

    expect(rate).toBeCloseTo((73 / 1110) * 100, 10)
    expect(formatPercent(rate)).toBe('6.6%')
  })

  it('is not the average of the sorters’ own rates', () => {
    const rates = SORTERS.map((sorter) => percentOf(sorter.reject_count, sorter.checkin_count) as number)
    const averaged = average(rates[0] + rates[1], rates.length) as number

    expect(rates.map(formatPercent)).toEqual(['5.0%', '10.0%'])
    expect(formatPercent(averaged)).toBe('7.5%')
    expect(formatPercent(percentOf(total((sorter) => sorter.reject_count), checkins) as number)).not.toBe(formatPercent(averaged))
  })

  it('makes the organization’s transit rate the same way', () => {
    expect(formatPercent(percentOf(total((sorter) => sorter.transit_count), checkins) as number)).toBe('19.7%')
    expect(SORTERS.map((sorter) => formatPercent(percentOf(sorter.transit_count, sorter.checkin_count) as number))).toEqual(['15.0%', '30.0%'])
  })

  it('gives each sorter a share of the organization’s check-ins, and the shares make the whole', () => {
    const shares = SORTERS.map((sorter) => percentOf(sorter.checkin_count, checkins) as number)

    expect(shares.map(formatPercent)).toEqual(['68.5%', '31.5%'])
    expect(shares[0] + shares[1]).toBeCloseTo(100, 10)
  })

  it('gives a single sorter the whole of it, and the organization that sorter’s own rate', () => {
    const [only] = SORTERS

    expect(percentOf(only.checkin_count, only.checkin_count)).toBe(100)
    expect(percentOf(only.reject_count, only.checkin_count)).toBe(5)
  })

  it('averages over every calendar day of the range', () => {
    expect(formatAverage(average(checkins, 7) as number)).toBe('159')
  })

  it('has no rate, share or average without a denominator: never 0% and never NaN', () => {
    // An organization that processed nothing, a sorter that processed nothing, and a sorter that could not be read.
    expect(percentOf(0, 0)).toBeNull()
    expect(percentOf(3, 0)).toBeNull()
    expect(average(0, 0)).toBeNull()
    // A sorter with nothing, in an organization with something, has a real share: none of it.
    expect(percentOf(0, checkins)).toBe(0)
  })
})
