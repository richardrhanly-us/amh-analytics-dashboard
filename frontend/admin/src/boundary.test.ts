import { describe, expect, it } from 'vitest'

import adminHtml from '../index.html?raw'

/**
 * The admin app's side of the boundary (A2 item 1): it is its own app, and borrows from the customer app only the
 * two API modules and the stylesheet named below -- never its App, router, auth provider, pages or components.
 */

const sources = import.meta.glob<string>('./**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true })
const shipped = Object.entries(sources).filter(([path]) => !path.includes('.test.'))

const ALLOWED_CUSTOMER_MODULES = ['../../src/api/auth.ts', '../../src/api/client.ts', '../../src/index.css']

const IMPORT = /(?:\bfrom\s*|\bimport\s*\(?\s*)['"]([^'"]+)['"]/g

function specifiers(text: string): string[] {
  return [...text.matchAll(IMPORT)].map((match) => match[1])
}

describe('admin app boundary', () => {
  it('finds the shipped admin source to check', () => {
    expect(shipped.map(([path]) => path).sort()).toEqual(['./AdminApp.tsx', './adminSession.ts', './main.tsx'])
  })

  it('imports from the customer app only the two API modules and the stylesheet', () => {
    const outside = shipped.flatMap(([path, text]) =>
      specifiers(text)
        .filter((specifier) => specifier.startsWith('../'))
        .map((specifier) => `${path} -> ${specifier}`),
    )
    const borrowed = new Set(outside.map((entry) => entry.split(' -> ')[1]))

    expect([...borrowed].sort()).toEqual(ALLOWED_CUSTOMER_MODULES)
  })

  it('is its own page, loading only its own entry', () => {
    expect([...adminHtml.matchAll(/<script\b[^>]*\bsrc="([^"]+)"/g)].map((match) => match[1])).toEqual(['/src/main.tsx'])
  })
})
