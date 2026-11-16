import type { CSSProperties, ReactNode } from 'react'

import type { BinVolumeBin } from '../api/reports.ts'
import { binLabel } from '../reports/binText.ts'
import { formatHourRange } from '../time/productTime.ts'
import { busiestHour, formatRate, reasonLabel, rejectRate, topRejectReasons } from './metrics.ts'
import { HoldsCounts } from './TodayHolds.tsx'
import type { LiveToday, Section } from './useLiveToday.ts'

/**
 * Today on one panel laid out like the sorter's own monitor: the intake end on the left -- induction, with the
 * reject chute beside it, since an item is rejected at check-in -- then the sort line, with the bins that received
 * check-ins today in sequence along both sides of it, and on the right the rail where the day's items left the
 * machine: routed home or in transit, and held. A summary of the day drawn after the machine, not a live mimic of
 * it: nothing here moves, and every figure is one the page already reads.
 *
 * The bins and the routing rail are never wired together. A bin is where an item was logged on the sorter; a
 * destination is where it was routed; the logs do not say which bin served which destination, so the drawing
 * does not either -- the line runs into the rail as a whole, never from one bin to one destination.
 *
 * Everything that carries meaning is text: headings, lists and words. The conveyor, the shading, the fill bars and
 * the routing bars are decoration, hidden from assistive technology, each beside the figure it pictures; and the
 * busiest bins are named in words, not marked by colour alone.
 */

const count = (value: number) => value.toLocaleString('en-US')
const checkIns = (value: number) => `${count(value)} ${value === 1 ? 'check-in' : 'check-ins'}`
/** A share of the day, only for a day that has check-ins: a share of nothing is not a number. */
const percent = (part: number, whole: number) => (whole > 0 ? (part / whole) * 100 : null)
const share = (part: number, whole: number) => {
  const value = percent(part, whole)
  return value === null ? null : `${value.toFixed(1)}%`
}
/** A bin longer than this many bins is set as a compact grid, not as one two-sided line: the line would be too fine. */
const LONGEST_LINE = 24
/** The bin at the left end of the line, on its own, as the sorter's screen draws it. */
const END_BIN = '0'

/** A part whose read has not answered, or could not be read: words, never a placeholder figure. */
function Pending({ section }: { section: Section<unknown> }) {
  return section.status === 'loading' ? <p className="machine-pending">Loading…</p> : <p className="machine-pending machine-failed">Could not load</p>
}

