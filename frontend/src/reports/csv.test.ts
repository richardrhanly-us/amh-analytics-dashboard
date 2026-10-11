import { describe, expect, it } from 'vitest'

import { CSV_BYTE_ORDER_MARK, csvFileName, csvText, rounded } from './csv.ts'

/** The records of a CSV text, without the byte order mark and the last line end. */
const records = (text: string) => text.slice(CSV_BYTE_ORDER_MARK.length, -2).split('\r\n')
const one = (cell: string | number | null) => records(csvText({ columns: ['Value'], rows: [[cell]] }))[1]

describe('the text of a CSV file', () => {
  it('is the headings, then a record for each row, comma-separated', () => {
    const text = csvText({
      columns: ['Date', 'Check-ins', 'Rejects'],
      rows: [
        ['2026-09-06', 0, 0],
        ['2026-09-07', 1274, 13],
      ],
    })

    expect(records(text)).toEqual(['Date,Check-ins,Rejects', '2026-09-06,0,0', '2026-09-07,1274,13'])
  })

  it('starts with a byte order mark, so a spreadsheet opens it as UTF-8, and keeps what is not ASCII', () => {
    const text = csvText({ columns: ['Sorter'], rows: [['Bibliothèque Côté']] })

    expect(text.startsWith('\uFEFF')).toBe(true)
    expect(Array.from(new TextEncoder().encode(text).slice(0, 3))).toEqual([0xef, 0xbb, 0xbf])
    expect(records(text)[1]).toBe('Bibliothèque Côté')
  })

  it('ends every line the same way, CRLF, the last one too', () => {
    const text = csvText({ columns: ['A', 'B'], rows: [['x', 1], ['y', 2]] })

    expect(text.slice(1)).toBe('A,B\r\nx,1\r\ny,2\r\n')
    expect(text.replaceAll('\r\n', '')).not.toMatch(/[\r\n]/)
  })

  it('is just its headings when there are no rows', () => {
    expect(csvText({ columns: ['Date', 'Check-ins'], rows: [] }).slice(1)).toBe('Date,Check-ins\r\n')
  })

  it('refuses a row that does not have a cell for every column', () => {
    expect(() => csvText({ columns: ['A', 'B'], rows: [['only one']] })).toThrow(RangeError)
    expect(() => csvText({ columns: ['A'], rows: [['one', 'too many']] })).toThrow(RangeError)
  })
})

describe('a cell', () => {
  it('is a number written as that number: no separators, no units, never in words', () => {
    expect(one(1274)).toBe('1274')
    expect(one(1234567)).toBe('1234567')
    expect(one(0)).toBe('0')
    expect(one(14.91)).toBe('14.91')
  })

  it('keeps a negative number a number: it is not text, and not a formula', () => {
    expect(one(-12)).toBe('-12')
    expect(one(-0.5)).toBe('-0.5')
  })

  it('is empty for a figure that does not exist: never 0, NaN or Infinity', () => {
    expect(one(null)).toBe('')
    expect(one(Number.NaN)).toBe('')
    expect(one(Number.POSITIVE_INFINITY)).toBe('')
  })

  it('is plain text written as it is, a date among it', () => {
    expect(one('Central Library AMH')).toBe('Central Library AMH')
    expect(one('2026-09-06')).toBe('2026-09-06')
    expect(one('item_not_found')).toBe('item_not_found')
  })

  it('is quoted when it holds a comma, a quote or a line break, with its quotes doubled', () => {
    expect(one('Westside, North')).toBe('"Westside, North"')
    expect(one('The "Annex"')).toBe('"The ""Annex"""')
    expect(one('"')).toBe('""""')
    // A line break inside a cell stays inside its quotes: it does not end the record.
    expect(csvText({ columns: ['Value'], rows: [['two\nlines'], ['and\r\nmore']] }).slice(1)).toBe('Value\r\n"two\nlines"\r\n"and\r\nmore"\r\n')
  })

  it('is quoted when it has space at either end, so the space is kept', () => {
    expect(one(' Main')).toBe('" Main"')
    expect(one('Main ')).toBe('"Main "')
    expect(one('Main Branch')).toBe('Main Branch')
  })
})

