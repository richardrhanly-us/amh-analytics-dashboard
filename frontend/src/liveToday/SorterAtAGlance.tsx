import type { ReactNode } from 'react'

import type { BinVolumeBin } from '../api/reports.ts'
import { binLabel } from '../reports/binText.ts'
import { formatHourRange } from '../time/productTime.ts'
import { busiestHour, formatRate, reasonLabel, rejectRate, topRejectReasons } from './metrics.ts'
import { HoldsCounts } from './TodayHolds.tsx'
import type { LiveToday, Section } from './useLiveToday.ts'

/**
 * Today on one drawing of the sorter: items come in at the induction point, travel the conveyor to the bin bank,
 * and leave it routed home or in transit -- or drop down the reject chute. A summary of the day, laid out the way
 * the machine is, not a live view of it: nothing here moves, and every figure is the same one the page gives
 * elsewhere, from the same reads.
 *
 * Bins and routing are drawn side by side and never wired together. A bin is where an item was logged on the
 * sorter; a destination is where it was routed. The logs do not say which bin served which destination, so the
 * drawing does not either.
 *
 * Everything that carries meaning is text -- headings, lists and words -- so it is read as it is seen. The
 * conveyor, the gauges and the bar are decoration, hidden from assistive technology, and each sits beside the
 * figure it pictures.
 */

const count = (value: number) => value.toLocaleString('en-US')
const checkIns = (value: number) => `${count(value)} ${value === 1 ? 'check-in' : 'check-ins'}`
/** A share of the day, only for a day that has check-ins: a share of nothing is not a number. */
const share = (part: number, whole: number) => (whole > 0 ? `${((part / whole) * 100).toFixed(1)}%` : null)

/** A part of the drawing whose read has not answered, or could not be read: words, never a placeholder figure. */
function Pending({ section }: { section: Section<unknown> }) {
  return section.status === 'loading' ? (
    <p className="sorter-pending">Loading…</p>
  ) : (
    <p className="sorter-pending sorter-failed">Could not load</p>
  )
}

/** One station on the drawing: a numbered stage, a heading, and what happened there today. */
function Station({ step, name, heading, children }: { step: string; name: string; heading: string; children: ReactNode }) {
  return (
    <div className={`sorter-station sorter-station-${name}`}>
      <p className="sorter-step" aria-hidden="true">
        {step}
      </p>
      <h4>{heading}</h4>
      {children}
    </div>
  )
}

function Induction({ live }: { live: LiveToday }) {
  const { checkinCount, checkinsByHour, currentHour } = live
  if (checkinCount.status !== 'ready') {
    return <Pending section={checkinCount} />
  }
  const peak = checkinsByHour.status === 'ready' ? busiestHour(checkinsByHour.data.hours) : null
  const now =
    checkinsByHour.status === 'ready' && currentHour !== null
      ? (checkinsByHour.data.hours.find((entry) => entry.hour === currentHour)?.checkin_count ?? 0)
      : null
  return (
    <>
      <p className="sorter-figure">
        {count(checkinCount.data.checkin_count)}
        <span className="sorter-figure-unit"> checked in</span>
      </p>
      <ul className="sorter-facts">
        {now !== null && currentHour !== null && (
          <li>
            This hour ({formatHourRange(currentHour)}): {count(now)}
          </li>
        )}
        {peak !== null && (
          <li>
            Peak hour so far: {formatHourRange(peak.hour)}, {count(peak.checkin_count)}
          </li>
        )}
      </ul>
    </>
  )
}

/** The bins ranked by today's check-ins, busiest first; a tie keeps the sorter's own order. Top three only. */
function ranks(bins: readonly BinVolumeBin[]): Map<string, number> {
  const ranked = bins
    .map((bin, index) => ({ bin, index }))
    .filter(({ bin }) => bin.checkin_count > 0)
    .sort((a, b) => b.bin.checkin_count - a.bin.checkin_count || a.index - b.index)
    .slice(0, 3)
  return new Map(ranked.map(({ bin }, place) => [bin.key, place + 1]))
}

const RANK_WORDS = ['', 'Busiest', '2nd busiest', '3rd busiest']

function BinBank({ live }: { live: LiveToday }) {
  const { binVolume } = live
  if (binVolume.status !== 'ready') {
    return <Pending section={binVolume} />
  }
  const { bins, unknown_bin_count: unknown } = binVolume.data
  if (bins.length === 0 && unknown === 0) {
    return <p className="sorter-quiet">Nothing has reached a bin yet today.</p>
  }
  const most = Math.max(0, ...bins.map((bin) => bin.checkin_count))
  const ranked = ranks(bins)
  return (
    <>
      {bins.length > 0 && (
        // In the sorter's own order, as the bins stand: the busiest are marked in words, not found by position.
        <ol className="bin-bank" aria-label="Bins that received check-ins today, in bin order">
          {bins.map((bin) => {
            const rank = ranked.get(bin.key)
            return (
              <li key={bin.key} className={rank === undefined ? 'bin-tile' : `bin-tile bin-tile-rank-${rank}`}>
                <span className="bin-tile-name">{binLabel(bin)}</span>
                <span className="bin-tile-count">
                  {count(bin.checkin_count)}
                  <span className="visually-hidden"> {bin.checkin_count === 1 ? 'check-in' : 'check-ins'}</span>
                </span>
                {rank !== undefined && <span className="bin-tile-rank">{RANK_WORDS[rank]}</span>}
                {/* The bin's count as a share of the busiest bin's: a picture of the figure above it, not of capacity. */}
                <span className="bin-tile-gauge" aria-hidden="true">
                  <span style={{ height: `${most > 0 ? (bin.checkin_count / most) * 100 : 0}%` }} />
                </span>
              </li>
            )
          })}
        </ol>
      )}
      {unknown > 0 && <p className="sorter-quiet">No bin recorded for {checkIns(unknown)}.</p>}
    </>
  )
}