/** One labelled part of the machine. Its heading is small and set like a label on the machine; it is a real heading. */
function Part({ name, heading, children }: { name: string; heading: string; children: ReactNode }) {
  return (
    <div className={`machine-part machine-part-${name}`}>
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
      <p className="machine-figure">
        {count(checkinCount.data.checkin_count)}
        <span className="machine-unit"> checked in</span>
      </p>
      <ul className="machine-facts">
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
      <p className="machine-figure machine-figure-small">
        {count(rejects)}
        <span className="machine-unit"> rejected</span>
      </p>
      <ul className="machine-facts">
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

/** The bins ranked by today's check-ins, busiest first; a tie keeps the sorter's own order. Top three only. */
function ranks(bins: readonly BinVolumeBin[]): Map<string, number> {
  const ranked = bins
    .map((bin, index) => ({ bin, index }))
    .filter(({ bin }) => bin.checkin_count > 0)
    .sort((a, b) => b.bin.checkin_count - a.bin.checkin_count || a.index - b.index)
    .slice(0, 3)
  return new Map(ranked.map(({ bin }, place) => [bin.key, place + 1]))
}

const RANK_MARKS = ['', '1st', '2nd', '3rd']

/** The sort line: the bins that received check-ins today, in bin order along both sides of the conveyor. */
function SortLine({ live }: { live: LiveToday }) {
  const { binVolume } = live
  if (binVolume.status !== 'ready') {
    return <Pending section={binVolume} />
  }
  const { bins, unknown_bin_count: unknown } = binVolume.data
  if (bins.length === 0 && unknown === 0) {
    return <p className="machine-quiet">Nothing has reached a bin yet today.</p>
  }
  const most = Math.max(0, ...bins.map((bin) => bin.checkin_count))
  const ranked = ranks(bins)
  // In ascending bin order, as the API client guarantees. Bin 0 is the end bin: it stands alone at the left end of
  // the line, as on the sorter's own screen, and the rest follow in pairs, one on each side of the belt.
  const along = bins.filter((bin) => bin.key !== END_BIN)
  return (
    <>
      {bins.length > 0 && (
        <ol
          className={bins.length > LONGEST_LINE ? 'machine-bins machine-bins-long' : 'machine-bins'}
          aria-label="Bins that received check-ins today, in bin order"
        >
          {bins.map((bin) => {
            const rank = ranked.get(bin.key)
            const relative = most > 0 ? bin.checkin_count / most : 0
            const side = bin.key === END_BIN ? 'bin-cell-end' : along.indexOf(bin) % 2 === 0 ? 'bin-cell-upper' : 'bin-cell-lower'
            return (
              <li
                key={bin.key}
                className={['bin-cell', side, ...(rank === undefined ? [] : ['bin-cell-top', `bin-cell-rank-${rank}`])].join(' ')}
                // Shading: the bin's count against the busiest bin's. A picture of the figure in it, not of capacity.
                style={{ '--heat': relative.toFixed(3) } as CSSProperties}
              >
                <span className="bin-cell-name">{binLabel(bin)}</span>
                <span className="bin-cell-count">
                  {count(bin.checkin_count)}
                  <span className="visually-hidden"> {bin.checkin_count === 1 ? 'check-in' : 'check-ins'}</span>
                </span>
                {rank !== undefined && (
                  <span className="bin-cell-rank">
                    {RANK_MARKS[rank]}
                    <span className="visually-hidden"> busiest</span>
                  </span>
                )}
                <span className="bin-cell-fill" aria-hidden="true">
                  <span style={{ width: `${Math.round(relative * 100)}%` }} />
                </span>
              </li>
            )
          })}
        </ol>
      )}
      <p className="machine-quiet">
        {bins.length > 0 && <>Shading and fill: each bin&rsquo;s check-ins against the busiest bin&rsquo;s. </>}
        {unknown > 0 && <>No bin recorded for {checkIns(unknown)}.</>}
      </p>
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
            {total > 0 && (
              // The outcome's share of the day, as a bar under its row.
              <span className="outflow-meter" aria-hidden="true">
                <span style={{ width: `${(percent(outcome.value, total) ?? 0).toFixed(1)}%` }} />
              </span>
            )}
          </li>
        )
      })}
    </ul>
  )
}

function Holds({ live }: { live: LiveToday }) {
  const { holds } = live
  return holds.status === 'ready' ? <HoldsCounts holds={holds.data} /> : <Pending section={holds} />
}

/** `transits` and `holds`: what the organization's plan includes. A part the plan does not have is not drawn. */
export function SorterAtAGlance({ live, transits, holds }: { live: LiveToday; transits: boolean; holds: boolean }) {
  const rail = transits || holds
  return (
    <section className="panel sorter-map" aria-labelledby="sorter-map-heading">
      <div className="sorter-map-head">
        <h3 id="sorter-map-heading">Sorter at a glance</h3>
        <p className="sorter-map-caption">Today, laid out like the sorter&rsquo;s own screen. A summary of the day, not a live view.</p>
      </div>
      <div className={rail ? 'machine' : 'machine machine-no-rail'}>
        {/* The intake end: where items are checked in, and where those that cannot be are rejected. */}
        <div className="machine-intake">
          <Part name="induction" heading="Induction">
            <Induction live={live} />
          </Part>
          <Part name="rejects" heading="Reject chute">
            <RejectChute live={live} />
          </Part>
        </div>
        <div className="machine-line">
          <Part name="bins" heading="Sort line">
            {/* The conveyor is drawn by the list of bins itself, between its two rows: decoration, not read. */}
            <SortLine live={live} />
          </Part>
        </div>
        {rail && (
          // Where the day's items left the machine, counted by outcome -- never per bin.
          <div className="machine-rail">
            {transits && (
              <Part name="routed" heading="Routed">
                <Routed live={live} />
                <p className="machine-quiet">Counted by destination, apart from the bins: the logs do not link the two.</p>
              </Part>
            )}
            {holds && (
              <Part name="holds" heading="Holds">
                <Holds live={live} />
              </Part>
            )}
          </div>
        )}
      </div>
    </section>
  )
}
