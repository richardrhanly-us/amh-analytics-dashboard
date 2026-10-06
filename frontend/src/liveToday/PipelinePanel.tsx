import type { PipelineState, PipelineStatus } from '../api/liveToday.ts'
import { formatInstant } from '../time/productTime.ts'

const STATE_LABELS: Record<PipelineState, string> = {
  ok: 'OK',
  degraded: 'Degraded',
  failed: 'Failed',
  unknown: 'Unknown',
}

// One outline per state, so the four differ in shape as well as in colour: a ticked circle, a warning
// triangle, a crossed square and a dashed, empty circle. The word beside it is what says which state it is.
const STATE_ICONS: Record<PipelineState, string[]> = {
  ok: ['M8 1.5a6.5 6.5 0 1 0 0 13a6.5 6.5 0 1 0 0-13Z', 'M5 8.25 7.25 10.5 11 6'],
  degraded: ['M8 1.5 15 14H1Z', 'M8 6v4M8 11.75v.5'],
  failed: ['M2 2h12v12H2Z', 'M5.5 5.5l5 5M10.5 5.5l-5 5'],
  unknown: ['M8 1.5a6.5 6.5 0 1 0 0 13a6.5 6.5 0 1 0 0-13Z', 'M5.5 8h5'],
}

/**
 * What the pipeline last reported, and when. The state is the API's word for
 * it and nothing more: the time of the report is shown beside it, separately,
 * and is not used to judge the state.
 */
export function PipelinePanel({ pipeline }: { pipeline: PipelineStatus }) {
  const reported = formatInstant(pipeline.last_reported_at, pipeline.timezone)
  const [outline, mark] = STATE_ICONS[pipeline.state]

  return (
    <section className={`panel pipeline-panel pipeline-panel-${pipeline.state}`} aria-labelledby="pipeline-heading">
      <h3 id="pipeline-heading">Pipeline</h3>
      <dl className="pipeline-facts">
        <div>
          <dt>Status</dt>
          <dd className={`pipeline-state pipeline-state-${pipeline.state}`}>
            <svg className="inline-icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
              <path
                d={outline}
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinejoin="round"
                strokeDasharray={pipeline.state === 'unknown' ? '2.5 2' : undefined}
              />
              <path d={mark} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span className="pipeline-state-label">{STATE_LABELS[pipeline.state]}</span>
          </dd>
        </div>
        <div>
          <dt>Last reported</dt>
          <dd>
            {reported === null || pipeline.last_reported_at === null ? (
              <span className="quiet">Nothing reported yet</span>
            ) : (
              <time dateTime={pipeline.last_reported_at}>{reported}</time>
            )}
          </dd>
        </div>
      </dl>
    </section>
  )
}
