import { ErrorMessage } from './ErrorMessage.tsx'

/** A load that failed for a reason that may pass: the safe message, and a way to try again. */
export function LoadFailure({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="load-failure">
      <ErrorMessage message={message} />
      <button type="button" onClick={onRetry}>
        Try again
      </button>
    </div>
  )
}
