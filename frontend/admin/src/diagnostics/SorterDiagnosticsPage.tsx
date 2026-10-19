import { useQuery } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { useParams } from 'react-router'

import { isApiError } from '../../../src/api/client.ts'
import { formatInstant } from '../../../src/time/productTime.ts'
import { installationStatusLabel, onboardingLabel, pipelineStateLabel } from '../libraries/labels.ts'
import { getSorterDiagnostics, type LegacyReport, type SorterDiagnostics, type V2Heartbeat } from './diagnosticsApi.ts'
import { AdminBreadcrumb } from '../AdminBreadcrumb.tsx'
import { AdminPageHeading } from '../AdminPageHeading.tsx'

const LOAD_FAILED = "This sorter's diagnostics could not be loaded. Please try again."
const NOT_REPORTED = 'Not reported'

const CONTRACT_LABEL = { v2: 'Contract v2', legacy: 'Legacy', unknown: 'Unknown' } as const

function Fact({ term, children }: { term: string; children: ReactNode }) {
  return (
    <div>
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  )
}

/** A time in the product's time zone, or "Not reported". */
function When({ at, timezone }: { at: string | null; timezone: string }) {
  return at === null ? <span className="quiet">{NOT_REPORTED}</span> : <time dateTime={at}>{formatInstant(at, timezone)}</time>
}

/** A code exactly as the API sent it (an administrator's view), or "Not reported". */
function Code({ value }: { value: string | null }) {
  return value === null ? <span className="quiet">{NOT_REPORTED}</span> : <code>{value}</code>
}

function Count({ value }: { value: number | null }) {
  return value === null ? <span className="quiet">{NOT_REPORTED}</span> : <>{value.toLocaleString('en-US')}</>
}

function Duration({ ms }: { ms: number | null }) {
  return ms === null ? <span className="quiet">{NOT_REPORTED}</span> : <>{(ms / 1000).toFixed(2)} seconds</>
}

function Heartbeat({ status, timezone }: { status: V2Heartbeat; timezone: string }) {
  return (
    <dl className="account-facts">
      <Fact term="Last heartbeat"><When at={status.last_heartbeat_at} timezone={timezone} /></Fact>
      <Fact term="Health"><Code value={status.health_status} /></Fact>
      <Fact term="Last error class"><Code value={status.last_error_class} /></Fact>
      <Fact term="Schedule"><Code value={status.collector_schedule_status} /></Fact>
      <Fact term="Last collector run"><When at={status.collector_last_run_at} timezone={timezone} /></Fact>
      <Fact term="Next scheduled run"><When at={status.collector_next_run_at} timezone={timezone} /></Fact>
      <Fact term="Run duration"><Duration ms={status.collector_run_duration_ms} /></Fact>
      <Fact term="Pending events"><Count value={status.pending_outbox_count} /></Fact>
      <Fact term="Quarantined events"><Count value={status.quarantined_count} /></Fact>
      <Fact term="Oldest pending event"><When at={status.oldest_pending_event_at} timezone={timezone} /></Fact>
      <Fact term="Last success"><When at={status.last_success_at} timezone={timezone} /></Fact>
      <Fact term="Watcher last active"><When at={status.watcher_last_active_at} timezone={timezone} /></Fact>
    </dl>
  )
}

function Legacy({ status, timezone }: { status: LegacyReport; timezone: string }) {
  return (
    <dl className="account-facts">
      <Fact term="Run status"><Code value={status.run_status} /></Fact>
      <Fact term="Run reported"><When at={status.run_reported_at} timezone={timezone} /></Fact>
      <Fact term="Agent health"><Code value={status.health_status} /></Fact>
      <Fact term="Health reported"><When at={status.health_reported_at} timezone={timezone} /></Fact>
      <Fact term="Pending events"><Count value={status.pending_outbox_count} /></Fact>
      <Fact term="Quarantined events"><Count value={status.quarantined_count} /></Fact>
      <Fact term="Check-in rows"><Count value={status.checkins_rows} /></Fact>
      <Fact term="Reject rows"><Count value={status.rejects_rows} /></Fact>
      <Fact term="Uploaded check-in rows"><Count value={status.uploaded_checkins_rows} /></Fact>
      <Fact term="Uploaded reject rows"><Count value={status.uploaded_rejects_rows} /></Fact>
      <Fact term="Check-in rows with a bad time"><Count value={status.checkins_bad_datetime_rows} /></Fact>
      <Fact term="Reject rows with a bad time"><Count value={status.rejects_bad_datetime_rows} /></Fact>
    </dl>
  )
}

