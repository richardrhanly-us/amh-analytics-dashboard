import { describe, expect, it } from 'vitest'

import customerHtml from '../../index.html?raw'

/**
 * The customer app's side of the admin boundary (A2 item 1). The separate admin app lives in admin/ with its own
 * entry and build (vite.admin.config.ts); nothing of it, and no call to the admin API, may reach the customer app.
 * The build-output check is in CI (.github/workflows/frontend-ci.yml): the built dist/ is searched as well.
 */

const sources = import.meta.glob<string>('../**/*.{ts,tsx,css}', { query: '?raw', import: 'default', eager: true })
const shipped = Object.entries(sources).filter(([path]) => !path.includes('.test.') && !path.startsWith('./'))

const IMPORT = /(?:\bfrom\s*|\bimport\s*\(?\s*)['"]([^'"]+)['"]/g

describe('customer app boundary', () => {
  it('finds the shipped customer source to check', () => {
    expect(shipped.map(([path]) => path)).toContain('../main.tsx')
  })

  it('never imports anything from the admin app', () => {
    const offenders = shipped.flatMap(([path, text]) =>
      [...text.matchAll(IMPORT)]
        .map((match) => match[1])
        .filter((specifier) => /(^|\/)admin\//.test(specifier))
        .map((specifier) => `${path} -> ${specifier}`),
    )

    expect(offenders).toEqual([])
  })

  it('never names the admin API', () => {
    expect(shipped.filter(([, text]) => /\/api\/admin\b/.test(text)).map(([path]) => path)).toEqual([])
  })

  it('is still built from its one entry only', () => {
    expect([...customerHtml.matchAll(/<script\b[^>]*\bsrc="([^"]+)"/g)].map((match) => match[1])).toEqual(['/src/main.tsx'])
  })
})
