/** What a section shows in place of its content while that is loading, or when it could not be loaded. */
export function SectionPlaceholder({ status }: { status: 'loading' | 'error' }) {
  return status === 'loading' ? (
    <p className="section-pending">Loading…</p>
  ) : (
    <p className="section-failed">
      <svg className="inline-icon" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
        <path d="M8 1.5 15 14H1Z" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
        <path d="M8 6v4M8 11.75v.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
      </svg>
      Could not load.
    </p>
  )
}