function Diagnostics({ data }: { data: SorterDiagnostics }) {
  const { sorter, timezone } = data
  return (
    <>
      <section aria-labelledby="sorter-section-heading">
        <h3 id="sorter-section-heading">Sorter</h3>
        <dl className="account-facts">
          <Fact term="Slug">{sorter.slug}</Fact>
          <Fact term="Host branch">{sorter.host_branch.name}</Fact>
          <Fact term="Installation">{installationStatusLabel(sorter.status)}</Fact>
          <Fact term="Collectors">{sorter.collector_count}</Fact>
          <Fact term="Onboarding">{onboardingLabel(sorter.onboarding_status)}</Fact>
          <Fact term="Pipeline">{pipelineStateLabel(data.pipeline)}</Fact>
          <Fact term="Last reported"><When at={data.pipeline.last_reported_at} timezone={timezone} /></Fact>
          <Fact term="Contract">{CONTRACT_LABEL[data.contract]}</Fact>
          <Fact term="Cutover"><When at={data.cutover_at} timezone={timezone} /></Fact>
        </dl>
      </section>

      {data.contract === 'unknown' && (
        <p className="notice" role="note">
          This library has no operational identity, so nothing can be read for this sorter.
        </p>
      )}

      {data.v2 !== null && (
        <section aria-labelledby="heartbeat-heading">
          <h3 id="heartbeat-heading">Collector heartbeat</h3>
          {data.v2.status === null ? (
            <p>This sorter has no active ingest key.</p>
          ) : (
            <Heartbeat status={data.v2.status} timezone={timezone} />
          )}
        </section>
      )}

      {data.legacy !== null && (
        <section aria-labelledby="legacy-heading">
          <h3 id="legacy-heading">Legacy report</h3>
          {data.legacy.status === null ? (
            <p>This sorter has no legacy report.</p>
          ) : (
            <Legacy status={data.legacy.status} timezone={timezone} />
          )}
        </section>
      )}
    </>
  )
}

/** /libraries/:orgSlug/sorters/:sorterSlug -- one sorter site's diagnostics, read-only. */
export function SorterDiagnosticsPage() {
  const { orgSlug = '', sorterSlug = '' } = useParams()
  const query = useQuery({
    queryKey: ['admin', 'sorter-diagnostics', orgSlug, sorterSlug],
    queryFn: ({ signal }) => getSorterDiagnostics(orgSlug, sorterSlug, signal),
  })
  const notFound = isApiError(query.error) && query.error.status === 404
  const title = query.data?.sorter.name ?? (notFound ? 'Sorter not found' : 'Sorter')

  return (
    <section aria-labelledby="sorter-heading">
      <AdminBreadcrumb
        trail={[
          { to: '/libraries', label: 'Libraries' },
          { to: `/libraries/${encodeURIComponent(orgSlug)}`, label: query.data?.library.name ?? 'Library' },
        ]}
        current={title}
      />
      <AdminPageHeading id="sorter-heading">{title}</AdminPageHeading>
      {query.isPending && <p role="status">Loading diagnostics…</p>}
      {notFound && <p>There is no sorter with this address.</p>}
      {query.isError && !notFound && (
        <>
          <p className="error-message" role="alert">
            {LOAD_FAILED}
          </p>
          <button type="button" onClick={() => void query.refetch()}>
            Try again
          </button>
        </>
      )}
      {query.data !== undefined && <Diagnostics data={query.data} />}
    </section>
  )
}
