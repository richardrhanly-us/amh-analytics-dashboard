import { useOutletContext } from 'react-router'

import { LiveToday } from '../liveToday/LiveToday.tsx'
import { hasTransits } from './capabilities.ts'
import { sorterStatusLabel } from './labels.ts'
import type { SorterContext } from './SorterLayout.tsx'

/**
 * /organizations/:orgSlug/sorters/:sorterSlug -- a sorter's Live Today
 * dashboard.
 *
 * The dashboard's reads are addressed by the sorter's HOST BRANCH: that is
 * the scope its collector uploads under, and the only one the operational API
 * takes. Nothing the person sees depends on that.
 */
export function SorterPage() {
  const { organization, sorter } = useOutletContext<SorterContext>()
  const status = sorterStatusLabel(sorter.status)

  return (
    <>
      {/* Everything this sorter processed, wherever it sent it -- not the returns that belong to one branch. */}
      <p className="page-context">
        Live activity for this sorter, at {sorter.host_branch.name}
        {status !== null && ` · ${status}`}
      </p>
      {sorter.collector_count > 1 && (
        <p className="notice" role="note">
          {sorter.collector_count} collectors report for this site. Their figures are combined here and cannot be
          shown separately.
        </p>
      )}
      {/* Keyed by sorter, so another sorter starts as a new dashboard: running, and with nothing carried over. */}
      <LiveToday
        key={`${organization.slug}/${sorter.slug}`}
        orgSlug={organization.slug}
        branchSlug={sorter.host_branch.slug}
        transits={hasTransits(organization)}
      />
    </>
  )
}
