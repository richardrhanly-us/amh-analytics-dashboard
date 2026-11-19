import type { HoldsReport } from '../api/reports.ts'
import { MetricCard } from '../liveToday/MetricCard.tsx'
import type { DateRange } from './dateRange.ts'
import { formatCount } from './derive.ts'
import { count } from './figures.ts'
import { ReportSection } from './ReportSections.tsx'
import { useHoldsReport } from './useSorterReports.ts'

/** Which holds the two counts are: the same words wherever they are shown. */
export const HOLDS_EXPLANATION =
  'Holds for library patrons: items checked in to wait for a patron to pick them up. Items in transit to another location are not holds. Holds for the library’s own service accounts and for interlibrary loans are not counted as public holds. Only confirmed holds are counted.'

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

/** "3 of the 30 days in this date range", or "this day" for a one-day range. */
function whichDays(affected: number, report: HoldsReport): string {
  const { days } = report.range
  return days === 1 ? 'this day' : `${formatCount(affected)} of the ${formatCount(days)} days in this date range`
}

/**
 * What the confirmed figures could not include, one sentence each, and only what applies (hold-count accuracy, D).
 * Each says what is missing or uncertain: none of it is a zero, and none of it is estimated.
 */
function holdsAccuracy(report: HoldsReport): { partial: string | null; ill: string | null; unknown: string | null } {
  const { public_holds_partial_days: partial, ill_uncertain_days: ill, unknown_alert_count: unknown } = report
  return {
    partial:
      partial > 0
        ? `Public holds are partial. On ${whichDays(partial, report)}, this sorter’s collector could not tell holds for pickup at another location apart from items in transit to it, so those holds are not included. They are not available, not zero.`
        : null,
    ill:
      ill > 0
        ? `ILL holds only: on ${whichDays(ill, report)}, collected before this sorter’s collector was updated, a few interlibrary loan holds could be missed or added.`
        : null,
    unknown:
      unknown > 0
        ? `${formatCount(unknown)} ${unknown === 1 ? 'item had an alert type that was' : 'items had an alert type that was'} not recognized, so ${unknown === 1 ? 'it is' : 'they are'} not included in either figure.`
        : null,
  }
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
        const accuracy = holdsAccuracy(report)
        const notes = [accuracy.partial, accuracy.ill, accuracy.unknown].filter((note) => note !== null)
        return (
          <>
            <p className="section-intro">{HOLDS_EXPLANATION}</p>
            {covered !== null && (
              <p className="notice" role="note">
                {covered}
              </p>
            )}
            <dl className="metrics">
              <MetricCard label="Public holds" {...count(report.public_hold_count, accuracy.partial === null ? undefined : 'Partial')} />
              <MetricCard label="Interlibrary loan (ILL) holds" {...count(report.ill_hold_count, accuracy.ill === null ? undefined : 'May miss or add a few')} />
            </dl>
            {notes.length > 0 && (
              <div className="notice holds-accuracy" role="note">
                {notes.map((note) => (
                  <p key={note}>{note}</p>
                ))}
              </div>
            )}
            <p className="section-intro quiet">{HOLDS_COUNTING}</p>
            <p className="section-intro quiet">{HOLDS_LEGACY_NOTE}</p>
          </>
        )
      }}
    </ReportSection>
  )
}
