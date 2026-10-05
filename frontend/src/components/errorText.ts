import { isApiError } from '../api/client.ts'

const FALLBACK = 'Something went wrong. Please try again.'

/**
 * The sentence to show for a failure. An ApiError's message is always safe to
 * show; anything else -- a bug, an unknown thrown value -- gets one fixed
 * sentence, so no stack trace or object dump can reach the page.
 */
export function messageFor(error: unknown): string {
  return isApiError(error) ? error.message : FALLBACK
}
