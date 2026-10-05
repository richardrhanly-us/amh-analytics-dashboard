/**
 * What a card has to show.
 *
 *   value    a figure
 *   empty    words in place of a figure that does not exist ("No check-ins yet")
 *   pending  still loading
 *   failed   could not be loaded
 *
 * `text` is always words or a real figure -- never a placeholder number.
 */
export interface Figure {
  tone: 'value' | 'empty' | 'pending' | 'failed'
  text: string
  /** A short line under the figure, where it needs one: which hour, or why there is no figure. */
  note?: string
}

/** One figure in a summary: a term and its value in the surrounding `<dl>`. */
export function MetricCard({ label, tone, text, note }: Figure & { label: string }) {
  return (
    <div className={`metric metric-${tone}`}>
      <dt>{label}</dt>
      <dd className="metric-value">{text}</dd>
      {note !== undefined && <dd className="metric-note">{note}</dd>}
    </div>
  )
}
