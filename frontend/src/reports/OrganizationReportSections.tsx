import type { ReactNode } from 'react'
import { Link } from 'react-router'

import type { OrganizationOverviewReport, OrganizationReliabilityReport, RoutingNetworkReport } from '../api/organizationReports.ts'
import { reasonLabel, topRejectReasons } from '../liveToday/metrics.ts'
import { MetricCard } from '../liveToday/MetricCard.tsx'
import { sorterStatusLabel } from '../pages/labels.ts'
import { sorterReportsPath } from '../router/paths.ts'
import { ChartFigure } from './ChartFigure.tsx'
import {
  average,
  busiestDay,
  formatAverage,
  formatCount,
  formatDate,
  formatDayOfWeek,
  formatPercent,
  formatShortDate,
  NOT_AVAILABLE,
  percentOf,
} from './derive.ts'
import { count, days, derived, rejectNote, transitNote } from './figures.ts'
import { ReportSection } from './ReportSections.tsx'
import type { ReportRead } from './useSorterReports.ts'

/**
 * An organization's reports: what all of its sorting machines did together,
 * and how they compare.
 *
 * EVERY RATE HERE IS ONE TOTAL OVER ANOTHER -- the organization's rejects
 * over the organization's check-ins -- and never an average of the sorters'
 * own rates, which would let a quiet machine count as much as a busy one.
 *
 * A sorter that is registered but has no data to read is shown, marked
 * Unavailable, with no figures: its zeros mean nothing could be read, not
 * that nothing happened.
 */

export const NO_SORTERS = 'No sorting machines are registered for this organization yet.'
const NO_ACTIVITY = 'No processing activity in this range'
const UNAVAILABLE = 'Unavailable'

/** One count over another as a percentage, in words when there is nothing to divide by. */
function rate(part: number, whole: number): string {
  const percent = percentOf(part, whole)
  return percent === null ? NOT_AVAILABLE : formatPercent(percent)
}

/** In place of a figure that does not exist: a dash to look at, and words to hear. */
function Missing({ meaning }: { meaning: string }) {
  return (
    <>
      <span aria-hidden="true">—</span>
      <span className="visually-hidden">{meaning}</span>
    </>
  )
}

/** A sorter's figure -- or, for a sorter that could not be read, the absence of one. Never a zero that was not counted. */
const figure = (available: boolean, text: string): ReactNode => (available ? text : <Missing meaning={NOT_AVAILABLE} />)

function UnavailableNote() {
  return (
    <p className="quiet">
      A sorting machine marked {UNAVAILABLE} has no reporting data yet. That is not a count of zero: nothing of its is
      included in the totals.
    </p>
  )
}

/**
 * A table whose first column names each row. `wide` lets it scroll sideways
 * inside its own box, which the keyboard can reach, when it has more columns
 * than the page has room for.
 */
