/**
 * A report table as a CSV file: what the text is, what the file is called,
 * and the one place a file is handed to the browser. Nothing else in the app
 * makes a file or a download.
 *
 * WHAT A CELL IS. A number is written as that number -- `1274`, `-12`,
 * `15.5` -- with no separators, signs of its own or units, so a spreadsheet
 * reads it as a number. A date is its `YYYY-MM-DD` text. Null is an empty
 * cell: a figure that does not exist is not written as 0.
 *
 * TEXT IS NEVER A FORMULA. A spreadsheet treats a cell that begins with `=`,
 * `+`, `-` or `@` as something to work out. Any TEXT cell that begins so
 * (after any leading space) is written with an apostrophe in front, which a
 * spreadsheet shows as plain text. That is decided by the cell's type, not
 * by how it looks: the number -12 is a number and is left alone; the text
 * "-12" is text and is made safe.
 *
 * THE FORMAT. Comma-separated, one record to a line, lines ended CRLF, the
 * last one too. A cell holding a comma, a double quote, a line break or
 * space at either end is wrapped in double quotes, and a double quote inside
 * it is doubled. The text starts with a byte order mark, so a spreadsheet
 * opens it as UTF-8.
 */

export type CsvCell = string | number | null

export interface CsvTable {
  /** The column headings: the first record of the file. */
  columns: readonly string[]
  rows: ReadonlyArray<readonly CsvCell[]>
}

export const CSV_BYTE_ORDER_MARK = '﻿'
const LINE_END = '\r\n'

const STARTS_A_FORMULA = /^\s*[=+\-@]/
const NEEDS_QUOTES = /[",\r\n]|^\s|\s$/

function textCell(value: string): string {
  const safe = STARTS_A_FORMULA.test(value) ? `'${value}` : value
  return NEEDS_QUOTES.test(safe) ? `"${safe.replaceAll('"', '""')}"` : safe
}

function cell(value: CsvCell): string {
  if (value === null) {
    return ''
  }
  if (typeof value === 'number') {
    // Not a figure at all: nothing is written rather than "NaN" or "Infinity".
    return Number.isFinite(value) ? String(value) : ''
  }
  return textCell(value)
}

/** The whole text of a CSV file for `table`. Every row must have a cell for every column. */
export function csvText(table: CsvTable): string {
  if (table.rows.some((row) => row.length !== table.columns.length)) {
    throw new RangeError('Every row of a CSV table must have one cell for each column.')
  }
  const records = [table.columns, ...table.rows].map((row) => row.map(cell).join(','))
  return CSV_BYTE_ORDER_MARK + records.join(LINE_END) + LINE_END
}

/**
 * A rate or an average for a CSV cell: the figure the page shows, as a
 * number, kept to `places` decimal places so that binary arithmetic does not
 * write 15.000000000000002. Null stays null.
 */
export function rounded(value: number | null, places = 2): number | null {
  return value === null ? null : Number(value.toFixed(places))
}

const MAX_NAME_LENGTH = 150

/**
 * A file name made of `parts`, in order: lower-case letters, digits and
 * hyphens only, the parts joined by underscores, ending `.csv`. Anything
 * else -- a path separator, a dot, a space, punctuation -- becomes a hyphen,
 * and an accent is dropped from its letter, so the name cannot name a folder
 * or another kind of file. The same parts always give the same name.
 */
export function csvFileName(parts: readonly string[]): string {
  const safe = parts
    .map((part) =>
      part
        .normalize('NFKD')
        .replace(/[\u0300-\u036f]/g, '')
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/^-+|-+$/g, ''),
    )
    .filter((part) => part !== '')
  const name = safe.join('_').slice(0, MAX_NAME_LENGTH).replace(/[-_]+$/, '')
  return `${name === '' ? 'report' : name}.csv`
}

/** How long the browser is given to begin a download before the file's address is given up. */
export const REVOKE_AFTER_MS = 1_000

/**
 * Hands `table` to the browser as a file to save. Nothing is sent anywhere: the file is made here, from `table`.
 *
 * The link that carries it is gone again at once. The file's address is given up a moment later, not in the same
 * breath as the click: a browser that has not yet begun the download when the address goes saves nothing.
 */
export function downloadCsv(fileName: string, table: CsvTable): void {
  const address = URL.createObjectURL(new Blob([csvText(table)], { type: 'text/csv;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = address
  link.download = fileName
  document.body.append(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(address), REVOKE_AFTER_MS)
}
