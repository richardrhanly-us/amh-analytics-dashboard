import { useState } from 'react'

import { LoadFailure } from '../components/LoadFailure.tsx'
import { DateRangeControl, RangeShown } from './DateRangeControl.tsx'
import { DEFAULT_PRESET_DAYS, lastDays, type DateRange } from './dateRange.ts'
import { BinVolumeSection } from './BinVolumeSection.tsx'
import { EfficiencySection } from './EfficiencySection.tsx'
import { OverviewSection, ReliabilitySection, RoutingSection, VolumeSection } from './ReportSections.tsx'
import { useProductDay, useSorterReports } from './useSorterReports.ts'

const UNAVAILABLE = 'Reports are not available for this sorter yet.'

interface Sorter {
  orgSlug: string
  branchSlug: string
  /** The person may see the sorter's Efficiency report: a sixth section, read on its own. */
  efficiency: boolean
}

/**
 * The reports for one range. Keyed by range from outside, so a new range starts with nothing carried over.
 * `today` is the product's date.
 */
function Reports({ orgSlug, branchSlug, efficiency, range, today }: Sorter & { range: DateRange; today: string }) {
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
      <BinVolumeSection read={reports.bins} />
      <ReliabilitySection read={reports.reliability} />
      {/* Not asked for at all unless the person may see it. Whatever becomes of it, the five above are untouched. */}
      {efficiency && <EfficiencySection orgSlug={orgSlug} branchSlug={branchSlug} range={range} today={today} />}
    </div>
  )
}

function ReportsForDay({ orgSlug, branchSlug, efficiency, timeZone, today }: Sorter & { timeZone: string; today: string }) {
  const [range, setRange] = useState<DateRange>(() => lastDays(DEFAULT_PRESET_DAYS, today))

  return (
    <>
      <DateRangeControl range={range} today={today} onChange={setRange} />
      <RangeShown range={range} today={today} timeZone={timeZone} />
      <Reports key={`${range.from}/${range.to}`} orgSlug={orgSlug} branchSlug={branchSlug} efficiency={efficiency} range={range} today={today} />
    </>
  )
}

/**
 * The reports of one sorter the user can see: Overview, Volume & capacity,
 * Routing, Bin volume and Reliability -- and, for the organization's owners
 * and admins, Efficiency -- over a range of days the person chooses.
 * `branchSlug` is the sorter's host branch: the scope the API reads by.
 *
 * The product's zone, and its date today, are read first: a range is made
 * of the product's calendar dates and may not go past its today, so nothing
 * can be asked for until both are known.
 */
export function SorterReports({ orgSlug, branchSlug, efficiency }: Sorter) {
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
      return <ReportsForDay orgSlug={orgSlug} branchSlug={branchSlug} efficiency={efficiency} timeZone={day.timeZone} today={day.today} />
  }
}
