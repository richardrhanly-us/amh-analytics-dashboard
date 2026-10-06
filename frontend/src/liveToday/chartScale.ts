/** The hourly chart's vertical scale. Pure: counts in, axis out. */

export interface ChartScale {
  /** The value at the top of the plot: the highest count, rounded up to a tick. At least 1. */
  top: number
  /** The values to label, from 0 up to `top`. */
  ticks: number[]
}

// A nonzero count is always drawn at least this tall, as a share of the plot, so that beside a very large
// hour it still looks different from an hour with nothing in it.
const SMALLEST_BAR = 0.02

/**
 * An axis from 0 to just above the highest count, in steps of 1, 2 or 5
 * times a power of ten -- at most four steps, and never a fraction, because
 * the counts are whole items. A day with no check-ins gets the shortest axis
 * there is (0 to 1) rather than none.
 */
export function chartScale(counts: readonly number[]): ChartScale {
  const highest = Math.max(0, ...counts)
  if (highest === 0) {
    return { top: 1, ticks: [0, 1] }
  }
  const magnitude = 10 ** Math.floor(Math.log10(highest / 4))
  const step = Math.max(1, [1, 2, 5, 10].map((factor) => factor * magnitude).find((size) => size * 4 >= highest) ?? 1)
  const top = Math.ceil(highest / step) * step
  return { top, ticks: Array.from({ length: Math.round(top / step) + 1 }, (_, index) => index * step) }
}

/** How tall a bar is, as a share (0 to 1) of the plot. Zero stays zero: an empty hour has no bar. */
export function barHeight(count: number, scale: ChartScale): number {
  return count <= 0 ? 0 : Math.min(1, Math.max(SMALLEST_BAR, count / scale.top))
}
