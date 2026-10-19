import { createContext, type RefObject } from 'react'

/**
 * The history entry (React Router's `location.key`) whose page has already been introduced to the reader. It starts as
 * the entry the signed-in admin app opened on, so the first page shown takes no focus; see AdminPageHeading. The same
 * idea as the customer app's PageArrivalContext.
 */
export const AdminPageArrivalContext = createContext<RefObject<string> | null>(null)
