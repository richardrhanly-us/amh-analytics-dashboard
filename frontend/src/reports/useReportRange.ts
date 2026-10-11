import { useEffect } from 'react'
import { useSearchParams } from 'react-router'

import { defaultRange, earliestDate, previousRange, rangeProblem, type DateRange } from './dateRange.ts'

/** The one thing a report can be compared with, as the address says it: `?compare=previous`. */
const COMPARE_PARAM = 'compare'
const COMPARE_PREVIOUS = 'previous'

/**
 * Whether the range is being compared with the period before it.
 *
 *   previous    the period it would be compared with (previousRange)
 *   available   that whole period can be asked for: none of it is before the organization's plan lets a report
 *               start. It is all or nothing -- a previous period is never shortened to fit.
 *   active      the address asks for the comparison AND it is available. Nothing is ever read for a comparison
 *               that is not active.
 */
export interface RangeComparison {
  previous: DateRange
  available: boolean
  active: boolean
}

/**
 * The range a report page is showing, kept in the address: `?from=YYYY-MM-DD&to=YYYY-MM-DD`,
 * the two dates the API itself is asked for. The address is the only place it
 * is kept, so it survives a reload, moving between a page's report views, and
 * Back and Forward.
 *
 * NO DATES IN THE ADDRESS is the range a report page opens on (defaultRange),
 * which moves with the product's today. Choosing that range again takes the
 * dates back out.
 *
 * DATES THAT CANNOT BE ASKED FOR -- one missing, not a date, reversed, after
 * today, too long, or before the organization's plan lets a report start --
 * are held to the same rule as dates typed into the range control
 * (rangeProblem). They never become the range: the page shows the range it
 * opens on, and the dates are taken out of the address, in place, so that it
 * says what is shown. The API holds every range to the same rules whatever
 * this does.
 *
 * COMPARING WITH THE PREVIOUS PERIOD is kept in the address the same way, as
 * `compare=previous`, beside the dates and by the same rules: nothing in the
 * address is no comparison; any other value, or a comparison whose previous
 * period cannot be asked for in full, never takes effect and is taken out of
 * the address in place.
 *
 * `today` is the product's date and `historyDays` pages/capabilities'
 * historyDays (null for no limit).
 */
export function useReportRange(
  today: string,
  historyDays: number | null,
): { range: DateRange; setRange: (range: DateRange) => void; comparison: RangeComparison; setCompare: (compare: boolean) => void } {
  const [params, setParams] = useSearchParams()
  const earliest = earliestDate(today, historyDays)
  const from = params.get('from')
  const to = params.get('to')
  const asked = from !== null || to !== null
  const requested: DateRange = { from: from ?? '', to: to ?? '' }
  const usable = asked && rangeProblem(requested, today, earliest) === null
  const opensOn = defaultRange(today, historyDays)
  const range = usable ? requested : opensOn

  const compare = params.get(COMPARE_PARAM)
  const previous = previousRange(range)
  const available = rangeProblem(previous, today, earliest) === null
  const active = compare === COMPARE_PREVIOUS && available

  useEffect(() => {
    const dates = asked && !usable
    const comparing = compare !== null && !active
    if (dates || comparing) {
      setParams(
        (current) => {
          const next = new URLSearchParams(current)
          if (dates) {
            next.delete('from')
            next.delete('to')
          }
          if (comparing) {
            next.delete(COMPARE_PARAM)
          }
          return next
        },
        { replace: true },
      )
    }
  }, [asked, usable, compare, active, setParams])

  function setRange(next: DateRange) {
    setParams((current) => {
      const changed = new URLSearchParams(current)
      if (next.from === opensOn.from && next.to === opensOn.to) {
        changed.delete('from')
        changed.delete('to')
      } else {
        changed.set('from', next.from)
        changed.set('to', next.to)
      }
      return changed
    })
  }

  function setCompare(next: boolean) {
    setParams((current) => {
      const changed = new URLSearchParams(current)
      if (next && available) {
        changed.set(COMPARE_PARAM, COMPARE_PREVIOUS)
      } else {
        changed.delete(COMPARE_PARAM)
      }
      return changed
    })
  }

  return { range, setRange, comparison: { previous, available, active }, setCompare }
}
