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
    ['a state library', /^(redux|@reduxjs\/.*|react-redux|zustand|jotai|mobx.*|recoil)$/],
    ['a UI or chart framework', /tailwind|^@mui\/|bootstrap|chart|recharts|^d3|visx|nivo|victory|plotly/],
    ['a date library', /^(moment.*|dayjs|date-fns.*|luxon|@js-joda\/.*|spacetime|temporal-polyfill|@date-io\/.*)$/],
    ['a browser test runner', /playwright|cypress/],
  ])('does not depend on %s', (_label, pattern) => {
    expect(installed.filter((name) => pattern.test(name))).toEqual([])
  })

  it('imports none of those libraries either', () => {
    expect(offenders(/from\s+['"](axios|redux|zustand|moment|dayjs|date-fns|luxon|chart|recharts|d3)/)).toEqual([])
  })

  it('uses TanStack Query for data, and keeps its cache in memory only', () => {
    expect(installed.filter((name) => name.startsWith('@tanstack/'))).toEqual(['@tanstack/react-query'])
    expect(offenders(/from\s+['"]@tanstack\/react-query['"]/)).toContain('../liveToday/useLiveToday.ts')
    expect(offenders(/persistQueryClient|Persister|dehydrate|hydrate\(/i)).toEqual([])
  })

  it('routes with react-router, and with nothing hand-rolled', () => {
    expect(installed).toContain('react-router')
    expect(installed.filter((name) => /router/.test(name))).toEqual(['react-router'])
    expect(offenders(/from\s+['"]react-router['"]/)).toContain('../router/AppRouter.tsx')
    expect(offenders(/\bhistory\.|pushState|replaceState|window\.location|location\.href/)).toEqual([])
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

  it('calls no API beyond auth, the two organization endpoints and the five Live Today reads', () => {
    const paths = shipped.flatMap(([, text]) => text.match(/['"`]\/api\/[^'"`]*['"`]/g) ?? [])

    expect([...new Set(paths.map((path) => path.slice(1, -1)))].sort()).toEqual([
      '/api/',
      '/api/auth/login',
      '/api/auth/logout',
      '/api/auth/session',
      '/api/organizations',
      '/api/organizations/${encodeURIComponent(orgSlug)}',
      '/api/organizations/${segment(orgSlug)}/branches/${segment(branchSlug)}',
    ])

    // Under a branch: exactly the five Live Today reads, each named once, in the one module that makes them.
    const endpoints = shipped.flatMap(([path, text]) =>
      (text.match(/['"`](pipeline-status|checkins\/[a-z-]+|rejects\/[a-z-]+)['"`]/g) ?? []).map(
        (found) => `${path} ${found.slice(1, -1)}`,
      ),
    )
    expect(endpoints.filter((found) => found.startsWith('../api/liveToday.ts ')).sort()).toEqual([
      '../api/liveToday.ts checkins/by-hour',
      '../api/liveToday.ts checkins/count',
      '../api/liveToday.ts rejects/by-reason',
      '../api/liveToday.ts rejects/count',
    ])
    expect(offenders(/\/pipeline-status/)).toEqual(['../api/liveToday.ts'])
  })

  it('never uses the older ingest-status endpoint, or any other operational one', () => {
    expect(offenders(/ingest[-_]?status|\/ingest\b|transits|heartbeat/i)).toEqual([])
  })

  it('judges nothing by age: there is no staleness threshold', () => {
    expect(offenders(/STALE_|stale_?after|stale_?threshold|max_?age|isStale|staleTime/i)).toEqual([])
  })

  it('builds addresses from slugs, never from database ids', () => {
    expect(offenders(/customer_id|branch_id|organization_id|tenant_id|org_id|\.id\b/)).toEqual([])
    const paths = shipped.find(([path]) => path === '../router/paths.ts')?.[1] ?? ''
    expect(paths).toMatch(/encodeURIComponent\(orgSlug\)/)
    expect(paths).toMatch(/encodeURIComponent\(branchSlug\)/)
    expect(paths).not.toMatch(/\d/)
  })
})
