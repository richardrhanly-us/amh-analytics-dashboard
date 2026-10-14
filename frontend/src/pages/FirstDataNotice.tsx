import { WAITING_FOR_FIRST_DATA } from './labels.ts'

/**
 * In place of a sorter's figures while its site waits for its first data: there are no figures yet, so none is
 * shown -- not zeros that would read as a quiet day, and not a pipeline state standing in for the reason. Information,
 * not a fault: nothing is blocked, and the machine's own status is said elsewhere, as for any sorter.
 */
export function FirstDataNotice() {
  return (
    <section className="notice first-data-notice" aria-labelledby="first-data-heading">
      <h3 id="first-data-heading">{WAITING_FOR_FIRST_DATA}</h3>
      <p>
        This sorter is set up or still being connected, and SortView has not received any activity from it yet. Its
        figures will appear here once its first data arrives.
      </p>
    </section>
  )
}
