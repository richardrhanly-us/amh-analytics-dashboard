import { useState } from 'react'

import { LoadFailure } from '../components/LoadFailure.tsx'
import { DateRangeControl } from './DateRangeControl.tsx'
import { DEFAULT_PRESET_DAYS, daysInRange, lastDays, type DateRange } from './dateRange.ts'
import { formatDate } from './derive.ts'
import { OverviewSection, ReliabilitySection, RoutingSection, VolumeSection } from './ReportSections.tsx'
import { useProductDay, useSorterReports } from './useSorterReports.ts'

const UNAVAILABLE = 'Reports are not available for this sorter yet.'

/** The four reports for one range. Keyed by range from outside, so a new range starts with nothing carried over. */
function Reports({ orgSlug, branchSlug, range }: { orgSlug: string; branchSlug: string; range: DateRange }) {
  const reports = useSorterReports(orgSlug, branchSlug, range)

  if (reports.unavailable) {
    // The sorter exists -- the organization lists it -- but has nothing to report on. That is not "not found".
    return (
      <p className="notice" role="note">
        {UNAVAILABLE}
      </p>
    )
  }

  return (
    <div className="report-sections">
      <OverviewSection read={reports.overview} />
      <VolumeSection read={reports.volume} />
      <RoutingSection read={reports.routing} />
      <ReliabilitySection read={reports.reliability} />
    </div>
  )
}

function ReportsForDay({ orgSlug, branchSlug, timeZone, today }: { orgSlug: string; branchSlug: string; timeZone: string; today: string }) {
  const [range, setRange] = useState<DateRange>(() => lastDays(DEFAULT_PRESET_DAYS, today))
  const length = daysInRange(range)

  return (
    <>
      <DateRangeControl range={range} today={today} onChange={setRange} />
      {/* What the figures below are for. It changes only when a range is applied, never while one is being typed. */}
      <p className="range-shown">
        Showing {formatDate(range.from)} to {formatDate(range.to)}: {length} {length === 1 ? 'day' : 'days'}, in{' '}
        {timeZone} time.
        {range.to === today && ' This range includes today, which is not over yet: its figures will still rise.'}
      </p>
      <Reports key={`${range.from}/${range.to}`} orgSlug={orgSlug} branchSlug={branchSlug} range={range} />
    </>
  )
}

/**
 * The reports of one sorter the user can see: Overview, Volume & capacity,
 * Routing and Reliability, over a range of days the person chooses.
 * `branchSlug` is the sorter's host branch: the scope the API reads by.
 *
 * The product's zone, and its date today, are read first: a range is made
 * of the product's calendar dates and may not go past its today, so nothing
 * can be asked for until both are known.
 */
export function SorterReports({ orgSlug, branchSlug }: { orgSlug: string; branchSlug: string }) {
  const { day, retry } = useProductDay(orgSlug, branchSlug)

  switch (day.status) {
    case 'loading':
      return <p role="status">Loading reports…</p>
    case 'unavailable':
      return (
        <p className="notice" role="note">
          {UNAVAILABLE}
        </p>
      )
    case 'error':
      return <LoadFailure message={day.message} onRetry={retry} />
    case 'ready':
      return <ReportsForDay orgSlug={orgSlug} branchSlug={branchSlug} timeZone={day.timeZone} today={day.today} />
  }
}
