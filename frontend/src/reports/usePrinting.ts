import { useEffect, useState } from 'react'
import { flushSync } from 'react-dom'

/**
 * Whether the page is being printed right now -- by the app's "Print report"
 * or by the browser's own Print, which are the same thing to a page.
 *
 * For what is on the page only on request, and belongs on paper regardless:
 * a chart's table of exact figures. The browser says it is about to print
 * and takes the page as it is the moment after, so the change is made at
 * once (flushSync), not when React next gets round to it.
 */
export function usePrinting(): boolean {
  const [printing, setPrinting] = useState(false)

  useEffect(() => {
    const before = () => flushSync(() => setPrinting(true))
    const after = () => setPrinting(false)
    window.addEventListener('beforeprint', before)
    window.addEventListener('afterprint', after)
    return () => {
      window.removeEventListener('beforeprint', before)
      window.removeEventListener('afterprint', after)
    }
  }, [])

  return printing
}
