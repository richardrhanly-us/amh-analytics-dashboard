import type { CSSProperties } from 'react'

import type { BinLayout } from '../api/organizations.ts'
import type { BinVolumeBin } from '../api/reports.ts'
import { binLabel } from '../reports/binText.ts'
import type { LiveToday, Section } from './useLiveToday.ts'

/**
 * Today's sort line, drawn on its own across the panel like the sorter's own monitor: the end bin standing alone at
 * the left end and the other bins along both sides of the belt -- every bin in its place for a sorter with a
 * configured layout, else those that received check-ins today in sequence. A summary of the day drawn after the
 * machine, not a live mimic of it: nothing here moves. The day's induction, routing and holds figures are in Today's
 * figures above and Holds today below.
 *
 * Everything that carries meaning is text: each bin says its own count, and the panel keeps its name as a heading for
 * assistive technology. The shading and fill that picture a bin against the busiest bin are decoration, as is the
 * conveyor, hidden from assistive technology and never the only sign of anything.
 */

const count = (value: number) => value.toLocaleString('en-US')
const checkIns = (value: number) => `${count(value)} ${value === 1 ? 'check-in' : 'check-ins'}`
/** A machine with more bins than this is set as a compact grid, not as one two-sided line: the line would be too fine. */
const LONGEST_LINE = 16
/** Without a configured layout, the bin at the left end of the line, on its own, as the sorter's screen draws it. */
const END_BIN = '0'

/**
 * The bins to draw. With the sorter's configured layout: every bin of the machine in its place, a bin with no
 * check-ins today drawn with 0 -- a bin, not a check-in record, so no figure changes -- and any bin the layout does
 * not name after them, never dropped. Without one: the bins that received check-ins.
 */
function binsOnLine(bins: BinVolumeBin[], layout: BinLayout | null): BinVolumeBin[] {
  if (layout === null) {
    return bins
  }
  const keys = [layout.end, ...layout.columns.flat()]
  const placed = keys.map((key) => bins.find((bin) => bin.key === key) ?? { key, checkin_count: 0, hours: Array<number>(24).fill(0) })
  return [...placed, ...bins.filter((bin) => !keys.includes(bin.key))]
}

/** A part whose read has not answered, or could not be read: words, never a placeholder figure. */
function Pending({ section }: { section: Section<unknown> }) {
  return section.status === 'loading' ? <p className="machine-pending">Loading…</p> : <p className="machine-pending machine-failed">Could not load</p>
}

/**
 * The sort line along both sides of the conveyor: a sorter with a known layout, every bin in its place; any other,
 * the bins that received check-ins today, in bin order.
 */
function SortLine({ live, layout }: { live: LiveToday; layout: BinLayout | null }) {
  const { binVolume } = live
  if (binVolume.status !== 'ready') {
    return <Pending section={binVolume} />
  }
  const { unknown_bin_count: unknown } = binVolume.data
  if (layout === null && binVolume.data.bins.length === 0 && unknown === 0) {
    return <p className="machine-quiet">Nothing has reached a bin yet today.</p>
  }
  // Shading is against the busiest bin's check-ins; a bin drawn with 0 changes nothing.
  const most = Math.max(0, ...binVolume.data.bins.map((bin) => bin.checkin_count))
  // In list order, the grid fills each column above the belt, then below. Without a layout that is ascending bin
  // order, as the API client guarantees. The end bin (the layout's, else Bin 0) stands alone at the left end of the
  // line, as on the sorter's own screen, and the rest follow in pairs, one on each side of the belt.
  const bins = binsOnLine(binVolume.data.bins, layout)
  const end = layout?.end ?? END_BIN
  const along = bins.filter((bin) => bin.key !== end)
  const classes = ['machine-bins']
  if (bins.length > LONGEST_LINE) {
    classes.push('machine-bins-long')
  }
  if (along.length < bins.length) {
    classes.push('machine-bins-with-end')
  }
  // How many columns of bins stand along the belt: the line's width is capped by it, so a short machine is not
  // stretched across a wide panel.
  const columns = Math.max(1, Math.ceil(along.length / 2))
  return (
    // The machine, centred across the panel: the bins, the belt they stand on (drawn by the list), and a line under it.
    <div className="machine-stage">
      {bins.length > 0 && (
        <ol
          className={classes.join(' ')}
          style={{ '--columns': columns } as CSSProperties}
          aria-label={layout === null ? 'Bins that received check-ins today, in bin order' : 'Bins on this sorter, in their places along the machine'}
        >
          {bins.map((bin) => {
            const relative = most > 0 ? bin.checkin_count / most : 0
            const side = bin.key === end ? 'bin-cell-end' : along.indexOf(bin) % 2 === 0 ? 'bin-cell-upper' : 'bin-cell-lower'
            return (
              <li
                key={bin.key}
                className={`bin-cell ${side}`}
                // Shading and fill: the bin's count against the busiest bin's -- a picture of the figure in it, not of
                // capacity. --fill is a width for a bin on the line and a height for the upright end bin.
                style={{ '--heat': relative.toFixed(3), '--fill': `${Math.round(relative * 100)}%` } as CSSProperties}
              >
                <span className="bin-cell-name">{binLabel(bin)}</span>
                <span className="bin-cell-count">
                  {count(bin.checkin_count)}
                  <span className="visually-hidden"> {bin.checkin_count === 1 ? 'check-in' : 'check-ins'}</span>
                </span>
                <span className="bin-cell-fill" aria-hidden="true">
                  <span />
                </span>
              </li>
            )
          })}
        </ol>
      )}
      {/* What the bins cannot show: a day with nothing binned yet, and check-ins logged without a bin. */}
      <p className="machine-quiet">
        {most === 0 && bins.length > 0 && unknown === 0 && <>Nothing has reached a bin yet today.</>}
        {unknown > 0 && <>No bin recorded for {checkIns(unknown)}.</>}
      </p>
    </div>
  )
}

/**
 * The sort line alone, across the panel. Its name stays a real heading for assistive technology, visually hidden:
 * the drawing is the panel. `binLayout`: the sorter's own configured bins, or null.
 */
export function SorterAtAGlance({ live, binLayout }: { live: LiveToday; binLayout: BinLayout | null }) {
  return (
    <section className="panel sorter-map" aria-labelledby="sorter-map-heading">
      <h3 id="sorter-map-heading" className="visually-hidden">
        Sorter at a glance
      </h3>
      <SortLine live={live} layout={binLayout} />
    </section>
  )
}
