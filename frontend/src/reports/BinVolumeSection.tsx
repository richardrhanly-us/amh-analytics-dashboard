import type { CSSProperties } from 'react'

import type { BinVolumeBin, BinVolumeReport } from '../api/reports.ts'
import { MetricCard } from '../liveToday/MetricCard.tsx'
import { formatHour } from '../time/productTime.ts'
import { Figure } from './ChartFigure.tsx'
import { formatCount, formatPercent, percentOf } from './derive.ts'
import { count, derived } from './figures.ts'
import { ReportSection } from './ReportSections.tsx'
import type { ReportRead } from './useSorterReports.ts'

const HOURS = Array.from({ length: 24 }, (_, hour) => hour)
// Above this many bins the bars are set in two columns where there is room, so the chart is not a page tall.
const ONE_COLUMN_OF_BARS = 10

/** What a bin is called: its number, and nothing else. No bin has a name or a meaning of its own. */
const binLabel = (bin: BinVolumeBin) => `Bin ${bin.key}`
const checkIns = (value: number) => `${formatCount(value)} ${value === 1 ? 'check-in' : 'check-ins'}`

/**
 * A bar for each observed bin, in the order given, each beside its name and
 * its count. Bars run sideways: every bin keeps its label however many there
 * are, and a bin's row is the same height whether there are three or twenty.
 *
 * One image to assistive technology, named here and described by the
 * sentence `describedBy` names; the exact figures are in the table that
 * accompanies it. Nothing in it takes focus or needs a pointer, and it has
 * one series in one colour.
 */
function BinBars({ label, describedBy, bins, known }: { label: string; describedBy: string; bins: readonly BinVolumeBin[]; known: number }) {
  const most = Math.max(...bins.map((bin) => bin.checkin_count))

  return (
    <div className={bins.length > ONE_COLUMN_OF_BARS ? 'bin-bars bin-bars-many' : 'bin-bars'} role="img" aria-label={label} aria-describedby={describedBy}>
      {bins.map((bin) => (
        <div className="bin-bar" key={bin.key} data-bin={bin.key} aria-hidden="true">
          <span className="bin-bar-name">{binLabel(bin)}</span>
          <span className="bin-bar-track">
            {/* Never thinner than a hairline: a bin with one check-in beside one with thousands is still a bar. */}
            <span style={{ width: `max(${(bin.checkin_count / most) * 100}%, 2px)` }} />
          </span>
          <span className="bin-bar-value">{formatCount(bin.checkin_count)}</span>
          <span className="bin-bar-share">{formatPercent(percentOf(bin.checkin_count, known) as number)}</span>
        </div>
      ))}
    </div>
  )
}

/**
 * Each observed bin by hour of the day: a row for a bin, a column for each
 * of the 24 wall-clock hours, and the bin's total. A real table, in a box
 * that scrolls sideways on its own when the page is narrower than 26
 * columns, with the bin's name kept in view.
 */
