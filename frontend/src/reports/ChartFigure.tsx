import { useState, type ReactNode } from 'react'

import { BarChart, type Bar } from './BarChart.tsx'

/**
 * A chart with everything needed to read it without seeing it or hovering
 * over it: a heading, a sentence that says what it shows, the chart, and --
 * on request -- the same figures as a table. The table is how anyone gets an
 * exact figure; the chart has no tooltips.
 *
 * `name` must be unique on the page: the ids that tie the pieces together
 * are built from it.
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
}) {
  return (
    <Figure name={name} heading={heading} summary={summary} columns={columns} rows={rows}>
      {(summaryName) => <BarChart label={chartLabel} describedBy={summaryName} bars={bars} emptyText={emptyText} />}
    </Figure>
  )
}

/**
 * The frame every chart here has: a heading, the sentence that says what the
 * chart shows, the chart itself -- whatever `children` draws, given the id
 * of that sentence to be described by -- and a button that shows the same
 * figures as a table.
 */
export function Figure({
  name,
  heading,
  summary,
  columns,
  rows,
  children,
}: {
  name: string
  heading: string
  summary: string
  columns: readonly string[]
  rows: ReadonlyArray<readonly string[]>
  children: (summaryName: string) => ReactNode
}) {
  const [tableShown, setTableShown] = useState(false)
  const headingName = `${name}-heading`
  const summaryName = `${name}-summary`
  const tableName = `${name}-table`

  return (
    <div className="chart-figure">
      <h5 id={headingName}>{heading}</h5>
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
      <div id={tableName} className="chart-table" hidden={!tableShown}>
        {tableShown && (
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
