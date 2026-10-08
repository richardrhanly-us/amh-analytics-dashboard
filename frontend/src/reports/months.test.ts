import { describe, expect, it } from 'vitest'

import { datesInRange } from './dateRange.ts'
import { DAILY_DISPLAY_MAX_DAYS, groupByMonth, timeSeries } from './months.ts'

/** A daily record for every date from..to, with `checkins` and `rejects` worked out from the date. */
function days(from: string, to: string, checkins: (date: string) => number = () => 1, rejects: (date: string) => number = () => 0) {
  return datesInRange({ from, to }).map((date) => ({ date, checkin_count: checkins(date), reject_count: rejects(date) }))
}
const figures = (day: { checkin_count: number; reject_count: number }) => [day.checkin_count, day.reject_count]
const sum = (values: number[]) => values.reduce((total, value) => total + value, 0)

describe('groupByMonth', () => {
  it('makes one month of a whole month', () => {
    expect(groupByMonth(days('2026-04-01', '2026-04-30'), figures)).toEqual([
      { key: '2026-04', label: 'Apr 2026', first: '2026-04-01', last: '2026-04-30', partial: false, values: [30, 0] },
    ])
  })

  it('makes two months of two whole months, in order', () => {
    const months = groupByMonth(days('2026-04-01', '2026-05-31'), figures)

    expect(months.map((month) => [month.label, month.values[0], month.partial])).toEqual([
      ['Apr 2026', 30, false],
      ['May 2026', 31, false],
    ])
  })

  it('marks a first month the range starts inside, and a last month it ends inside, as partial', () => {
    const months = groupByMonth(days('2025-10-06', '2026-01-20'), figures)

    expect(months.map((month) => [month.label, month.first, month.last, month.partial, month.values[0]])).toEqual([
      ['Oct 2025', '2025-10-06', '2025-10-31', true, 26],
      ['Nov 2025', '2025-11-01', '2025-11-30', false, 30],
      ['Dec 2025', '2025-12-01', '2025-12-31', false, 31],
      ['Jan 2026', '2026-01-01', '2026-01-20', true, 20],
    ])
  })

  it('crosses a year, and counts a leap February as 29 days and an ordinary one as 28', () => {
    const leap = groupByMonth(days('2028-02-01', '2028-02-29'), figures)
    const ordinary = groupByMonth(days('2027-02-01', '2027-02-28'), figures)

    expect([leap[0].values[0], leap[0].partial]).toEqual([29, false])
    expect([ordinary[0].values[0], ordinary[0].partial]).toEqual([28, false])
    expect(groupByMonth(days('2027-12-30', '2028-01-02'), figures).map((month) => month.label)).toEqual(['Dec 2027', 'Jan 2028'])
    // 28 February of a leap year is not the end of the month.
    expect(groupByMonth(days('2028-02-01', '2028-02-28'), figures)[0].partial).toBe(true)
  })

  it('makes nothing of no days, and keeps months whose days counted nothing', () => {
    expect(groupByMonth([], figures)).toEqual([])
    const months = groupByMonth(days('2026-04-01', '2026-05-31', (date) => (date < '2026-05-01' ? 0 : 2)), figures)
    expect(months.map((month) => month.values)).toEqual([[0, 0], [62, 0]])
  })

  it('adds up every figure of a day, each on its own, and loses nothing', () => {
    const daily = days('2025-06-21', '2026-06-20', (date) => Number(date.slice(8)), (date) => Number(date.slice(5, 7)))

    const months = groupByMonth(daily, figures)

    expect(sum(months.map((month) => month.values[0]))).toBe(sum(daily.map((day) => day.checkin_count)))
    expect(sum(months.map((month) => month.values[1]))).toBe(sum(daily.map((day) => day.reject_count)))
    expect(months).toHaveLength(13)                     // 21 June 2025 to 20 June 2026: two partial Junes
    expect(months.map((month) => month.key)).toEqual([...months.map((month) => month.key)].sort())
  })

  it('refuses days that do not all have the same figures', () => {
    expect(() => groupByMonth([{ date: '2026-04-01', v: [1] }, { date: '2026-04-02', v: [1, 2] }], (day) => day.v)).toThrow(RangeError)
  })

  it('never changes the days it is given', () => {
    const daily = days('2026-04-01', '2026-05-31')
    const copy = structuredClone(daily)

    groupByMonth(daily, figures)

    expect(daily).toEqual(copy)
  })
})

describe('timeSeries', () => {
  it(`shows up to ${DAILY_DISPLAY_MAX_DAYS} days day by day, exactly as before`, () => {
    const series = timeSeries(days('2026-07-06', '2026-10-05'), figures)       // 92 days

    expect(DAILY_DISPLAY_MAX_DAYS).toBe(92)
    expect(series.monthly).toBe(false)
    expect(series.period).toBe('Date')
    expect(series.points).toHaveLength(92)
    expect(series.points[0]).toEqual({ bar: 'Jul 6', row: 'Mon, Jul 6', values: [1, 0] })
  })

  it('shows a longer range a month at a time, saying which days a partial month covers', () => {
    const series = timeSeries(days('2026-07-05', '2026-10-05'), figures)       // 93 days

    expect(series.monthly).toBe(true)
    expect(series.period).toBe('Month')
    expect(series.points.map((point) => [point.bar, point.row, point.values[0]])).toEqual([
      ['Jul 2026', 'Jul 2026 (from Jul 5)', 27],
      ['Aug 2026', 'Aug 2026', 31],
      ['Sep 2026', 'Sep 2026', 30],
      ['Oct 2026', 'Oct 2026 (to Oct 5)', 5],
    ])
    expect(sum(series.points.map((point) => point.values[0]))).toBe(93)
  })

  it('says both ends of a month a range starts and ends inside', () => {
    const series = timeSeries(days('2026-01-10', '2026-04-20'), figures)

    expect(series.points[0].row).toBe('Jan 2026 (from Jan 10)')
    expect(timeSeries([...days('2026-03-05', '2026-03-20'), ...days('2026-04-01', '2026-07-31')], figures).points[0].row).toBe(
      'Mar 2026 (Mar 5 – Mar 20)',
    )
  })
})
