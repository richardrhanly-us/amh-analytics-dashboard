import { barHeight, chartScale } from '../liveToday/chartScale.ts'

const AXIS_NUMBER = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 })

// The plot is drawn one unit wide per bar and 100 high, and stretched to whatever box it is given.
const PLOT_HEIGHT = 100
const BAR_INSET = 0.15
// About this many labels fit under a chart at desktop width; a narrow chart shows every other one.
const AXIS_LABELS = 8

export interface Bar {
  /** What the bar is, for the horizontal axis: "Jun 10", "9 AM", "Mon". */
  label: string
  value: number
}

/**
 * A bar for each value, in the order given: days of a range, hours of a
 * day, days of the week. Any number of bars.
 *
 * Like the Live Today chart, it is one image to assistive technology, named
 * here and described by the element `describedBy` names; the exact figures
 * are in the table that accompanies it (ChartFigure), not in the drawing.
 * Nothing in it takes focus or needs a pointer, and it has one series in one
 * colour: nothing is told by colour.
 *
 * The bars are an SVG stretched to the width available, so there is nothing
 * to measure. Its labels are ordinary text laid over it. With many bars only
 * some are labelled -- evenly spaced, always including the first.
 */
export function BarChart({
  label,
  describedBy,
  bars,
  emptyText,
}: {
  label: string
  describedBy: string
  bars: readonly Bar[]
  emptyText: string
}) {
  const scale = chartScale(bars.map((bar) => bar.value))
  const quiet = bars.every((bar) => bar.value === 0)
  const columns = { gridTemplateColumns: `repeat(${Math.max(bars.length, 1)}, minmax(0, 1fr))` }
  const step = Math.max(1, Math.ceil(bars.length / AXIS_LABELS))

  return (
    <div className="chart" role="img" aria-label={label} aria-describedby={describedBy}>
      <div className="chart-y" aria-hidden="true">
        {scale.ticks.map((tick) => (
          <span key={tick} style={{ bottom: `${(tick / scale.top) * 100}%` }}>
            {AXIS_NUMBER.format(tick)}
          </span>
        ))}
      </div>
      <div className="chart-plot">
        <svg viewBox={`0 0 ${Math.max(bars.length, 1)} ${PLOT_HEIGHT}`} preserveAspectRatio="none" aria-hidden="true" focusable="false">
          {scale.ticks.map((tick) => {
            const y = PLOT_HEIGHT - (tick / scale.top) * PLOT_HEIGHT
            return (
              <line key={tick} className={tick === 0 ? 'chart-baseline' : 'chart-grid'} x1={0} x2={Math.max(bars.length, 1)} y1={y} y2={y} />
            )
          })}
          {bars.map((bar, index) => {
            const height = barHeight(bar.value, scale) * PLOT_HEIGHT
            return (
              <rect
                key={index}
                className="chart-bar"
                data-bar={index}
                x={index + BAR_INSET}
                y={PLOT_HEIGHT - height}
                width={1 - BAR_INSET * 2}
                height={height}
              />
            )
          })}
        </svg>
        {quiet && <p className="chart-empty">{emptyText}</p>}
      </div>
      <div className="chart-x" style={columns} aria-hidden="true">
        {bars.map((bar, index) =>
          index % step === 0 ? (
            <span
              key={index}
              className={(index / step) % 2 === 1 ? 'chart-x-minor' : undefined}
              style={{ gridColumn: `${index + 1} / span ${Math.min(step, bars.length - index)}` }}
            >
              {bar.label}
            </span>
          ) : null,
        )}
      </div>
    </div>
  )
}
