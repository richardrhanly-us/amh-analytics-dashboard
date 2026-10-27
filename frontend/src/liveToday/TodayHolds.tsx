import type { HoldsReport } from '../api/reports.ts'
import { count } from '../reports/figures.ts'
import { HOLDS_EXPLANATION } from '../reports/HoldsSection.tsx'
import { MetricCard } from './MetricCard.tsx'
import { SectionPlaceholder } from './SectionPlaceholder.tsx'
import type { Section } from './useLiveToday.ts'

/**
 * Today's holds: the Operations report's Holds for today alone -- the same two
 * counts, by the same rules, as that report gives for this date. Only shown
 * (and only asked for) when the organization's plan includes holds.
 */
export function TodayHolds({ holds }: { holds: Section<HoldsReport> }) {
  return (
    <section className="panel" aria-labelledby="today-holds-heading">
      <h3 id="today-holds-heading">Holds today</h3>
      {holds.status !== 'ready' ? (
        <SectionPlaceholder status={holds.status} />
      ) : (
        <>
          <p className="section-intro">{HOLDS_EXPLANATION}</p>
          <dl className="metrics">
            <MetricCard label="Public holds today" {...count(holds.data.public_hold_count)} />
            <MetricCard label="Interlibrary loan (ILL) holds today" {...count(holds.data.ill_hold_count)} />
          </dl>
        </>
      )}
    </section>
  )
}
