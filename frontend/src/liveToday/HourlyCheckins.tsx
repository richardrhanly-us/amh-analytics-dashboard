import { useState } from 'react'

import type { CheckinHourCount, CheckinsByHour } from '../api/liveToday.ts'
import { formatHour, formatHourRange } from '../time/productTime.ts'
import { HourlyCheckinsChart } from './HourlyCheckinsChart.tsx'
import { busiestHour, checkinsInHour } from './metrics.ts'
import { SectionPlaceholder } from './SectionPlaceholder.tsx'
import type { Section } from './useLiveToday.ts'

const checkins = (count: number) => `${count.toLocaleString('en-US')} ${count === 1 ? 'check-in' : 'check-ins'}`

/** The chart in a sentence or two: what a reader would take from it, and the zone its hours are in. */
function summary(hours: readonly CheckinHourCount[], currentHour: number | null, timeZone: string | null): string {
  const busiest = busiestHour(hours)
  const parts = [
    busiest === null
      ? 'No check-ins yet today.'
      : `Busiest hour: ${formatHourRange(busiest.hour)}, ${checkins(busiest.checkin_count)}.`,
  ]
  if (currentHour !== null) {
    parts.push(`Current hour: ${formatHourRange(currentHour)}, ${checkins(checkinsInHour(hours, currentHour))}.`)
  }
  if (timeZone !== null) {
    parts.push(`Hours are in ${timeZone} time.`)
  }
  return parts.join(' ')
}

/**
 * Check-ins by hour: a chart, a sentence that says what it shows, and -- on
 * request -- the same 24 figures as a table. The table is how anyone gets an
 * exact count, with or without a pointer; the chart has no tooltips.
 */
export function HourlyCheckins({
  checkinsByHour,
  currentHour,
  timeZone,
}: {
  checkinsByHour: Section<CheckinsByHour>
  currentHour: number | null
  timeZone: string | null
}) {
  const [tableShown, setTableShown] = useState(false)

  return (
    <section className="panel" aria-labelledby="hourly-heading">
      <h3 id="hourly-heading">Hourly check-ins</h3>
      {checkinsByHour.status !== 'ready' && <SectionPlaceholder status={checkinsByHour.status} />}
      {checkinsByHour.status === 'ready' && (
        <>
          <p className="chart-summary" id="hourly-summary">
            {summary(checkinsByHour.data.hours, currentHour, timeZone)}
          </p>
          <HourlyCheckinsChart hours={checkinsByHour.data.hours} currentHour={currentHour} describedBy="hourly-summary" />
          <button
            type="button"
            className="button-secondary"
            aria-expanded={tableShown}
            aria-controls="hourly-table"
            onClick={() => setTableShown(!tableShown)}
          >
            {tableShown ? 'Hide hourly table' : 'Show hourly table'}
          </button>
          <div id="hourly-table" hidden={!tableShown}>
            {tableShown && (
              <table className="data-table" aria-labelledby="hourly-heading">
                <thead>
                  <tr>
                    <th scope="col">Hour</th>
                    <th scope="col">Check-ins</th>
                  </tr>
                </thead>
                <tbody>
                  {checkinsByHour.data.hours.map((entry) => (
                    <tr key={entry.hour} aria-current={entry.hour === currentHour ? 'true' : undefined}>
                      <th scope="row">
                        {formatHour(entry.hour)}
                        {entry.hour === currentHour && ' (current hour)'}
                      </th>
                      <td>{entry.checkin_count.toLocaleString('en-US')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </>
      )}
    </section>
  )
}
