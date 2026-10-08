import { useOutletContext } from 'react-router'

import { SorterReports } from '../reports/SorterReports.tsx'
import { canSeeEfficiency } from '../reports/useEfficiency.ts'
import { hasTransits, historyDays } from './capabilities.ts'
import { sorterStatusLabel } from './labels.ts'
import type { SorterContext } from './SorterLayout.tsx'

/**
 * /organizations/:orgSlug/sorters/:sorterSlug/reports -- a sorter's reports
 * over a range of days.
 *
 * Like Live Today, the reports are read by the sorter's HOST BRANCH, the
 * scope the operational API takes. The person sees the sorter.
 */
export function SorterReportsPage() {
  const { organization, sorter } = useOutletContext<SorterContext>()
  const status = sorterStatusLabel(sorter.status)

  return (
    <>
      <h3 id="reports-heading">Reports</h3>
      {/* Processing events of this one sorter -- not distinct items, and not the returns that belong to one branch. */}
      <p className="page-context">
        What this sorter processed, at {sorter.host_branch.name}
        {status !== null && ` · ${status}`}
      </p>
      {sorter.collector_count > 1 && (
        <p className="notice" role="note">
          {sorter.collector_count} collectors report for this site. Their figures are combined here and cannot be
          shown separately.
        </p>
      )}
      {/* Keyed by sorter, so another sorter starts as a new set of reports, with nothing carried over. */}
      <SorterReports
        key={`${organization.slug}/${sorter.slug}`}
        orgSlug={organization.slug}
        branchSlug={sorter.host_branch.slug}
        efficiency={canSeeEfficiency(organization.role)}
        // The API decides (403 without it); this only keeps the app from asking for what the plan does not include.
        holds={organization.entitlements.internal_workflow?.enabled === true}
        transits={hasTransits(organization)}
        historyDays={historyDays(organization)}
      />
    </>
  )
}
