import { useEffect } from 'react'
import { useSearchParams } from 'react-router'

import { defaultRange, earliestDate, rangeProblem, type DateRange } from './dateRange.ts'

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
 * `today` is the product's date and `historyDays` pages/capabilities'
 * historyDays (null for no limit).
 */
export function useReportRange(today: string, historyDays: number | null): { range: DateRange; setRange: (range: DateRange) => void } {
  const [params, setParams] = useSearchParams()
  const from = params.get('from')
  const to = params.get('to')
  const asked = from !== null || to !== null
  const requested: DateRange = { from: from ?? '', to: to ?? '' }
  const usable = asked && rangeProblem(requested, today, earliestDate(today, historyDays)) === null
  const opensOn = defaultRange(today, historyDays)

  useEffect(() => {
    if (asked && !usable) {
      setParams(
        (current) => {
          const next = new URLSearchParams(current)
          next.delete('from')
          next.delete('to')
          return next
        },
        { replace: true },
      )
    }
  }, [asked, usable, setParams])

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

  return { range: usable ? requested : opensOn, setRange }
}