describe('text a spreadsheet would work out as a formula', () => {
  it.each([
    ['=1+1', "'=1+1"],
    ['=HYPERLINK("http://example.test","x")', '"\'=HYPERLINK(""http://example.test"",""x"")"'],
    ['+1+1', "'+1+1"],
    ['-1+1', "'-1+1"],
    ['@SUM(A1:A9)', "'@SUM(A1:A9)"],
    // Behind leading space too: a spreadsheet looks past it.
    ['  =1+1', "'  =1+1"],
    ['\t=1+1', "'\t=1+1"],
  ])('%j is written as text, with an apostrophe in front', (value, written) => {
    expect(one(value)).toBe(written)
  })

  it('is decided by what the cell is, not by how it looks: the text "-12" is made safe, the number -12 is left alone', () => {
    expect(one('-12')).toBe("'-12")
    expect(one(-12)).toBe('-12')
    expect(one('+44 20 7946 0000')).toBe("'+44 20 7946 0000")
  })

  it('leaves alone text that only has such a character further in', () => {
    expect(one('A=B')).toBe('A=B')
    expect(one('East-Side')).toBe('East-Side')
    expect(one('info@example.test')).toBe('info@example.test')
    expect(one('2026-09-06')).toBe('2026-09-06')
  })

  it('makes a heading safe the same way', () => {
    expect(records(csvText({ columns: ['=cmd', 'Check-ins'], rows: [] }))[0]).toBe("'=cmd,Check-ins")
  })
})

describe('a rate or an average in a cell', () => {
  it('is the figure as a number, to two places, without the dust of binary arithmetic', () => {
    expect(rounded((190 / 1274) * 100)).toBe(14.91)
    expect(rounded(0.1 + 0.2)).toBe(0.3)
    expect(rounded(100)).toBe(100)
    expect(rounded(637.5, 1)).toBe(637.5)
  })

  it('stays missing when it does not exist: not 0', () => {
    expect(rounded(null)).toBeNull()
    expect(rounded(0)).toBe(0)
  })
})

describe('a CSV file’s name', () => {
  it('is its parts in order, joined by underscores, ending .csv', () => {
    expect(csvFileName(['northbridge', 'central', 'Daily check-ins', '2026-09-06', 'to', '2026-10-05'])).toBe(
      'northbridge_central_daily-check-ins_2026-09-06_to_2026-10-05.csv',
    )
  })

  it('is the same name every time for the same parts', () => {
    const parts = ['northbridge', 'Sorter comparison', '2026-09-06', 'to', '2026-10-05']
    expect(csvFileName(parts)).toBe(csvFileName([...parts]))
  })

  it.each([
    [['../../etc/passwd'], 'etc-passwd.csv'],
    [['C:\\Windows\\system32'], 'c-windows-system32.csv'],
    [['a/b', 'c\\d'], 'a-b_c-d.csv'],
    [['report.exe'], 'report-exe.csv'],
    [['Where check-ins went?'], 'where-check-ins-went.csv'],
    [['  Volume & capacity  '], 'volume-capacity.csv'],
    [['Bibliothèque Côté'], 'bibliotheque-cote.csv'],
    [['line\nbreak', 'tab\there'], 'line-break_tab-here.csv'],
    [['<script>', '"quoted"', 'a:b|c*d'], 'script_quoted_a-b-c-d.csv'],
  ])('has nothing in it but letters, digits, hyphens and underscores: %j', (parts, name) => {
    expect(csvFileName(parts)).toBe(name)
    expect(csvFileName(parts)).toMatch(/^[a-z0-9_-]+\.csv$/)
  })

  it('leaves out a part with nothing usable in it, and is still a name when nothing is left', () => {
    expect(csvFileName(['northbridge', '///', '', 'bins'])).toBe('northbridge_bins.csv')
    expect(csvFileName([])).toBe('report.csv')
    expect(csvFileName(['...', '??'])).toBe('report.csv')
  })

  it('is never longer than a file system takes, and does not end on a separator', () => {
    const name = csvFileName(['x'.repeat(149), 'tail'])

    expect(name.length).toBeLessThanOrEqual(154)
    expect(name).toMatch(/^[a-z0-9_-]*[a-z0-9]\.csv$/)
  })
})
