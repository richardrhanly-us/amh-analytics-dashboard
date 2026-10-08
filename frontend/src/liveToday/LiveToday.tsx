import { useState, type ReactNode } from 'react'

import type { CheckinsByDestination } from '../api/liveToday.ts'
import { ErrorMessage } from '../components/ErrorMessage.tsx'
import { formatCalendarDate, formatHourRange, formatInstant } from '../time/productTime.ts'
import { HourlyCheckins } from './HourlyCheckins.tsx'
import { MetricCard, type Figure } from './MetricCard.tsx'
import { busiestHour, checkinsInHour, formatRate, rejectRate } from './metrics.ts'
import { PipelinePanel } from './PipelinePanel.tsx'
import { RejectReasons } from './RejectReasons.tsx'
import { REFRESH_INTERVAL_MS, useLiveToday, type LiveToday as LiveTodayData, type Section } from './useLiveToday.ts'

const UNAVAILABLE = 'Live dashboard data is not available for this sorter yet.'

/** A section's figure once loaded; until then, words saying why there is none -- never a placeholder number. */
function figure<T>(section: Section<T>, read: (data: T) => Figure): Figure {
  switch (section.status) {
    case 'loading':
      return { tone: 'pending', text: 'Loading…' }
    case 'error':
      return { tone: 'failed', text: 'Could not load' }
    case 'ready':
      return read(section.data)
  }
}

const count = (value: number): Figure => ({ tone: 'value', text: value.toLocaleString('en-US') })

/**
 * What the person just asked for, to be confirmed aloud. Only their own
 * actions are announced: a timed refresh changes the page without saying so.
 */
type Announcement = { kind: 'paused' } | { kind: 'resumed' } | { kind: 'refresh'; asOfBefore: number | null } | null

function announcementText(announcement: Announcement, live: LiveTodayData): string {
  switch (announcement?.kind) {
    case 'paused':
      return 'Automatic refresh paused.'
    case 'resumed':
      return 'Automatic refresh resumed.'
    case 'refresh':
      // Said once new data has arrived and all of it loaded. A refresh that failed is announced by the alert.
      return live.asOf !== announcement.asOfBefore && problem(live) === null ? 'Live data refreshed.' : ''
    default:
      return ''
  }
}

