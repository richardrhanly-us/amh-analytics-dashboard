import { describe, expect, it } from 'vitest'

import { REJECT_REASONS, type RejectReasonCount } from '../api/liveToday.ts'
import { busiestHour, checkinsInHour, formatRate, reasonLabel, rejectRate, topRejectReasons } from './metrics.ts'

const hours = (counts: Record<number, number>) =>
  Array.from({ length: 24 }, (_, hour) => ({ hour, checkin_count: counts[hour] ?? 0 }))

const reasons = (counts: number[]): RejectReasonCount[] =>
  REJECT_REASONS.map((reason, index) => ({ reason, reject_count: counts[index] ?? 0 }))

describe('checkinsInHour', () => {
  it('is the count for that wall-clock hour', () => {
    const day = hours({ 9: 20, 13: 17 })

    expect(checkinsInHour(day, 13)).toBe(17)
    expect(checkinsInHour(day, 9)).toBe(20)
  })

  it('is zero for an hour in which nothing happened', () => {
    expect(checkinsInHour(hours({ 9: 20 }), 15)).toBe(0)
    expect(checkinsInHour(hours({}), 0)).toBe(0)
  })

  it('is zero for an hour that is not in the list at all', () => {
    expect(checkinsInHour([{ hour: 9, checkin_count: 20 }], 10)).toBe(0)
  })
})

describe('busiestHour', () => {
  it('is the hour with the most check-ins', () => {
    expect(busiestHour(hours({ 9: 20, 11: 40, 13: 17 }))).toEqual({ hour: 11, checkin_count: 40 })
  })

  it('is the earliest of the hours that tie', () => {
    expect(busiestHour(hours({ 14: 40, 9: 40, 11: 40 }))).toEqual({ hour: 9, checkin_count: 40 })
    expect(busiestHour(hours({ 0: 1, 23: 1 }))).toEqual({ hour: 0, checkin_count: 1 })
  })

  it('is null on a day with no check-ins, not midnight with zero', () => {
    expect(busiestHour(hours({}))).toBeNull()
    expect(busiestHour([])).toBeNull()
  })
})

describe('rejectRate', () => {
  it('is rejects as a percentage of check-ins', () => {
    expect(rejectRate(6, 120)).toBe(5)
    expect(rejectRate(1, 3)).toBeCloseTo(33.333, 3)
    expect(rejectRate(0, 120)).toBe(0)
  })

  it('is null with no check-ins, whether or not there were rejects', () => {
    expect(rejectRate(0, 0)).toBeNull()
    expect(rejectRate(4, 0)).toBeNull()
  })

  it('can exceed 100 and is not capped', () => {
    expect(rejectRate(3, 2)).toBe(150)
  })
})

describe('formatRate', () => {
  it.each([
    [5, '5.0%'],
    [33.3333, '33.3%'],
    [0, '0.0%'],
    [2.45, '2.5%'],
    [150, '150.0%'],
  ])('writes %f as %s', (rate, text) => {
    expect(formatRate(rate)).toBe(text)
  })
})

describe('topRejectReasons', () => {
  it('lists the reasons that occurred, most frequent first', () => {
    expect(topRejectReasons(reasons([3, 0, 1, 0, 0, 2, 0, 0]))).toEqual([
      { reason: 'item_not_found', reject_count: 3 },
      { reason: 'communication_error', reject_count: 2 },
      { reason: 'rfid_collision', reject_count: 1 },
    ])
  })

  it('keeps the order the API sent for reasons with the same count', () => {
    expect(topRejectReasons(reasons([2, 5, 2, 0, 2, 0, 5, 0])).map((entry) => entry.reason)).toEqual([
      'ils_acs_failure',
      'other',
      'item_not_found',
      'rfid_collision',
      'routing_error',
    ])
  })

  it('is empty when there were no rejects', () => {
    expect(topRejectReasons(reasons([]))).toEqual([])
  })

  it('does not change the list it was given', () => {
    const given = reasons([1, 2, 3])
    const before = JSON.stringify(given)

    topRejectReasons(given)

    expect(JSON.stringify(given)).toBe(before)
  })
})

describe('reasonLabel', () => {
  it('has a plain name for every reason code, and no two the same', () => {
    const labels = REJECT_REASONS.map(reasonLabel)

    expect(labels.every((label) => label.length > 0 && !label.includes('_'))).toBe(true)
    expect(new Set(labels).size).toBe(REJECT_REASONS.length)
  })
})
