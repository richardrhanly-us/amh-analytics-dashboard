import { useEffect, useRef, useState, type ReactNode } from 'react'

import type { OverviewReport, ReliabilityReport, RoutingReport, VolumeReport } from '../api/reports.ts'
import { reasonLabel, topRejectReasons } from '../liveToday/metrics.ts'
import { MetricCard } from '../liveToday/MetricCard.tsx'
import { SectionPlaceholder } from '../liveToday/SectionPlaceholder.tsx'
import { formatHour, formatHourRange } from '../time/productTime.ts'
import { ChartFigure } from './ChartFigure.tsx'
import {
  activeDays,
  average,
  busiestDay,
  busiestHour,
  busiestWeekday,
  formatAverage,
  formatCount,
  formatDate,
  formatPercent,
  hourlyAverages,
  NOT_AVAILABLE,
  percentOf,
  weekdayAverages,
} from './derive.ts'
import { count, days, derived, rejectNote, transitNote } from './figures.ts'
import { timeSeries } from './months.ts'
import type { ReportRead } from './useSorterReports.ts'

const NO_CHECKINS = 'No check-ins in this range'

/**
 * One report: a heading, then its content once loaded -- or, until then,
 * words saying it is loading, or that it could not be loaded, with a way to
 * try again. Each section stands alone: one that fails says so here and
 * changes nothing in the others.
 *
 * The "Try again" button stays where it is while it is trying, so whoever
 * pressed it keeps their place; when the report then arrives the button is
 * gone, and focus moves to the section's heading.
 */
export function ReportSection<T>({
  name,
  heading,
  read,
  children,
}: {
  name: string
  heading: string
  read: ReportRead<T>
  children: (data: T) => ReactNode
}) {
  const { section } = read
  const headingElement = useRef<HTMLHeadingElement>(null)
  const [retried, setRetried] = useState(false)
  const retrying = retried && section.status === 'loading'

  useEffect(() => {
    if (retried && section.status === 'ready') {
      headingElement.current?.focus()
    }
  }, [retried, section.status])

  return (
    <section className="panel report-section" aria-labelledby={`${name}-heading`}>
      <h4 id={`${name}-heading`} ref={headingElement} tabIndex={-1}>
        {heading}
      </h4>
      {section.status === 'loading' && !retrying && <SectionPlaceholder status="loading" />}
      {(section.status === 'error' || retrying) && (
        <div className="section-retry">
          {retrying ? <p className="section-pending">Trying again…</p> : <SectionPlaceholder status="error" />}
          <button
            type="button"
            className="button-secondary"
            // Unavailable while trying again, but not `disabled`: a disabled button drops keyboard focus.
            aria-disabled={read.loading}
            onClick={() => {
              if (!read.loading) {
                setRetried(true)
                read.retry()
              }
            }}
          >
            Try again<span className="visually-hidden">: {heading}</span>
          </button>
        </div>
      )}
      {section.status === 'ready' && children(section.data)}
    </section>
  )
}

/** `transits`: the organization's plan includes transit routing. Without it, the in-transit figure is not shown. */
export function OverviewSection({ read, transits }: { read: ReportRead<OverviewReport>; transits: boolean }) {
  return (
    <ReportSection name="report-overview" heading="Overview" read={read}>
      {(report) => {
        const total = report.checkin_count
        const busiest = busiestDay(report.days)
        // Day by day, or a month at a time for a long range: what is SHOWN. Every figure above is the days'.
        const series = timeSeries(report.days, (day) => [day.checkin_count, day.reject_count])
        return (
          <>
            <dl className="metrics">
              <MetricCard label="Check-ins" {...count(total, `Over ${days(report.range.days)}`)} />
              <MetricCard
                label="Average per day"
                {...derived(average(total, report.range.days), formatAverage, 'Every calendar day in the range')}
              />
              <MetricCard label="Active days" {...count(report.active_days, `Of ${days(report.range.days)}`)} />
              {transits && (
                <MetricCard
                  label="In transit"
                  {...count(report.transit_count, transitNote(percentOf(report.transit_count, total)))}
                />
              )}
              <MetricCard label="Rejects" {...count(report.reject_count, rejectNote(percentOf(report.reject_count, total)))} />
              <MetricCard
                label="Busiest day"
                {...(busiest === null
                  ? { tone: 'empty', text: NO_CHECKINS }
                  : count(busiest.checkin_count, formatDate(busiest.date)))}
              />
            </dl>
            <ChartFigure
              name="daily-checkins"
              heading={series.monthly ? 'Check-ins by month' : 'Daily check-ins'}
              summary={
                busiest === null
                  ? `${NO_CHECKINS}.`
                  : `${formatCount(total)} check-ins over ${days(report.range.days)}. Busiest day: ${formatDate(busiest.date)}, with ${formatCount(busiest.checkin_count)}.`
              }
              chartLabel={`Bar chart of check-ins ${series.monthly ? 'in each month' : 'on each day'} of the range`}
              bars={series.points.map((point) => ({ label: point.bar, value: point.values[0] }))}
              emptyText={NO_CHECKINS}
              columns={[series.period, 'Check-ins', 'Rejects']}
              rows={series.points.map((point) => [point.row, ...point.values.map(formatCount)])}
            />
          </>
        )
      }}
    </ReportSection>
  )
}

