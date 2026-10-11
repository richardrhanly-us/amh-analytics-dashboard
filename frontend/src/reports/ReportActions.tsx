import { useContext } from 'react'

import { csvFileName, downloadCsv, type CsvTable } from './csv.ts'
import { ReportExportsContext } from './reportExports.ts'

/**
 * Downloads one table of a report as a CSV file. Shown only where the
 * organization's plan includes exports; otherwise there is nothing here.
 *
 * `dataset` names the table -- to whoever cannot see which table the button
 * is beside, and in the file's name, with whose report it is and the range.
 * `table` gives the figures when the button is pressed: the report's own,
 * already loaded, as values and not as they are written on the page. Nothing
 * is asked of the API.
 *
 * `inline`: the button sits beside another one. Otherwise it has a line of
 * its own under its table.
 */
export function CsvButton({ dataset, table, inline = false }: { dataset: string; table: () => CsvTable; inline?: boolean }) {
  const exports = useContext(ReportExportsContext)

  if (exports === null || !exports.enabled) {
    return null
  }
  const { scope, range } = exports
  const button = (
    <button
      type="button"
      className="button-secondary csv-download"
      onClick={() => downloadCsv(csvFileName([...scope, dataset, range.from, 'to', range.to]), table())}
    >
      Download CSV<span className="visually-hidden">: {dataset}</span>
    </button>
  )
  return inline ? button : <p className="table-actions">{button}</p>
}

/**
 * Prints the report view that is open, by the browser's own Print -- which is
 * also how it is saved as a PDF. Shown only where the organization's plan
 * includes exports. The page's print styles decide what goes on paper.
 */
export function PrintButton() {
  const exports = useContext(ReportExportsContext)

  if (exports === null || !exports.enabled) {
    return null
  }
  return (
    <p className="report-actions">
      <button type="button" className="button-secondary" onClick={() => window.print()}>
        Print report
      </button>
    </p>
  )
}
