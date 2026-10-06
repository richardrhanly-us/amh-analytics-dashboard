import { useContext, useEffect, useRef } from 'react'
import { useLocation, useNavigationType } from 'react-router'

import { PageArrivalContext } from '../router/PageArrivalContext.ts'

const APP_TITLE = 'SortView'

/**
 * A page's heading, and where focus goes on arriving at the page.
 *
 * Following a link replaces the page and removes the link that had focus.
 * So that a keyboard or screen-reader user is not left on nothing, the first
 * heading shown for a new history entry takes focus -- whether it appears at
 * once or after the page's data has loaded. That covers links and Back and
 * Forward. It does not cover the page the app opened on, or a redirect:
 * nobody asked to be moved there, and focus is left alone.
 *
 * The heading can be focused from code but is not a tab stop. It also names
 * the page in the browser tab.
 */
export function PageHeading({ children }: { children: string }) {
  const heading = useRef<HTMLHeadingElement>(null)
  const { key } = useLocation()
  const navigationType = useNavigationType()
  const introducedRef = useContext(PageArrivalContext)

  useEffect(() => {
    if (introducedRef === null || introducedRef.current === key) {
      return
    }
    introducedRef.current = key
    if (navigationType !== 'REPLACE') {
      heading.current?.focus()
    }
  }, [introducedRef, key, navigationType])

  useEffect(() => {
    document.title = `${children} – ${APP_TITLE}`
    return () => {
      document.title = APP_TITLE
    }
  }, [children])

  return (
    <h2 ref={heading} tabIndex={-1}>
      {children}
    </h2>
  )
}
