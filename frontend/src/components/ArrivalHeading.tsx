import { useEffect, useRef } from 'react'

/**
 * The heading of a view that REPLACES another without the address changing:
 * the sign-in form giving way to "forgot password", a reset form giving way
 * to its result. Whatever had focus is gone with the old view, so when
 * `arrive` is set the heading takes focus as it appears and the next Tab
 * reaches what replaced it. It can be focused from code but is not a tab
 * stop.
 *
 * `arrive` is false for a view someone simply opened the app on: nobody was
 * moved there, and focus is left alone.
 */
export function ArrivalHeading({ id, arrive, children }: { id: string; arrive: boolean; children: string }) {
  const heading = useRef<HTMLHeadingElement>(null)

  useEffect(() => {
    if (arrive) {
      heading.current?.focus()
    }
  }, [arrive])

  return (
    <h2 id={id} ref={heading} tabIndex={-1}>
      {children}
    </h2>
  )
}
