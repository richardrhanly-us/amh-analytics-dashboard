import { MetricCard } from '../liveToday/MetricCard.tsx'
import type { DateRange } from './dateRange.ts'
import { count } from './figures.ts'
import { ReportSection } from './ReportSections.tsx'
import { useHoldsReport } from './useSorterReports.ts'

/**
 * A sorter's Holds report: how many public holds and how many interlibrary
 * loan holds it handled over the range. Two counts, nothing else -- no
 * holds by destination, by patron or by item.
 *
 * Only for a plan that has it: the section is not there at all otherwise,
 * so nothing is asked for. It reads its report itself, and whatever becomes
 * of it changes nothing in the other sections.
 */
export function HoldsSection({ orgSlug, branchSlug, range }: { orgSlug: string; branchSlug: string; range: DateRange }) {
  const read = useHoldsReport(orgSlug, branchSlug, range)

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
