import type { RejectsByReason } from '../api/liveToday.ts'
import { reasonLabel, topRejectReasons } from './metrics.ts'
import { SectionPlaceholder } from './SectionPlaceholder.tsx'
import type { Section } from './useLiveToday.ts'

/** The reject reasons that occurred today, most frequent first, exactly as the API categorises them. */
export function RejectReasons({ rejectsByReason }: { rejectsByReason: Section<RejectsByReason> }) {
  const reasons = rejectsByReason.status === 'ready' ? topRejectReasons(rejectsByReason.data.reasons) : []

  return (
    <section className="panel" aria-labelledby="reasons-heading">
      <h3 id="reasons-heading">Top reject reasons</h3>
      {rejectsByReason.status !== 'ready' && <SectionPlaceholder status={rejectsByReason.status} />}
      {rejectsByReason.status === 'ready' &&
        (reasons.length === 0 ? (
          <p className="quiet">No rejects today.</p>
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