export function VolumeSection({ read }: { read: ReportRead<VolumeReport> }) {
  return (
    <ReportSection name="report-volume" heading="Volume & capacity" read={read}>
      {(report) => {
        const total = report.checkin_count
        const active = activeDays(report.days)
        const weekdays = weekdayAverages(report.days)
        const hours = hourlyAverages(report.hours, report.range.days)
        const topDay = busiestDay(report.days)
        const topWeekday = busiestWeekday(weekdays)
        const topHour = busiestHour(report.hours)
        return (
          <>
            <dl className="metrics">
              <MetricCard label="Check-ins" {...count(total)} />
              <MetricCard
                label="Average per day"
                {...derived(average(total, report.range.days), formatAverage, `All ${days(report.range.days)}`)}
              />
              <MetricCard
                label="Average per active day"
                {...derived(average(total, active), formatAverage, `${days(active)} with check-ins`, 'No day had check-ins')}
              />
              <MetricCard
                label="Busiest day"
                {...(topDay === null ? { tone: 'empty', text: NO_CHECKINS } : count(topDay.checkin_count, formatDate(topDay.date)))}
              />
              <MetricCard
                label="Busiest weekday"
                {...(topWeekday === null
                  ? { tone: 'empty', text: NO_CHECKINS }
                  : { tone: 'value', text: topWeekday.name, note: `${formatAverage(topWeekday.average as number)} a day on average` })}
              />
              <MetricCard
                label="Busiest hour"
                {...(topHour === null
                  ? { tone: 'empty', text: NO_CHECKINS }
                  : { tone: 'value', text: formatHourRange(topHour.hour), note: `${formatCount(topHour.checkin_count)} check-ins in the range` })}
              />
            </dl>
            <ChartFigure
              name="typical-week"
              heading="Typical week"
              summary={
                topWeekday === null
                  ? `${NO_CHECKINS}.`
                  : `Average check-ins on each day of the week. Every time a weekday falls in the range counts, including days with no check-ins. Busiest: ${topWeekday.name}, ${formatAverage(topWeekday.average as number)} a day.`
              }
              chartLabel="Bar chart of average check-ins for each day of the week"
              bars={weekdays.map((weekday) => ({ label: weekday.name.slice(0, 3), value: weekday.average ?? 0 }))}
              emptyText={NO_CHECKINS}
              columns={['Weekday', 'Days in range', 'Check-ins', 'Average per day']}
              rows={weekdays.map((weekday) => [
                weekday.name,
                formatCount(weekday.occurrences),
                formatCount(weekday.checkin_count),
                weekday.average === null ? NOT_AVAILABLE : formatAverage(weekday.average),
              ])}
            />
            <ChartFigure
              name="typical-day"
              heading="Typical day"
              summary={
                topHour === null
                  ? `${NO_CHECKINS}.`
                  : `Average check-ins in each hour of the day, over all ${days(report.range.days)} of the range. Busiest hour: ${formatHourRange(topHour.hour)}. Hours are in ${report.range.timezone} time.`
              }
              chartLabel="Bar chart of average check-ins in each hour of the day"
              bars={hours.map((hour) => ({ label: formatHour(hour.hour), value: hour.average ?? 0 }))}
              emptyText={NO_CHECKINS}
              columns={['Hour', 'Check-ins', 'Average per day']}
              rows={hours.map((hour) => [
                formatHour(hour.hour),
                formatCount(hour.checkin_count),
                hour.average === null ? NOT_AVAILABLE : formatAverage(hour.average),
              ])}
            />
          </>
        )
      }}
    </ReportSection>
  )
}

/**
 * Where the sorter sent the range's check-ins. Destinations are whatever the
 * sorter has configured, in that order, however many. They are outcomes of
 * this one sorter: nothing here is a link, because a destination has no
 * reports of its own.
 */
