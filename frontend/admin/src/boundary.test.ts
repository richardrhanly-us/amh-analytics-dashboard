import { describe, expect, it } from 'vitest'

import adminHtml from '../index.html?raw'

/**
 * The admin app's side of the boundary: it is its own app, and borrows from the customer app only the pure modules
 * named below -- never its App, router, auth provider, pages or components.
 *
 *   src/api/client.ts, src/api/auth.ts   the API client and sign-in/sign-out (A2 item 1)
 *   src/index.css                        the stylesheet (A2 item 1)
 *   src/query/queryClient.ts             the session's query client and its retry rules (A2 item 2)
 *   src/time/productTime.ts              formatting an instant in the product's time zone (A2 item 2)
 */

const sources = import.meta.glob<string>('./**/*.{ts,tsx}', { query: '?raw', import: 'default', eager: true })
const shipped = Object.entries(sources).filter(([path]) => !path.includes('.test.'))

const ALLOWED_CUSTOMER_MODULES = [
  '/src/api/auth.ts',
  '/src/api/client.ts',
  '/src/index.css',
  '/src/query/queryClient.ts',
  '/src/time/productTime.ts',
]

const IMPORT = /(?:\bfrom\s*|\bimport\s*\(?\s*)['"]([^'"]+)['"]/g

/** A relative import of `path` (relative to admin/src), as a path from the frontend root. Packages are left out. */
function resolved(path: string, specifier: string): string | null {
  if (!specifier.startsWith('.')) {
    return null
  }
  return new URL(specifier, new URL(path, 'file:///admin/src/')).pathname
}

describe('admin app boundary', () => {
  it('finds the shipped admin source to check', () => {
    expect(shipped.map(([path]) => path).sort()).toEqual([
      './AdminApp.tsx',
      './AdminRoutes.tsx',
      './adminSession.ts',
      './libraries/LibrariesPage.tsx',
      './libraries/LibraryPage.tsx',
      './libraries/labels.ts',
      './libraries/librariesApi.ts',
      './main.tsx',
    ])
  })

  it('imports from the customer app only the pure modules named above', () => {
    const outside = new Set(
      shipped.flatMap(([path, text]) =>
        [...text.matchAll(IMPORT)]
          .map((match) => resolved(path, match[1]))
          .filter((target): target is string => target !== null && !target.startsWith('/admin/')),
      ),
    )

    expect([...outside].sort()).toEqual(ALLOWED_CUSTOMER_MODULES)
  })

  it('is its own page, loading only its own entry', () => {
    expect([...adminHtml.matchAll(/<script\b[^>]*\bsrc="([^"]+)"/g)].map((match) => match[1])).toEqual(['/src/main.tsx'])
  })
})
