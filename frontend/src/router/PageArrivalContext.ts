import { createContext, type RefObject } from 'react'

/**
 * The history entry (React Router's `location.key`) whose page has already
 * been introduced to the reader. It starts as the entry the signed-in app
 * opened on, so the first page shown takes no focus; see PageHeading.
 */
export const PageArrivalContext = createContext<RefObject<string> | null>(null)