function Controls({ live }: { live: LiveTodayData }) {
  const [announcement, setAnnouncement] = useState<Announcement>(null)
  const minutes = REFRESH_INTERVAL_MS / 60_000
  const updated = live.asOf === null || live.timeZone === null ? null : formatInstant(live.asOf, live.timeZone)

  return (
    <div className="live-controls">
      {/* What kind of page this is, at a glance: figures that are still moving -- or, while paused, are not. */}
      <p className={live.paused ? 'live-badge live-badge-paused' : 'live-badge'}>
        <span className="live-badge-mark" aria-hidden="true" />
        {live.paused ? 'Paused' : 'Live'}
      </p>
      <div className="live-buttons">
        <button
          type="button"
          // Unavailable while a refresh runs, but not `disabled`: a disabled button drops keyboard focus.
          aria-disabled={live.refreshing}
          onClick={() => {
            if (!live.refreshing) {
              setAnnouncement({ kind: 'refresh', asOfBefore: live.asOf })
              live.refresh()
            }
          }}
        >
          {live.refreshing ? 'Refreshing…' : 'Refresh'}
        </button>
        <button
          type="button"
          className="button-secondary"
          onClick={() => {
            setAnnouncement({ kind: live.paused ? 'resumed' : 'paused' })
            live.setPaused(!live.paused)
          }}
        >
          {live.paused ? 'Resume automatic refresh' : 'Pause automatic refresh'}
        </button>
      </div>
      {/* Read when reached, not announced: it changes with every timed refresh. */}
      <p className="live-status">
        <span className="refresh-mode">
          <svg className="inline-icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
            {live.paused ? (
              <path d="M5.5 3.5v9M10.5 3.5v9" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            ) : (
              <path
                d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 2v3h-3"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            )}
          </svg>
          {live.paused ? 'Automatic refresh is paused.' : `Refreshes automatically every ${minutes} minutes.`}
        </span>
        {updated !== null && live.asOf !== null && (
          <span>
            {' '}
            Last updated <time dateTime={new Date(live.asOf).toISOString()}>{updated}</time>.
          </span>
        )}
      </p>
      <p className="visually-hidden" role="status">
        {announcementText(announcement, live)}
      </p>
    </div>
  )
}

/** A share of the day's check-ins, to one decimal place: "10.8% of today". Never asked of a day with none. */
function shareOfToday(part: number, total: number): string {
  return `${((part / total) * 100).toFixed(1)}% of today`
}

/** A routed count and what share of the day it is. A day with no check-ins has no shares, and says so. */
function routed(part: number, total: number): Figure {
  return { ...count(part), note: total > 0 ? shareOfToday(part, total) : 'No check-ins yet' }
}

/** One named group of figures within today's summary. A group, not a landmark: the page has enough of those. */
function SummaryGroup({ name, heading, children }: { name: string; heading: string; children: ReactNode }) {
  return (
    <div className={`summary-group summary-group-${name}`} role="group" aria-labelledby={`${name}-heading`}>
      <h4 id={`${name}-heading`}>{heading}</h4>
      {children}
    </div>
  )
}

/**
 * Where the sorter sent today's check-ins: the total in transit, then one
 * figure for each destination the site has configured, however many that is.
 * These are outcomes of this one sorter -- nothing here is a link, because a
 * destination has no dashboard of its own.
 */
function Routing({ routing }: { routing: Section<CheckinsByDestination> }) {
  if (routing.status !== 'ready') {
    return (
      <dl className="metrics">
        <MetricCard label="Total transit" {...figure(routing, () => count(0))} />
      </dl>
    )
  }

  const { checkin_count: total, home, transit, transit_count: transitCount, other_count: otherCount } = routing.data

  return (
    <>
      {transit.length === 0 ? (
        <p className="quiet">No transit destinations are configured for this sorter.</p>
      ) : (
        <dl className="metrics">
          <MetricCard label="Total transit" {...routed(transitCount, total)} />
          {transit.map((destination) => (
            <MetricCard key={destination.key} label={destination.label} {...routed(destination.checkin_count, total)} />
          ))}
        </dl>
      )}
      {/* The rest of the day's check-ins, so the figures above can be seen to add up. */}
      <p className="routing-accounting">
        Kept at {home.label}: {home.checkin_count.toLocaleString('en-US')}.
        {otherCount > 0 && ` Other routing: ${otherCount.toLocaleString('en-US')}.`}
      </p>
    </>
  )
}

function Today({ live, transits }: { live: LiveTodayData; transits: boolean }) {
  const { checkinCount, checkinsByHour, checkinsByDestination, rejectCount, currentHour } = live
  const dateText = live.date === null ? null : formatCalendarDate(live.date)

  return (
    <section aria-labelledby="today-heading">
      <div className="today-heading">
        <h3 id="today-heading">Today</h3>
        {dateText !== null && live.timeZone !== null && (
          <p className="live-date">
            {dateText} ({live.timeZone})
          </p>
        )}
      </div>
      {/* Three zones, each its own band. In reading order here; a wide screen sets Rejects beside Operations. */}
      <div className="summary-zones">
        <SummaryGroup name="operations" heading="Operations">
          <dl className="metrics">
            <MetricCard label="Check-ins today" {...figure(checkinCount, (data) => count(data.checkin_count))} />
            <MetricCard
              label="Current hour"
              {...figure(checkinsByHour, (data) =>
                currentHour === null
                  ? { tone: 'empty', text: 'Not available' }
                  : { ...count(checkinsInHour(data.hours, currentHour)), note: formatHourRange(currentHour) },
              )}
            />
            <MetricCard
              label="Busiest hour"
              {...figure(checkinsByHour, (data) => {
                const busiest = busiestHour(data.hours)
                return busiest === null
                  ? { tone: 'empty', text: 'No check-ins yet' }
                  : { ...count(busiest.checkin_count), note: formatHourRange(busiest.hour) }
              })}
            />
          </dl>
        </SummaryGroup>
        {/* Only when the organization's plan includes transit routing: otherwise it is not asked for either. */}
        {transits && (
          <SummaryGroup name="routing" heading="Routing">
            <p className="summary-caption">Where this sorter sent today&rsquo;s check-ins.</p>
            <Routing routing={checkinsByDestination} />
          </SummaryGroup>
        )}
        <SummaryGroup name="rejects" heading="Rejects">
          <dl className="metrics">
            <MetricCard label="Rejects today" {...figure(rejectCount, (data) => count(data.reject_count))} />
            <MetricCard
              label="Reject rate"
              {...figure(rejectCount, (rejects) =>
                figure(checkinCount, (checkins) => {
                  const rate = rejectRate(rejects.reject_count, checkins.checkin_count)
                  return rate === null
                    ? { tone: 'empty', text: 'Not available', note: 'No check-ins yet' }
                    : { tone: 'value', text: formatRate(rate) }
                }),
              )}
            />
          </dl>
        </SummaryGroup>
      </div>
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
  const sections = [
    live.pipeline,
    live.checkinCount,
    live.checkinsByHour,
    live.checkinsByDestination,
    live.rejectCount,
    live.rejectsByReason,
  ]
  if (sections.some((part) => part.status === 'error')) {
    return 'Some live data could not be loaded. Use Refresh to try again.'
  }
  if (sections.some((part) => part.status === 'ready' && part.stale)) {
    return 'The latest refresh failed. Some of what is shown may be out of date. Use Refresh to try again.'
  }
  return null
}

/**
 * The Live Today dashboard for one sorter the user can see. `branchSlug` is the sorter's host branch: the scope
 * the operational API reads by.
 */
export function LiveToday({ orgSlug, branchSlug, transits }: { orgSlug: string; branchSlug: string; transits: boolean }) {
  const live = useLiveToday(orgSlug, branchSlug, transits)

  if (live.unavailable) {
    // The sorter exists -- the organization lists it -- but has no live data. That is not "not found".
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
    // The outline of what is coming holds the page's shape. It is empty boxes: no label, no figure, and
    // nothing for assistive technology, which is told in words that the data is loading.
    return (
      <div className="live-today">
        <p role="status">Loading live data…</p>
        <div className="skeleton" aria-hidden="true">
          <div className="skeleton-block skeleton-panel" />
          <div className="skeleton-cards">
            {[0, 1, 2, 3, 4].map((card) => (
              <div key={card} className="skeleton-block" />
            ))}
          </div>
          <div className="skeleton-block skeleton-chart" />
        </div>
      </div>
    )
  }

  if (live.pipeline.status === 'error') {
    // Without pipeline status there is no product time zone, so no "today" to ask about. Nothing is guessed.
    return (
      <div className="load-failure">
        <ErrorMessage message={live.pipeline.message} />
        <button
          type="button"
          // Unavailable while trying again, but not `disabled`: a disabled button drops keyboard focus.
          aria-disabled={live.refreshing}
          onClick={() => {
            if (!live.refreshing) {
              live.refresh()
            }
          }}
        >
          {live.refreshing ? 'Trying again…' : 'Try again'}
        </button>
      </div>
    )
  }

  return (
    <div className="live-today">
      {/* The top of the dashboard: that this is live, when it was last updated, and the pipeline's state. */}
      <div className="live-hero">
        <Controls live={live} />
        <ErrorMessage message={problem(live)} />
        <PipelinePanel pipeline={live.pipeline.data} />
      </div>
      <Today live={live} transits={transits} />
      <div className="live-detail">
        <HourlyCheckins checkinsByHour={live.checkinsByHour} currentHour={live.currentHour} timeZone={live.timeZone} />
        <RejectReasons rejectsByReason={live.rejectsByReason} />
      </div>
    </div>
  )
}
