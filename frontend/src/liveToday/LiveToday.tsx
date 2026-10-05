import type { ReactNode } from 'react'

import type { PipelineState } from '../api/liveToday.ts'
import { ErrorMessage } from '../components/ErrorMessage.tsx'
import { formatCalendarDate, formatHour, formatInstant } from '../time/productTime.ts'
import { busiestHour, checkinsInHour, formatRate, reasonLabel, rejectRate, topRejectReasons } from './metrics.ts'
import { REFRESH_INTERVAL_MS, useLiveToday, type LiveToday as LiveTodayData, type Section } from './useLiveToday.ts'

const UNAVAILABLE = 'Live dashboard data is not available for this branch yet.'
const NOT_LOADED = 'Could not load'

const STATE_LABELS: Record<PipelineState, string> = {
  ok: 'OK',
  degraded: 'Degraded',
  failed: 'Failed',
  unknown: 'Unknown',
}

/** One figure in a summary. `children` is the value, or the reason there is none. */
function Metric({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="metric">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  )
}

/** A section's value once loaded; until then, words saying why there is none -- never a placeholder number. */
function value<T>(section: Section<T>, render: (data: T) => ReactNode): ReactNode {
  switch (section.status) {
    case 'loading':
      return <span className="metric-empty">Loading…</span>
    case 'error':
      return <span className="metric-empty">{NOT_LOADED}</span>
    case 'ready':
      return render(section.data)
  }
}

function Controls({ live }: { live: LiveTodayData }) {
  const minutes = REFRESH_INTERVAL_MS / 60_000
  const updated = live.asOf === null || live.timeZone === null ? null : formatInstant(live.asOf, live.timeZone)

  return (
    <div className="live-controls">
      <button type="button" onClick={live.refresh} disabled={live.refreshing}>
        {live.refreshing ? 'Refreshing…' : 'Refresh'}
      </button>
      <button type="button" onClick={() => live.setPaused(!live.paused)}>
        {live.paused ? 'Resume automatic refresh' : 'Pause automatic refresh'}
      </button>
      <p className="live-status" role="status">
        {live.paused ? 'Automatic refresh is paused.' : `Refreshes automatically every ${minutes} minutes.`}
        {updated !== null && ` Last updated ${updated}.`}
      </p>
    </div>
  )
}

function Pipeline({ live }: { live: LiveTodayData }) {
  return (
    <section aria-labelledby="pipeline-heading">
      <h3 id="pipeline-heading">Pipeline</h3>
      <dl className="metrics">
        <Metric label="Status">{value(live.pipeline, (pipeline) => STATE_LABELS[pipeline.state])}</Metric>
        <Metric label="Last reported">
          {value(live.pipeline, (pipeline) => {
            const reported = formatInstant(pipeline.last_reported_at, pipeline.timezone)
            return reported === null || pipeline.last_reported_at === null ? (
              <span className="metric-empty">Nothing reported yet</span>
            ) : (
              <time dateTime={pipeline.last_reported_at}>{reported}</time>
            )
          })}
        </Metric>
      </dl>
    </section>
  )
}

function Today({ live }: { live: LiveTodayData }) {
  const { checkinCount, checkinsByHour, rejectCount, currentHour } = live
  const dateText = live.date === null ? null : formatCalendarDate(live.date)

  return (
    <section aria-labelledby="today-heading">
      <h3 id="today-heading">Today</h3>
      {dateText !== null && live.timeZone !== null && (
        <p className="live-date">
          {dateText} ({live.timeZone})
        </p>
      )}
      <dl className="metrics">
        <Metric label="Check-ins">{value(checkinCount, (data) => data.checkin_count.toLocaleString('en-US'))}</Metric>
        <Metric label={currentHour === null ? 'Current hour' : `Current hour (${formatHour(currentHour)})`}>
          {value(checkinsByHour, (data) =>
            currentHour === null ? null : checkinsInHour(data.hours, currentHour).toLocaleString('en-US'),
          )}
        </Metric>
        <Metric label="Busiest hour">
          {value(checkinsByHour, (data) => {
            const busiest = busiestHour(data.hours)
            return busiest === null ? (
              <span className="metric-empty">No check-ins yet</span>
            ) : (
              `${formatHour(busiest.hour)} (${busiest.checkin_count.toLocaleString('en-US')})`
            )
          })}
        </Metric>
        <Metric label="Rejects">{value(rejectCount, (data) => data.reject_count.toLocaleString('en-US'))}</Metric>
        <Metric label="Reject rate">
          {value(rejectCount, (rejects) =>
            value(checkinCount, (checkins) => {
              const rate = rejectRate(rejects.reject_count, checkins.checkin_count)
              return rate === null ? <span className="metric-empty">Not available (no check-ins)</span> : formatRate(rate)
            }),
          )}
        </Metric>
      </dl>
    </section>
  )
}

