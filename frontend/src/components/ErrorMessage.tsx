/** An error announced to assistive technology as soon as it appears. Renders nothing without a message. */
export function ErrorMessage({ message }: { message: string | null }) {
  if (message === null || message === '') {
    return null
  }
  return (
    <p className="error-message" role="alert">
      {message}
    </p>
  )
}
