import type { SorterReportView } from '../router/paths.ts'
import { LoadFailure } from '../components/LoadFailure.tsx'
import { DateRangeControl, RangeShown } from './DateRangeControl.tsx'
import type { DateRange } from './dateRange.ts'
import { BinVolumeSection } from './BinVolumeSection.tsx'
import { EfficiencySection } from './EfficiencySection.tsx'
import { HoldsSection } from './HoldsSection.tsx'
import { OverviewSection, ReliabilitySection, RoutingSection, VolumeSection } from './ReportSections.tsx'
import { useReportRange, type RangeComparison } from './useReportRange.ts'
import { usePreviousOverview, useProductDay, useSorterReports } from './useSorterReports.ts'

const UNAVAILABLE = 'Reports are not available for this sorter yet.'
const BEFORE_HISTORY = "The selected range starts before this organization's available reporting window. Choose a later start date."

interface Sorter {
  orgSlug: string
  branchSlug: string
  /** Which of the sorter's report views this is: its operational reports, or its Efficiency report alone. */
  view: SorterReportView
  /** The organization's plan includes the sorter's Holds report: another section, read on its own. */
  holds: boolean
  /** The organization's plan includes transit routing: the Routing report, and the in-transit figure. */
  transits: boolean
  /** How far back the organization's plan lets a report start (pages/capabilities' historyDays; null: no limit). */
  historyDays: number | null
}

/**
 * The operational reports for one range. Keyed by range from outside, so a new range starts with nothing carried
 * over. `comparison` says whether the Overview is set beside the previous period's: that period's own Overview is
 * then read too, and only then.
 */
function Reports({ orgSlug, branchSlug, holds, transits, range, comparison }: Sorter & { range: DateRange; comparison: RangeComparison }) {
  const reports = useSorterReports(orgSlug, branchSlug, range, transits)
  const previous = usePreviousOverview(orgSlug, branchSlug, comparison.previous, comparison.active)

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
      <OverviewSection
        read={reports.overview}
        transits={transits}
        comparison={comparison.active ? { range: comparison.previous, read: previous } : undefined}
      />
      <VolumeSection read={reports.volume} />
      {/* Not asked for at all unless the plan includes transit routing. */}
      {transits && <RoutingSection read={reports.routing} />}
      <BinVolumeSection read={reports.bins} />
      <ReliabilitySection read={reports.reliability} />
      {/* Not asked for at all unless the plan has it. Whatever becomes of it, the five above are untouched. */}
      {holds && <HoldsSection orgSlug={orgSlug} branchSlug={branchSlug} range={range} />}
    </div>
  )
}

function ReportsForDay({ timeZone, today, ...sorter }: Sorter & { timeZone: string; today: string }) {
  // The range is the address's: the same in every view of these reports, and after a reload.
  const { range, setRange, comparison, setCompare } = useReportRange(today, sorter.historyDays)
  const rangeKey = `${range.from}/${range.to}`
  // Only the operational reports have anything to compare: the Efficiency view is not offered the choice.
  const compares = sorter.view === 'operations'

  return (
    <>
      <DateRangeControl
        range={range}
        today={today}
        historyDays={sorter.historyDays}
        onChange={setRange}
        comparison={compares ? comparison : undefined}
        onCompare={setCompare}
      />
      <RangeShown range={range} today={today} timeZone={timeZone} />
      {sorter.view === 'efficiency' ? (
        // Read on its own: none of the operational reports is asked for here. `today` is the product's date.
        <div className="report-sections" key={rangeKey}>
          <EfficiencySection orgSlug={sorter.orgSlug} branchSlug={sorter.branchSlug} range={range} today={today} />
        </div>
      ) : (
        <Reports key={rangeKey} {...sorter} range={range} comparison={comparison} />
      )}
    </>
  )
}

/**
 * The reports of one sorter the user can see, over a range of days the person
 * chooses, a view at a time: Operations is Overview, Volume & capacity,
 * Routing, Bin volume and Reliability -- and, where the plan has it, Holds;
 * Efficiency, for the organization's owners and admins, is the Efficiency
 * report. The range is the same in both: it is kept in the address
 * (useReportRange).
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