function HourlyCheckins({ live }: { live: LiveTodayData }) {
  const { checkinsByHour, currentHour } = live

  return (
    <section aria-labelledby="hourly-heading">
      <h3 id="hourly-heading">Hourly check-ins</h3>
      {checkinsByHour.status === 'loading' && <p>Loading…</p>}
      {checkinsByHour.status === 'error' && <p>{NOT_LOADED}.</p>}
      {checkinsByHour.status === 'ready' && (
        <table className="data-table" aria-labelledby="hourly-heading">
          <thead>
            <tr>
              <th scope="col">Hour</th>
              <th scope="col">Check-ins</th>
            </tr>
          </thead>
          <tbody>
            {checkinsByHour.data.hours.map((entry) => (
              <tr key={entry.hour} aria-current={entry.hour === currentHour ? 'true' : undefined}>
                <th scope="row">
                  {formatHour(entry.hour)}
                  {entry.hour === currentHour && ' (current hour)'}
                </th>
                <td>{entry.checkin_count.toLocaleString('en-US')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

function RejectReasons({ live }: { live: LiveTodayData }) {
  const { rejectsByReason } = live
  const reasons = rejectsByReason.status === 'ready' ? topRejectReasons(rejectsByReason.data.reasons) : []

  return (
    <section aria-labelledby="reasons-heading">
      <h3 id="reasons-heading">Top reject reasons</h3>
      {rejectsByReason.status === 'loading' && <p>Loading…</p>}
      {rejectsByReason.status === 'error' && <p>{NOT_LOADED}.</p>}
      {rejectsByReason.status === 'ready' &&
        (reasons.length === 0 ? (
          <p>No rejects today.</p>
        ) : (
          <table className="data-table" aria-labelledby="reasons-heading">
            <thead>
              <tr>
                <th scope="col">Reason</th>
                <th scope="col">Rejects</th>
              </tr>
            </thead>
            <tbody>
              {reasons.map((entry) => (
                <tr key={entry.reason}>
                  <th scope="row">{reasonLabel(entry.reason)}</th>
                  <td>{entry.reject_count.toLocaleString('en-US')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
    </section>
  )
}

/**
 * What needs saying above the figures, if anything: something could not be
 * loaded, or what is shown is older than it looks because a refresh failed.
 * One sentence, whichever parts are affected -- each part says for itself
 * that it could not load.
 */
function problem(live: LiveTodayData): string | null {
  const sections = [live.pipeline, live.checkinCount, live.checkinsByHour, live.rejectCount, live.rejectsByReason]
  if (sections.some((part) => part.status === 'error')) {
    return 'Some live data could not be loaded. Use Refresh to try again.'
  }
  if (sections.some((part) => part.status === 'ready' && part.stale)) {
    return 'The latest refresh failed. Some of what is shown may be out of date. Use Refresh to try again.'
  }
  return null
}

/** The Live Today dashboard for one branch the user can see. */
export function LiveToday({ orgSlug, branchSlug }: { orgSlug: string; branchSlug: string }) {
  const live = useLiveToday(orgSlug, branchSlug)

  if (live.unavailable) {
    // The branch exists -- the organization lists it -- but has no live data. That is not "not found".
    return (
      <section aria-labelledby="live-heading">
        <h3 id="live-heading">Live Today</h3>
        <p className="notice" role="note">
          {UNAVAILABLE}
        </p>
      </section>
    )
  }

  if (live.pipeline.status === 'loading') {
    return <p role="status">Loading live data…</p>
  }

  if (live.pipeline.status === 'error') {
    // Without pipeline status there is no product time zone, so no "today" to ask about. Nothing is guessed.
    return (
      <div className="load-failure">
        <ErrorMessage message={live.pipeline.message} />
        <button type="button" onClick={live.refresh} disabled={live.refreshing}>
          {live.refreshing ? 'Trying again…' : 'Try again'}
        </button>
      </div>
    )
  }

  return (
    <div className="live-today">
      <Controls live={live} />
      <ErrorMessage message={problem(live)} />
      <Pipeline live={live} />
      <Today live={live} />
      <HourlyCheckins live={live} />
      <RejectReasons live={live} />
    </div>
  )
}