export function RoutingSection({ read }: { read: ReportRead<RoutingReport> }) {
  return (
    <ReportSection name="report-routing" heading="Routing" read={read}>
      {(report) => {
        const total = report.checkin_count
        const share = (part: number) => {
          const percent = percentOf(part, total)
          return percent === null ? NOT_AVAILABLE : formatPercent(percent)
        }
        const shareNote = (part: number) => (total === 0 ? NO_CHECKINS : `${share(part)} of check-ins`)
        const transitByDay = report.days.map((day) => ({
          date: day.date,
          checkin_count: day.transit_counts.reduce((sum, value) => sum + value, 0),
        }))
        const topTransitDay = busiestDay(transitByDay)
        const transitSeries = timeSeries(report.days, (day) => [day.transit_counts.reduce((sum, value) => sum + value, 0), ...day.transit_counts])
        const rows = [
          { key: 'home', label: `${report.home.label} (home)`, value: report.home.checkin_count },
          ...report.transit.map((destination) => ({ key: `to-${destination.key}`, label: destination.label, value: destination.checkin_count })),
          ...(report.other_count > 0 ? [{ key: 'other', label: 'Other', value: report.other_count }] : []),
        ]
        return (
          <>
            <dl className="metrics">
              <MetricCard label="Total transit" {...count(report.transit_count)} />
              <MetricCard
                label="Transit rate"
                {...derived(percentOf(report.transit_count, total), formatPercent, 'Of all check-ins', NO_CHECKINS)}
              />
              <MetricCard label={`Kept at ${report.home.label}`} {...count(report.home.checkin_count, shareNote(report.home.checkin_count))} />
              {report.other_count > 0 && (
                <MetricCard label="Other routing" {...count(report.other_count, shareNote(report.other_count))} />
              )}
            </dl>

            <h5 id="report-destinations-heading">Where check-ins went</h5>
            {report.transit.length === 0 && <p className="quiet">No transit destinations are configured for this sorter.</p>}
            <table className="data-table destination-table" aria-labelledby="report-destinations-heading">
              <thead>
                <tr>
                  <th scope="col">Destination</th>
                  <th scope="col">Check-ins</th>
                  <th scope="col">Share of check-ins</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.key}>
                    <th scope="row">
                      {row.label}
                      {/* The same share as the last column, drawn: decoration, since the figure is beside it. */}
                      <span className="share-bar" aria-hidden="true">
                        <span style={{ width: `${percentOf(row.value, total) ?? 0}%` }} />
                      </span>
                    </th>
                    <td>{formatCount(row.value)}</td>
                    <td>{share(row.value)}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            {report.transit.length > 0 && (
              <ChartFigure
                name="daily-transit"
                heading={transitSeries.monthly ? 'Transit by month' : 'Daily transit'}
                summary={
                  topTransitDay === null
                    ? 'No check-ins were sent to a transit destination in this range.'
                    : `${formatCount(report.transit_count)} check-ins were sent to a transit destination over ${days(report.range.days)}. Most on one day: ${formatCount(topTransitDay.checkin_count)}, on ${formatDate(topTransitDay.date)}.`
                }
                chartLabel={`Bar chart of check-ins sent to transit destinations ${transitSeries.monthly ? 'in each month' : 'on each day'} of the range`}
                bars={transitSeries.points.map((point) => ({ label: point.bar, value: point.values[0] }))}
                emptyText="No transit in this range"
                columns={[transitSeries.period, 'Total transit', ...report.transit.map((destination) => destination.label)]}
                rows={transitSeries.points.map((point) => [point.row, ...point.values.map(formatCount)])}
              />
            )}
          </>
        )
      }}
    </ReportSection>
  )
}

export function ReliabilitySection({ read }: { read: ReportRead<ReliabilityReport> }) {
  return (
    <ReportSection name="report-reliability" heading="Reliability" read={read}>
      {(report) => {
        const rejectsByDay = report.days.map((day) => ({ date: day.date, checkin_count: day.reject_count }))
        const worstDay = busiestDay(rejectsByDay)
        const reasons = topRejectReasons(report.reasons)
        const series = timeSeries(report.days, (day) => [day.reject_count, day.checkin_count])
        return (
          <>
            <dl className="metrics">
              <MetricCard label="Rejects" {...count(report.reject_count)} />
              <MetricCard
                label="Reject rate"
                {...derived(percentOf(report.reject_count, report.checkin_count), formatPercent, 'Rejects against check-ins', NO_CHECKINS)}
              />
            </dl>
            <ChartFigure
              name="daily-rejects"
              heading={series.monthly ? 'Rejects by month' : 'Daily rejects'}
              summary={
                worstDay === null
                  ? 'No rejects in this range.'
                  : `${formatCount(report.reject_count)} rejects over ${days(report.range.days)}. Most on one day: ${formatCount(worstDay.checkin_count)}, on ${formatDate(worstDay.date)}.`
              }
              chartLabel={`Bar chart of rejects ${series.monthly ? 'in each month' : 'on each day'} of the range`}
              bars={series.points.map((point) => ({ label: point.bar, value: point.values[0] }))}
              emptyText="No rejects in this range"
              columns={[series.period, 'Rejects', 'Check-ins']}
              rows={series.points.map((point) => [point.row, ...point.values.map(formatCount)])}
            />

            <h5 id="report-reasons-heading">Reject reasons</h5>
            {reasons.length === 0 ? (
              <p className="quiet">No rejects in this range.</p>
            ) : (
              <table className="data-table" aria-labelledby="report-reasons-heading">
                <thead>
                  <tr>
                    <th scope="col">Reason</th>
                    <th scope="col">Rejects</th>
                    <th scope="col">Share of rejects</th>
                  </tr>
                </thead>
                <tbody>
                  {reasons.map((entry) => (
                    <tr key={entry.reason}>
                      <th scope="row">{reasonLabel(entry.reason)}</th>
                      <td>{formatCount(entry.reject_count)}</td>
                      <td>{formatPercent(percentOf(entry.reject_count, report.reject_count) as number)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )
      }}
    </ReportSection>
  )
}
