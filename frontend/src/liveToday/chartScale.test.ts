import { describe, expect, it } from 'vitest'

import { barHeight, chartScale } from './chartScale.ts'

describe('chartScale', () => {
  it.each([
    [[20, 25, 40, 18, 17], 40, [0, 10, 20, 30, 40]],
    [[41], 60, [0, 20, 40, 60]],
    [[1], 1, [0, 1]],
    [[3, 2], 3, [0, 1, 2, 3]],
    [[5], 6, [0, 2, 4, 6]],
    [[7], 8, [0, 2, 4, 6, 8]],
    [[99], 100, [0, 50, 100]],
    [[130], 150, [0, 50, 100, 150]],
    [[1_000_000, 3], 1_000_000, [0, 500_000, 1_000_000]],
  ])('fits %j under a top of %d', (counts, top, ticks) => {
    expect(chartScale(counts)).toEqual({ top, ticks })
  })

  it('gives a day with no check-ins a real, if short, axis', () => {
    expect(chartScale(Array.from({ length: 24 }, () => 0))).toEqual({ top: 1, ticks: [0, 1] })
    expect(chartScale([])).toEqual({ top: 1, ticks: [0, 1] })
  })

  it('never tops out below the highest count, labels a fraction, or needs more than five steps', () => {
    for (let highest = 1; highest <= 5000; highest++) {
      const { top, ticks } = chartScale([highest])

      expect(top).toBeGreaterThanOrEqual(highest)
      expect(ticks.every(Number.isInteger)).toBe(true)
      expect(ticks[0]).toBe(0)
      expect(ticks.at(-1)).toBe(top)
      expect(ticks.length).toBeLessThanOrEqual(6)
    }
  })

  it('does not change the counts it is given', () => {
    const counts = Object.freeze([4, 0, 9])

    expect(() => chartScale(counts)).not.toThrow()
    expect(counts).toEqual([4, 0, 9])
  })
})

describe('barHeight', () => {
  const scale = chartScale([40])

  it('is the count as a share of the top of the axis', () => {
    expect(barHeight(40, scale)).toBe(1)
    expect(barHeight(20, scale)).toBe(0.5)
    expect(barHeight(10, scale)).toBe(0.25)
  })

  it('is nothing at all for an hour with no check-ins', () => {
    expect(barHeight(0, scale)).toBe(0)
    expect(barHeight(0, chartScale([0]))).toBe(0)
  })

  it('never lets a count that is not zero disappear beside a very large one', () => {
    const lopsided = chartScale([1_000_000])

    expect(barHeight(1, lopsided)).toBeGreaterThan(0)
    expect(barHeight(1, lopsided)).toBe(barHeight(500, lopsided))
    expect(barHeight(1, lopsided)).toBeLessThan(barHeight(100_000, lopsided))
  })

  it('never draws a bar taller than the plot', () => {
    expect(barHeight(50, scale)).toBe(1)
  })
})
