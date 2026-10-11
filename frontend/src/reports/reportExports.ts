import { createContext } from 'react'

import type { DateRange } from './dateRange.ts'

/**
 * What a report's "Download CSV" and "Print report" controls need to know,
 * handed down from the page to wherever a table is drawn.
 *
 *   enabled   the organization's plan includes exports (pages/capabilities' hasExports). Without it no control is
 *             shown. This is what the app offers, not a lock: the figures are already on the person's screen.
 *   scope     whose report it is, for a file's name: the organization, and the sorter where it is one sorter's.
 *   range     the range being reported on -- the address's (useReportRange).
 *
 * Null outside a report: nothing there offers either control.
 */
export interface ReportExports {
  enabled: boolean
  scope: readonly string[]
  range: DateRange
}

export const ReportExportsContext = createContext<ReportExports | null>(null)
