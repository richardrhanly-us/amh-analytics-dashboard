import { useCallback, useEffect, useState, type ReactNode } from 'react'

import type { OrganizationDetail } from '../api/organizations.ts'
import { LoadFailure } from '../components/LoadFailure.tsx'
import { hasTransits, historyDays } from '../pages/capabilities.ts'
import { NotFoundPage } from '../pages/NotFoundPage.tsx'
import { DateRangeControl, RangeShown } from './DateRangeControl.tsx'
import { defaultRange, type DateRange } from './dateRange.ts'
import {
  NO_SORTERS,
  OrganizationOverviewSection,
  RoutingNetworkSection,
  SorterComparisonSection,
  SystemReliabilitySection,
} from './OrganizationReportSections.tsx'
import { useOrganizationProductDay, useOrganizationReports } from './useOrganizationReports.ts'

const BEFORE_HISTORY = "The selected range starts before this organization's available reporting window. Choose a later start date."

function ReportsForDay({
  organization,
  timeZone,
  today,
  onGone,
}: {
  organization: OrganizationDetail
  timeZone: string
  today: string
  onGone: () => void
}) {
  const orgSlug = organization.slug
  const transits = hasTransits(organization)
  const history = historyDays(organization)
  const [range, setRange] = useState<DateRange>(() => defaultRange(today, history))
  const reports = useOrganizationReports(orgSlug, range, transits)

  // A 404 for a report: the API no longer returns this organization to this user. The whole page says so.
  useEffect(() => {
    if (reports.unavailable) {
      onGone()
    }
  }, [reports.unavailable, onGone])

  if (reports.unavailable) {
    return null
  }

  return (
    <>
      <DateRangeControl range={range} today={today} historyDays={history} onChange={setRange} />
      <RangeShown range={range} today={today} timeZone={timeZone} />
      {reports.beforeHistory ? (
        // The plan's window changed after this range was chosen: the API refuses it. A later start date is the answer.
        <p className="notice" role="note">
          {BEFORE_HISTORY}
        </p>
      ) : (
        // Keyed by range, so a new range starts with nothing carried over.
        <div className="report-sections" key={`${range.from}/${range.to}`}>
          <OrganizationOverviewSection read={reports.overview} transits={transits} />
          <SorterComparisonSection orgSlug={orgSlug} read={reports.overview} transits={transits} />
          {/* Not asked for at all unless the plan includes transit routing. */}
          {transits && <RoutingNetworkSection read={reports.routingNetwork} />}
          <SystemReliabilitySection read={reports.reliability} />
        </div>
      )}
    </>
  )
}

function ReportsOfSorters({ organization, onGone }: { organization: OrganizationDetail; onGone: () => void }) {
  const { day, retry } = useOrganizationProductDay(
    organization.slug,
    organization.sorters.map((sorter) => sorter.host_branch.slug),
  )

  switch (day.status) {
    case 'loading':
      return <p role="status">Loading reports…</p>
    case 'unavailable':
      // Every sorter is registered and none has anything to read yet. That is not "nothing happened".
      return (
        <p className="notice" role="note">
          Reports are not available for this organization yet: none of its sorting machines has reporting data.
        </p>
      )
    case 'error':
      return <LoadFailure message={day.message} onRetry={retry} />
    case 'ready':
      return <ReportsForDay organization={organization} timeZone={day.timeZone} today={day.today} onGone={onGone} />
  }
}

/**
 * The reports of one organization the user can see: Overview, Sorter
 * comparison, Routing network and System reliability, over a range of days
 * the person chooses. `header` is the page's own top -- its breadcrumb and
 * headings -- which stays where it is, above whatever state the reports are
 * in, so the heading that took focus on arrival keeps it. The one exception
 * is an organization the API stops returning: that is the not-found page,
 * the same as for any other the user cannot see.
 *
 * As for a sorter's reports, the product's zone and its date today are read
 * first: nothing can be asked for until both are known.
 */
export function OrganizationReports({ organization, header }: { organization: OrganizationDetail; header: ReactNode }) {
  const [gone, setGone] = useState(false)
  const onGone = useCallback(() => setGone(true), [])

  if (gone) {
    return <NotFoundPage />
  }
  return (
    <>
      {header}
      {organization.sorters.length === 0 ? <p>{NO_SORTERS}</p> : <ReportsOfSorters organization={organization} onGone={onGone} />}
    </>
  )
}
