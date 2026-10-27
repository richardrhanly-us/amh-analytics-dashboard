import { useOutletContext } from 'react-router'

import { ReportNav } from '../reports/ReportNav.tsx'
import { SorterReports } from '../reports/SorterReports.tsx'
import { canSeeEfficiency, EFFICIENCY_FOR_ADMINS } from '../reports/useEfficiency.ts'
import { sorterReportViewPath, type SorterReportView } from '../router/paths.ts'
import { hasExports, hasHolds, hasTransits, historyDays } from './capabilities.ts'
import { FirstDataNotice } from './FirstDataNotice.tsx'
import { isWaitingForFirstData, sorterStatusLabel } from './labels.ts'
import type { SorterContext } from './SorterLayout.tsx'

/**
 * /organizations/:orgSlug/sorters/:sorterSlug/reports/operations and
 * .../reports/efficiency -- a sorter's reports over a range of days, a view
 * at a time. `view` is the one in the address.
 *
 * Like Live Today, the reports are read by the sorter's HOST BRANCH, the
 * scope the operational API takes. The person sees the sorter.
 *
 * Efficiency is for the organization's owners and admins. Nobody else is
 * offered it, and someone else who opens its address is told so and nothing
 * is asked of the API on their behalf.
 */
export function SorterReportsPage({ view }: { view: SorterReportView }) {
  const { organization, sorter } = useOutletContext<SorterContext>()
  const status = sorterStatusLabel(sorter.status)
  const efficiency = canSeeEfficiency(organization.role)

  return (
    <>
      {/* `report-view`: what marks this page as a report for the print styles (index.css), and nothing else. */}
      <h3 id="reports-heading" className="report-view">
        Reports
      </h3>
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
      <ReportNav
        views={[
          { to: sorterReportViewPath(organization.slug, sorter.slug, 'operations'), label: 'Operations' },
          ...(efficiency ? [{ to: sorterReportViewPath(organization.slug, sorter.slug, 'efficiency'), label: 'Efficiency' }] : []),
        ]}
      />
      {view === 'efficiency' && !efficiency ? (
        <p className="notice" role="note">
          {EFFICIENCY_FOR_ADMINS}
        </p>
      ) : isWaitingForFirstData(sorter) ? (
        // No range has anything in it yet, so none is asked for: an empty report would read as days with no activity.
        <FirstDataNotice />
      ) : (
        // Keyed by sorter, so another sorter starts as a new set of reports, with nothing carried over.
        <SorterReports
          key={`${organization.slug}/${sorter.slug}`}
          orgSlug={organization.slug}
          branchSlug={sorter.host_branch.slug}
          view={view}
          // The API decides (403 without it); this only keeps the app from asking for what the plan does not include.
          holds={hasHolds(organization)}
          transits={hasTransits(organization)}
          historyDays={historyDays(organization)}
          // What the app offers, not a lock: the figures a file or a printout holds are the ones already shown.
          exports={hasExports(organization)}
        />
      )}
    </>
  )
}