function Routed({ live }: { live: LiveToday }) {
  const routing = live.checkinsByDestination
  if (routing.status !== 'ready') {
    return <Pending section={routing} />
  }
  const { checkin_count: total, home, transit, other_count: other } = routing.data
  const outcomes = [
    { key: 'home', name: `Kept at ${home.label}`, value: home.checkin_count, kind: 'home' },
    ...transit.map((destination) => ({ key: destination.key, name: destination.label, value: destination.checkin_count, kind: 'transit' })),
    ...(other > 0 ? [{ key: 'other', name: 'Other routing', value: other, kind: 'other' }] : []),
  ]
  return (
    <>
      {total > 0 && (
        // The day split by outcome, each part as wide as its share. The figures are in the list under it.
        <div className="outflow-bar" aria-hidden="true">
          {outcomes
            .filter((outcome) => outcome.value > 0)
            .map((outcome) => (
              <span key={outcome.key} className={`outflow-${outcome.kind}`} style={{ flexGrow: outcome.value }} />
            ))}
        </div>
      )}
      <ul className="outflow-list">
        {outcomes.map((outcome) => {
          const part = share(outcome.value, total)
          return (
            <li key={outcome.key} className={`outflow-item outflow-item-${outcome.kind}`}>
              <span className="outflow-name">{outcome.name}</span>
              <span className="outflow-count">
                {count(outcome.value)}
                {part !== null && <span className="outflow-share"> · {part}</span>}
              </span>
            </li>
          )
        })}
      </ul>
    </>
  )
}

function RejectChute({ live }: { live: LiveToday }) {
  const { rejectCount, checkinCount, rejectsByReason } = live
  if (rejectCount.status !== 'ready') {
    return <Pending section={rejectCount} />
  }
  const rejects = rejectCount.data.reject_count
  const rate = checkinCount.status === 'ready' ? rejectRate(rejects, checkinCount.data.checkin_count) : null
  const [top] = rejectsByReason.status === 'ready' ? topRejectReasons(rejectsByReason.data.reasons) : []
  return (
    <>
      <p className="sorter-figure sorter-figure-small">
        {count(rejects)}
        <span className="sorter-figure-unit"> rejected</span>
      </p>
      <ul className="sorter-facts">
        {rate !== null && <li>{formatRate(rate)} of check-ins</li>}
        {top !== undefined && (
          <li>
            Most often: {reasonLabel(top.reason)} ({count(top.reject_count)})
          </li>
        )}
      </ul>
    </>
  )
}

function Holds({ live }: { live: LiveToday }) {
  const { holds } = live
  return holds.status === 'ready' ? <HoldsCounts holds={holds.data} /> : <Pending section={holds} />
}

/** `transits` and `holds`: what the organization's plan includes. A part the plan does not have is not drawn. */
export function SorterAtAGlance({ live, transits, holds }: { live: LiveToday; transits: boolean; holds: boolean }) {
  return (
    <section className="panel sorter-map" aria-labelledby="sorter-map-heading">
      <div className="sorter-map-head">
        <h3 id="sorter-map-heading">Sorter at a glance</h3>
        <p className="sorter-map-caption">Today&rsquo;s check-ins, laid out like the sorter. A summary of the day, not a live view.</p>
      </div>
      <div className="sorter-flow">
        <Station step="01" name="induction" heading="Induction">
          <Induction live={live} />
        </Station>
        {/* The conveyor between the induction point and the bins: drawn, not read. */}
        <div className="sorter-conveyor" aria-hidden="true" />
        <Station step="02" name="bins" heading="Bins">
          <BinBank live={live} />
        </Station>
        <div className="sorter-outlets">
          {transits && (
            <Station step="03" name="routed" heading="Routed">
              <Routed live={live} />
              <p className="sorter-note">Counted by destination, apart from the bins: the logs do not link the two.</p>
            </Station>
          )}
          <Station step={transits ? '04' : '03'} name="rejects" heading="Reject chute">
            <RejectChute live={live} />
          </Station>
          {holds && (
            <Station step={transits ? '05' : '04'} name="holds" heading="Holds">
              <Holds live={live} />
            </Station>
          )}
        </div>
      </div>
    </section>
  )
}
