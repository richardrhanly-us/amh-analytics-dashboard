import type { CheckinHourCount } from '../api/liveToday.ts'
import { formatHour } from '../time/productTime.ts'
import { barHeight, chartScale } from './chartScale.ts'

const AXIS_NUMBER = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 })

// The plot is drawn 24 units wide (one per hour) and 100 high, and stretched to whatever box it is given.
const PLOT_HEIGHT = 100
const BAR_INSET = 0.15

/**
 * A bar for each of the day's 24 hours.
 *
 * It is one image to assistive technology, named here and described by the
 * element `describedBy` names; the exact figures are in the table that
 * accompanies it, not in the drawing. Nothing in it takes focus or needs a
 * pointer.
 *
 * The bars are an SVG stretched to the width available, so there is nothing
 * to measure. Its labels are ordinary text laid over it, so they stay the
 * size of the page's text however narrow the chart gets. The current hour is
 * marked with the word "Now" above its column, as well as a band behind it.
 */
export function HourlyCheckinsChart({
  hours,
  currentHour,
  describedBy,
}: {
  hours: readonly CheckinHourCount[]
  currentHour: number | null
  describedBy: string
}) {
  const scale = chartScale(hours.map((entry) => entry.checkin_count))
  const quiet = hours.every((entry) => entry.checkin_count === 0)

  return (
    <div className="chart" role="img" aria-label="Bar chart of check-ins in each hour of the day" aria-describedby={describedBy}>
      <div className="chart-now" aria-hidden="true">
        {currentHour !== null && <span style={{ gridColumn: currentHour + 1 }}>Now</span>}
      </div>
      <div className="chart-y" aria-hidden="true">
        {scale.ticks.map((tick) => (
          <span key={tick} style={{ bottom: `${(tick / scale.top) * 100}%` }}>
            {AXIS_NUMBER.format(tick)}
          </span>
        ))}
      </div>
      <div className="chart-plot">
        <svg viewBox={`0 0 24 ${PLOT_HEIGHT}`} preserveAspectRatio="none" aria-hidden="true" focusable="false">
          {currentHour !== null && <rect className="chart-band" data-current-hour={currentHour} x={currentHour} y={0} width={1} height={PLOT_HEIGHT} />}
          {scale.ticks.map((tick) => {
            const y = PLOT_HEIGHT - (tick / scale.top) * PLOT_HEIGHT
            return <line key={tick} className={tick === 0 ? 'chart-baseline' : 'chart-grid'} x1={0} x2={24} y1={y} y2={y} />
          })}
          {hours.map((entry) => {
            const height = barHeight(entry.checkin_count, scale) * PLOT_HEIGHT
            return (
              <rect
                key={entry.hour}
                className="chart-bar"
                data-hour={entry.hour}
                x={entry.hour + BAR_INSET}
                y={PLOT_HEIGHT - height}
                width={1 - BAR_INSET * 2}
                height={height}
              />
            )
          })}
        </svg>
        {quiet && <p className="chart-empty">No check-ins yet today</p>}
      </div>
      <div className="chart-x" aria-hidden="true">
        {hours
          .filter((entry) => entry.hour % 3 === 0)
          .map((entry) => (
            <span key={entry.hour} className={entry.hour % 6 === 0 ? undefined : 'chart-x-minor'} style={{ gridColumn: `${entry.hour + 1} / span 3` }}>
              {formatHour(entry.hour)}
            </span>
          ))}
      </div>
    </div>
  )
}
