import type { HoldsReport } from '../api/reports.ts'
import { MetricCard } from '../liveToday/MetricCard.tsx'
import type { DateRange } from './dateRange.ts'
import { formatCount } from './derive.ts'
import { count } from './figures.ts'
import { ReportSection } from './ReportSections.tsx'
import { useHoldsReport } from './useSorterReports.ts'

/** Which holds the two counts are: the same words wherever they are shown. */
export const HOLDS_EXPLANATION =
  'Holds for library patrons. Holds for the library’s own service accounts and for interlibrary loans are not counted as public holds.'

/** How a range's figures are made (UI2 v7): said beside every range's figures, one day or ten years. */
export const HOLDS_COUNTING =
  'Each day counts an item at most once, by its last record that day, and the days are added together. Over more than one day these are daily hold counts added up: an item held on several days is counted on each, so the totals are not the number of distinct holds placed or distinct items.'

/** The legacy era's limits: its holds are classified with today's lists, not the lists of the day. */
export const HOLDS_LEGACY_NOTE =
  'Days before this sorter moved to the current collector data format (Contract 2) are classified using the library’s current lists of its own service accounts. If those lists were different then, older public-hold figures may include some service-account holds.'

/** What part of the range had anything to count. Nothing when every day did. */
function coverage(report: HoldsReport): string | null {
  const { days } = report.range
  if (report.days_with_data === days) {
    return null
  }
  if (report.days_with_data === 0) {
    return 'No sorter records were found for any day in this date range, so there is nothing to count.'
  }
  return `Sorter records were found on ${formatCount(report.days_with_data)} of the ${formatCount(days)} days in this date range. Days without records are not counted as zero holds: there is no data for them.`
}

/**
 * A sorter's Holds report: how many public holds and how many interlibrary
 * loan holds it handled over the range. Two counts -- no holds by
 * destination, by patron or by item -- and how much of the range had data.
 *
 * Only for a plan that has it: the section is not there at all otherwise,
 * so nothing is asked for. It reads its report itself, and whatever becomes
 * of it changes nothing in the other sections.
 *
 * Any range the other reports take (UI2 v7): the counts are sums of daily
 * counts, which mean the same over a day or ten years.
 */
export function HoldsSection({ orgSlug, branchSlug, range }: { orgSlug: string; branchSlug: string; range: DateRange }) {
  const read = useHoldsReport(orgSlug, branchSlug, range)

  return (
    <ReportSection name="report-holds" heading="Holds" read={read}>
      {(report) => {
        const covered = coverage(report)
        return (
          <>
            <p className="section-intro">{HOLDS_EXPLANATION}</p>
            {covered !== null && (
              <p className="notice" role="note">
                {covered}
              </p>
            )}
            <dl className="metrics">
              <MetricCard label="Public holds" {...count(report.public_hold_count)} />
              <MetricCard label="Interlibrary loan (ILL) holds" {...count(report.ill_hold_count)} />
            </dl>
            <p className="section-intro quiet">{HOLDS_COUNTING}</p>
            <p className="section-intro quiet">{HOLDS_LEGACY_NOTE}</p>
          </>
        )
      }}
    </ReportSection>
  )
}
