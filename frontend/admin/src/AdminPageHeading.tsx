import { useContext, useEffect, useRef } from 'react'
import { useLocation, useNavigationType } from 'react-router'

import { AdminPageArrivalContext } from './pageArrival.ts'

const APP_TITLE = 'SortView Administration'

/**
 * An admin view's heading: it names the view in the browser tab, and is where focus goes on arriving at the view.
 *
 * The admin app's version of the customer app's PageHeading, with the same rules. Following a link replaces the view
 * and removes the link that had focus, so the first heading shown for a new history entry takes focus -- whether it
 * appears at once or after the view's data has loaded. Not for the entry the app opened on, a redirect (REPLACE), or a
 * new entry for the same path. Outside the admin routes (the sign-in form, "No access") there is no arrival context,
 * and the heading only names the view.
 *
 * It can be focused from code but is not a tab stop.
 */
export function AdminPageHeading({ id, children }: { id: string; children: string }) {
  const heading = useRef<HTMLHeadingElement>(null)
  const { key, pathname } = useLocation()
  // The path this heading last introduced. Null until it has introduced one.
  const introducedPath = useRef<string | null>(null)
  const navigationType = useNavigationType()
  const introducedRef = useContext(AdminPageArrivalContext)

  useEffect(() => {
    const samePage = introducedPath.current === pathname
    introducedPath.current = pathname
    if (introducedRef === null || introducedRef.current === key) {
      return
    }
    introducedRef.current = key
    if (navigationType !== 'REPLACE' && !samePage) {
      heading.current?.focus()
    }
  }, [introducedRef, key, pathname, navigationType])

  useEffect(() => {
    document.title = `${children} – ${APP_TITLE}`
    return () => {
      document.title = APP_TITLE
    }
  }, [children])

  return (
    <h2 id={id} ref={heading} tabIndex={-1}>
      {children}
    </h2>
  )
}