function DataTable({
  labelledBy,
  columns,
  rows,
  wide = false,
}: {
  labelledBy: string
  columns: readonly string[]
  rows: ReadonlyArray<{ key: string; cells: readonly ReactNode[] }>
  wide?: boolean
}) {
  const table = (
    <table className="data-table" aria-labelledby={labelledBy}>
      <thead>
        <tr>
          {columns.map((column, index) => (
            <th key={index} scope="col">
              {column}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.key}>
            <th scope="row">{row.cells[0]}</th>
            {row.cells.slice(1).map((cell, index) => (
              <td key={index}>{cell}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  )
  return wide ? (
    <div className="table-scroll" role="group" aria-labelledby={labelledBy} tabIndex={0}>
      {table}
    </div>
  ) : (
    table
  )
}

/** `transits`: the organization's plan includes transit routing. Without it, the in-transit figure is not shown. */
export function OrganizationOverviewSection({ read, transits }: { read: ReportRead<OrganizationOverviewReport>; transits: boolean }) {
  return (
    <ReportSection name="org-overview" heading="Overview" read={read}>
      {(report) => {
        if (report.sorters.length === 0) {
          return <p>{NO_SORTERS}</p>
        }
        const { totals, range } = report
        const total = totals.checkin_count
        const reporting = report.sorters.filter((sorter) => sorter.available).length
        const busiest = busiestDay(report.days)
        return (
          <>
            <p className="quiet">
              Check-ins are processing events, added up across the organization&rsquo;s sorting machines. An item
              handled at two machines is counted at each.
            </p>
            <dl className="metrics">
              <MetricCard label="Check-ins" {...count(total, `Over ${days(range.days)}`)} />
              <MetricCard label="Average per day" {...derived(average(total, range.days), formatAverage, 'Every calendar day in the range')} />
              {transits && (
                <MetricCard label="In transit" {...count(totals.transit_count, transitNote(percentOf(totals.transit_count, total)))} />
              )}
              <MetricCard label="Rejects" {...count(totals.reject_count, rejectNote(percentOf(totals.reject_count, total)))} />
              <MetricCard
                label="Sorting machines"
                {...count(
                  report.sorters.length,
                  reporting === report.sorters.length ? 'All with reporting data' : `${formatCount(reporting)} with reporting data`,
                )}
              />
            </dl>
            <ChartFigure
              name="org-daily-checkins"
              heading="Daily check-ins"
              summary={
                busiest === null
                  ? `${NO_ACTIVITY}.`
                  : `${formatCount(total)} check-ins across the organization over ${days(range.days)}. Busiest day: ${formatDate(busiest.date)}, with ${formatCount(busiest.checkin_count)}.`
              }
              chartLabel="Bar chart of the organization's check-ins on each day of the range"
              bars={report.days.map((day) => ({ label: formatShortDate(day.date), value: day.checkin_count }))}
              emptyText={NO_ACTIVITY}
              columns={['Date', 'Check-ins', 'Rejects']}
              rows={report.days.map((day) => [formatDayOfWeek(day.date), formatCount(day.checkin_count), formatCount(day.reject_count)])}
            />
          </>
        )
      }}
    </ReportSection>
  )
}

/**
 * The organization's sorters side by side, in the organization's own order.
 * A sorter that has reports is a link to them.
 */
export function SorterComparisonSection({
  orgSlug,
  read,
  transits,
}: {
  orgSlug: string
  read: ReportRead<OrganizationOverviewReport>
  /** The organization's plan includes transit routing. Without it, the two transit columns are not shown. */
  transits: boolean
}) {
  return (
    <ReportSection name="org-comparison" heading="Sorter comparison" read={read}>
      {(report) => {
        if (report.sorters.length === 0) {
          return <p>{NO_SORTERS}</p>
        }
        const total = report.totals.checkin_count
        return (
          <>
            <DataTable
              wide
              labelledBy="org-comparison-heading"
              columns={[
                'Sorter',
                'Location',
                'Status',
                'Check-ins',
                'Share of check-ins',
                ...(transits ? ['In transit', 'Transit rate'] : []),
                'Rejects',
                'Reject rate',
                'Active days',
                'Collectors',
              ]}
              rows={report.sorters.map((sorter) => {
                const status = sorterStatusLabel(sorter.status)
                return {
                  key: sorter.slug,
                  cells: [
                    sorter.available ? (
                      <Link to={sorterReportsPath(orgSlug, sorter.slug)}>
                        {sorter.name}
                        <span className="visually-hidden">: sorter reports</span>
                      </Link>
                    ) : (
                      sorter.name
                    ),
                    sorter.host_branch.name,
                    sorter.available ? (status ?? 'Active') : [UNAVAILABLE, status].filter((word) => word !== null).join(' · '),
                    figure(sorter.available, formatCount(sorter.checkin_count)),
                    figure(sorter.available, rate(sorter.checkin_count, total)),
                    ...(transits
                      ? [
                          figure(sorter.available, formatCount(sorter.transit_count)),
                          figure(sorter.available, rate(sorter.transit_count, sorter.checkin_count)),
                        ]
                      : []),
                    figure(sorter.available, formatCount(sorter.reject_count)),
                    figure(sorter.available, rate(sorter.reject_count, sorter.checkin_count)),
                    figure(sorter.available, `${formatCount(sorter.active_days)} of ${formatCount(report.range.days)}`),
                    formatCount(sorter.collector_count),
                  ],
                }
              })}
            />
            <p className="quiet">
              Each rate is that machine&rsquo;s own count over its own check-ins. A machine&rsquo;s name opens its own
              reports.
            </p>
            {report.sorters.some((sorter) => !sorter.available) && <UnavailableNote />}
          </>
        )
      }}
    </ReportSection>
  )
}

/**
 * Where the organization's sorters sent their check-ins.
 *
 * A destination is a ROUTING OUTCOME. Two sorters share a column because
 * they have a destination by the same key, and that is all it means: it is
 * not a sorter, it has no reports, and nothing here is a link to one -- even
 * where a destination and a sorter go by the same name.
 */
export function RoutingNetworkSection({ read }: { read: ReportRead<RoutingNetworkReport> }) {
  return (
    <ReportSection name="org-routing" heading="Routing network" read={read}>
      {(report) => {
        const { totals, sources, destinations } = report
        if (sources.length === 0) {
          return <p>No sorting machine of this organization has routing data to show yet.</p>
        }
        return (
          <>
            <p className="quiet">Routing is shown for the sorting machines that have reporting data.</p>
            <dl className="metrics">
              <MetricCard label="Check-ins" {...count(totals.checkin_count)} />
              <MetricCard label="Total transit" {...count(totals.transit_count)} />
              <MetricCard
                label="Transit rate"
                {...derived(percentOf(totals.transit_count, totals.checkin_count), formatPercent, 'Of all check-ins', NO_ACTIVITY)}
              />
            </dl>
            {totals.checkin_count === 0 && <p className="quiet">{NO_ACTIVITY}.</p>}

            <h5 id="org-routing-sources-heading">Routing by sorter</h5>
            <DataTable
              labelledBy="org-routing-sources-heading"
              columns={['Sorter', 'Check-ins', 'Kept at home', 'In transit', 'Transit rate', 'Other routing']}
              rows={sources.map((source) => ({
                key: source.sorter.slug,
                cells: [
                  source.sorter.name,
                  formatCount(source.checkin_count),
                  `${formatCount(source.home.checkin_count)} (${source.home.label})`,
                  formatCount(source.transit_count),
                  rate(source.transit_count, source.checkin_count),
                  formatCount(source.other_count),
                ],
              }))}
            />

            <h5 id="org-routing-destinations-heading">Destination totals</h5>
            {destinations.length === 0 ? (
              <p className="quiet">No routed destinations are configured for reporting.</p>
            ) : (
              <>
                {totals.transit_count === 0 && totals.checkin_count > 0 && (
                  <p className="quiet">No check-ins were routed to a configured destination in this range.</p>
                )}
                <DataTable
                  labelledBy="org-routing-destinations-heading"
                  columns={['Destination', 'Check-ins routed', 'Sorters with this destination']}
                  rows={destinations.map((destination) => ({
                    key: destination.key,
                    cells: [
                      destination.label,
                      formatCount(destination.checkin_count),
                      `${formatCount(destination.source_count)} of ${formatCount(sources.length)}`,
                    ],
                  }))}
                />

                <h5 id="org-routing-matrix-heading">Routing matrix</h5>
                <p className="chart-summary" id="org-routing-matrix-summary">
                  Each row is a sorting machine and each column a destination it routes to. A dash means that machine
                  has no such destination. A column groups destinations that go by the same name; it is a routing
                  outcome, not a sorting machine.
                </p>
                <div className="table-scroll" role="group" aria-labelledby="org-routing-matrix-heading" tabIndex={0}>
                  <table className="data-table" aria-labelledby="org-routing-matrix-heading" aria-describedby="org-routing-matrix-summary">
                    <thead>
                      <tr>
                        <th scope="col">From sorter</th>
                        {destinations.map((destination) => (
                          <th key={destination.key} scope="col">
                            {destination.label}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {sources.map((source) => (
                        <tr key={source.sorter.slug}>
                          <th scope="row">{source.sorter.name}</th>
                          {destinations.map((destination) => {
                            const routed = source.transit.find((candidate) => candidate.key === destination.key)
                            return (
                              <td key={destination.key}>
                                {routed === undefined ? <Missing meaning="Not configured" /> : formatCount(routed.checkin_count)}
                              </td>
                            )
                          })}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </>
        )
      }}
    </ReportSection>
  )
}

/** The organization's rejects: how many, of what kinds, and at which sorter. Counts and nothing concluded from them. */
export function SystemReliabilitySection({ read }: { read: ReportRead<OrganizationReliabilityReport> }) {
  return (
    <ReportSection name="org-reliability" heading="System reliability" read={read}>
      {(report) => {
        const { totals } = report
        if (report.sorters.length === 0) {
          return <p>{NO_SORTERS}</p>
        }
        const worstDay = busiestDay(report.days.map((day) => ({ date: day.date, checkin_count: day.reject_count })))
        const reasons = topRejectReasons(totals.reasons)
        return (
          <>
            <dl className="metrics">
              <MetricCard label="Rejects" {...count(totals.reject_count)} />
              <MetricCard
                label="Reject rate"
                {...derived(percentOf(totals.reject_count, totals.checkin_count), formatPercent, 'All rejects against all check-ins', NO_ACTIVITY)}
              />
            </dl>
            <ChartFigure
              name="org-daily-rejects"
              heading="Daily rejects"
              summary={
                worstDay === null
                  ? 'No rejects in this range.'
                  : `${formatCount(totals.reject_count)} rejects across the organization over ${days(report.range.days)}. Most on one day: ${formatCount(worstDay.checkin_count)}, on ${formatDate(worstDay.date)}.`
              }
              chartLabel="Bar chart of the organization's rejects on each day of the range"
              bars={report.days.map((day) => ({ label: formatShortDate(day.date), value: day.reject_count }))}
              emptyText="No rejects in this range"
              columns={['Date', 'Rejects', 'Check-ins']}
              rows={report.days.map((day) => [formatDayOfWeek(day.date), formatCount(day.reject_count), formatCount(day.checkin_count)])}
            />

            <h5 id="org-reasons-heading">Reject reasons</h5>
            {reasons.length === 0 ? (
              <p className="quiet">No rejects in this range.</p>
            ) : (
              <DataTable
                labelledBy="org-reasons-heading"
                columns={['Reason', 'Rejects', 'Share of rejects']}
                rows={reasons.map((entry) => ({
                  key: entry.reason,
                  cells: [reasonLabel(entry.reason), formatCount(entry.reject_count), rate(entry.reject_count, totals.reject_count)],
                }))}
              />
            )}

            <h5 id="org-reliability-sorters-heading">Rejects by sorter</h5>
            <DataTable
              labelledBy="org-reliability-sorters-heading"
              columns={['Sorter', 'Check-ins', 'Rejects', 'Reject rate', 'Most frequent reason']}
              rows={report.sorters.map((entry) => {
                const [top] = topRejectReasons(entry.reasons)
                return {
                  key: entry.sorter.slug,
                  cells: [
                    entry.available ? entry.sorter.name : `${entry.sorter.name} (${UNAVAILABLE})`,
                    figure(entry.available, formatCount(entry.checkin_count)),
                    figure(entry.available, formatCount(entry.reject_count)),
                    figure(entry.available, rate(entry.reject_count, entry.checkin_count)),
                    figure(entry.available, top === undefined ? 'No rejects' : reasonLabel(top.reason)),
                  ],
                }
              })}
            />
            {report.sorters.some((entry) => !entry.available) && <UnavailableNote />}
          </>
        )
      }}
    </ReportSection>
  )
}
