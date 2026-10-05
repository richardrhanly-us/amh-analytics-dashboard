import { describe, expect, it } from 'vitest'

import packageJson from '../../package.json?raw'

/**
 * Guards on what this block is allowed to contain. They read the shipped
 * source as text, so they look for specific things that must not be there and
 * say nothing about formatting.
 */

const allSources = import.meta.glob<string>('../**/*.{ts,tsx,css}', { query: '?raw', import: 'default', eager: true })

// The code a browser runs: everything under src/ except tests and test helpers.
const shipped = Object.entries(allSources).filter(([path]) => !path.includes('.test.') && !path.startsWith('./'))

function offenders(pattern: RegExp): string[] {
  return shipped.filter(([, text]) => pattern.test(text)).map(([path]) => path)
}

const manifest = JSON.parse(packageJson) as {
  dependencies?: Record<string, string>
  devDependencies?: Record<string, string>
}
const installed = [...Object.keys(manifest.dependencies ?? {}), ...Object.keys(manifest.devDependencies ?? {})]

describe('scope of this block', () => {
  it('finds the shipped source to check', () => {
    const paths = shipped.map(([path]) => path)

    expect(paths).toContain('../api/client.ts')
    expect(paths).toContain('../auth/AuthProvider.tsx')
    expect(paths).toContain('../components/LoginForm.tsx')
    expect(paths.some((path) => path.includes('test'))).toBe(false)
  })

  it.each([
    ['axios', /^axios$/],
    ['a router', /router/],
    ['TanStack Query', /^@tanstack\//],
    ['a state library', /^(redux|@reduxjs\/.*|react-redux|zustand|jotai|mobx.*|recoil)$/],
    ['a UI or chart framework', /tailwind|^@mui\/|bootstrap|chart|recharts|^d3/],
    ['a browser test runner', /playwright|cypress/],
  ])('does not depend on %s', (_label, pattern) => {
    expect(installed.filter((name) => pattern.test(name))).toEqual([])
  })

  it('imports none of those libraries either', () => {
    expect(offenders(/from\s+['"](axios|react-router|@tanstack\/|redux|zustand)/)).toEqual([])
  })

  it('calls fetch only from the API client', () => {
    expect(offenders(/\bfetch\s*\(/)).toEqual(['../api/client.ts'])
    expect(offenders(/XMLHttpRequest|sendBeacon|WebSocket|EventSource/)).toEqual([])
  })

  it('names no host: every request is a relative /api path', () => {
    expect(offenders(/https?:\/\//)).toEqual([])
    expect(offenders(/sortview\.com|digitalocean|ondigitalocean|neon\.tech|amazonaws|cloudfront/i)).toEqual([])
  })

  it('uses no browser storage', () => {
    expect(offenders(/localStorage|sessionStorage|indexedDB/)).toEqual([])
  })

  it('never touches document.cookie', () => {
    expect(offenders(/\.cookie\b/)).toEqual([])
  })

  it('invents no token header and never sends cross-site credentials', () => {
    expect(offenders(/Authorization|Bearer|X-Api-Key|X-CSRF/i)).toEqual([])
    expect(offenders(/credentials:\s*['"]include['"]/)).toEqual([])
  })

  it('logs nothing', () => {
    expect(offenders(/console\./)).toEqual([])
  })

  it('calls no API beyond the three auth endpoints', () => {
    const paths = shipped.flatMap(([, text]) => text.match(/['"`]\/api\/[^'"`]*['"`]/g) ?? [])

    expect([...new Set(paths.map((path) => path.slice(1, -1)))].sort()).toEqual([
      '/api/',
      '/api/auth/login',
      '/api/auth/logout',
      '/api/auth/session',
    ])
  })
})