function BinHours({ bins, timeZone }: { bins: readonly BinVolumeBin[]; timeZone: string }) {
  // The busiest single hour of any bin: what the faint tint behind a figure is measured against.
  const most = Math.max(...bins.flatMap((bin) => bin.hours))

  return (
    <>
      <h5 id="bin-hours-heading">Bin volume by hour</h5>
      <p className="chart-summary" id="bin-hours-summary">
        Check-ins in each bin by hour of the day, added up across the date range. Hours are in {timeZone} time.
      </p>
      <div className="table-scroll" role="group" aria-labelledby="bin-hours-heading" tabIndex={0}>
        <table className="data-table bin-hours" aria-labelledby="bin-hours-heading" aria-describedby="bin-hours-summary">
          <thead>
            <tr>
              <th scope="col">Bin</th>
              {HOURS.map((hour) => (
                <th key={hour} scope="col">
                  {formatHour(hour)}
                </th>
              ))}
              <th scope="col">Total</th>
            </tr>
          </thead>
          <tbody>
            {bins.map((bin) => (
              <tr key={bin.key}>
                <th scope="row">{binLabel(bin)}</th>
                {bin.hours.map((value, hour) => (
                  // An hour with nothing is still a figure, 0, set quieter. One with volume has a tint behind its
                  // figure, deeper the busier it is: decoration that repeats the number beside it.
                  <td
                    key={hour}
                    className={value === 0 ? 'cell-zero' : undefined}
                    style={value === 0 ? undefined : ({ '--heat': (value / most).toFixed(3) } as CSSProperties)}
                  >
                    {formatCount(value)}
                  </td>
                ))}
                <td className="cell-total">{formatCount(bin.checkin_count)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

function BinVolume({ report }: { report: BinVolumeReport }) {
  const { bins } = report
  const total = report.checkin_count
  const known = report.known_bin_count
  const unknown = report.unknown_bin_count

  if (total === 0) {
    return <p className="report-empty">No check-ins were recorded in this date range.</p>
  }

  const busiest = bins.reduce<BinVolumeBin | null>((top, bin) => (top === null || bin.checkin_count > top.checkin_count ? bin : top), null)

  return (
    <>
      <dl className="metrics">
        <MetricCard label="Known-bin check-ins" {...count(known, `Of ${checkIns(total)}`)} />
        <MetricCard label="Bins observed" {...count(bins.length, 'With check-ins in this date range')} />
        {unknown > 0 && <MetricCard label="Unknown-bin check-ins" {...count(unknown, 'No recognized bin value')} />}
        <MetricCard label="Bin coverage" {...derived(percentOf(known, total), formatPercent, 'Check-ins with a recognized bin')} />
      </dl>

      {/* Said in words as well as counted: these are check-ins like any other, with no bin to put them under. */}
      {unknown > 0 && known > 0 && (
        <p className="notice" role="note">
          {checkIns(unknown)} did not have a recognized bin value. {unknown === 1 ? 'It is' : 'They are'} counted here
          and {unknown === 1 ? 'is' : 'are'} not part of any bin below.
        </p>
      )}

      {busiest === null ? (
        <p className="report-empty">Check-ins were recorded, but none had a recognized bin value.</p>
      ) : (
        <>
          <Figure
            name="bin-checkins"
            heading="Check-ins by bin"
            summary={`${checkIns(known)} across ${formatCount(bins.length)} observed ${bins.length === 1 ? 'bin' : 'bins'}. Most in one bin: ${formatCount(busiest.checkin_count)}, in ${binLabel(busiest)}.`}
            columns={['Bin', 'Check-ins', 'Share of known-bin check-ins']}
            rows={bins.map((bin) => [binLabel(bin), formatCount(bin.checkin_count), formatPercent(percentOf(bin.checkin_count, known) as number)])}
          >
            {(summaryName) => (
              <BinBars label="Bar chart of check-ins in each observed bin" describedBy={summaryName} bins={bins} known={known} />
            )}
          </Figure>
          <BinHours bins={bins} timeZone={report.range.timezone} />
        </>
      )}
    </>
  )
}

/**
 * Which physical sorter bins received the range's check-ins. Bins are
 * whatever the sorter logged, however many, in the order of their numbers.
 * Nothing here knows which bins the sorter has, what any of them is for, or
 * how full one was: a bin is its number and a count.
 */
export function BinVolumeSection({ read }: { read: ReportRead<BinVolumeReport> }) {
  return (
    <ReportSection name="report-bins" heading="Bin volume" read={read}>
      {(report) => (
        <>
          <p className="section-intro">Shows which physical sorter bins received check-ins during the selected date range.</p>
          <p className="section-intro quiet">
            Only bins observed in this date range are listed. SortView does not currently store the sorter’s configured
            bin inventory, so a bin with no check-ins during this range may not appear.
          </p>
          <BinVolume report={report} />
        </>
      )}
    </ReportSection>
  )
}
