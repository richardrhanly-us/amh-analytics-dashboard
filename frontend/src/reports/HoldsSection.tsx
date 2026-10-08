import { MetricCard } from '../liveToday/MetricCard.tsx'
import { daysInRange, HOLDS_MAX_RANGE_DAYS, type DateRange } from './dateRange.ts'
import { count } from './figures.ts'
import { ReportSection } from './ReportSections.tsx'
import { useHoldsReport } from './useSorterReports.ts'

const HOLDS_RANGE_NOTE = `Holds reporting is currently available for ranges up to ${HOLDS_MAX_RANGE_DAYS} days.`

/**
 * A sorter's Holds report: how many public holds and how many interlibrary
 * loan holds it handled over the range. Two counts, nothing else -- no
 * holds by destination, by patron or by item.
 *
 * Only for a plan that has it: the section is not there at all otherwise,
 * so nothing is asked for. It reads its report itself, and whatever becomes
 * of it changes nothing in the other sections.
 *
 * Only for a range of up to HOLDS_MAX_RANGE_DAYS days, whatever the plan's
 * history allows: each item is counted by its latest record in the whole
 * range, which over a long range stops meaning "holds handled". A longer
 * range is not asked for -- the API would refuse it -- and the section says
 * so instead. That is how the report works, not what the plan includes.
 */
export function HoldsSection({ orgSlug, branchSlug, range }: { orgSlug: string; branchSlug: string; range: DateRange }) {
  const covered = daysInRange(range) <= HOLDS_MAX_RANGE_DAYS
  const read = useHoldsReport(orgSlug, branchSlug, range, covered)

  if (!covered) {
    return (
      <section className="panel report-section" aria-labelledby="report-holds-heading">
        <h4 id="report-holds-heading">Holds</h4>
        <p className="notice" role="note">
          {HOLDS_RANGE_NOTE}
        </p>
      </section>
    )
  }

  return (
    <ReportSection name="report-holds" heading="Holds" read={read}>
      {(report) => (
        <>
          <p className="section-intro">
            Holds for library patrons. Holds for the library&rsquo;s own service accounts and for interlibrary loans are
            not counted as public holds.
          </p>
          <dl className="metrics">
            <MetricCard label="Public holds" {...count(report.public_hold_count)} />
            <MetricCard label="Interlibrary loan (ILL) holds" {...count(report.ill_hold_count)} />
          </dl>
        </>
      )}
    </ReportSection>
  )
}
