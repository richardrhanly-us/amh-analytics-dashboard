import { useState, type FormEvent } from 'react'

import {
  daysInRange,
  earliestDate,
  lastDays,
  MAX_RANGE_DAYS,
  presetOf,
  presetsFor,
  rangeProblem,
  rangeProblemText,
  type DateRange,
} from './dateRange.ts'
import { formatDate } from './derive.ts'

/**
 * Chooses the range a report covers: one of the presets, or two dates.
 *
 * `range` is the range now being reported on. A preset applies at once. Two
 * typed dates apply when the form is submitted, and only if they make a
 * range that can be asked for -- in order, not after today, not longer than
 * the longest range available. Until then the report stays on the range it
 * had, which is the one its figures say they are for.
 *
 * Dates are calendar dates in the product's zone. `today` is the product's
 * date, not this browser's. `historyDays` is how far back the organization's
 * plan lets a range start (pages/capabilities' historyDays; null for no
 * limit): only the presets within it are offered, and no earlier start date
 * can be chosen. The API holds every range to the same rule.
 */
export function DateRangeControl({
  range,
  today,
  historyDays,
  onChange,
}: {
  range: DateRange
  today: string
  historyDays: number | null
  onChange: (range: DateRange) => void
}) {
  // What is in the two date boxes: the applied range until someone types.
  const [draft, setDraft] = useState<DateRange>(range)
  const [problem, setProblem] = useState<string | null>(null)
  const activePreset = presetOf(range, today)
  const earliest = earliestDate(today, historyDays)

  function choose(next: DateRange) {
    setDraft(next)
    setProblem(null)
    onChange(next)
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const found = rangeProblem(draft, today, earliest)
    if (found !== null) {
      setProblem(rangeProblemText(found))
      return
    }
    choose(draft)
  }

  return (
    <div className="range-control">
      <div className="range-presets" role="group" aria-label="Date range presets">
        {presetsFor(historyDays).map((preset) => (
          <button
            key={preset.days}
            type="button"
            className={activePreset === preset.days ? undefined : 'button-secondary'}
            aria-pressed={activePreset === preset.days}
            onClick={() => choose(lastDays(preset.days, today))}
          >
            {preset.label}
          </button>
        ))}
      </div>

      <form className="range-custom" onSubmit={handleSubmit} aria-label="Custom date range" noValidate>
        <div className="field">
          <label htmlFor="report-from">From</label>
          <input
            id="report-from"
            name="from"
            type="date"
            value={draft.from}
            min={earliest ?? undefined}
            max={today}
            aria-invalid={problem !== null}
            aria-describedby="report-range-help"
            onChange={(event) => setDraft({ ...draft, from: event.target.value })}
          />
        </div>
        <div className="field">
          <label htmlFor="report-to">To</label>
          <input
            id="report-to"
            name="to"
            type="date"
            value={draft.to}
            // The earliest and latest end dates that could go with this start date.
            min={draft.from || undefined}
            max={today}
            aria-invalid={problem !== null}
            aria-describedby="report-range-help"
            onChange={(event) => setDraft({ ...draft, to: event.target.value })}
          />
        </div>
        <button type="submit" className="button-secondary">
          Apply dates
        </button>
      </form>

      <p className="range-help" id="report-range-help">
        Up to {MAX_RANGE_DAYS} days can be shown at a time at present.
        {earliest !== null && ` The earliest date that can be chosen is ${formatDate(earliest)}.`} The latest date that can
        be chosen is today, {formatDate(today)}.
      </p>
      {problem !== null && (
        <p className="error-message" role="alert">
          {problem}
        </p>
      )}
    </div>
  )
}

/** What the figures below are for. It changes only when a range is applied, never while one is being typed. */
export function RangeShown({ range, today, timeZone }: { range: DateRange; today: string; timeZone: string }) {
  const length = daysInRange(range)
  return (
    <p className="range-shown">
      Showing {formatDate(range.from)} to {formatDate(range.to)}: {length} {length === 1 ? 'day' : 'days'}, in{' '}
      {timeZone} time.
      {range.to === today && ' This range includes today, which is not over yet: its figures will still rise.'}
    </p>
  )
}
