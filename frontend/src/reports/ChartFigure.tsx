import { useState, type ReactNode } from 'react'

import { BarChart, type Bar } from './BarChart.tsx'
import type { CsvTable } from './csv.ts'
import { CsvButton } from './ReportActions.tsx'
import { usePrinting } from './usePrinting.ts'

/**
 * A chart's figures as a file: what the file's table is called, and the
 * figures themselves as values -- a date as a date, a count as a number --
 * and not as `rows` writes them for reading. Where a long range is shown a
 * month at a time, these are still its days.
 */
export interface FigureCsv {
  dataset: string
  table: () => CsvTable
}

/**
 * A chart with everything needed to read it without seeing it or hovering
 * over it: a heading, a sentence that says what it shows, the chart, and --
 * on request -- the same figures as a table. The table is how anyone gets an
 * exact figure; the chart has no tooltips.
 *
 * `name` must be unique on the page: the ids that tie the pieces together
 * are built from it. `csv`, where the figures can be taken away as a file,
 * adds that choice beside the table's (ReportActions' CsvButton decides
 * whether it is offered).
 */
export function ChartFigure({
  name,
  heading,
  summary,
  chartLabel,
  bars,
  emptyText,
  columns,
  rows,
  csv,
}: {
  name: string
  heading: string
  summary: string
  chartLabel: string
  bars: readonly Bar[]
  emptyText: string
  /** The table's column headings. The first column names the row. */
  columns: readonly string[]
  rows: ReadonlyArray<readonly string[]>
  csv?: FigureCsv
}) {
  return (
    <Figure name={name} heading={heading} summary={summary} columns={columns} rows={rows} csv={csv}>
      {(summaryName) => <BarChart label={chartLabel} describedBy={summaryName} bars={bars} emptyText={emptyText} />}
    </Figure>
  )
}

/**
 * The frame every chart here has: a heading, the sentence that says what the
 * chart shows, the chart itself -- whatever `children` draws, given the id
 * of that sentence to be described by -- and a button that shows the same
 * figures as a table.
 *
 * On paper there is no button to press, so the table is there while the page
 * is being printed whether or not it was asked for on screen.
 */
export function Figure({
  name,
  heading,
  summary,
  columns,
  rows,
  csv,
  headingLevel = 5,
  children,
}: {
  name: string
  heading: string
  summary: string
  columns: readonly string[]
  rows: ReadonlyArray<readonly string[]>
  csv?: FigureCsv
  /** The heading's level: 5 under a report section's h4 (the default), 4 directly under a dashboard panel's h3. */
  headingLevel?: 4 | 5
  children: (summaryName: string) => ReactNode
}) {
  const Heading = headingLevel === 4 ? 'h4' : 'h5'
  const [tableShown, setTableShown] = useState(false)
  const printing = usePrinting()
  const tableThere = tableShown || printing
  const headingName = `${name}-heading`
  const summaryName = `${name}-summary`
  const tableName = `${name}-table`

  return (
    <div className="chart-figure">
      <Heading id={headingName}>{heading}</Heading>
      <p className="chart-summary" id={summaryName}>
        {summary}
      </p>
      {children(summaryName)}
      <button
        type="button"
        className="button-secondary"
        aria-expanded={tableShown}
        aria-controls={tableName}
        onClick={() => setTableShown(!tableShown)}
      >
        {tableShown ? 'Hide table' : 'Show table'}
        <span className="visually-hidden">: {heading}</span>
      </button>
      {csv !== undefined && <CsvButton inline dataset={csv.dataset} table={csv.table} />}
      <div id={tableName} className="chart-table" hidden={!tableThere}>
        {tableThere && (
          <table className="data-table" aria-labelledby={headingName}>
            <thead>
              <tr>
                {columns.map((column) => (
                  <th key={column} scope="col">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row[0]}>
                  <th scope="row">{row[0]}</th>
                  {row.slice(1).map((cell, index) => (
                    <td key={index}>{cell}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
