import { useState } from 'react'

import { LoadFailure } from '../components/LoadFailure.tsx'
import { DateRangeControl, RangeShown } from './DateRangeControl.tsx'
import { defaultRange, type DateRange } from './dateRange.ts'
import { BinVolumeSection } from './BinVolumeSection.tsx'
import { EfficiencySection } from './EfficiencySection.tsx'
import { HoldsSection } from './HoldsSection.tsx'
import { OverviewSection, ReliabilitySection, RoutingSection, VolumeSection } from './ReportSections.tsx'
import { useProductDay, useSorterReports } from './useSorterReports.ts'

const UNAVAILABLE = 'Reports are not available for this sorter yet.'
const BEFORE_HISTORY = "The selected range starts before this organization's available reporting window. Choose a later start date."

interface Sorter {
  orgSlug: string
  branchSlug: string
  /** The person may see the sorter's Efficiency report: a sixth section, read on its own. */
  efficiency: boolean
  /** The organization's plan includes the sorter's Holds report: another section, read on its own. */
  holds: boolean
  /** The organization's plan includes transit routing: the Routing report, and the in-transit figure. */
  transits: boolean
  /** How far back the organization's plan lets a report start (pages/capabilities' historyDays; null: no limit). */
  historyDays: number | null
}

/**
 * The reports for one range. Keyed by range from outside, so a new range starts with nothing carried over.
 * `today` is the product's date.
 */
function Reports({ orgSlug, branchSlug, efficiency, holds, transits, range, today }: Sorter & { range: DateRange; today: string }) {
  const reports = useSorterReports(orgSlug, branchSlug, range, transits)

  if (reports.unavailable) {
    // The sorter exists -- the organization lists it -- but has nothing to report on. That is not "not found".
    return (
      <p className="notice" role="note">
        {UNAVAILABLE}
      </p>
    )
  }

  if (reports.beforeHistory) {
    // The plan's window changed after this range was chosen: the API refuses it. A later start date is the answer.
    return (
      <p className="notice" role="note">
        {BEFORE_HISTORY}
      </p>
    )
  }

  return (
    <div className="report-sections">
      <OverviewSection read={reports.overview} transits={transits} />
      <VolumeSection read={reports.volume} />
      {/* Not asked for at all unless the plan includes transit routing. */}
      {transits && <RoutingSection read={reports.routing} />}
      <BinVolumeSection read={reports.bins} />
      <ReliabilitySection read={reports.reliability} />
      {/* Not asked for at all unless the plan has it. Whatever becomes of it, the five above are untouched. */}
      {holds && <HoldsSection orgSlug={orgSlug} branchSlug={branchSlug} range={range} />}
      {/* Not asked for at all unless the person may see it. Whatever becomes of it, the five above are untouched. */}
      {efficiency && <EfficiencySection orgSlug={orgSlug} branchSlug={branchSlug} range={range} today={today} />}
    </div>
  )
}

function ReportsForDay({ timeZone, today, ...sorter }: Sorter & { timeZone: string; today: string }) {
  const [range, setRange] = useState<DateRange>(() => defaultRange(today, sorter.historyDays))

  return (
    <>
      <DateRangeControl range={range} today={today} historyDays={sorter.historyDays} onChange={setRange} />
      <RangeShown range={range} today={today} timeZone={timeZone} />
      <Reports key={`${range.from}/${range.to}`} {...sorter} range={range} today={today} />
    </>
  )
}

/**
 * The reports of one sorter the user can see: Overview, Volume & capacity,
 * Routing, Bin volume and Reliability -- and, where the plan has it, Holds,
 * and for the organization's owners and admins, Efficiency -- over a range of
 * days the person chooses.
 * `branchSlug` is the sorter's host branch: the scope the API reads by.
 *
 * The product's zone, and its date today, are read first: a range is made
 * of the product's calendar dates and may not go past its today, so nothing
 * can be asked for until both are known.
 */
export function SorterReports(sorter: Sorter) {
  const { orgSlug, branchSlug } = sorter
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
      return <ReportsForDay {...sorter} timeZone={day.timeZone} today={day.today} />
  }
}
